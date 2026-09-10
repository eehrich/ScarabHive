// Arrow-key history for the web composer, exercised against a DOM stub.
//
// The point of the module is restraint: Up/Down must reach the history ONLY
// when the caret had nowhere left to go. That decision is made a tick after
// the keystroke, by comparing the caret position, so the stub has to be able
// to play both a browser that moved the caret and one that could not.
//
// Run: node tests/js/input_history.test.js   (driven by test_static_js.py)
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const MODULE = path.join(__dirname, '..', '..', 'static', 'js', 'input_history.js');

function makeElement(tag) {
  const listeners = {};
  return {
    tag,
    value: '',
    selectionStart: 0,
    listeners,
    addEventListener(type, fn) {
      (listeners[type] = listeners[type] || []).push(fn);
    },
    setSelectionRange(pos) {
      this.selectionStart = pos;
    },
    dispatchEvent(event) {
      (listeners[event.type] || []).forEach((fn) => fn(event));
      return !event.defaultPrevented;
    },
  };
}

/**
 * Where a real textarea puts the caret for an unhandled arrow key.
 *
 * Modelled on explicit newlines only, which is what the tests use: Up from
 * the first line goes to offset 0, Down from the last line goes to the end,
 * otherwise the caret moves one line and keeps its column as far as it fits.
 */
function defaultCaretMove(key, input) {
  const value = input.value;
  const pos = input.selectionStart;
  if (key !== 'ArrowUp' && key !== 'ArrowDown') return pos;
  const lineStart = value.lastIndexOf('\n', pos - 1) + 1;
  const column = pos - lineStart;
  if (key === 'ArrowUp') {
    if (lineStart === 0) return 0;
    const prevStart = value.lastIndexOf('\n', lineStart - 2) + 1;
    return Math.min(prevStart + column, lineStart - 1);
  }
  const lineEnd = value.indexOf('\n', pos);
  if (lineEnd === -1) return value.length;
  const nextEnd = value.indexOf('\n', lineEnd + 1);
  const nextStop = nextEnd === -1 ? value.length : nextEnd;
  return Math.min(lineEnd + 1 + column, nextStop);
}

function makeEvent(type, props) {
  const event = Object.assign({ type, defaultPrevented: false }, props || {});
  event.preventDefault = function () { this.defaultPrevented = true; };
  return event;
}

/**
 * Load the module into a fresh sandbox and hand back its handles.
 *
 * `clearsInput` installs a form listener that empties the box, registered
 * BEFORE the module runs -- which is the real order, since chat_module.js
 * has the earlier script tag.
 */
function load(options) {
  const settings = options || {};
  const input = makeElement('textarea');
  const form = makeElement('form');
  const windowListeners = {};
  const captureListeners = {};
  const sandbox = {
    document: {
      readyState: 'complete',
      getElementById: (id) => (id === 'task' ? input : id === 'f' ? form : null),
      addEventListener(type, fn, capture) {
        if (capture) (captureListeners[type] = captureListeners[type] || []).push(fn);
      },
      createEvent: () => makeEvent('input'),
    },
    window: {
      setTimeout,
      addEventListener(type, fn) {
        (windowListeners[type] = windowListeners[type] || []).push(fn);
      },
    },
    Event: function (type) { return makeEvent(type); },
    setTimeout,
  };
  sandbox.window.slashCommands = { close() {} };
  if (settings.clearsInput) {
    form.addEventListener('submit', () => { input.value = ''; });
  }
  vm.createContext(sandbox);
  vm.runInContext(fs.readFileSync(MODULE, 'utf8'), sandbox, { filename: MODULE });

  return {
    input,
    form,
    loadSession(messages) {
      (windowListeners['session:loaded'] || []).forEach((fn) =>
        fn({ detail: { session: { messages } } }));
    },
    /** The "New conversation" button: session:new, never session:loaded. */
    newConversation() {
      (windowListeners['session:new'] || []).forEach((fn) => fn({}));
    },
    /**
     * Submit the form the way the browser does it: capture listeners on the
     * document first, then the listeners on the form itself. The order is
     * the whole point -- the chat module's handler sits on the form and
     * empties the box synchronously.
     */
    submit() {
      const event = makeEvent('submit', { target: form });
      (captureListeners['submit'] || []).forEach((fn) => fn(event));
      (form.listeners['submit'] || []).forEach((fn) => fn(event));
    },
    /**
     * One keystroke, with the browser's own default action modelled.
     *
     * This is the part that decides whether the tests describe a browser or a
     * fantasy. In a real textarea ArrowUp on the first line moves the caret to
     * offset 0 and ArrowDown on the last line moves it to the end -- it only
     * stays put when it is ALREADY there. A stub with a frozen caret makes
     * every arrow reach the history on the first press, which is exactly the
     * behaviour this module is built to avoid.
     *
     * `caretMovesTo` overrides the model for cases it cannot know (soft wrap).
     */
    async press(key, opts) {
      const options = opts || {};
      const event = makeEvent('keydown', Object.assign({ key }, options.flags));
      if (options.alreadyHandled) event.defaultPrevented = true;
      (input.listeners['keydown'] || []).forEach((fn) => fn(event));
      if (options.caretMovesTo !== undefined) {
        input.selectionStart = options.caretMovesTo;
      } else if (!event.defaultPrevented) {
        input.selectionStart = defaultCaretMove(key, input);
      }
      await new Promise((resolve) => setTimeout(resolve, 0));
      return event;
    },
  };
}

const SESSION = [
  { role: 'user', content: 'erste frage' },
  { role: 'assistant', content: 'eine antwort' },
  { role: 'user', content: [{ type: 'text', text: 'zweite frage' }] },
];

const tests = {
  async 'the caret keeps the key when it can still move'() {
    const ui = load();
    ui.loadSession(SESSION);
    ui.input.value = 'zeile eins\nzeile zwei';
    ui.input.selectionStart = 15;
    await ui.press('ArrowUp', { caretMovesTo: 4 });
    assert.strictEqual(ui.input.value, 'zeile eins\nzeile zwei',
      'a draft was thrown away by a plain caret move');
  },

  async 'a caret that cannot move hands the key to the history'() {
    const ui = load();
    ui.loadSession(SESSION);
    await ui.press('ArrowUp');
    assert.strictEqual(ui.input.value, 'zweite frage');
  },

  async 'pressing up again reaches further back'() {
    const ui = load();
    ui.loadSession(SESSION);
    await ui.press('ArrowUp');
    await ui.press('ArrowUp');
    assert.strictEqual(ui.input.value, 'erste frage');
    await ui.press('ArrowUp');
    assert.strictEqual(ui.input.value, 'erste frage', 'walked off the oldest entry');
  },

  async 'only user messages become history'() {
    const ui = load();
    ui.loadSession(SESSION);
    // Every step is collected, not just the last one: the assistant reply
    // sits BETWEEN the two questions, so checking where the walk ends up
    // would pass whether or not it was filtered out.
    const seen = [];
    for (let i = 0; i < 4; i += 1) {
      await ui.press('ArrowUp');
      seen.push(ui.input.value);
    }
    assert.ok(!seen.includes('eine antwort'),
      'an assistant reply leaked in: ' + seen.join(' | '));
    assert.deepStrictEqual(seen.slice(0, 2), ['zweite frage', 'erste frage']);
  },

  async 'coming back down restores the unsent draft'() {
    const ui = load();
    ui.loadSession(SESSION);
    ui.input.value = 'halb getippt';
    ui.input.selectionStart = 0;
    await ui.press('ArrowUp');
    assert.strictEqual(ui.input.value, 'zweite frage');

    // Two presses, and that is the design, not a defect. A recall parks the
    // caret at the START so that walking further back costs one press per
    // step -- the common move. The first Down therefore still has somewhere
    // to go (the end of the recalled text) and belongs to the caret; only
    // the second one reaches the history.
    await ui.press('ArrowDown');
    assert.strictEqual(ui.input.value, 'zweite frage', 'the caret move was stolen');
    await ui.press('ArrowDown');
    assert.strictEqual(ui.input.value, 'halb getippt');
  },

  async 'an edit made in a recalled entry is not lost when walking on'() {
    const ui = load();
    ui.loadSession(SESSION);
    await ui.press('ArrowUp');
    assert.strictEqual(ui.input.value, 'zweite frage');
    ui.input.value = 'zweite frage, ergaenzt';   // the user types
    ui.input.selectionStart = 0;

    await ui.press('ArrowUp');                    // walk further back...
    assert.strictEqual(ui.input.value, 'erste frage');
    await ui.press('ArrowDown');                  // ...caret to the end...
    await ui.press('ArrowDown');                  // ...and return
    assert.strictEqual(ui.input.value, 'zweite frage, ergaenzt');
  },

  async 'escape is left alone when not browsing'() {
    const ui = load();
    ui.loadSession(SESSION);
    ui.input.value = 'halb getippt';
    const event = await ui.press('Escape');
    assert.strictEqual(ui.input.value, 'halb getippt', 'wiped a draft');
    assert.strictEqual(event.defaultPrevented, false, 'swallowed the key');
  },

  async 'arrow down does nothing when not browsing'() {
    const ui = load();
    ui.loadSession(SESSION);
    ui.input.value = 'halb getippt';
    ui.input.selectionStart = ui.input.value.length;
    await ui.press('ArrowDown');
    assert.strictEqual(ui.input.value, 'halb getippt');
  },

  async 'an expanded skill body is too long to recall'() {
    const ui = load();
    ui.loadSession([
      { role: 'user', content: 'kurz genug' },
      { role: 'user', content: 'x'.repeat(5000) },
    ]);
    await ui.press('ArrowUp');
    assert.strictEqual(ui.input.value, 'kurz genug');
  },

  async 'an escaped command comes back escaped'() {
    const ui = load();
    ui.loadSession([{ role: 'user', content: '/compact' }]);
    await ui.press('ArrowUp');
    // Raw, Enter on this would RUN the command instead of re-sending it.
    assert.strictEqual(ui.input.value, '//compact');
  },

  async 'a message that is not a command word keeps its single slash'() {
    // Escaping this would hand the agent one slash more than it was sent:
    // "//3d ..." is not unescaped on the way in, because the head is not
    // command-word shaped.
    const ui = load();
    ui.loadSession([{ role: 'user', content: '/3d drucker bauen' }]);
    await ui.press('ArrowUp');
    assert.strictEqual(ui.input.value, '/3d drucker bauen');
  },

  async 'a path is left alone'() {
    const ui = load();
    ui.loadSession([{ role: 'user', content: '/etc/nginx/nginx.conf lesen' }]);
    await ui.press('ArrowUp');
    assert.strictEqual(ui.input.value, '/etc/nginx/nginx.conf lesen');
  },

  async 'switching sessions hands the draft back'() {
    const ui = load();
    ui.loadSession(SESSION);
    ui.input.value = 'mein entwurf';
    ui.input.selectionStart = 0;
    await ui.press('ArrowUp');
    assert.strictEqual(ui.input.value, 'zweite frage', 'fixture did not browse');

    // The composer is holding an entry from a conversation that is about to
    // leave the screen. The draft is the person's own text and survives.
    ui.loadSession([{ role: 'user', content: 'andere session' }]);
    assert.strictEqual(ui.input.value, 'mein entwurf');
  },

  async 'a new conversation starts with an empty history'() {
    const ui = load();
    ui.loadSession(SESSION);
    ui.newConversation();
    await ui.press('ArrowUp');
    assert.strictEqual(ui.input.value, '', 'the old conversation bled through');
  },

  async 'escape leaves the history and restores the draft'() {
    const ui = load();
    ui.loadSession(SESSION);
    ui.input.value = 'halb getippt';
    ui.input.selectionStart = 0;
    await ui.press('ArrowUp');
    await ui.press('Escape');
    assert.strictEqual(ui.input.value, 'halb getippt');
  },

  async 'a key the slash dropdown already handled is left alone'() {
    const ui = load();
    ui.loadSession(SESSION);
    await ui.press('ArrowUp', { alreadyHandled: true });
    assert.strictEqual(ui.input.value, '', 'stole the dropdown key');
  },

  async 'modified arrows are never history'() {
    const ui = load();
    ui.loadSession(SESSION);
    await ui.press('ArrowUp', { flags: { shiftKey: true } });
    assert.strictEqual(ui.input.value, '', 'shift-select turned into a recall');
  },

  async 'what was just sent is recallable'() {
    const ui = load();
    ui.loadSession([]);
    ui.input.value = 'gerade abgeschickt';
    ui.submit();
    ui.input.value = '';
    ui.input.selectionStart = 0;
    await ui.press('ArrowUp');
    assert.strictEqual(ui.input.value, 'gerade abgeschickt');
  },

  async 'the text is captured before the chat module empties the box'() {
    // chat_module clears taskInput SYNCHRONOUSLY on an ordinary message, from
    // a listener registered earlier -- so a history listener on the form
    // would only ever see an empty string.
    const ui = load({ clearsInput: true });
    ui.loadSession([]);
    ui.input.value = 'wird sofort geleert';
    ui.submit();
    assert.strictEqual(ui.input.value, '', 'fixture did not clear -- test is vacuous');

    ui.input.selectionStart = 0;
    await ui.press('ArrowUp');
    assert.strictEqual(ui.input.value, 'wird sofort geleert');
  },

  async 'switching sessions swaps the history'() {
    const ui = load();
    ui.loadSession(SESSION);
    ui.loadSession([{ role: 'user', content: 'andere session' }]);
    await ui.press('ArrowUp');
    assert.strictEqual(ui.input.value, 'andere session');
    await ui.press('ArrowUp');
    assert.strictEqual(ui.input.value, 'andere session', 'old session bled through');
  },
};

(async () => {
  let failed = 0;
  for (const [name, fn] of Object.entries(tests)) {
    try {
      await fn();
      console.log(`  ok    ${name}`);
    } catch (error) {
      failed += 1;
      console.log(`  FAIL  ${name}\n        ${error.message}`);
    }
  }
  console.log(`\n${Object.keys(tests).length - failed}/${Object.keys(tests).length} passed`);
  process.exit(failed ? 1 : 0);
})();
