const THREE = require('./three');

const main = async () =>
{
    const three = new THREE('ws://matterandform.local:8081');
    try
    {
        // Open connection with the server.
        await three.connect();
        console.log(`Connected to the server @ ${three.url}`)

        // Create a new project.
        const createProjectResponse = await three.send({
            Index: 1,
            Type: "NewProject",
            Input: "My Project Name"
        });
        console.log('Project created:', createProjectResponse);

        // Open the newly created project.
        const openProjectResponse = await three.send({
            Index: 2,
            Type: "OpenProject",
            Input: createProjectResponse.index
        });
        console.log('Project opened:', openProjectResponse);

        // Add a new scan to the opened project
        const newScanResponse = await three.send({
            Index: 3,
            Type: "NewScan",
            Input: {
                camera: { exposure: 18000, analogGain: 256, digitalGain: 256 },
                capture: { quality: "Medium", texture: true },
                projector: { brightness: 0.8 }
            }
        });
        console.log('New scan added:', newScanResponse);
    }
    catch (error) { console.error('Error:', error); }
    finally { three.ws?.close(); }
}

main();
