const path = require('node:path');
const fs = require('node:fs');
const root = path.resolve(__dirname, '../..');
const modules = path.join(__dirname, 'node_modules');
require('esbuild').buildSync({
    entryPoints: [path.join(root, 'OpenBench/static/training_editor.source.js')],
    outfile: path.join(root, 'OpenBench/static/training_editor.js'),
    bundle: true,
    minify: true,
    format: 'esm',
    target: 'es2020',
    legalComments: 'none',
    nodePaths: [modules],
});
const licenses = [];
for (const scope of ['@codemirror', '@lezer']) {
    for (const name of fs.readdirSync(path.join(modules, scope))) {
        const file = path.join(modules, scope, name, 'LICENSE');
        if (fs.existsSync(file)) licenses.push(`${scope}/${name}\n\n${fs.readFileSync(file, 'utf8')}`);
    }
}
for (const name of ['style-mod', 'w3c-keyname', 'crelt']) {
    const file = path.join(modules, name, 'LICENSE');
    if (fs.existsSync(file)) licenses.push(`${name}\n\n${fs.readFileSync(file, 'utf8')}`);
}
fs.writeFileSync(path.join(root, 'OpenBench/static/training-editor-LICENSE.txt'), licenses.join('\n\n'));
