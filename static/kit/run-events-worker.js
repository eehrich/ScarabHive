// One stream of the run events (GET /api/agent-runs/stream) for every page of this origin: the shell, each panel
// frame and each panel in a tab of its own. A browser keeps at most six HTTP/1.1 connections per host, and a stream
// holds one for good -- six pages, each with its own, and every further request of the origin waits (a panel opened in
// a new tab stayed empty). run-events.js connects here; its pages get the messages as they would from the stream.
//   page -> worker: {kind: 'close'}                       the page stops following
//   worker -> page: {kind: 'data', data}, {kind: 'state', state: 'open'|'lost'}
//
// The stream is the account's that opened it, and the worker outlives a log-out in one tab while another stays open.
// So a page is let in only after asking who is signed in now (GET /auth/me): someone else, and the stream is opened
// anew under the cookie the browser holds now -- never are one account's runs handed to the next.

const ports = new Set();
const gone = new WeakSet();   // pages that stopped following while they were let in
let source = null;
let owner;                    // who the open stream belongs to: a username, '' with auth off, null when not known
let snapshotSeen = false;     // the stream sent its first snapshot: a page joining later asks for one of its own
let admitting = Promise.resolve();
let asked = 0;                // questions of who is signed in, numbered as they were asked
let applied = 0;              // the newest one whose answer was used: an older one no longer says who it is now
const ASK_AGAIN_MS = [1000, 3000];   // /auth/me failed for a passing reason (a 429, the network): asked again after these
const ASK_TIMEOUT_MS = 5000;         // an answer that does not come is such a failure too
const ASK_LATER_MS = 15000;          // still no answer: the page was told it is lost, and is let in once there is one

const pause = (ms) => new Promise((done) => setTimeout(done, ms));

function send(port, message) {
  try { port.postMessage(message); } catch { ports.delete(port); }
}

function broadcast(message) {
  for (const port of ports) send(port, message);
}

function close() {
  source?.close();
  source = null;
}

function open() {
  snapshotSeen = false;
  source = new EventSource('/api/agent-runs/stream');
  source.onopen = () => broadcast({ kind: 'state', state: 'open' });
  source.onerror = () => broadcast({ kind: 'state', state: 'lost' });
  source.onmessage = (message) => {
    if (message.data.includes('"runs.snapshot"')) snapshotSeen = true;
    broadcast({ kind: 'data', data: message.data });
  };
}

/** Who is signed in now, as {who, question}: the username, '' with auth off (no /auth/me), null for nobody (401, 403),
 * and the number of the question that told. Null when it cannot be told after asking again. */
async function signedIn() {
  for (const wait of [0, ...ASK_AGAIN_MS]) {
    if (wait) await pause(wait);
    const question = ++asked;
    try {
      const answer = await fetch('/auth/me', { credentials: 'same-origin', signal: AbortSignal.timeout(ASK_TIMEOUT_MS) });
      if (answer.status === 404) return { who: '', question };
      if (answer.status === 401 || answer.status === 403) return { who: null, question };
      if (answer.ok) {
        const me = await answer.json();
        return { who: String(me?.username ?? me?.id ?? ''), question };
      }
    } catch { /* asked again */ }
  }
  return null;
}

/** Who this page is let in as, once that can be told. Until then it stays off a stream that may be another account's,
 * and the rest keep it. An answer overtaken by a newer question is asked anew: applied in the order they arrive, a
 * late one would name the wrong owner for the stream the browser opens under today's cookie. Null: the page went. */
async function whoFor(port) {
  let toldLost = false;
  while (!gone.has(port)) {
    const answer = await signedIn();
    if (!answer) {
      if (!toldLost) send(port, { kind: 'state', state: 'lost' });
      toldLost = true;
      await pause(ASK_LATER_MS);
    } else if (answer.question >= applied) {
      applied = answer.question;
      return answer;
    }
  }
  return null;
}

/** A page joining a stream already open: the runs as they stand now, as the stream's snapshot would say them. */
async function snapshotFor(port) {
  try {
    const answer = await fetch('/api/agent-runs', { credentials: 'same-origin' });
    if (!answer.ok) return;
    const { runs } = await answer.json();
    send(port, { kind: 'data', data: JSON.stringify({ type: 'runs.snapshot', runs: runs || [] }) });
  } catch { /* the page goes on with the events from now on */ }
}

function admit(port, who) {
  if (gone.has(port)) return;
  if (source && who !== owner) close();   // another account now: its own stream, for every page
  ports.add(port);
  // none yet, another account's, or one the browser gave up (refused: logged out): opened anew for the page
  if (!source || source.readyState === EventSource.CLOSED) {
    owner = who;
    open();
    return;
  }
  if (source.readyState === EventSource.OPEN) send(port, { kind: 'state', state: 'open' });
  if (snapshotSeen) snapshotFor(port);
}

self.onconnect = (event) => {
  const port = event.ports[0];
  port.onmessage = (message) => {
    if (message.data?.kind !== 'close') return;
    gone.add(port);
    ports.delete(port);
    if (!ports.size) close();
  };
  port.start();
  // asked side by side, so an answer that hangs keeps only its own page waiting; let in one at a time: two pages of a
  // new account open one stream, not two. A failure costs that page, not the ones after it.
  whoFor(port).then((answer) => {
    if (!answer) return;
    admitting = admitting.then(() => admit(port, answer.who)).catch((error) => {
      console.error('run events: a page was not let in', error);
      send(port, { kind: 'state', state: 'lost' });
    });
  });
};
