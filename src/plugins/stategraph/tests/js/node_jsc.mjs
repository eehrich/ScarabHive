// Runs a test written for jsc under node: node node_jsc.mjs <test module>, from this folder.
// jsc gives a test print, load (a classic script into the global scope, as the panel loads ELK) and readFile;
// these are the same three for node, so one test source serves both (test_plugin_stategraph_js.py picks the one
// that is installed).
import { readFileSync, writeSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { runInThisContext } from 'node:vm';

// straight to stdout: a fake DOM may point console.log at print
globalThis.print = (...args) => { writeSync(1, `${args.join(' ')}\n`); };
globalThis.readFile = (path) => readFileSync(path, 'utf8');
globalThis.load = (path) => runInThisContext(readFileSync(path, 'utf8'), { filename: path });

await import(pathToFileURL(resolve(process.argv[2])).href);
