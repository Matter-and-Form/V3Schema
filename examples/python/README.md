# Python examples

Examples of driving a THREE scanner over its websocket API. The scanner listens
on port 8081 and speaks the JSON protocol described in the
[API documentation](https://github.com/Matter-and-Form/V3Schema/wiki).

| File | What it does |
|---|---|
| `example.py` | Creates a project, opens it, and adds a scan. |
| `depthmap.py` | Captures a single depth map, texture and point cloud. |
| `focus.html` | A browser page for focusing the cameras. No install. |

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Everything below assumes `.venv/bin/python`. Replace `192.168.50.116` with your
scanner's address; `matterandform.local` also works on most networks.

## Focusing the cameras — `focus.html`

Nothing to install. Open the file in a browser; it talks to the scanner
directly.

```sh
xdg-open focus.html          # or: firefox focus.html?host=192.168.50.116
```

Enter the scanner address and press **Connect** to get a live feed from both
cameras. Click anywhere on a view to autofocus on that spot — the green box
marks where it last focused. There are manual focus sliders per camera, an
exposure slider, and a "focus both together" option.

Press **Light on** first if the subject is dark. It projects a flat white
field, which is usually the difference between autofocus locking on and
failing, because the projector is otherwise off while you are aiming.

Focus is stored on the scanner and persists after you close the page, so you
can focus here and then capture with `depthmap.py --no-focus`.

## Capturing a depth map — `depthmap.py`

```sh
.venv/bin/python depthmap.py --host 192.168.50.116 --out ./capture
```

This autofocuses both cameras on the centre of the frame, projects every
structured-light pattern at full brightness, averages 16 frames per pattern for
noise reduction, and writes the results to `./capture`. It takes about 20
seconds.

No project is created on the scanner and nothing is left behind — the scan goes
to a temporary directory that the server deletes when it is done.

Check your framing first, which costs a couple of seconds and no scan:

```sh
.venv/bin/python depthmap.py --host 192.168.50.116 --preview --out ./capture
```

That writes `preview.png`: both camera views with the autofocus box drawn on
them, brightened if the scene is dark.

### Output

| File | Contents |
|---|---|
| `depth.npy` | float32 `height x width`, millimetres, `0` = no data |
| `depth.png` | 16-bit greyscale, 1 unit = 1/16 mm, `0` = no data |
| `depth_preview.png` | colour-mapped depth, for looking at |
| `texture.png` | undistorted colour image, pixel-aligned with the depth map |
| `points.ply` | colour point cloud unprojected from the depth map |
| `intrinsics.json` | camera matrix, image size, units, settings used |

Depth is the Z distance from the camera along its optical axis, in millimetres.
The texture is undistorted with the same camera matrix, so texture pixel
`(u, v)` and depth pixel `(u, v)` are the same point. To unproject:

```python
import json, numpy as np

depth = np.load("capture/depth.npy")
K = json.load(open("capture/intrinsics.json"))

v, u = np.nonzero(depth > 0)
z = depth[depth > 0]
points = np.stack([(u - K["cx"]) * z / K["fx"],
                   (v - K["cy"]) * z / K["fy"],
                   z], axis=1)          # millimetres, camera space
```

The depth map is always rendered at the full camera resolution (2104x1560)
whatever the quality setting, because it is a render of the reconstructed mesh
from the camera's viewpoint.

### Options

| Option | Default | Notes |
|---|---|---|
| `--quality` | `High` | Processing resolution: Low 528x392, Medium 1052x780, High 2104x1560. |
| `--exposure` | `180` | As shown on the UI slider. Microseconds = this x 100. Range 90-900. |
| `--brightness` | `1.0` | Projector brightness, 0 to 1. |
| `--patterns` | `all` | `all` projects every frequency (45 patterns); `preset` uses the subset the scanner normally uses (21). |
| `--denoise` | `16` | Frames averaged per pattern, 1 to 32. The UI's denoise slider. |
| `--denoise-from` | unset | Lowest frequency to denoise. Unset matches the UI and denoises only the highest frequency; `0` denoises every pattern. |
| `--no-focus` | — | Keep the scanner's current focus instead of autofocusing. |
| `--analog-gain`, `--digital-gain` | `256` | Both are at their minimum, which is the least noisy. |

Denoising every frequency rather than just the highest costs roughly 3.5x the
time for no measurable gain, which is why the default matches the UI.

### Autofocus

Autofocus fails with "did not converge" when there is nothing with enough
detail inside the box — an empty scanner, or a flat, dark, featureless surface.
Put the subject in the centre of the frame, or use `focus.html` to focus
somewhere else in the frame, then capture with `--no-focus`.

Focus changes the intrinsics, and that is expected: the scanner stores a
calibration per focus position and interpolates it, so the camera matrix you
get back always matches the focus the scan was taken at.
