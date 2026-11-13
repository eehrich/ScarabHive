// A call's thinking, opened while it streams, shows its newest words: the box keeps its end in
// view as the text grows, and opening a live box at the chat's end leaves the chat following.
// A box the viewer scrolled up in stays put; one opened after its call ended is read back, as
// before -- and a call ends with its result, the run's final, or a stream that ends without them.
//
// Run: node tests/js/thinking_follow.test.js   (driven by test_thinking_follow.py)
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');

// LF: the bounds of a function are found by '\n  }\n', and a Windows checkout (core.autocrlf) has CRLF
const SOURCE = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'chat_module.js'), 'utf8')
  .replace(/\r\n/g, '\n');

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

const document = { createTextNode: (text) => ({ text }) };

// Frames run when the test says so: what a frame does is what the page does once per frame.
function frames() {
  const queued = [];
  return { requestAnimationFrame: (fn) => queued.push(fn), flush: () => queued.splice(0).forEach((fn) => fn()),
           get pending() { return queued.length; } };
}

// A scrolling box 100 px high whose content grows 50 px per piece of text; the browser clamps scrollTop.
class Box {
  constructor() { this.parts = []; this.top = 0; this.clientHeight = 100; this.dataset = {}; this.hidden = false; }
  appendChild(node) { this.parts.push(node.text); }
  get scrollHeight() { return Math.max(this.clientHeight, 50 * this.parts.length); }
  get scrollTop() { return this.top; }
  set scrollTop(value) { this.top = Math.max(0, Math.min(value, this.scrollHeight - this.clientHeight)); }
  get atEnd() { return this.top === this.scrollHeight - this.clientHeight; }
}

function thinking() {
  const frame = frames();
  const append = load('appendThinking', { document, requestAnimationFrame: frame.requestAnimationFrame });
  const scrolled = load('noteThinkingScroll', {});
  return { frame, append, scrolled, box: new Box() };
}

function testAnOpenBoxKeepsShowingTheNewestWordsOnceAFrame() {
  const { frame, append, box } = thinking();
  for (let i = 0; i < 6; i += 1) append(box, `word${i} `);
  assert.strictEqual(frame.pending, 1, 'brought to its end per delta, not once a frame');
  frame.flush();
  assert.ok(box.atEnd, 'the box did not follow its text');
  assert.strictEqual(box.dataset.live, 'true');
}

function testTheBoxsOwnPinIsNotTheViewerScrolling() {
  // The pin's scroll event comes after more words arrived: the box is no longer at its end then.
  const { frame, append, scrolled, box } = thinking();
  for (let i = 0; i < 6; i += 1) append(box, 'x ');
  frame.flush();
  append(box, 'y ');
  scrolled(box);
  frame.flush();
  assert.ok(box.atEnd, 'the box took its own pin for the viewer and stopped following');
}

function testABoxScrolledUpStaysWhereTheViewerPutItUntilBroughtBack() {
  const { frame, append, scrolled, box } = thinking();
  for (let i = 0; i < 6; i += 1) append(box, 'x ');
  frame.flush();
  box.scrollTop = 20;                       // the viewer reads further up
  scrolled(box);
  append(box, 'more ');
  frame.flush();
  assert.strictEqual(box.scrollTop, 20, 'the box was pulled away from what the viewer reads');
  box.scrollTop = box.scrollHeight;         // and scrolls back down to its end
  scrolled(box);
  append(box, 'again ');
  frame.flush();
  assert.ok(box.atEnd, 'brought back to its end, the box does not follow again');
}

function fold(scope, live, held) {
  const listeners = {};
  const trigger = { setAttribute() {}, addEventListener: (type, fn) => { listeners[type] = fn; } };
  const box = new Box();
  for (let i = 0; i < 6; i += 1) box.parts.push('x');
  box.hidden = true;
  if (live) box.dataset.live = 'true';
  if (held) { box.dataset.held = 'true'; box.top = 30; }
  load('foldOnActivate', scope)(trigger, box);
  listeners.click();
  return box;
}

function testOpeningALiveBox() {
  // at the chat's end: watched as it comes, the chat keeps following
  let stopped = 0;
  let box = fold({ following: true, readingHere: () => { stopped += 1; } }, true);
  assert.strictEqual(stopped, 0, 'opening a box still being written stopped the chat following');
  assert.ok(box.atEnd, 'the opened box does not show its newest words');
  // away from the chat's end: the chat stays where it is, the box still opens at its newest words
  stopped = 0;
  box = fold({ following: false, readingHere: () => { stopped += 1; } }, true);
  assert.strictEqual(stopped, 1);
  assert.ok(box.atEnd, 'a live box opened above the end shows its oldest words');
  // one the viewer scrolled up in before: left where they put it
  box = fold({ following: true, readingHere: () => {} }, true, true);
  assert.strictEqual(box.scrollTop, 30, 'opening it pulled the box away from where the viewer left it');
}

function testOpeningAnEndedBoxIsReading() {
  let stopped = 0;
  const box = fold({ following: true, readingHere: () => { stopped += 1; } }, false);
  assert.strictEqual(stopped, 1, 'opening a finished box did not stop the chat following');
  assert.strictEqual(box.scrollTop, 0, 'a finished box opens at its start');
}

function liveRoot() {
  const box = new Box();
  box.dataset.live = 'true';
  const root = { querySelectorAll: (sel) => (sel === '.thinking-content[data-live]' && box.dataset.live ? [box] : []),
                 querySelector: () => ({}) };
  return { box, root };
}

function testEveryWayACallEndsEndsItsThinking() {
  const endThinking = load('endThinking', {});
  const { box, root } = liveRoot();
  const render = load('renderRunEvent', { document, endThinking, stepOf: () => root, thinkingOf: () => box,
                                          appendThinking: () => {}, showAnswer: () => {}, settleView: () => {} });
  render({ openStep: 1 }, { type: 'thinking', step: 1, assistant: { content: 'done' } });
  assert.strictEqual(box.dataset.live, undefined, 'a finished call still reads as live');

  box.dataset.live = 'true';
  render({ openStep: 1, steps: root, answerLoaded: true }, { type: 'final', content: 'answer' });
  assert.strictEqual(box.dataset.live, undefined, 'the run ended and its thinking still reads as live');

  // A sub-run that fails, is cancelled or ends without its final.
  const view = { steps: root, t: {} };
  const sub = load('handleSubRunEvent', {
    endThinking, takeOverStoredPath: () => {}, waitingFor: () => null, subRunView: () => view,
    markSubRun: () => {}, showReason: () => {}, formatTextWithLineBreaks: (t) => t, renderRunEvent: () => {},
  });
  for (const type of ['error', 'cancelled', 'end']) {
    box.dataset.live = 'true';
    sub({}, { run_id: 'r', event: { type, message: 'x' } });
    assert.strictEqual(box.dataset.live, undefined, `a sub-run's ${type} left its thinking live`);
  }

  // The run's stream ends -- after an error or a cancel there is no final.
  box.dataset.live = 'true';
  const handle = load('handleSSEEvent', {
    endThinking, chatContainer: root, pendingAppendRebind: false, run: {}, currentEventSource: null,
    scrollBottom: () => {}, markOpenScopesUnfinished: () => {}, endSubRuns: () => {},
    watchForASuccessorRun: () => {}, forgetRun: () => {}, idleControls: () => {}, window: {},
  });
  handle({ type: 'end' }, { row: null, steps: {}, t: { children: [] } });
  assert.strictEqual(box.dataset.live, undefined, 'the stream ended and its thinking still reads as live');
}

try {
  testAnOpenBoxKeepsShowingTheNewestWordsOnceAFrame();
  testTheBoxsOwnPinIsNotTheViewerScrolling();
  testABoxScrolledUpStaysWhereTheViewerPutItUntilBroughtBack();
  testOpeningALiveBox();
  testOpeningAnEndedBoxIsReading();
  testEveryWayACallEndsEndsItsThinking();
  console.log('6 passed');
} catch (error) {
  console.error(error && error.stack ? error.stack : String(error));
  process.exit(1);
}
