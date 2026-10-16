// The canvas's ELK worker (graph.js ELK_WORKER, WorkerElk) against the vendored ELK in a real worker thread, without a
// browser: node's worker_threads with the names a page's worker scope has (self, importScripts, postMessage,
// onmessage) and neither a document nor CommonJS -- where the bundle runs its layout engine as ELK's worker.
//   node elk_worker_r15.mjs <graph.js as file URL> <elk.bundled.js path>
// Prints "LAID OUT <states>" or "FAIL <why>".
import { readFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import { runInThisContext } from 'node:vm';
import { Worker } from 'node:worker_threads';

const [graphJs, elkPath] = process.argv.slice(2);
runInThisContext(readFileSync(elkPath, 'utf8'));  // the page's ELK, as panel.html loads it
const G = await import(graphJs);

const scope = `
const { parentPort } = require('node:worker_threads');
const { readFileSync } = require('node:fs');
const { runInThisContext } = require('node:vm');
const { fileURLToPath } = require('node:url');
globalThis.module = undefined;
globalThis.exports = undefined;
globalThis.self = globalThis;
globalThis.importScripts = (url) => runInThisContext(readFileSync(fileURLToPath(url), 'utf8'));
globalThis.postMessage = (message) => parentPort.postMessage(message);
parentPort.on('message', (data) => globalThis.onmessage({ data }));
runInThisContext(${JSON.stringify(G.ELK_WORKER(pathToFileURL(elkPath).href))});`;
let thread;
let fellBack = false;
const elk = new G.WorkerElk((failed) => new ELK({ workerFactory: () => {  // eslint-disable-line no-undef
  thread = new Worker(scope, { eval: true });
  thread.on('error', (error) => { console.log(`worker error: ${error.message}`); failed(); });
  return {  // a browser Worker's shape over the thread
    postMessage: (message) => thread.postMessage(message),
    set onmessage(fn) { thread.on('message', (data) => fn({ data })); },
    terminate: () => thread.terminate(),
  };
} }), () => { fellBack = true; return new ELK(); });  // eslint-disable-line no-undef
const states = Array.from({ length: 6 }, (_, i) => ({ name: `s${i}`, parent: null, type: 'state', composite: false, kind: null,
  label: '', icon: null, line: i + 1 }));
const transitions = states.slice(1).map((s, i) => ({ id: `s${i}#0`, source: `s${i}`, index: 0, target: s.name, trigger: 'done',
  guard: null, effect: null }));
let timer;
try {
  const out = await Promise.race([
    G.autoLayout(elk, { initial: 's0', states, transitions }, 'flow'),
    new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('no answer from the worker in 30 s')), 30000); }),
  ]);
  console.log(fellBack ? 'FAIL laid out on the page, not in the worker'
    : `LAID OUT ${Object.keys(out.nodes).filter((id) => id.startsWith('s:')).length}`);
} catch (error) {
  console.log(`FAIL ${error.message}`);
} finally {
  clearTimeout(timer);
  await thread?.terminate();
}
