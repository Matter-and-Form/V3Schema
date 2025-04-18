const child_process = require('child_process');
const fs = require('fs');
const glob = require('glob');

const SCHEMA_ROOT_DIR = 'MF/V3';

const exec = command =>
{
    return new Promise((resolve, reject) => 
    {
        const proc = child_process.exec(command, (error, stdout, stderr) => 
        {
            if (error) reject(error);
            else if (stderr) resolve(stderr.trim());
            else resolve(stdout.trim());
        });

        proc.stdout.on('data', data => console.log(data.trim()));
    });
};

(async () =>
{
    // Transpile proto definitions 
    await exec(`npx buf generate ../..`);

    // Generate export definitions
    const pbIndex = glob.sync(`${SCHEMA_ROOT_DIR}/**/*.ts`)
        .reduce((map, pb) =>
        {
            const [_, pathname] = pb.split(`${SCHEMA_ROOT_DIR}/`);
            const components = pathname.split('/');
            const [exportName] = components.at(-1).split('.ts');
            const exportPath = `export * as ${exportName} from './${exportName}';`;
            const exportDir = components.slice(0, -1).join('/');

            return {
                ...map,
                [exportDir]: map[exportDir] ? map[exportDir].concat([exportPath]) : [exportPath]
            }
        }, {});

    delete pbIndex[''];
    delete pbIndex['Tasks'];

    for (const dir in pbIndex)
    {
        fs.writeFileSync(
            `${SCHEMA_ROOT_DIR}/${dir}/index.ts`,
            `${pbIndex[dir].join('\n')}\n`
        );
    }

    fs.writeFileSync(
        `${SCHEMA_ROOT_DIR}/index.ts`,
        Object.keys(pbIndex).map(d => `export * as ${d.split('/').join('_')} from './${d}';`).join('\n')
    );

    await exec('npm run transpile');
    await exec('rm -rf MF');
    await exec('rm -rf google');

    const dirs = [...new Set(Object.keys(pbIndex).map(dir => dir.split('/').at(0)))].filter(d => d !== '');

    // Move generated proto definitions to root dir
    for (const dir of dirs)
    {
        await exec(`mv build/${SCHEMA_ROOT_DIR}/${dir} .`);
    }
    await exec(`mv build/${SCHEMA_ROOT_DIR}/index.d.ts .`);
    await exec(`mv build/${SCHEMA_ROOT_DIR}/index.js .`);

    await exec(`rm -rf build`);
})();
