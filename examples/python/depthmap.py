#!/usr/bin/env python3
"""
Capture a single depth map from a THREE scanner.

The scanner's `DepthMap` task captures one structured-light scan from the
current position and returns three things:

  * a depth map      - float32, one value per pixel, millimetres, 0 = no data
  * a colour texture - undistorted, pixel-aligned with the depth map
  * the intrinsics   - the 3x3 camera matrix the depth map was rendered with

No project is created on the scanner and nothing is left behind: the scan is
captured into a temporary directory which the server deletes when it is done.

Usage:
    pip install websockets numpy pillow
    ./depthmap.py --host 192.168.50.116 --out ./capture

By default this autofocuses both cameras on the centre of the frame, runs the
projector at full brightness, uses every structured-light pattern rather than
the subset the quality presets use, and averages 16 frames per pattern for
noise reduction. Put the subject in the centre of the frame first, or the
autofocus has nothing to lock onto and will not converge.

Outputs written to the output directory:
    depth.npy       float32 HxW depth in millimetres, 0 = no data
    depth.png       16-bit greyscale depth, 1 unit = 1/16 mm (0 = no data)
    depth_preview.png   colour-mapped depth, for looking at
    texture.png     the undistorted colour image, aligned to the depth map
    points.ply      colour point cloud unprojected from the depth map
    intrinsics.json camera matrix, image size and units
"""

import argparse
import asyncio
import json
import struct
import sys
from pathlib import Path

import numpy as np
import websockets

# The scanner's video stream, which is the coordinate space the autofocus box
# is expressed in. See the StartVideo task.
VIDEO_WIDTH = 510
VIDEO_HEIGHT = 380

# Structured-light pattern frequencies the scanner supports, from
# Descriptors::Settings::Advanced::Capture. Frequency 0 is always included by
# the server, so we only ever send the positive ones. The quality presets use
# a subset ({3,5,7} and {3,5}); "all" uses every one of them.
MAX_HORIZONTAL_FREQUENCY = 7
MAX_VERTICAL_FREQUENCY = 6
DEFAULT_HORIZONTAL_FREQUENCIES = [3, 5, 7]
DEFAULT_VERTICAL_FREQUENCIES = [3, 5]

# Each frequency is projected at 3 phases.
PHASES = 3

# Video frames stream on this reserved task index. See MF/V3/ReservedTasks.hpp.
VIDEO_TASK_INDEX = -1

# ---------------------------------------------------------------------------
# OpenCV image type decoding.
#
# An OpenCV type packs the element type and the channel count into one int:
#     type = depth + ((channels - 1) << 3)
# The scanner sends the depth map as CV_32FC1 (5) and the texture as CV_8UC3 (16).
# ---------------------------------------------------------------------------

CV_DEPTHS = {
    0: np.uint8,
    1: np.int8,
    2: np.uint16,
    3: np.int16,
    4: np.int32,
    5: np.float32,
    6: np.float64,
}


def decode_image(payload, descriptor):
    """Turn a raw buffer plus its descriptor into a numpy array."""
    # The DepthMap documentation calls these cols/rows; the server sends
    # width/height. Accept either.
    width = descriptor.get("width", descriptor.get("cols"))
    height = descriptor.get("height", descriptor.get("rows"))
    step = descriptor["step"]
    cv_type = descriptor["type"]

    dtype = np.dtype(CV_DEPTHS[cv_type & 7])
    channels = (cv_type >> 3) + 1

    if len(payload) < height * step:
        raise ValueError(
            f"buffer is {len(payload)} bytes, expected at least {height * step}"
        )

    # Rows are `step` bytes apart, which is not necessarily width * pixel size.
    rows = np.frombuffer(payload[: height * step], dtype=np.uint8)
    rows = rows.reshape(height, step)
    pixels = rows[:, : width * channels * dtype.itemsize]
    image = pixels.copy().view(dtype).reshape(height, width, channels)

    return image[:, :, 0] if channels == 1 else image


# ---------------------------------------------------------------------------
# Talking to the scanner.
# ---------------------------------------------------------------------------

# The scanner captures and processes on two threads, so progress for both
# phases arrives interleaved. Keep the latest of each and render one line.
_progress = {}


def report_progress(progress):
    """Print a scanner progress update over a single line."""
    if not isinstance(progress, dict):
        return
    # Progress arrives as {"ScanCapture": {"current", "total", "step"}} and
    # likewise for "ScanProcess".
    for phase, detail in progress.items():
        if isinstance(detail, dict):
            _progress[phase] = detail

    parts = []
    for phase, label in (("ScanCapture", "capture"), ("ScanProcess", "process")):
        detail = _progress.get(phase)
        if detail is None:
            continue
        current, total = detail.get("current", 0), detail.get("total", 0)
        filled = round(10 * current / total) if total else 0
        parts.append(f"{label} [{'#' * filled}{'.' * (10 - filled)}]")

    step = _progress.get("ScanProcess", _progress.get("ScanCapture", {})).get("step", "")
    print(f"\r  {'  '.join(parts)}  {step:<26}"[:78], end="", flush=True)


class TaskFailed(RuntimeError):
    pass


async def run_task(ws, index, task_type, task_input=None, timeout=600, quiet=False):
    """Send one task, collect any data buffers, return (output, buffers)."""
    task = {"Index": index, "Type": task_type}
    if task_input is not None:
        task["Input"] = task_input

    buffers = []  # [{"index", "size", "descriptor", "data": bytearray}]
    await ws.send(json.dumps({"Task": task}))

    while True:
        message = await asyncio.wait_for(ws.recv(), timeout=timeout)

        # Binary frames are the payload of the most recent buffer message.
        if isinstance(message, (bytes, bytearray)):
            pending = next((b for b in buffers if len(b["data"]) < b["size"]), None)
            if pending is None:
                if not quiet:
                    print(f"\r  warning: {len(message)} unexpected bytes", flush=True)
                continue
            pending["data"].extend(message)
            if len(pending["data"]) >= pending["size"] and not quiet:
                print(
                    f"\r  buffer {pending['index']}: {pending['size']} bytes received"
                    f"{'':<22}",
                    flush=True,
                )
            continue

        response = json.loads(message)

        if "Buffer" in response:
            buffer = response["Buffer"]
            if buffer["Task"]["Index"] != index:
                continue
            buffers.append(
                {
                    "index": buffer["Index"],
                    "size": int(buffer["Size"]),
                    "descriptor": buffer.get("Descriptor", {}),
                    "data": bytearray(),
                }
            )
            continue

        if "Task" not in response:
            continue

        reply = response["Task"]
        if reply.get("Index") != index:
            continue

        state = reply.get("State")
        if state == "Failed":
            raise TaskFailed(reply.get("Error", "task failed"))
        if state == "Cancelled":
            raise TaskFailed("task cancelled by the scanner")
        if "Progress" in reply:
            report_progress(reply["Progress"])
        elif state and not quiet:
            print(f"\r  {state:<52}", flush=True)
        if state == "Completed":
            return reply.get("Output"), buffers


def centre_box(box_size):
    """The autofocus box, centred in the video frame."""
    return {
        "x": (VIDEO_WIDTH - box_size) // 2,
        "y": (VIDEO_HEIGHT - box_size) // 2,
        "width": box_size,
        "height": box_size,
    }


async def preview(ws, out, box_size, timeout):
    """Save one frame from each camera with the autofocus box drawn on it."""
    import io
    from PIL import Image, ImageDraw

    await run_task(ws, 1, "StartVideo", timeout=timeout, quiet=True)

    # Video frames arrive as buffers on the reserved video task index,
    # camera 0 in buffer 0 and camera 1 in buffer 1.
    frames = {}
    pending = None
    try:
        while len(frames) < 2:
            message = await asyncio.wait_for(ws.recv(), timeout=timeout)
            if isinstance(message, (bytes, bytearray)):
                if pending is not None:
                    frames[pending] = bytes(message)
                    pending = None
                continue
            buffer = json.loads(message).get("Buffer")
            if buffer and buffer.get("Task", {}).get("Index") == VIDEO_TASK_INDEX:
                pending = buffer["Index"]
    finally:
        await run_task(ws, 2, "StopVideo", timeout=timeout, quiet=True)

    box = centre_box(box_size)
    images = [Image.open(io.BytesIO(frames[i])).convert("RGB") for i in sorted(frames)]

    # Aiming happens with the projector off, so the frame is often nearly black.
    # Stretch it for visibility - this is for framing, not for judging exposure.
    gain = 1.0
    peak = max(np.percentile(np.asarray(i), 99) for i in images)
    if peak > 0 and peak < 200:
        gain = 200.0 / peak
        images = [
            Image.fromarray(
                np.clip(np.asarray(i).astype(np.float32) * gain, 0, 255).astype(np.uint8)
            )
            for i in images
        ]
    sheet = Image.new("RGB", (sum(i.width for i in images), images[0].height))
    for offset, image in zip([0, images[0].width], images):
        draw = ImageDraw.Draw(image)
        draw.rectangle(
            [box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"]],
            outline=(255, 80, 80),
            width=2,
        )
        sheet.paste(image, (offset, 0))

    out.mkdir(parents=True, exist_ok=True)
    path = out / "preview.png"
    sheet.save(path)
    brightened = f", brightened {gain:.0f}x for visibility" if gain > 1.5 else ""
    print(f"  wrote {path}  (left camera, right camera; the red box is where "
          f"autofocus looks{brightened})")


async def autofocus(ws, index, box_size, timeout):
    """Autofocus both cameras on a box in the centre of the video frame."""
    box = centre_box(box_size)
    print(f"Focusing both cameras on a {box_size}x{box_size} box at the centre")
    output, _ = await run_task(
        ws,
        index,
        "AutoFocus",
        {
            "cameras": [{"index": 0, "box": box}, {"index": 1, "box": box}],
            "applyAll": False,
        },
        timeout=timeout,
        quiet=True,
    )
    focus = output.get("focus", {}).get("value", {}).get("value", []) if output else []
    print(f"  focus set to {focus} (range 0-1024)")


# ---------------------------------------------------------------------------
# Saving the results.
# ---------------------------------------------------------------------------

# Viridis, sampled at ten stops. Perceptually even, and readable printed.
VIRIDIS = np.array(
    [
        (68, 1, 84), (72, 40, 120), (62, 74, 137), (49, 104, 142), (38, 130, 142),
        (31, 158, 137), (53, 183, 121), (109, 205, 89), (180, 222, 44), (253, 231, 37),
    ],
    dtype=np.float64,
)


def colour_map(normalized):
    """Map an array in [0, 1] through viridis to uint8 RGB."""
    position = np.clip(normalized, 0.0, 1.0) * (len(VIRIDIS) - 1)
    low = np.floor(position).astype(np.int32)
    high = np.minimum(low + 1, len(VIRIDIS) - 1)
    weight = (position - low)[..., None]
    return (VIRIDIS[low] * (1 - weight) + VIRIDIS[high] * weight).astype(np.uint8)


def write_ply(path, points, colours):
    """Write a binary little-endian colour point cloud."""
    header = (
        "ply\n"
        "format binary_little_endian 1.0\n"
        "comment THREE scanner depth map, units are millimetres\n"
        f"element vertex {len(points)}\n"
        "property float x\nproperty float y\nproperty float z\n"
        "property uchar red\nproperty uchar green\nproperty uchar blue\n"
        "end_header\n"
    )
    with open(path, "wb") as f:
        f.write(header.encode("ascii"))
        for (x, y, z), (r, g, b) in zip(points, colours):
            f.write(struct.pack("<fffBBB", x, y, z, int(r), int(g), int(b)))


def save(out, depth, texture, camera_matrix, settings):
    from PIL import Image

    out.mkdir(parents=True, exist_ok=True)

    valid = depth > 0
    if not valid.any():
        raise RuntimeError(
            "the depth map is empty - nothing was in view, or the exposure "
            "was wrong for the surface"
        )

    height, width = depth.shape
    near = float(depth[valid].min())
    far = float(depth[valid].max())

    # The authoritative data.
    np.save(out / "depth.npy", depth)

    # 16-bit depth at 1/16 mm, which covers the scanner's whole working range.
    # 0 stays reserved for "no data".
    quantized = np.where(valid, np.clip(depth * 16.0, 1, 65535), 0)
    Image.fromarray(quantized.astype(np.uint16)).save(out / "depth.png")

    # A preview worth looking at: near is yellow, far is blue, no data is black.
    normalized = np.zeros_like(depth, dtype=np.float64)
    if far > near:
        normalized[valid] = 1.0 - (depth[valid] - near) / (far - near)
    preview = colour_map(normalized)
    preview[~valid] = 0
    Image.fromarray(preview, mode="RGB").save(out / "depth_preview.png")

    # The texture arrives from OpenCV as BGR.
    rgb = texture[:, :, ::-1] if texture.ndim == 3 else texture
    Image.fromarray(np.ascontiguousarray(rgb)).save(out / "texture.png")

    fx, _, cx, _, fy, cy = camera_matrix[:6]
    with open(out / "intrinsics.json", "w") as f:
        json.dump(
            {
                "width": width,
                "height": height,
                "fx": fx,
                "fy": fy,
                "cx": cx,
                "cy": cy,
                "cameraMatrix": camera_matrix,
                "units": "millimetres",
                "noDataValue": 0,
                "depthPngScale": 16,
                "note": (
                    "Depth is the Z distance from the camera in millimetres. "
                    "Unproject with X=(u-cx)*Z/fx, Y=(v-cy)*Z/fy."
                ),
                "captureSettings": settings,
            },
            f,
            indent=2,
        )

    # Unproject to a point cloud, which is the proof the depth map is metric
    # and that the texture lines up with it.
    v, u = np.nonzero(valid)
    z = depth[valid]
    points = np.stack([(u - cx) * z / fx, (v - cy) * z / fy, z], axis=1)
    colours = rgb[v, u] if rgb.ndim == 3 else np.repeat(rgb[v, u, None], 3, axis=1)
    write_ply(out / "points.ply", points, colours)

    coverage = 100.0 * valid.sum() / valid.size
    print()
    print(f"  resolution      {width} x {height} ({valid.size / 1e6:.1f} MP)")
    print(f"  valid depth     {valid.sum()} pixels ({coverage:.1f}% of the frame)")
    print(f"  depth range     {near:.1f} to {far:.1f} mm")
    print(f"  mean depth      {float(depth[valid].mean()):.1f} mm")
    print(f"  focal length    fx {fx:.1f}  fy {fy:.1f} px")
    print(f"  principal point cx {cx:.1f}  cy {cy:.1f} px")
    print(f"  sampling        {1000.0 * depth[valid].mean() / fx:.1f} um per pixel "
          f"at the mean depth")
    print(f"  point cloud     {len(points)} points")
    print()
    for name in sorted(p.name for p in out.iterdir()):
        print(f"  wrote {out / name}")


# ---------------------------------------------------------------------------


def build_capture_settings(args):
    """Build the capture settings and describe what they will cost."""
    if args.patterns == "all":
        horizontal = list(range(1, MAX_HORIZONTAL_FREQUENCY + 1))
        vertical = list(range(1, MAX_VERTICAL_FREQUENCY + 1))
    else:
        horizontal = list(DEFAULT_HORIZONTAL_FREQUENCIES)
        vertical = list(DEFAULT_VERTICAL_FREQUENCIES)

    capture = {
        "quality": args.quality,
        "texture": True,
        "horizontalFrequencies": horizontal,
        "verticalFrequencies": vertical,
        "blendCount": args.denoise,
    }

    # The server blends only frequencies at or above the blend frequency, and
    # defaults that to the highest frequency - which is what the UI's denoise
    # slider does, since it never sends a blend frequency of its own.
    if args.denoise_from is not None:
        capture["horizontalBlendFrequency"] = args.denoise_from
        capture["verticalBlendFrequency"] = args.denoise_from
        blend_from_h = blend_from_v = args.denoise_from
    else:
        # The server always prepends frequency 0, so the highest is max().
        blend_from_h, blend_from_v = max(horizontal), max(vertical)

    # The server always adds frequency 0 to each orientation.
    all_h = sorted({0, *horizontal})
    all_v = sorted({0, *vertical})
    patterns = (len(all_h) + len(all_v)) * PHASES
    blended = (
        sum(1 for f in all_h if f >= blend_from_h)
        + sum(1 for f in all_v if f >= blend_from_v)
    ) * PHASES
    frames = (patterns - blended) + blended * (args.denoise + 1)

    return capture, {
        "frequencies": f"{len(all_h)} horizontal + {len(all_v)} vertical",
        "patterns": patterns,
        "blended_patterns": blended,
        "frames": frames,
    }


def main():
    parser = argparse.ArgumentParser(
        description="Capture a single depth map from a THREE scanner.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default="matterandform.local",
                        help="scanner hostname or IP address")
    parser.add_argument("--port", type=int, default=8081)
    parser.add_argument("--quality", default="High", choices=["Low", "Medium", "High"],
                        help="processing resolution: Low 528x392, Medium 1052x780, "
                             "High 2104x1560")
    parser.add_argument("--exposure", type=int, default=180,
                        help="exposure as shown on the UI slider; microseconds = "
                             "this x 100, valid range 90 to 900")
    parser.add_argument("--analog-gain", type=float, default=256,
                        help="256 to 1024; 256 is the least noisy")
    parser.add_argument("--digital-gain", type=int, default=256,
                        help="256 to 65536; 256 is the least noisy")
    parser.add_argument("--brightness", type=float, default=1.0,
                        help="projector brightness, 0 to 1")
    parser.add_argument("--patterns", default="all", choices=["all", "preset"],
                        help="'all' projects every frequency; 'preset' uses the "
                             "subset the scanner normally uses")
    parser.add_argument("--denoise", type=int, default=16,
                        help="frames averaged per pattern, 1 to 32 (the UI's "
                             "denoise slider)")
    parser.add_argument("--denoise-from", type=int, default=None,
                        help="lowest frequency to apply denoising to; the default "
                             "matches the UI and denoises the highest frequency "
                             "only, 0 denoises every pattern")
    parser.add_argument("--no-focus", dest="focus", action="store_false",
                        help="skip autofocus and use the scanner's current focus")
    parser.add_argument("--focus-size", type=int, default=64,
                        help="side length of the centred autofocus box, in video "
                             f"pixels ({VIDEO_WIDTH}x{VIDEO_HEIGHT} frame)")
    parser.add_argument("--preview", action="store_true",
                        help="save a camera preview showing the autofocus box "
                             "and exit, without scanning")
    parser.add_argument("--out", type=Path, default=Path("depthmap-capture"),
                        help="directory to write the results into")
    parser.add_argument("--timeout", type=float, default=900.0,
                        help="seconds to wait for any single message")
    args = parser.parse_args()

    if not 1 <= args.denoise <= 32:
        sys.exit("--denoise must be between 1 and 32.")
    if not 90 <= args.exposure <= 900:
        sys.exit("--exposure must be between 90 and 900 (9 ms to 90 ms).")

    capture, cost = build_capture_settings(args)
    scan_settings = {
        "camera": {
            "exposure": args.exposure * 100,
            "analogGain": args.analog_gain,
            "digitalGain": args.digital_gain,
        },
        "capture": capture,
        "projector": {"brightness": args.brightness},
    }

    url = f"ws://{args.host}:{args.port}"
    if args.preview:
        print(f"Grabbing a camera preview from {url}")
    else:
        print(f"Capturing a depth map from {url}")
        print(f"  exposure   {args.exposure} on the UI ({args.exposure * 100} us)")
        print(f"  projector  {args.brightness:.0%} brightness")
        print(f"  patterns   {cost['frequencies']} frequencies, "
              f"{cost['patterns']} patterns")
        print(f"  denoise    {args.denoise} frames on {cost['blended_patterns']} "
              f"of them -> {cost['frames']} camera frames")
        print(f"  quality    {args.quality}")
    print()

    async def session():
        async with websockets.connect(url, max_size=None, ping_interval=None) as ws:
            if args.preview:
                await preview(ws, args.out, args.focus_size, args.timeout)
                return None, None
            if args.focus:
                await autofocus(ws, 1, args.focus_size, args.timeout)
            return await run_task(ws, 2, "DepthMap", scan_settings, args.timeout)

    try:
        output, buffers = asyncio.run(session())
    except OSError as e:
        sys.exit(f"Cannot reach the scanner at {url}: {e}")
    except asyncio.TimeoutError:
        sys.exit(f"The scanner stopped responding after {args.timeout}s.")
    except TaskFailed as e:
        if "converge" in str(e):
            sys.exit(
                f"\nAutofocus failed: {e}\n"
                "Put the subject in the centre of the frame and try again, or "
                "pass --no-focus to keep the scanner's current focus."
            )
        sys.exit(f"\nThe scanner rejected the task: {e}")

    if args.preview:
        return

    if not output or len(output) < 9:
        sys.exit("The scanner did not return a camera matrix.")
    if len(buffers) < 2:
        sys.exit(f"Expected a depth map and a texture, got {len(buffers)} buffer(s).")

    buffers.sort(key=lambda b: b["index"])
    depth = decode_image(buffers[0]["data"], buffers[0]["descriptor"])
    texture = decode_image(buffers[1]["data"], buffers[1]["descriptor"])

    if depth.shape != texture.shape[:2]:
        print(f"  warning: depth {depth.shape} and texture {texture.shape[:2]} differ")

    save(args.out, depth, texture, [float(v) for v in output], scan_settings)


if __name__ == "__main__":
    main()
