// The browser's half of `/sessions [count]`, exercised against a DOM stub.
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
    addEventListener() {}, removeEventListener() {},
    querySelector() { return makeElement(); }, querySelectorAll() { return []; },
    getAttribute() { return null; }, setAttribute() {}, insertAdjacentHTML() {},
    scrollIntoView() {}, focus() {}, remove() {}, closest() { return null; },
    // A real element has these, and the code under test uses them: an input
    // says it changed, and the export link is clicked to start the download.
    dispatchEvent() {}, click() {},
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
  const store = { getItem() { return null; }, setItem() {}, removeItem() {} };
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
      calls.push(url);
      if (init && init.body) bodies.push(JSON.parse(init.body));
      const answer = (settings.answers || {})[String(url).split('?')[0]];
      if (answer && answer.fails) {
        // The status matters: /undo tells a 409 (the session is running) from
        // anything else, and offers the way past it only for that one.
        return { ok: false, status: answer.status || 500, statusText: 'Boom',
                 json: async () => ({ detail: answer.fails }) };
      }
      return {
        ok: true, status: 200,
        json: async () => (answer === undefined ? sessions : answer),
        blob: async () => ({ kind: 'blob' }),
      };
    },
    sessionManager: {
      loadSession: async (id) => { acted.push('load:' + id); },
      newConversation: async () => {
        acted.push('new');
        // The real one returns undefined either way: it starts a session, or
        // the viewer keeps a running request and it does nothing.
        if (settings.newConversationRefused) return;
        window.dispatchEvent({ type: 'session:new', detail: {} });
      },
      renameTo: async (id, title) => {
        acted.push('rename:' + id + ':' + title);
        return settings.renameFails !== true;
      },
    },
    selectorModule: {
      agents: () => settings.agents || ['coder', 'writer'],
      getCurrentAgent: () => settings.agent || 'coder',
      setAgent: (name) => { acted.push('setAgent:' + name); return settings.setAgentFails !== true; },
    },
    slashCommands: { helpLines() { return []; }, catalogue: {}, attach() {}, close() {} },
  };
  window.window = window;

  const sandbox = Object.assign({}, window, {
    window, document, console, Event: window.Event, URL: window.URL,
    setTimeout, clearTimeout, setInterval, clearInterval,
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    sessionStorage: store, localStorage: store,
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
           acted, input: document.getElementById('task') };
}

function sessionsFixture(count) {
  return Array.from({ length: count }, (_, i) => ({
    session_id: 'sid' + i,
    title: 'session ' + i,
    agent_name: 'basic_agent',
    message_count: i,
  }));
}

const tests = [];
function test(name, fn) { tests.push([name, fn]); }

test('a bare /sessions lists the same default the terminal does', async () => {
  const { chatModule, container, calls } = load(sessionsFixture(25));
  await chatModule.runCommand('sessions', '');
  const note = notesOf(container).join('\n');
  assert.ok(note.includes('Sessions (20 of 25):'), note);
  assert.ok(note.includes('... 5 more'), note);
  // The stub answers every url with the fixture, so without this the endpoint
  // could be renamed away and all six tests would still pass -- the browser
  // would answer /sessions with a 404, or with a healthy-looking 'No sessions
  // yet.' for a payload shape it never asked for.
  assert.deepStrictEqual(calls, ['/api/sessions']);
});

test('a typed count reaches the listing', async () => {
  const { chatModule, container } = load(sessionsFixture(25));
  await chatModule.runCommand('sessions', '3');
  const note = notesOf(container).join('\n');
  assert.ok(note.includes('Sessions (3 of 25):'), note);
  assert.ok(note.includes('sid2'), note);
  assert.ok(!note.includes('sid3'), 'the count was not honoured: ' + note);
});

test('0 means all of them, as the help says', async () => {
  const { chatModule, container } = load(sessionsFixture(25));
  await chatModule.runCommand('sessions', '0');
  const note = notesOf(container).join('\n');
  assert.ok(note.includes('Sessions (25 of 25):'), note);
  assert.ok(!note.includes('more --'), note);
});

test('a count that is not a count gets the usage line, not a listing', async () => {
  const { chatModule, container, calls } = load(sessionsFixture(25));
  await chatModule.runCommand('sessions', '2o');
  const note = notesOf(container).join('\n');
  assert.ok(note.includes('Usage: /sessions [count]   (got: 2o)'), note);
  assert.ok(!note.includes('Sessions ('), 'it listed anyway: ' + note);
  assert.deepStrictEqual(calls, [], 'it asked the server before reading the argument');
});

test('a negative count is not read as all of them', async () => {
  const { chatModule, container } = load(sessionsFixture(25));
  await chatModule.runCommand('sessions', '-1');
  const note = notesOf(container).join('\n');
  assert.ok(note.includes('Usage: /sessions [count]'), note);
});

test('an empty store says so', async () => {
  const { chatModule, container } = load([]);
  await chatModule.runCommand('sessions', '');
  assert.ok(notesOf(container).join('\n').includes('No sessions yet.'));
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

test('/rename goes through the session list, not a PATCH of its own', async () => {
  const { chatModule, container, acted, calls } = load([], { session: 'sid7' });
  await chatModule.runCommand('rename', 'Blitter umbauen');
  assert.deepStrictEqual(acted, ['rename:sid7:Blitter umbauen']);
  assert.deepStrictEqual(calls, [], 'it wrote past the session list');
  assert.ok(notesOf(container).join('\n').includes('Title: Blitter umbauen'));
});

test('/rename without a title gets the usage line', async () => {
  const { chatModule, container, acted } = load([], { session: 'sid7' });
  await chatModule.runCommand('rename', '   ');
  assert.deepStrictEqual(acted, [], 'it renamed with nothing');
  assert.ok(notesOf(container).join('\n').includes('Usage: /rename <title>'));
});

test('a rename that failed is not reported as done', async () => {
  const { chatModule, container } = load([], { session: 'sid7', renameFails: true });
  await chatModule.runCommand('rename', 'Neu');
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
