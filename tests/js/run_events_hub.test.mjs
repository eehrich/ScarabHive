// The tab's one stream of run events (static/kit/run-events.js, followRuns): the shell and the panels framed in it
// share the top window's EventSource -- a browser allows about six connections per host, and a stream per panel used
// them up. A panel that joins later gets the runs as they stand; the last one to let go closes the stream.
//
// Run: node tests/js/run_events_hub.test.mjs   (driven by test_run_events_hub.py)
import assert from 'node:assert/strict';
import path from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const sources = [];

class FakeSource {
  constructor(url) { this.url = url; this.closed = false; sources.push(this); }
  close() { this.closed = true; }
  say(data) { this.onmessage({ data: JSON.stringify(data) }); }
}

function windowOf(top, handlers) {
  return {
    top, location: { origin: 'http://app' }, EventSource: FakeSource,
    addEventListener: (type, fn) => { (handlers[type] ||= []).push(fn); },
    removeEventListener: (type, fn) => { handlers[type] = (handlers[type] || []).filter((one) => one !== fn); },
  };
}
const fire = (handlers, type, event = {}) => { for (const fn of [...(handlers[type] || [])]) fn(event); };

const topHandlers = {}, frameHandlers = {};
const top = windowOf(null, topHandlers);
top.top = top;
top.fetch = async (url) => ({ ok: url === '/api/agent-runs', json: async () => ({ runs: [{ run_id: 'r0', state: 'working' }] }) });
const frame = windowOf(top, frameHandlers);
globalThis.EventSource = FakeSource;

const { followRuns } = await import(pathToFileURL(path.join(here, '..', '..', 'static', 'kit', 'run-events.js')).href);
const seen = { shell: [], panel: [], broken: [] };

globalThis.window = top;
const shell = followRuns({ onSnapshot: (runs) => seen.shell.push(['snapshot', runs.length]), onEvent: (e) => seen.shell.push(e.run_id) });
assert.equal(sources.length, 1, 'the shell opens the stream');
sources[0].say({ type: 'runs.snapshot', runs: [] });

globalThis.window = frame;
const panel = followRuns({ onSnapshot: (runs) => seen.panel.push(['snapshot', runs[0].run_id]), onEvent: (e) => seen.panel.push(e.run_id) });
const broken = followRuns({ onEvent: () => { seen.broken.push('called'); throw new Error('a drawing bug'); } });
const quietError = console.error;
console.error = () => {};
assert.equal(sources.length, 1, 'a panel in the same tab shares the stream instead of opening its own');
await new Promise((done) => setTimeout(done, 0));
assert.deepEqual(seen.panel, [['snapshot', 'r0']], 'a panel that joins gets the runs as they stand');

sources[0].say({ run_id: 'r1', type: 'run.step' });
sources[0].say({ run_id: 'r2', type: 'run.done' });
assert.deepEqual(seen.shell, [['snapshot', 0], 'r1', 'r2']);
assert.deepEqual(seen.panel, [['snapshot', 'r0'], 'r1', 'r2'], 'every event reaches every listener of the tab');
assert.deepEqual(seen.broken, ['called', 'called'], 'a listener that throws loses only that message, the others go on');
console.error = quietError;

fire(frameHandlers, 'pagehide', { persisted: false });   // the panel's frame goes
sources[0].say({ run_id: 'r3', type: 'run.step' });
assert.deepEqual(seen.panel.slice(-1), ['r2'], 'a frame that went hears nothing more');
assert.equal(sources[0].closed, false, 'the shell still follows');
broken.close();
shell.close();
assert.equal(sources[0].closed, true, 'the last one to let go closes the stream');

globalThis.window = top;
const again = followRuns({ onEvent: (e) => seen.shell.push(e.run_id) });
assert.equal(sources.length, 2, 'after that a new stream is opened');
sources[1].readyState = 2;   // the browser gave it up (an error answer to a reconnect)
globalThis.window = frame;
followRuns({});
assert.equal(sources.length, 3, 'a stream given up for good is replaced, not joined');

globalThis.window = top;
fire(topHandlers, 'pagehide', { persisted: true });   // into the back/forward cache
fire(topHandlers, 'pageshow', { persisted: true });   // and back
sources.at(-1).say({ run_id: 'r9', type: 'run.step' });
assert.equal(seen.shell.at(-1), 'r9', 'a page the back/forward cache brings back follows again');
again.close();
console.log('run events hub: ok');
