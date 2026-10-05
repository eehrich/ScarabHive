// A model call that started over after a dropped stream sends `reasoning_reset`:
// the step's thinking box empties, so the retried reasoning is not shown twice.
// The run's own stream and a sub-run's both reach renderRunEvent.
//
// Run: node tests/js/reasoning_reset.test.js   (driven by test_reasoning_reset.py)
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'chat_module.js'), 'utf8');

function functionSource(name) {
  const start = SOURCE.indexOf(`\n  function ${name}(`);
  assert.ok(start >= 0, `${name} is not in chat_module.js`);
  const end = SOURCE.indexOf('\n  }\n', start);
  assert.ok(end > start, `the end of ${name} was not found`);
  return SOURCE.slice(start, end + 4);
}

function load(name, scope) {
  const names = Object.keys(scope);
  // eslint-disable-next-line no-new-func
  return new Function(...names, `${functionSource(name)}\nreturn ${name};`)(...names.map((n) => scope[n]));
}

class Box {
  constructor() { this.parts = []; }
  appendChild(node) { this.parts.push(node.text); }
  get textContent() { return this.parts.join(''); }
  set textContent(value) { this.parts = value ? [value] : []; }
}

function testAResetEmptiesTheStepsThinking() {
  const box = new Box();
  const document = { createTextNode: (text) => ({ text }) };
  const render = load('renderRunEvent', { document, stepOf: () => ({}), thinkingOf: () => box });
  const view = { openStep: 1 };
  render(view, { type: 'reasoning_delta', step: 1, delta: 'first try' });
  render(view, { type: 'reasoning_reset', step: 1 });
  render(view, { type: 'reasoning_delta', step: 1, delta: 'second try' });
  assert.strictEqual(box.textContent, 'second try');
}

function testTheStreamHandsTheResetOn() {
  const handed = [];
  const handle = load('handleSSEEvent', {
    pendingAppendRebind: false, chatContainer: null, scrollBottom: () => {},
    renderRunEvent: (blk, data) => handed.push(data.type),
  });
  handle({ type: 'reasoning_reset', step: 1 }, { row: null, steps: {} });
  assert.deepStrictEqual(handed, ['reasoning_reset'], 'the stream dropped the reset');
}

try {
  testAResetEmptiesTheStepsThinking();
  testTheStreamHandsTheResetOn();
  console.log('2 passed');
} catch (error) {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
}
