// The browser's half of the chat commands, exercised against a DOM stub.
//
// The count is advertised by the SHARED command catalogue (chat_commands.py),
// which both surfaces render -- the browser prints it in /help and in the
// autocomplete. A surface that shows a grammar and then discards the argument
// is worse than one that never offered it: the listing looks healthy either
// way, because the header reports whatever it did.
//
// Run: node tests/js/chat_commands_web.test.js   (driven by test_chat_commands_web.py)
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const MODULE = path.join(__dirname, '..', '..', 'static', 'js', 'chat_module.js');
const PAGE = path.join(__dirname, '..', '..', 'templates', 'index.html');
const PAGE_IDS = (fs.readFileSync(PAGE, 'utf8').match(/id="[^"]+"/g) || [])
  .map((hit) => hit.slice(4, -1));

function makeElement() {
  const el = {
    style: {}, dataset: {}, children: [], value: '', textContent: '', innerHTML: '',
    className: '',
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    appendChild(child) { this.children.push(child); return child; },
    // Kept, so a test can do what the page does: submit the form.
    addEventListener(type, fn) { (this.listeners = this.listeners || {})[type] = fn; },
    removeEventListener() {},
    querySelector() { return makeElement(); }, querySelectorAll() { return []; },
    getAttribute() { return null; }, setAttribute() {}, insertAdjacentHTML() {},
    scrollIntoView() {}, focus() {}, remove() {}, closest() { return null; },
    // A real element has these, and the code under test uses them: an input
    // says it changed, and the export link is clicked to start the download.
    dispatchEvent() {}, click() {},
    // What a run's block reaches for as it is built (its folds); a fresh stub each time will do.
    get nextElementSibling() { return makeElement(); },
    hasChildNodes() { return this.children.length > 0; },
    get parentElement() { return makeElement(); },
  };
  return el;
}

/** Everything a note written by addNote() put on screen, oldest first. */
function notesOf(container) {
  const out = [];
  const walk = (node) => {
    if (node.className === 'note-text' && node.textContent) out.push(node.textContent);
    (node.children || []).forEach(walk);
  };
  (container.children || []).forEach(walk);
  return out;
}

function load(sessions, options) {
  const settings = options || {};
  const byId = {};
  // Only the ids the PAGE really has, read off the page itself. A stub that
  // answers every id hides the bug this harness exists to catch: a handler
  // reaching for an element by a name that is not there gets a
  // healthy-looking object here and null in the browser. Everything else
  // answers null, exactly as the DOM would -- including an element the
  // module creates at runtime and then looks up (the read-only banner).
  const document = {
    getElementById(id) {
      if (!PAGE_IDS.includes(id)) return null;
      return (byId[id] = byId[id] || makeElement());
    },
    querySelector() { return makeElement(); },
    querySelectorAll() { return []; },
    createElement() { return makeElement(); },
    addEventListener() {}, removeEventListener() {},
    body: makeElement(), documentElement: makeElement(),
  };
  const calls = [];
  const bodies = [];
  const acted = [];
  const updated = [];  // sessions a run's start named to the session list
  const headers = [];  // what the header was set to, id:title
  // What the tab keeps: two loads share it when a test reloads the page.
  const kept = settings.storage || {};
  const store = { getItem(key) { return key in kept ? kept[key] : null; },
                  setItem(key, value) { kept[key] = String(value); }, removeItem(key) { delete kept[key]; } };
  const listeners = {};
  const window = {
    document,
    location: { href: 'http://localhost/', origin: 'http://localhost' },
    localStorage: store, sessionStorage: store,
    addEventListener(name, fn) { (listeners[name] = listeners[name] || []).push(fn); },
    removeEventListener() {},
    dispatchEvent(event) { (listeners[event.type] || []).forEach((fn) => fn(event)); },
    setTimeout, clearTimeout, setInterval, clearInterval,
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    URL: { createObjectURL() { return 'blob:x'; }, revokeObjectURL() {} },
    EventSource: function () { this.close = function () {}; },
    Event: function (type) { this.type = type; },
    navigator: { clipboard: {} },
    console,
    fetch: async (url, init) => {
      // What the page asks on its own -- the viewer's preferences at load, whether
      // the open session works somewhere -- is not what a command asked: counted,
      // every "asked nothing but X" here failed on requests no command made.
      const own = ['/auth/me/preferences', '/api/sessions/active'];
      if (own.includes(String(url).split('?')[0])) {
        return { ok: true, status: 200, json: async () => ({ chat: {}, active: {} }) };
      }
      calls.push(url);
      // A form (a message with files) as its fields, a JSON body as its object.
      if (init && init.body) {
        bodies.push(init.body instanceof FormData ? Object.fromEntries(init.body) : JSON.parse(init.body));
      }
      let answer = (settings.answers || {})[String(url).split('?')[0]];
      // a list answers call by call, the last one from then on
      if (Array.isArray(answer)) answer = answer.length > 1 ? answer.shift() : answer[0];
      if (answer && answer.sse) {
        // A run's stream: the events, once, then its end.
        const text = answer.sse.map((event) => 'data: ' + JSON.stringify(event) + '\n\n').join('');
        let read = false;
        return { ok: true, status: 200, body: { getReader: () => ({ read: async () => {
          if (read) return { done: true, value: undefined };
          // held back until the test lets it through: the run has not started yet
          if (answer.until) await answer.until;
          read = true;
          return { done: false, value: new TextEncoder().encode(text) };
        } }) } };
      }
      if (answer && answer.fails) {
        // The status matters: /undo tells a 409 (the session is running) from
        // anything else, and offers the way past it only for that one.
        return { ok: false, status: answer.status || 500, statusText: 'Boom',
                 json: async () => ({ detail: answer.fails }),
                 text: async () => JSON.stringify({ detail: answer.fails }) };
      }
      return {
        ok: true, status: 200,
        json: async () => (answer === undefined ? sessions : answer),
        blob: async () => ({ kind: 'blob' }),
      };
    },
    sessionManager: {
      isBeingDeleted: () => false,
      getCurrentSessionId: () => settings.session || null,
      // a new chat's session the chat lets go of before the list has it (shell/sessions.js)
      awaitListing() {},
      loadSessions: async () => {},
      messageWritten: () => {},
      // What the session list holds for each session, stored title included.
      byId: new Map(settings.title ? [[settings.session, { title: settings.title }]] : []),
      loadSession: async (id) => {
        acted.push('load:' + id);
        // The real one announces the switch; the chat learns its session there.
        if (settings.loads) {
          window.dispatchEvent({ type: 'session:loaded',
            detail: { session: { session_id: id, messages: [] } } });
        }
        // false: nothing there to load (a new session before its first save)
        return settings.loadReturns;
      },
      setCurrentSession() {},
      newConversation: async () => {
        acted.push('new');
        // The real one returns undefined either way: it starts a session, or
        // the viewer keeps a running request and it does nothing.
        if (settings.newConversationRefused) return;
        window.dispatchEvent({ type: 'session:new', detail: {} });
      },
      onSessionUpdated: (id) => updated.push(id),
      setCurrent: (id, title) => headers.push(id + ':' + title),
      renameTo: async (id, title) => {
        acted.push('rename:' + id + ':' + title);
        // held back until the test lets it through: a PATCH still on its way
        if (settings.renameUntil && settings.renameUntil[title]) await settings.renameUntil[title];
        return settings.renameFails !== true;
      },
    },
    selectorModule: {
      agents: () => settings.agents || ['coder', 'writer'],
      getCurrentAgent: () => settings.agent || 'coder',
      setAgent: (name) => { acted.push('setAgent:' + name); return settings.setAgentFails !== true; },
    },
    slashCommands: { helpLines() { return []; }, catalogue: {}, attach() {}, close() {} },
    // A file waiting to go out with the next message, when a test asks for one.
    fileUploadModule: settings.files ? {
      hasValidFiles: () => true, getFiles: () => ['pic.png'], removeFiles() {},
      // an image: its preview is an object URL, which the stub has (a text file's needs FileReader)
      getFilesByType: () => ({ images: [{ name: 'pic.png' }], audio: [], text: [] }),
    } : undefined,
  };
  window.window = window;

  const sandbox = Object.assign({}, window, {
    window, document, console, Event: window.Event, URL: window.URL,
    setTimeout, clearTimeout, setInterval, clearInterval,
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    sessionStorage: store, localStorage: store,
    // The module builds one at load (stored sub-runs, the run stream). A vm
    // context has no web globals, so without this every test here failed to
    // load the module -- red since 2a0fe5a28 (19.09.2026).
    AbortController, AbortSignal, TextDecoder, FormData,
    // The chat keeps its end in view with one once it scrolls (a5b1b35f7): every
    // note scrolls, so without it every command here threw on its first note.
    ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
  });
  vm.runInNewContext(fs.readFileSync(MODULE, 'utf8'), sandbox,
    { filename: 'chat_module.js' });

  const chatModule = sandbox.window.chatModule;
  chatModule.init({});
  // Into a session the way the browser gets there: the event the session
  // manager dispatches. No test hook -- the module learns its session id in
  // exactly one place, and a test that set it past that place would not
  // notice if that place stopped working.
  if (settings.session) {
    window.dispatchEvent({
      type: 'session:loaded',
      detail: { session: { session_id: settings.session, messages: [] } },
    });
  }
  return { chatModule, container: document.getElementById('chat'), calls, bodies,
           acted, updated, headers, input: document.getElementById('task'), window,
           // What the page does with the Run button: submit the form with the input's text.
           send: async (text) => {
             document.getElementById('task').value = text;
             await document.getElementById('f').listeners.submit({ preventDefault() {} });
           } };
}

function sessionsFixture(count) {
  return Array.from({ length: count }, (_, i) => ({
    session_id: 'sid' + i,
    title: 'session ' + i,
    agent_name: 'basic_agent',
    message_count: i,
  }));
}

/** What GET /api/sessions/listing answers: the server reads, filters and counts. */
function listingOf(count, extra) {
  return Object.assign({ sessions: sessionsFixture(count), total: count, left_out: 0,
                         most_left_out: null }, extra || {});
}

/** A stream held back until `open()`: the time a run takes to start. */
function held() {
  let open;
  const until = new Promise((resolve) => { open = resolve; });
  return { until, open };
}

const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

const tests = [];
function test(name, fn) { tests.push([name, fn]); }

// The count (a number, 0, `all`), the filter and the tally are the server's --
// the terminal's own functions (tests/app/test_session_resolve_endpoint.py).
// What is left here: what the browser sends, and what it makes of the answer.
test('a bare /sessions asks the shared listing and prints what it says', async () => {
  const { chatModule, container, calls } = load([], {
    answers: { '/api/sessions/listing': listingOf(20, { total: 25 }) } });
  await chatModule.runCommand('sessions', '');
  const note = notesOf(container).join('\n');
  assert.ok(note.includes('Sessions (20 of 25):'), note);
  // 0 lifts the limit and `all` the filter: two words, two meanings
  assert.ok(note.includes('... 5 more -- /sessions <count>, /sessions 0 for no limit'), note);
  // The stub answers by path, so without this the route could be renamed away
  // and every test here would still pass on the answer it was handed.
  assert.deepStrictEqual(calls, ['/api/sessions/listing?count=&agent=coder&current=']);
});

test('the count, this chat\'s agent and its session reach the server', async () => {
  const { chatModule, calls } = load([], { session: 'sid7',
    answers: { '/api/sessions/listing': listingOf(3) } });
  await chatModule.runCommand('sessions', 'all');
  assert.deepStrictEqual(calls, ['/api/sessions/listing?count=all&agent=coder&current=sid7']);
});

test('the runs left out are counted, with the way to see them', async () => {
  const { chatModule, container } = load([], { answers: { '/api/sessions/listing':
    listingOf(2, { left_out: 4562, most_left_out: { agent: 'v4_chapter_scorer', count: 1412 } }) } });
  await chatModule.runCommand('sessions', '');
  const note = notesOf(container).join('\n');
  assert.ok(note.includes('(4562 more on agents not meant for chat, most v4_chapter_scorer 1412 ' +
    '-- /sessions all)'), note);
});

test('a listing whose every row was left out says so, not "No sessions yet."', async () => {
  const { chatModule, container } = load([], { answers: { '/api/sessions/listing':
    listingOf(0, { left_out: 3, most_left_out: { agent: 'v4_chapter_scorer', count: 3 } }) } });
  await chatModule.runCommand('sessions', '');
  const note = notesOf(container).join('\n');
  assert.ok(note.includes('(3 more on agents not meant for chat'), note);
  assert.ok(!note.includes('No sessions yet.'), note);
});

test('a server error is not taken for a bad count', async () => {
  const { chatModule, container } = load([], { answers: { '/api/sessions/listing':
    { fails: 'index gone', status: 500 } } });
  await chatModule.runCommand('sessions', '');
  const note = notesOf(container).join('\n');
  assert.ok(!note.includes('Usage:'), 'a broken server was blamed on the typing: ' + note);
  assert.ok(note.includes('index gone'), note);
});

test('a count the server refuses gets the usage line, not a listing', async () => {
  const { chatModule, container } = load([], { answers: { '/api/sessions/listing':
    { fails: "count must be a number or 'all', got '2o'", status: 400 } } });
  await chatModule.runCommand('sessions', '2o');
  const note = notesOf(container).join('\n');
  assert.ok(note.includes('Usage: /sessions [count|all]   (got: 2o)'), note);
  assert.ok(!note.includes('Sessions ('), 'it listed anyway: ' + note);
});

test('an empty store says so', async () => {
  const { chatModule, container } = load([], { answers: { '/api/sessions/listing': listingOf(0) } });
  await chatModule.runCommand('sessions', '');
  assert.ok(notesOf(container).join('\n').includes('No sessions yet.'));
});

test('a title several sessions share resumes the newest and says it chose', async () => {
  const { chatModule, container, calls, acted } = load([], { session: 'sid1', loads: true,
    answers: { '/api/sessions/resolve': { session_id: 'sid2', others: ['sid0', 'sid1b'] } } });
  await chatModule.runCommand('resume', 'Der Blitter');
  assert.strictEqual(calls[0], '/api/sessions/resolve?ref=Der%20Blitter');
  assert.deepStrictEqual(acted, ['load:sid2']);
  const note = notesOf(container).join('\n');
  assert.ok(note.includes('Resumed session: sid2   (the newest of 3 with this title'), note);
});

test('a resume that did not switch is not reported as done', async () => {
  // loadSession returns quietly when the viewer cancels the "run still
  // active" dialog -- the chat must still be on its old session to say so.
  const { chatModule, container, acted } = load([], { session: 'sid1',
    answers: { '/api/sessions/resolve': { session_id: 'sid2', others: [] } } });
  await chatModule.runCommand('resume', 'sid2');
  assert.deepStrictEqual(acted, ['load:sid2']);
  const note = notesOf(container).join('\n');
  assert.ok(note.includes('was not loaded'), note);
  assert.ok(!note.includes('Resumed session'), note);
});

test('a bare /resume takes the newest listed session that is not this one', async () => {
  const { chatModule, container, calls, acted } = load([], { session: 'sid0', loads: true,
    answers: { '/api/sessions/listing': listingOf(2) } });
  await chatModule.runCommand('resume', '');
  assert.strictEqual(calls[0], '/api/sessions/listing?count=2&agent=coder&current=sid0');
  assert.deepStrictEqual(acted, ['load:sid1'], 'it resumed the session it was already on');
  assert.ok(notesOf(container).join('\n').includes('Resumed session: sid1'));
});

test('a bare /resume with nothing earlier says so', async () => {
  const { chatModule, container, acted } = load([], { session: 'sid0',
    answers: { '/api/sessions/listing': listingOf(1) } });
  await chatModule.runCommand('resume', '');
  assert.deepStrictEqual(acted, []);
  assert.ok(notesOf(container).join('\n').includes('No earlier session to continue'));
});

test('/context names what is filling the window, biggest first', async () => {
  const { chatModule, container, calls } = load([], {
    session: 'sid7',
    answers: { '/chat/context': {
      session_id: 'sid7', agent_name: 'coder',
      window: 200000,
      last_call: { window: 200000, prompt_tokens: 42100, cached: 31000 },
      estimated: { total: 44000, parts: {
        questions: { tokens: 1200, count: 12 },
        tool_results: { tokens: 29900, count: 34 },
        answers: { tokens: 7600, count: 30 },
        system_prompt: { tokens: 1900, count: 1 },
        tools: { tokens: 3400, count: 47 },
      } },
    } },
  });
  await chatModule.runCommand('context', '');
  assert.deepStrictEqual(calls, ['/chat/context?session_id=sid7&agent_name=coder']);
  const note = notesOf(container).join('\n');
  const order = ['tool results', 'answers', 'tool schemas', 'system prompt', 'your messages'];
  const places = order.map((label) => note.indexOf(label));
  assert.deepStrictEqual(places, places.slice().sort((a, b) => a - b),
    'the parts are not sorted by size: ' + note);
  assert.ok(places[0] > -1, note);
  // The measurement and the estimate are BOTH there, and apart.
  assert.ok(note.includes('last call'), note);
  // No thousands separator in the assertion: toLocaleString gives the
  // VIEWER's, which is the right thing on screen and the wrong thing to nail
  // down in a test that runs wherever node happens to be configured.
  assert.ok(note.includes('of them cached'), note);
  assert.ok(note.includes('together'), note);
});

test('/context says when the measurement describes a conversation that is gone', async () => {
  const { chatModule, container } = load([], {
    session: 'sid7',
    answers: { '/chat/context': {
      session_id: 'sid7',
      window: 128000,
      last_call: { window: 100, prompt_tokens: 40, is_stale: true },
      estimated: { total: 10, parts: { questions: { tokens: 10, count: 1 } } },
    } },
  });
  await chatModule.runCommand('context', '');
  const note = notesOf(container).join('\n');
  assert.ok(note.includes('stale'),
    'a number from before a rewrite was shown as if it still held');
  // ...and the measurement is held against the window IT ran on (100), while
  // the estimate is held against the one the NEXT call will use (128000). A
  // /model switch is exactly what makes those two different numbers.
  assert.ok(note.includes('of 100'), note);
  assert.ok(/of 128[.,]000/.test(note), note);
});

test('/context without a session says so instead of asking the server', async () => {
  const { chatModule, container, calls } = load([], { session: null });
  await chatModule.runCommand('context', '');
  assert.deepStrictEqual(calls, []);
  assert.ok(notesOf(container).join('\n').includes('No session yet'));
});

test('/title goes through the session list, not a PATCH of its own', async () => {
  const { chatModule, container, acted, calls } = load([], { session: 'sid7' });
  await chatModule.runCommand('title', 'Blitter umbauen');
  assert.deepStrictEqual(acted, ['rename:sid7:Blitter umbauen']);
  assert.deepStrictEqual(calls, [], 'it wrote past the session list');
  assert.ok(notesOf(container).join('\n').includes('Title: Blitter umbauen'));
});

test('a bare /title of a session without one says so -- not the header\'s "Untitled"', async () => {
  const { chatModule, container } = load([], { session: 'sid7' });
  await chatModule.runCommand('title', '');
  const note = notesOf(container).join('\n');
  assert.ok(note.includes('This session has no title yet.'), note);
  assert.ok(!note.includes('Title:'), note);
});

// Before the first message there is no session to rename: the title waits,
// goes out with that message and is written by the run's first save -- as in
// agent-cli, where `/title` then answers "(written with the first message)".
test('a /title before the first message waits for it, as in the terminal', async () => {
  const { chatModule, container, acted, calls } = load([], {});
  await chatModule.runCommand('title', 'Blitter umbauen');
  await chatModule.runCommand('title', '');
  assert.deepStrictEqual(acted, [], 'it renamed a session that is not there');
  assert.deepStrictEqual(calls, []);
  const note = notesOf(container).join('\n');
  assert.ok(note.includes('Title: Blitter umbauen   (written with the first message)'), note);
  assert.ok(note.split('Title: Blitter umbauen').length === 3, 'a bare /title did not show it: ' + note);
});

test('the first message takes the waiting title along, and the run has it from then', async () => {
  const { chatModule, container, bodies, send, window } = load([], { answers: {
    '/events': { sse: [{ type: 'start', request_id: 'r1', session_id: 'new1' }] } } });
  await chatModule.runCommand('title', 'Blitter umbauen');
  await send('hallo');
  assert.strictEqual(bodies[0].task, 'hallo');
  assert.strictEqual(bodies[0].session_title, 'Blitter umbauen');
  assert.strictEqual(bodies[0].session_id, undefined);
  // handed over at the start: leaving the new session later has nothing to say about it
  window.dispatchEvent({ type: 'session:new', detail: {} });
  const note = notesOf(container).join('\n');
  assert.ok(!note.includes('(the title'), note);
});

test('a first message the server refused gives its title back to the next one', async () => {
  const { chatModule, container, bodies, send } = load([], { answers: {
    '/events': { fails: 'Boom', status: 500 } } });
  await chatModule.runCommand('title', 'Blitter umbauen');
  await send('hallo');
  await chatModule.runCommand('title', '');
  const notes = notesOf(container);
  assert.strictEqual(notes[notes.length - 1].split('\n')[0], 'Title: Blitter umbauen', notes.join('\n'));
  await send('nochmal');
  assert.strictEqual(bodies[1].session_title, 'Blitter umbauen');
});

test('leaving after a /title typed while the first message starts says that one was not written', async () => {
  const gate = held();
  const { chatModule, container, send, window } = load([], { answers: {
    '/events': { sse: [{ type: 'start', request_id: 'r1', session_id: 'new1' }], until: gate.until } } });
  await chatModule.runCommand('title', 'Blitter umbauen');
  const sent = send('hallo');
  await tick();
  await chatModule.runCommand('title', 'Copper-Liste');
  window.dispatchEvent({ type: 'session:new', detail: { chosen: true } });
  gate.open();
  await sent;
  const note = notesOf(container).join('\n');
  assert.ok(note.includes("(the title 'Copper-Liste' was not written -- the chat left before"), note);
  assert.ok(!note.includes('no message went out'), 'a message went out: ' + note);
});

test('a bare /title during the first run names what its first save writes', async () => {
  const { chatModule, container, send } = load([], { answers: {
    '/events': { sse: [{ type: 'start', request_id: 'r1', session_id: 'new1' }] } } });
  await chatModule.runCommand('title', 'Blitter umbauen');
  await send('hallo');
  // the list has no record of new1 until that save
  await chatModule.runCommand('title', '');
  let notes = notesOf(container);
  assert.strictEqual(notes[notes.length - 1].split('\n')[0], 'Title: Blitter umbauen', notes.join('\n'));
  await chatModule.runCommand('title', 'Copper-Liste');
  await chatModule.runCommand('title', '');
  notes = notesOf(container);
  assert.strictEqual(notes[notes.length - 1].split('\n')[0], 'Title: Copper-Liste', notes.join('\n'));
});

test('a /title typed while the first message starts goes to its session by name', async () => {
  const gate = held();
  const { chatModule, acted, bodies, container, send } = load([], { answers: {
    '/events': { sse: [{ type: 'start', request_id: 'r1', session_id: 'new1' }], until: gate.until } } });
  await chatModule.runCommand('title', 'Blitter umbauen');
  const sent = send('hallo');
  await tick();
  await chatModule.runCommand('title', 'Copper-Liste');
  gate.open();
  await sent;
  assert.strictEqual(bodies[0].session_title, 'Blitter umbauen');
  // the server keeps it for the run's first save -- lost at the start before
  assert.deepStrictEqual(acted, ['rename:new1:Copper-Liste']);
  await tick();
  await chatModule.runCommand('title', '');
  const notes = notesOf(container);
  assert.strictEqual(notes[notes.length - 1].split('\n')[0], 'Title: Copper-Liste', notes.join('\n'));
});

test('a /title typed while the first message starts that could not be written leaves the one that went out', async () => {
  const gate = held();
  const { chatModule, container, send } = load([], { renameFails: true, answers: {
    '/events': { sse: [{ type: 'start', request_id: 'r1', session_id: 'new1' }], until: gate.until } } });
  await chatModule.runCommand('title', 'Blitter umbauen');
  const sent = send('hallo');
  await tick();
  await chatModule.runCommand('title', 'Copper-Liste');
  gate.open();
  await sent;
  await tick();
  assert.ok(notesOf(container).join('\n').includes(
    "(the title 'Copper-Liste' was not written -- the session keeps 'Blitter umbauen')"), notesOf(container).join('\n'));
  await chatModule.runCommand('title', '');
  const notes = notesOf(container);
  assert.strictEqual(notes[notes.length - 1].split('\n')[0], 'Title: Blitter umbauen', notes.join('\n'));
});

test('a refused rename answered after the chat went elsewhere says nothing there', async () => {
  const gate = held();
  const renamed = held();
  const { chatModule, container, send, window } = load([], { renameFails: true,
    renameUntil: { 'Copper-Liste': renamed.until }, answers: {
      '/events': { sse: [{ type: 'start', request_id: 'r1', session_id: 'new1' }], until: gate.until } } });
  await chatModule.runCommand('title', 'Blitter umbauen');
  const sent = send('hallo');
  await tick();
  await chatModule.runCommand('title', 'Copper-Liste');
  gate.open();
  await sent;
  window.dispatchEvent({ type: 'session:loaded', detail: { session: { session_id: 's9', messages: [] } } });
  renamed.open();
  await tick();
  const note = notesOf(container).join('\n');
  assert.ok(!note.includes("'Copper-Liste' was not written"), 'said in another session\'s chat: ' + note);
});

test('a refused rename still says so when the list got the session meanwhile', async () => {
  const gate = held();
  const renamed = held();
  const { chatModule, container, send, window } = load([], { renameFails: true,
    renameUntil: { 'Copper-Liste': renamed.until }, answers: {
      '/events': { sse: [{ type: 'start', request_id: 'r1', session_id: 'new1' }], until: gate.until } } });
  await chatModule.runCommand('title', 'Blitter umbauen');
  const sent = send('hallo');
  await tick();
  await chatModule.runCommand('title', 'Copper-Liste');
  gate.open();
  await sent;
  // the list has the session -- the title kept beside it not yet let go of
  window.sessionManager.byId.set('new1', { title: 'Blitter umbauen' });
  renamed.open();
  await tick();
  const note = notesOf(container).join('\n');
  // the list has the session: what it keeps is the list's, maybe renamed since
  assert.ok(note.includes("(the title 'Copper-Liste' was not written)"), note);
});

test('a /title after the start stands over an older rename still on its way', async () => {
  const gate = held();
  const renamed = held();
  const { chatModule, container, send } = load([], { renameUntil: { 'Copper-Liste': renamed.until }, answers: {
    '/events': { sse: [{ type: 'start', request_id: 'r1', session_id: 'new1' }], until: gate.until } } });
  await chatModule.runCommand('title', 'Blitter umbauen');
  const sent = send('hallo');
  await tick();
  await chatModule.runCommand('title', 'Copper-Liste');
  gate.open();
  await sent;
  await chatModule.runCommand('title', 'Zweiter Name');  // the session is there: renamed at once
  renamed.open();
  await tick();
  await chatModule.runCommand('title', '');
  const notes = notesOf(container);
  assert.strictEqual(notes[notes.length - 1].split('\n')[0], 'Title: Zweiter Name', notes.join('\n'));
});

test('a rename answered late does not undo another session\'s title', async () => {
  const gateA = held();
  const gateB = held();
  const renamedA = held();
  const renamedB = held();
  const { chatModule, container, send, window } = load([], {
    renameUntil: { 'Copper-Liste': renamedA.until, 'Zweiter Name': renamedB.until }, answers: { '/events': [
      { sse: [{ type: 'start', request_id: 'r1', session_id: 'new1' }], until: gateA.until },
      { sse: [{ type: 'start', request_id: 'r2', session_id: 'new2' }], until: gateB.until }] } });
  await chatModule.runCommand('title', 'Blitter umbauen');
  const sentA = send('hallo');
  await tick();
  await chatModule.runCommand('title', 'Copper-Liste');
  gateA.open();
  await sentA;  // new1 started: its rename is on its way
  window.dispatchEvent({ type: 'session:new', detail: { chosen: true } });
  const sentB = send('ganz andere Sache');
  await tick();
  await chatModule.runCommand('title', 'Zweiter Name');
  gateB.open();
  await sentB;  // new2 started: its rename is on its way too
  renamedA.open();
  await tick();
  renamedB.open();
  await tick();
  await chatModule.runCommand('title', '');
  const notes = notesOf(container);
  assert.strictEqual(notes[notes.length - 1].split('\n')[0], 'Title: Zweiter Name', notes.join('\n'));
});

test('a bare /title while the first message starts names the title that went with it', async () => {
  const gate = held();
  const { chatModule, container, send } = load([], { answers: {
    '/events': { sse: [{ type: 'start', request_id: 'r1', session_id: 'new1' }], until: gate.until } } });
  await chatModule.runCommand('title', 'Blitter umbauen');
  const sent = send('hallo');
  await tick();
  await chatModule.runCommand('title', '');
  gate.open();
  await sent;
  const notes = notesOf(container);
  assert.strictEqual(notes[notes.length - 1].split('\n')[0], 'Title: Blitter umbauen', notes.join('\n'));
});

test('the header names a titled first run while the session list does not have it', async () => {
  const { chatModule, headers, send } = load([], { answers: {
    '/events': { sse: [{ type: 'start', request_id: 'r1', session_id: 'new1' }] } } });
  await chatModule.runCommand('title', 'Blitter umbauen');
  await send('hallo');
  assert.deepStrictEqual(headers, ['new1:Blitter umbauen']);
});

test('a message into a titled first run\'s session keeps its title in the header', async () => {
  const gate = held();
  const { chatModule, headers, send, window } = load([], { answers: { '/events': [
    { sse: [{ type: 'start', request_id: 'r1', session_id: 'new1' }] }, { sse: [], until: gate.until }] } });
  // as the real pane does: the header from its list, which does not have the session yet
  window.sessionManager.messageWritten = (id) => window.sessionManager.setCurrent(id, undefined);
  await chatModule.runCommand('title', 'Blitter umbauen');
  await send('hallo');
  const next = send('weiter');  // sent: its run is starting
  await tick();
  assert.strictEqual(headers[headers.length - 1], 'new1:Blitter umbauen', headers.join(', '));
  await send('noch was');  // held until that run has started
  assert.strictEqual(headers[headers.length - 1], 'new1:Blitter umbauen', headers.join(', '));
  gate.open();
  await next;
});

test('a reload during the first run still knows its title', async () => {
  const storage = {};
  const first = load([], { storage, answers: {
    '/events': { sse: [{ type: 'start', request_id: 'r1', session_id: 'new1' }] } } });
  await first.chatModule.runCommand('title', 'Blitter umbauen');
  await first.send('hallo');
  // the page again, in the session the run started -- the list has no record of it yet
  const again = load([], { storage, session: 'new1' });
  await again.chatModule.runCommand('title', '');
  const notes = notesOf(again.container);
  assert.strictEqual(notes[notes.length - 1].split('\n')[0], 'Title: Blitter umbauen', notes.join('\n'));
});

test('a reload during the first run shows its title in the header again', async () => {
  // the run this tab followed, still going; its session has no record to load yet
  const storage = { activeRequestId: 'r1', activeRequestSession: 'new1',
                    unlistedSessionTitles: JSON.stringify({ new1: { title: 'Blitter umbauen' } }) };
  const { acted, headers } = load([], { storage, loadReturns: false, answers: {
    '/api/requests/r1/status': { status: 'running' } } });
  for (let i = 0; i < 10 && !headers.length; i++) await tick();
  assert.deepStrictEqual(acted, ['load:new1'], 'fixture: the reload did not look for the run\'s session');
  assert.deepStrictEqual(headers, ['new1:Blitter umbauen']);
});

test('the header keeps the title a listed session has', async () => {
  // the first run's title, kept -- the session renamed in the list since
  const storage = { unlistedSessionTitles: JSON.stringify({ sid7: { title: 'Blitter umbauen' } }) };
  const { headers, send, updated } = load([], { storage, session: 'sid7', title: 'Neuer Name', answers: {
    '/events': { sse: [{ type: 'start', request_id: 'r1', session_id: 'sid7' }] } } });
  await send('weiter');
  assert.deepStrictEqual(updated, ['sid7'], 'fixture: the run did not start');
  assert.deepStrictEqual(headers, []);
});

test('a title the session list has is not kept beside it', async () => {
  const storage = {};
  const { chatModule, send, window } = load([], { storage, answers: {
    '/events': { sse: [{ type: 'start', request_id: 'r1', session_id: 'new1' }] } } });
  await chatModule.runCommand('title', 'Blitter umbauen');
  await send('hallo');
  assert.ok(JSON.parse(storage.unlistedSessionTitles).new1, 'fixture: the first run\'s title was not kept');
  // the first save is done: the list has the session -- renamed elsewhere since
  window.sessionManager.byId.set('new1', { title: 'Anderswo umbenannt' });
  await chatModule.runCommand('title', '');
  assert.strictEqual(JSON.parse(storage.unlistedSessionTitles).new1, undefined);
});

test('renaming a listed session keeps no title beside the list', async () => {
  const storage = {};
  const { chatModule } = load([], { storage, session: 'sid7', title: 'alt' });
  await chatModule.runCommand('title', 'neu');
  assert.ok(!('unlistedSessionTitles' in storage), JSON.stringify(storage));
});

test('the first run\'s title is its session\'s only', async () => {
  const { chatModule, container, send, window } = load([], { answers: {
    '/events': { sse: [{ type: 'start', request_id: 'r1', session_id: 'new1' }] } } });
  await chatModule.runCommand('title', 'Blitter umbauen');
  await send('hallo');
  window.dispatchEvent({ type: 'session:loaded', detail: { session: { session_id: 's2', messages: [] } } });
  await chatModule.runCommand('title', '');
  const notes = notesOf(container);
  assert.strictEqual(notes[notes.length - 1].split('\n')[0], 'This session has no title yet.', notes.join('\n'));
});

test('a stored session that could not be shown lets go of a title typed for the message on its way', async () => {
  const gate = held();
  const { chatModule, container, bodies, send, window } = load([], { answers: {
    '/events': { sse: [{ type: 'start', request_id: 'r1', session_id: 'new1' }], until: gate.until } } });
  const sent = send('hallo');
  await tick();
  await chatModule.runCommand('title', 'Copper-Liste');
  window.dispatchEvent({ type: 'session:new', detail: { chosen: false } });
  gate.open();
  await sent;
  assert.ok(notesOf(container).join('\n').includes(
    "(the title 'Copper-Liste' was not written -- the chat left before"), notesOf(container).join('\n'));
  await send('ganz andere Sache');
  assert.ok(!('session_title' in bodies[1]), 'went with an unrelated message: ' + JSON.stringify(bodies[1]));
});

test('opening a stored session drops the waiting title with a word', async () => {
  const { chatModule, container, window } = load([], {});
  await chatModule.runCommand('title', 'Blitter umbauen');
  window.dispatchEvent({ type: 'session:loaded', detail: { session: { session_id: 's9', messages: [] } } });
  const note = notesOf(container).join('\n');
  assert.ok(note.includes("(the title 'Blitter umbauen' was not written -- no message went out)"), note);
  await chatModule.runCommand('title', '');
  const notes = notesOf(container);
  assert.strictEqual(notes[notes.length - 1].split('\n')[0], 'This session has no title yet.', notes.join('\n'));
});

test('leaving while the first message starts says the title went with it', async () => {
  const gate = held();
  const { chatModule, container, send, window } = load([], { answers: {
    '/events': { sse: [{ type: 'start', request_id: 'r1', session_id: 'new1' }], until: gate.until } } });
  await chatModule.runCommand('title', 'Blitter umbauen');
  const sent = send('hallo');
  await tick();
  window.dispatchEvent({ type: 'session:new', detail: { chosen: true } });
  gate.open();
  await sent;
  const note = notesOf(container).join('\n');
  assert.ok(note.includes("(the title 'Blitter umbauen' went out with your message: a session the server starts for it is named so)"), note);
  assert.ok(!note.includes('was not written'), 'the run writes it -- "not written" was untrue: ' + note);
});

test('leaving while a first message with a file starts says the same', async () => {
  const gate = held();
  const { chatModule, container, send, window } = load([], { files: true, answers: {
    '/run': { sse: [{ type: 'start', request_id: 'r1', session_id: 'new1' }], until: gate.until } } });
  await chatModule.runCommand('title', 'Blitter umbauen');
  const sent = send('lies das');
  await tick();
  window.dispatchEvent({ type: 'session:new', detail: { chosen: true } });
  gate.open();
  await sent;
  const note = notesOf(container).join('\n');
  assert.ok(note.includes("went out with your message: a session the server starts for it is named so)"), note);
});

test('a stored session that could not be shown lets go of a first message on its way too', async () => {
  // the chat let go of the run: nothing waits for its start any more
  const gate = held();
  const { chatModule, container, send, window } = load([], { answers: {
    '/events': { sse: [{ type: 'start', request_id: 'r1', session_id: 'new1' }], until: gate.until } } });
  await chatModule.runCommand('title', 'Blitter umbauen');
  const sent = send('hallo');
  await tick();
  window.dispatchEvent({ type: 'session:new', detail: { chosen: false } });
  gate.open();
  await sent;
  assert.ok(notesOf(container).join('\n').includes("went out with your message: a session the server starts for it is named so)"), notesOf(container).join('\n'));
  await chatModule.runCommand('title', '');
  const notes = notesOf(container);
  assert.strictEqual(notes[notes.length - 1].split('\n')[0], 'This session has no title yet.', notes.join('\n'));
});

test('a stored session that could not be shown keeps the waiting title', async () => {
  // session:new nobody chose: the chat stays the new one it was
  const { chatModule, container, window } = load([], {});
  await chatModule.runCommand('title', 'Blitter umbauen');
  window.dispatchEvent({ type: 'session:new', detail: { chosen: false } });
  await chatModule.runCommand('title', '');
  const note = notesOf(container).join('\n');
  assert.ok(!note.includes('was not written'), note);
  assert.ok(note.split('Title: Blitter umbauen').length === 3, 'dropped: ' + note);
});

test('a waiting title is dropped with a word when the chat goes elsewhere first', async () => {
  const { chatModule, container, window } = load([], {});
  await chatModule.runCommand('title', 'Blitter umbauen');
  window.dispatchEvent({ type: 'session:new', detail: {} });
  await chatModule.runCommand('title', '');
  const note = notesOf(container).join('\n');
  assert.ok(note.includes("(the title 'Blitter umbauen' was not written"), note);
  assert.ok(note.includes('This session has no title yet.'), 'it was kept: ' + note);
});

test('a run the chat starts says a person reads it, both ways out', async () => {
  // tool_approval asks a person only where one can answer: the client that starts
  // the run says so, and only this one does (a program reading /events does not).
  const text = load([], { answers: {
    '/events': { sse: [{ type: 'start', request_id: 'r1', session_id: 'new1' }] } } });
  await text.send('hallo');
  assert.strictEqual(text.bodies[0].attended, true, JSON.stringify(text.bodies[0]));
  const withFile = load([], { files: true, answers: {
    '/run': { sse: [{ type: 'start', request_id: 'r1', session_id: 'new1' }] } } });
  await withFile.send('lies das');
  assert.strictEqual(withFile.calls[0], '/run');
  assert.strictEqual(withFile.bodies[0].attended, 'true', JSON.stringify(withFile.bodies[0]));
});

test('a first message with a file takes the waiting title along too', async () => {
  // Sent as a form to /run, not as JSON to /events: the other of the two ways out.
  const { chatModule, bodies, calls, send } = load([], { files: true, answers: {
    '/run': { sse: [{ type: 'start', request_id: 'r1', session_id: 'new1' }] } } });
  await chatModule.runCommand('title', 'Blitter umbauen');
  await send('lies das');
  assert.strictEqual(calls[0], '/run');
  assert.strictEqual(bodies[0].session_title, 'Blitter umbauen');
  assert.ok(!('session_id' in bodies[0]), JSON.stringify(bodies[0]));
});

test('a first message with a file the server refused gives its title back too', async () => {
  const { chatModule, bodies, send } = load([], { files: true, answers: {
    '/run': { fails: 'Boom', status: 500 } } });
  await chatModule.runCommand('title', 'Blitter umbauen');
  await send('lies das');
  await send('nochmal');
  assert.strictEqual(bodies[1].session_title, 'Blitter umbauen');
});

test('a message into a session sends no title', async () => {
  const { bodies, send, updated } = load([], { session: 'sid7', answers: {
    '/events': { sse: [{ type: 'start', request_id: 'r1', session_id: 'sid7' }] } } });
  await send('weiter');
  assert.strictEqual(bodies[0].session_id, 'sid7');
  assert.ok(!('session_title' in bodies[0]), JSON.stringify(bodies[0]));
  // the start went on past the title's settling: the session list was told
  assert.deepStrictEqual(updated, ['sid7']);
});

test('a bare /title says the stored title, and how to set one', async () => {
  const { chatModule, container, acted } = load([], { session: 'sid7', title: 'Blitter umbauen' });
  await chatModule.runCommand('title', '   ');
  assert.deepStrictEqual(acted, [], 'it renamed with nothing');
  const note = notesOf(container).join('\n');
  assert.ok(note.includes('Title: Blitter umbauen'), note);
  assert.ok(note.includes('Usage: /title <text>'), note);
});

test('a rename that failed is not reported as done', async () => {
  const { chatModule, container } = load([], { session: 'sid7', renameFails: true });
  await chatModule.runCommand('title', 'Neu');
  const note = notesOf(container).join('\n');
  assert.ok(note.includes('was not renamed'), note);
  assert.ok(!note.includes('Title: Neu'), note);
});

test('a bare /agent lists the selector\'s agents and marks the current one', async () => {
  const { chatModule, container } = load([], { session: 'sid7' });
  await chatModule.runCommand('agent', '');
  const note = notesOf(container).join('\n');
  assert.ok(note.includes('* coder'), note);
  assert.ok(note.includes('writer'), note);
});

test('/agent switches the selector and starts a new session', async () => {
  const { chatModule, acted } = load([], { session: 'sid7' });
  await chatModule.runCommand('agent', 'writer');
  assert.deepStrictEqual(acted, ['setAgent:writer', 'new'],
    'the switch and the new session have to happen, and in that order');
});

test('/agent does not claim a new session that never started', async () => {
  // newConversation() says nothing about whether it happened. Claimed anyway,
  // the next message runs the OLD session under the NEW agent -- and the save
  // after it writes that agent into the old session's record.
  const { chatModule, container, acted } = load([], {
    session: 'sid7', newConversationRefused: true,
  });
  await chatModule.runCommand('agent', 'writer');
  assert.deepStrictEqual(acted, ['setAgent:writer', 'new']);
  const note = notesOf(container).join('\n');
  assert.ok(note.includes('the session stayed'), note);
  assert.ok(!note.includes('(new session)'), note);
});

test('/undo names the way past a lock a crashed process left behind', async () => {
  const { chatModule, container } = load([], {
    session: 'sid7',
    answers: { '/chat/undo': { fails: 'Session sid7 is running', status: 409 } },
  });
  await chatModule.runCommand('undo', '');
  const note = notesOf(container).join('\n');
  assert.ok(note.includes('Session sid7 is running'), note);
  assert.ok(note.includes('/undo force'), note);
});

test('/undo force asks the server to take it anyway', async () => {
  const { chatModule, calls } = load([], {
    session: 'sid7', answers: { '/chat/undo': { dropped: { text: 'frage' } } },
  });
  await chatModule.runCommand('undo', 'force');
  assert.deepStrictEqual(calls, ['/chat/undo?force=true']);
});

test('/agent refuses a name the selector does not know', async () => {
  const { chatModule, container, acted } = load([], { session: 'sid7' });
  await chatModule.runCommand('agent', 'writr');
  assert.deepStrictEqual(acted, [], 'it switched to an agent that does not exist');
  assert.ok(notesOf(container).join('\n').includes('Unknown agent: writr'));
});

test('/undo asks the server to cut and reloads what is left', async () => {
  const { chatModule, container, calls, bodies, acted } = load([], {
    session: 'sid7',
    answers: { '/chat/undo': { dropped: { text: 'schreib die routine' } } },
  });
  await chatModule.runCommand('undo', '');
  assert.deepStrictEqual(calls, ['/chat/undo']);
  assert.strictEqual(bodies[0].session_id, 'sid7');
  assert.deepStrictEqual(acted, ['load:sid7'],
    'the dropped turn stayed on screen');
  assert.ok(notesOf(container).join('\n').includes('schreib die routine'));
});

test('/undo with nothing to take back reloads nothing', async () => {
  const { chatModule, container, acted } = load([], {
    session: 'sid7', answers: { '/chat/undo': { dropped: null } },
  });
  await chatModule.runCommand('undo', '');
  assert.deepStrictEqual(acted, []);
  assert.ok(notesOf(container).join('\n').includes('Nothing to take back'));
});

test('/retry puts the question back in the input instead of sending it', async () => {
  const { chatModule, container, input } = load([], {
    session: 'sid7',
    answers: { '/chat/undo': { dropped: { text: 'schreib die routine' } } },
  });
  await chatModule.runCommand('retry', '');
  assert.strictEqual(input.value, 'schreib die routine');
  assert.ok(notesOf(container).join('\n').includes('Ask it again with Enter'));
});

test('/retry says when the file it carried cannot come along', async () => {
  const { chatModule, container } = load([], {
    session: 'sid7',
    answers: { '/chat/undo': { dropped: { text: 'was ist das?', had_attachments: true } } },
  });
  await chatModule.runCommand('retry', '');
  assert.ok(notesOf(container).join('\n').includes('has to be attached again'));
});

// /undo files, /rewind: the words are chat_commands.parse_undo's, the lines the
// server's (the plugin renders them for both surfaces). What is left here: what
// the browser sends, that a refusal keeps the exchange on screen, and that a
// word it does not know sends nothing.
test('/undo files asks the server to put the files back and says what it did', async () => {
  const { chatModule, container, calls, bodies, acted } = load([], {
    session: 'sid7',
    answers: { '/chat/undo': { dropped: { text: 'schreib die routine' },
                               files: { status: 'rewound', text: 'Files rewound to before the last turn: 1 put back, 0 removed.' } } },
  });
  await chatModule.runCommand('undo', 'files');
  assert.deepStrictEqual(calls, ['/chat/undo']);
  assert.strictEqual(bodies[0].files, true);
  assert.strictEqual(bodies[0].overwrite, false);
  assert.deepStrictEqual(acted, ['load:sid7']);
  assert.ok(notesOf(container).join('\n').includes('1 put back'), notesOf(container).join('\n'));
});

test('/undo files refused keeps the exchange and says why -- not the lock hint', async () => {
  const { chatModule, container, acted } = load([], {
    session: 'sid7',
    answers: { '/chat/undo': { fails: 'Files not rewound: b.txt was changed since', status: 409 } },
  });
  await chatModule.runCommand('undo', 'files');
  const note = notesOf(container).join('\n');
  assert.ok(note.includes('b.txt was changed since') && note.includes('The exchange stays'), note);
  assert.ok(!note.includes('/undo force'), note);
  assert.deepStrictEqual(acted, [], 'it reloaded a conversation nothing was taken from');
});

test('/undo --files overwrite sends both words', async () => {
  const { chatModule, bodies } = load([], {
    session: 'sid7', answers: { '/chat/undo': { dropped: { text: 'q' }, files: { text: 'ok' } } },
  });
  await chatModule.runCommand('undo', '--files overwrite');
  assert.strictEqual(bodies[0].files, true);
  assert.strictEqual(bodies[0].overwrite, true);
});

test('/undo with a word it does not know sends nothing', async () => {
  const { chatModule, container, calls } = load([], { session: 'sid7' });
  await chatModule.runCommand('undo', 'fils');
  assert.deepStrictEqual(calls, []);
  assert.ok(notesOf(container).join('\n').includes('/undo files'));
});

test('/rewind lists the checkpoints the server numbers', async () => {
  const { chatModule, container, calls } = load([], {
    session: 'sid7', agent: 'coder',
    answers: { '/chat/checkpoints': { checkpoints: [], text: 'File checkpoints -- /rewind <n> puts ...' } },
  });
  await chatModule.runCommand('rewind', '');
  assert.deepStrictEqual(calls, ['/chat/checkpoints?session_id=sid7&agent_name=coder']);
  assert.ok(notesOf(container).join('\n').includes('File checkpoints'));
});

test('/rewind <n> overwrite asks for that checkpoint', async () => {
  const { chatModule, container, calls, bodies } = load([], {
    session: 'sid7', answers: { '/chat/rewind': { status: 'rewound', text: 'Files rewound to before checkpoint 2.' } },
  });
  await chatModule.runCommand('rewind', '2 overwrite');
  assert.deepStrictEqual(calls, ['/chat/rewind']);
  assert.strictEqual(bodies[0].checkpoint, 2);
  assert.strictEqual(bodies[0].overwrite, true);
  assert.strictEqual(bodies[0].session_id, 'sid7');
  assert.ok(notesOf(container).join('\n').includes('before checkpoint 2'));
});

test('/rewind says what the server refused', async () => {
  const { chatModule, container } = load([], {
    session: 'sid7', answers: { '/chat/rewind': { fails: 'There is no checkpoint 9', status: 404 } },
  });
  await chatModule.runCommand('rewind', '9');
  const note = notesOf(container).join('\n');
  assert.ok(note.includes('There is no checkpoint 9'), note);
  assert.ok(!note.includes('failed'), note);
});

test('/rewind -1 is not checkpoint 1', async () => {
  const { chatModule, container, calls } = load([], { session: 'sid7' });
  await chatModule.runCommand('rewind', '-1');
  assert.deepStrictEqual(calls, [], 'it rewound to checkpoint 1');
  assert.ok(notesOf(container).join('\n').includes('Usage: /rewind'));
});

test('/rewind <n> force asks past a leftover lock', async () => {
  const { chatModule, calls } = load([], {
    session: 'sid7', answers: { '/chat/rewind': { status: 'rewound', text: 'ok' } },
  });
  await chatModule.runCommand('rewind', '3 force');
  assert.deepStrictEqual(calls, ['/chat/rewind?force=true']);
});

test('/rewind overwrite without a number sends nothing', async () => {
  const { chatModule, container, calls } = load([], { session: 'sid7' });
  await chatModule.runCommand('rewind', 'overwrite');
  assert.deepStrictEqual(calls, []);
  assert.ok(notesOf(container).join('\n').includes('Usage: /rewind'));
});

test('/export downloads the markdown the server rendered', async () => {
  const { chatModule, container, calls } = load([], {
    session: 'sid7', answers: { '/chat/transcript': {} },
  });
  await chatModule.runCommand('export', '');
  assert.deepStrictEqual(calls, ['/chat/transcript?session_id=sid7']);
  assert.ok(notesOf(container).join('\n').includes('chat-sid7.md'));
});

test('/export says a path only means something in the terminal', async () => {
  const { chatModule, container } = load([], {
    session: 'sid7', answers: { '/chat/transcript': {} },
  });
  await chatModule.runCommand('export', '/tmp/x.md');
  assert.ok(notesOf(container).join('\n').includes('only means something in the terminal'));
});

test('a failed export is not reported as written', async () => {
  const { chatModule, container } = load([], {
    session: 'sid7', answers: { '/chat/transcript': { fails: 'No session' } },
  });
  await chatModule.runCommand('export', '');
  const note = notesOf(container).join('\n');
  assert.ok(note.includes('Could not export: No session'), note);
  assert.ok(!note.includes('Written:'), note);
  // ...and it STOPPED there. Without the return it walks on into resp.blob()
  // of a failed response, which throws -- the person then gets the failure a
  // second time as '/export failed: ...'. Both notes lack 'Written:', so only
  // this tells the two apart.
  assert.ok(!note.includes('/export failed'),
    'it fell through into the download: ' + note);
});

(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try {
      await fn();
      console.log('  ok   ' + name);
    } catch (e) {
      failed += 1;
      console.log('  FAIL ' + name + '\n       ' + (e && e.message));
    }
  }
  console.log(failed ? failed + ' failed' : 'all ' + tests.length + ' passed');
  process.exit(failed ? 1 : 0);
})();
