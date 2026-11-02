// The origin's one stream of run events (static/kit/run-events-worker.js): whom a page is let in as, and what a failure
// costs. The real script in a context with stand-ins for fetch, EventSource and the clock.
//
// Run: node tests/js/run_events_worker.test.mjs   (driven by test_run_events_hub.py)
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const here = path.dirname(fileURLToPath(import.meta.url));
const script = readFileSync(path.join(here, '..', '..', 'static', 'kit', 'run-events-worker.js'), 'utf8');
const settle = async () => { for (let i = 0; i < 40; i += 1) await new Promise((done) => setImmediate(done)); };

/** A fresh worker: `me` answers /auth/me (a function: status, or a promise that never settles). */
function worker() {
  const world = { sources: [], me: () => ({ status: 200, username: 'alice' }), breakOpen: 0, errors: [], snapshots: 0 };
  class FakeSource {
    static CLOSED = 2;
    static OPEN = 1;
    constructor() {
      if (world.breakOpen > 0) { world.breakOpen -= 1; throw new Error('cannot open'); }
      this.readyState = 1;
      this.closed = false;
      world.sources.push(this);
    }
    close() { this.closed = true; this.readyState = 2; }
  }
  const self = {};
  const context = {
    self,
    EventSource: FakeSource,
    AbortSignal: { timeout: () => undefined },
    setTimeout: (fn) => setImmediate(fn),   // the clock does not hold the test up
    console: { error: (...args) => world.errors.push(args) },
    fetch: async (url) => {
      if (url === '/api/agent-runs') {
        world.snapshots += 1;
        return { ok: true, json: async () => ({ runs: [{ run_id: 'r0' }] }) };
      }
      assert.equal(url, '/auth/me');
      const answer = await world.me();
      return { status: answer.status, ok: answer.status === 200, json: async () => ({ username: answer.username }) };
    },
  };
  vm.runInNewContext(script, context);
  world.connect = () => {
    const port = { told: [], postMessage(message) { this.told.push(message); }, start() {} };
    self.onconnect({ ports: [port] });
    port.close = () => port.onmessage({ data: { kind: 'close' } });
    port.states = () => port.told.filter((m) => m.kind === 'state').map((m) => m.state);
    return port;
  };
  return world;
}

// One account, one stream; a new account, a new one -- pages of it arriving together open one.
{
  const w = worker();
  w.connect();
  w.connect();
  await settle();
  assert.equal(w.sources.length, 1, 'two pages of one account share a stream');
  w.me = () => ({ status: 200, username: 'bob' });
  w.connect();
  w.connect();
  await settle();
  assert.equal(w.sources.length, 2, 'two pages of a new account open one stream');
  assert.equal(w.sources[0].closed, true, 'the last account\'s stream is closed');
}

// An answer that hangs keeps only its own page waiting.
{
  const w = worker();
  w.me = () => new Promise(() => {});
  const hanging = w.connect();
  await settle();
  w.me = () => ({ status: 200, username: 'alice' });
  const next = w.connect();
  await settle();
  assert.equal(w.sources.length, 1, 'the next page got its stream');
  assert.equal(hanging.states().length, 0, 'the page whose question hangs is not on it');
  w.sources[0].onmessage({ data: '{"run_id":"r1"}' });
  assert.equal(next.told.filter((m) => m.kind === 'data').length, 1, 'it hears the stream');
  assert.equal(hanging.told.filter((m) => m.kind === 'data').length, 0, 'the other one does not');
}

// A passing failure (a 429, no network): the stream of the others stays, the page is not put on it unasked.
{
  const w = worker();
  const first = w.connect();
  await settle();
  w.me = () => ({ status: 429 });
  const refused = w.connect();
  await settle();
  assert.equal(w.sources.length, 1, 'a healthy stream is not reopened');
  assert.equal(w.sources[0].closed, false, 'nor closed');
  assert.deepEqual(refused.states(), ['lost'], 'the page is told, and is not on the stream');
  w.sources[0].onmessage({ data: '{"run_id":"r1"}' });
  assert.equal(refused.told.filter((m) => m.kind === 'data').length, 0, 'it gets no events of a stream of unknown account');
  assert.equal(first.told.filter((m) => m.kind === 'data').length, 1, 'the others do');
  w.me = () => ({ status: 200, username: 'alice' });   // the failure passes: asked on, it comes back by itself
  await settle();
  w.sources[0].onmessage({ data: '{"run_id":"r2"}' });
  assert.equal(refused.told.filter((m) => m.kind === 'data').length, 1, 'let in: it hears the stream now');
  assert.deepEqual(refused.states(), ['lost', 'open'], 'told once that it is lost, then that it is back');
  assert.equal(w.sources.length, 1);
}

// A page that goes while it waits for an answer stops asking.
{
  const w = worker();
  let asked = 0;
  w.me = () => { asked += 1; return { status: 429 }; };
  const waiting = w.connect();
  await settle();
  waiting.close();
  await settle();
  const after = asked;
  await settle();
  assert.equal(asked, after, 'nobody is left to ask for');
  w.me = () => ({ status: 200, username: 'alice' });
  await settle();
  assert.equal(w.sources.length, 0);
}

// A late answer is not applied over a newer one: Bob's question overtaken by Alice's sign-in names no owner.
{
  const w = worker();
  let answerBob;
  w.me = () => new Promise((done) => { answerBob = () => done({ status: 200, username: 'bob' }); });
  const bobsPage = w.connect();
  await settle();
  w.me = () => ({ status: 200, username: 'alice' });
  const alicesPage = w.connect();
  await settle();
  assert.equal(w.sources.length, 1, 'Alice has her stream');
  answerBob();
  await settle();
  assert.equal(w.sources.length, 1, 'the late answer costs Alice\'s stream nothing');
  assert.equal(w.sources[0].closed, false);
  w.sources[0].onmessage({ data: '{"run_id":"alice-run"}' });
  assert.equal(alicesPage.told.filter((m) => m.data?.includes('alice-run')).length, 1);
  assert.equal(bobsPage.told.filter((m) => m.data?.includes('alice-run')).length, 1, 'that page was asked anew: it is Alice\'s now');
  w.me = () => ({ status: 200, username: 'bob' });
  const bobsNew = w.connect();
  await settle();
  assert.equal(w.sources.length, 2, 'Bob signed in again: a stream of his own');
  assert.equal(w.sources[0].closed, true);
  w.sources[1].onmessage({ data: '{"run_id":"bob-run"}' });
  assert.equal(bobsNew.told.filter((m) => m.data?.includes('alice-run')).length, 0, 'he never hears hers');
}

// A retry counts as the later question it is: the page behind it overtakes one whose first question still hangs.
{
  const w = worker();
  let answerBob;
  const script = [
    () => ({ status: 429 }),
    () => new Promise((done) => { answerBob = () => done({ status: 200, username: 'bob' }); }),
  ];
  w.me = () => (script.length ? script.shift()() : { status: 200, username: 'alice' });
  w.connect();
  w.connect();
  await settle();
  assert.equal(w.sources.length, 1, 'the retry was answered: Alice has her stream');
  answerBob();
  await settle();
  assert.equal(w.sources.length, 1, 'the question it overtook does not reopen it as Bob\'s');
  assert.equal(w.sources[0].closed, false);
}

// A stream the browser gave up (refused) is opened anew for the next page; one joining an open stream gets the runs.
{
  const w = worker();
  const first = w.connect();
  await settle();
  w.sources[0].onopen();
  assert.deepEqual(first.states(), ['open']);
  const before = w.connect();
  await settle();
  assert.equal(w.snapshots, 0, 'no snapshot went out yet: the stream\'s own one reaches it');
  assert.deepEqual(before.states(), ['open'], 'a page joining an open stream is told');
  w.sources[0].onmessage({ data: '{"type":"runs.snapshot","runs":[]}' });
  const late = w.connect();
  await settle();
  assert.equal(w.snapshots, 1, 'after it, a page joining asks for its own');
  assert.equal(late.told.filter((m) => m.data?.includes('r0')).length, 1, 'and gets the runs as they stand');
  w.sources[0].readyState = 2;
  w.connect();
  await settle();
  assert.equal(w.sources.length, 2, 'refused for good: a fresh one');
  w.connect();
  await settle();
  assert.equal(w.snapshots, 1, 'its first snapshot is still to come: not the old stream\'s word');
}

// Asked again, it comes through.
{
  const w = worker();
  let tries = 0;
  w.me = () => { tries += 1; return tries < 3 ? { status: 429 } : { status: 200, username: 'alice' }; };
  w.connect();
  await settle();
  assert.equal(tries, 3);
  assert.equal(w.sources.length, 1);
}

// Signed out: the stream goes at once.
{
  const w = worker();
  w.connect();
  await settle();
  w.me = () => ({ status: 401 });
  w.connect();
  await settle();
  assert.equal(w.sources[0].closed, true, 'nobody signed in: the account\'s stream ends');
}

// A failure costs that page, not the ones after it.
{
  const w = worker();
  w.breakOpen = 1;
  const failed = w.connect();
  await settle();
  assert.deepEqual(failed.states(), ['lost']);
  assert.equal(w.errors.length, 1);
  w.connect();
  await settle();
  assert.equal(w.sources.length, 1, 'the next page is let in');
}

// A page that goes while it is asked for is not let in; the last to go ends the stream.
{
  const w = worker();
  const gone = w.connect();
  gone.close();
  await settle();
  assert.equal(w.sources.length, 0, 'nobody is left to follow');
  const one = w.connect();
  await settle();
  one.close();
  assert.equal(w.sources[0].closed, true);
}
console.log('run events worker: ok');
