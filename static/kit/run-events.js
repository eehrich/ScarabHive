// What the agent runs say while they work (agent_system/run_events.py, docs/run_events.md): one stream of Server-Sent
// Events for every page of the origin (run-events-worker.js; without a SharedWorker one per tab), and the words to show
// a run's state in. Used by the panels' "Agent runs" sheet (ai-tasks.js)
// and by the shell's status pill.
//   GET /api/agent-runs/stream  -> first {type: "runs.snapshot", runs: [...]}, then each event, its SSE id the event_id
// The browser reconnects by itself and sends Last-Event-ID; the server reads on from there.

/** The states that end a run, and those in which it is at work (only these can go quiet). */
export const FINAL = new Set(['done', 'handed_over', 'cancelled', 'failed']);
export const ACTIVE = new Set(['queued', 'working']);
/** How long a run may say nothing before it is shown silent, and when a run counts as long, in ms (tests set them). */
export const LIVE = { silentMs: 30000, longMs: 120000 };

/** A run as the page keeps it: from a snapshot row or an event, with the time this page last heard of it. */
export function liveRun(data, previous = null) {
  const fromEvent = 'event_id' in data;
  const step = fromEvent ? data.step
    : (data.step_label ? { index: data.step_index, total: data.step_total, label: data.step_label, labels: data.step_labels || {} } : null);
  return {
    run_id: data.run_id,
    agent: data.agent || previous?.agent || '',
    source: data.source || previous?.source || '',
    task: data.task || previous?.task || '',
    title: (fromEvent ? data.subject?.title : data.title) || previous?.title || '',
    state: data.state,
    // a heartbeat repeats the step; an event without one (a start, an end) clears it
    step: step || null,
    // a new turn (a person answered) counts from its own start
    started: (fromEvent && data.type === 'run.started' ? data.ts : previous?.started || (fromEvent ? '' : data.started)) || '',
    seen: Date.now(),
    silentAtStart: !fromEvent && Boolean(data.silent),
    // waits for its person: a result to check, or a failure or handover not seen yet
    needsYou: Boolean(data.needs_you),
    // a heartbeat says nothing new: the question a run waits with stays; a row brings the question it waits for
    payload: fromEvent ? (data.type === 'run.heartbeat' ? previous?.payload || null : data.payload)
      : data.question || previous?.payload || null,
  };
}

/** The step's label in the page's language, else as the agent named it. */
export function stepLabel(step, lang = document.documentElement.lang.slice(0, 2).toLowerCase()) {
  if (!step) return '';
  return (step.labels && step.labels[lang]) || step.label || '';
}

/** Whether a run not ended has said nothing for too long. */
export function isSilent(run, now = Date.now()) {
  return Boolean(run) && ACTIVE.has(run.state) && (run.silentAtStart || now - run.seen > LIVE.silentMs);
}

/** "0:47", "12:05", "1:02:10" -- how long since `since` (an ISO time), or '' without one. */
export function elapsed(since, now = Date.now()) {
  const start = Date.parse(since || '');
  if (Number.isNaN(start)) return '';
  const total = Math.max(0, Math.floor((now - start) / 1000));
  const hours = Math.floor(total / 3600), minutes = Math.floor((total % 3600) / 60), seconds = total % 60;
  const pad = (n) => String(n).padStart(2, '0');
  return hours ? `${hours}:${pad(minutes)}:${pad(seconds)}` : `${minutes}:${pad(seconds)}`;
}

/** Whether a run has been at work longer than usual. */
export function isLong(run, now = Date.now()) {
  const start = Date.parse(run?.started || '');
  return !Number.isNaN(start) && ACTIVE.has(run.state) && now - start > LIVE.longMs;
}

/** A message of the stream to one listener. */
function deliver(listener, text) {
  let data;
  try { data = JSON.parse(text); } catch { return; }
  if (data.type === 'runs.snapshot') listener.onSnapshot?.(data.runs || []);
  else if (data.run_id) listener.onEvent?.(data);
}

//: The worker's address: raise the number when what it and run-events.js say to each other changes -- a running
//: SharedWorker keeps its old code while any tab is open, and a new address gets a new one.
const WORKER = '/static/kit/run-events-worker.js?v=1';

/** The origin's one stream, held by the SharedWorker run-events-worker.js for every page and frame of every tab: a
 * browser allows about six connections per host and an open stream holds one for good -- a stream per page used them
 * up, and a panel opened in a new tab got its page but none of its data. Null where there is no SharedWorker;
 * `broken` is called when its script does not load. */
function viaWorker(listener, broken) {
  if (typeof SharedWorker === 'undefined') return null;
  let worker;
  try {
    worker = new SharedWorker(WORKER, { name: 'run-events' });
  } catch {
    return null;  // not allowed here (a sandboxed frame, say): the tab's stream
  }
  worker.onerror = broken;
  const { port } = worker;
  port.onmessage = ({ data: message }) => {
    if (message?.kind === 'data') deliver(listener, message.data);
    else if (message?.kind === 'state') listener.onState?.(message.state);
  };
  port.start();
  let open = true;
  return {
    close() {  // twice is once: pagehide, then the panel's own close
      if (!open) return;
      open = false;
      port.postMessage({ kind: 'close' });
      port.close();
    },
  };
}

/** Without a SharedWorker, the tab's one stream: the shell and every panel in it (frames of the same origin) share the
 * top window's connection. */
function streamHub() {
  let top = window;
  try {
    if (window.top !== window && window.top.location.origin === window.location.origin) top = window.top;
  } catch { /* a frame of another origin: its own stream */ }
  if (!top.__agentRunsHub) top.__agentRunsHub = makeHub(top);
  return top.__agentRunsHub;
}

function makeHub(win) {
  const listeners = new Set();
  let source = null;
  // a listener that throws costs only that message, as with a stream of its own (a frame that goes lets go on pagehide)
  const each = (call) => {
    for (const listener of [...listeners]) {
      try { call(listener); } catch (error) { console.error('run events listener', error); }
    }
  };
  const open = () => {
    source = new win.EventSource('/api/agent-runs/stream');
    source.onopen = () => each((listener) => listener.onState?.('open'));
    source.onerror = () => each((listener) => listener.onState?.('lost'));
    source.onmessage = (message) => each((listener) => deliver(listener, message.data));
  };
  return {
    add(listener) {
      listeners.add(listener);
      // none yet, or the browser gave it up for good (an error answer to a reconnect): a fresh one
      if (!source || source.readyState === 2) open();
      else {
        // joined a stream already open: its snapshot went out before -- the runs as they stand now instead
        win.fetch('/api/agent-runs', { credentials: 'same-origin' })
          .then((answer) => (answer.ok ? answer.json() : null))
          .then((answer) => { if (answer && listeners.has(listener)) listener.onSnapshot?.(answer.runs || []); })
          .catch(() => { /* the events still come; the snapshot only fills in what came before */ });
      }
      return {
        close() {
          listeners.delete(listener);
          if (!listeners.size && source) {
            source.close();
            source = null;
          }
        },
      };
    },
  };
}

/** Follow the stream: `onSnapshot(runs)` once per (re)connect without a known last event (and once on joining a
 * stream already open), `onEvent(event)` for each event, `onState('open'|'lost')`. Answers a handle whose close() lets
 * go of it, or null where there is no stream (neither a SharedWorker nor an EventSource). */
export function followRuns({ onSnapshot, onEvent, onState } = {}) {
  const listener = { onSnapshot, onEvent, onState };
  const tabStream = () => (typeof EventSource === 'undefined' ? null : streamHub().add(listener));
  let handle = null;
  let following = true;   // a script that fails to load after the page let go must not open a stream for nobody
  const follow = () => viaWorker(listener, () => {
    if (!following) return;
    handle?.close();
    handle = tabStream();
  }) || tabStream();
  handle = follow();
  if (!handle) return null;
  // a page that goes lets go of the stream it shares; one the back/forward cache brings back follows again
  const hide = () => {
    following = false;
    handle?.close();
  };
  const show = (event) => {
    if (!event.persisted) return;
    following = true;
    handle = follow();
  };
  window.addEventListener('pagehide', hide);
  window.addEventListener('pageshow', show);
  return {
    close() {
      hide();
      window.removeEventListener('pagehide', hide);
      window.removeEventListener('pageshow', show);
    },
  };
}
