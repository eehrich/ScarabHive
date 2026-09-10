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

function load(sessions) {
  const byId = {};
  const document = {
    getElementById(id) { return (byId[id] = byId[id] || makeElement()); },
    querySelector() { return makeElement(); },
    querySelectorAll() { return []; },
    createElement() { return makeElement(); },
    addEventListener() {}, removeEventListener() {},
    body: makeElement(), documentElement: makeElement(),
  };
  const calls = [];
  const store = { getItem() { return null; }, setItem() {}, removeItem() {} };
  const window = {
    document,
    location: { href: 'http://localhost/', origin: 'http://localhost' },
    localStorage: store, sessionStorage: store,
    addEventListener() {}, removeEventListener() {},
    setTimeout, clearTimeout, setInterval, clearInterval,
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    URL: { createObjectURL() { return 'blob:x'; }, revokeObjectURL() {} },
    EventSource: function () { this.close = function () {}; },
    navigator: { clipboard: {} },
    console,
    fetch: async (url) => {
      calls.push(url);
      return { ok: true, status: 200, json: async () => sessions };
    },
    slashCommands: { helpLines() { return []; }, catalogue: {}, attach() {}, close() {} },
  };
  window.window = window;

  const sandbox = Object.assign({}, window, {
    window, document, console,
    setTimeout, clearTimeout, setInterval, clearInterval,
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    sessionStorage: store, localStorage: store,
  });
  vm.runInNewContext(fs.readFileSync(MODULE, 'utf8'), sandbox,
    { filename: 'chat_module.js' });

  const chatModule = sandbox.window.chatModule;
  chatModule.init({});
  return { chatModule, container: document.getElementById('chat'), calls };
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
