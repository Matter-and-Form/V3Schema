import asyncio
from three import THREE

async def main():
    three = THREE('ws://matterandform.local:8081')
    try:
        await three.connect()
        print(f"Connected to the server @ {three.url}")

        # Create a new project
        create_project_response = await three.send({
            "Index": 1,
            "Type": "NewProject",
            "Input": "My Project Name"
        })
        print('Project created:', create_project_response)

        # Open the newly created project
        open_project_response = await three.send({
            "Index": 2,
            "Type": "OpenProject",
            "Input": create_project_response['index']
        })
        print('Project opened:', open_project_response)

        # Add a new scan to the opened project
        new_scan_response = await three.send({
            "Index": 3,
            "Type": "NewScan",
            "Input": {
                "camera": { "exposure": 18000, "analogGain": 256, "digitalGain": 256 },
                "capture": { "quality": "Medium", "texture": True },
                "projector": { "brightness": 0.8 }
            }
        })
        print('New scan added:', new_scan_response)

    except Exception as e:
        print(f"Error: {e}")
    finally:
        await three.ws.close()

if __name__ == "__main__":
    asyncio.run(main())
