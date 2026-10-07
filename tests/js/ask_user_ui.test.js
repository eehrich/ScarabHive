// The chat's half of ask_user: the model's question on the row that asks it.
//
// ask_user puts its question on the call's status row with `meta.ask_user`: its
// form (questions.UserQuestion.form) and where the answer goes. The chat draws the
// box from the form alone (questionBox), as it draws any kind of question: the
// question, a button per option (boxes to tick where several may be picked) and a
// field for an answer in one's own words; it posts the answer and takes the box
// down with the row's last line. A kind the chat has never heard of gets its box
// the same way.
//
// The functions are taken out of chat_module.js as they are and run against a
// small DOM stub: the module as a whole needs a page, these need a row.
//
// Run: node tests/js/ask_user_ui.test.js   (driven by test_ask_user_ui.py)
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');

const SOURCE = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'chat_module.js'), 'utf8');

/** The text of `function <name>(...) { ... }` at the module's own indent. */
function functionSource(name) {
  const start = SOURCE.indexOf(`\n  function ${name}(`);
  assert.ok(start >= 0, `${name} is not in chat_module.js`);
  const end = SOURCE.indexOf('\n  }\n', start);
  assert.ok(end > start, `the end of ${name} was not found`);
  return SOURCE.slice(start, end + 4);
}

/** A function of the module, with the names it reaches for handed in. */
function load(name, scope) {
  const names = Object.keys(scope);
  // eslint-disable-next-line no-new-func
  return new Function(...names, `${functionSource(name)}\nreturn ${name};`)(...names.map((n) => scope[n]));
}

class Element {
  constructor(tag) {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parent = null;
    this.className = '';
    this.textContent = '';
    this.innerHTML = '';
    this.value = '';
    this.checked = false;
    this.disabled = false;
    this.listeners = {};
    const element = this;
    this.classList = {
      add(...names) { element.className = [...new Set(element.className.split(' ').filter(Boolean).concat(names))].join(' '); },
      contains(name) { return element.className.split(' ').includes(name); },
    };
  }
  appendChild(child) { child.parent = this; this.children.push(child); return child; }
  append(...children) { children.forEach((child) => this.appendChild(child)); }
  remove() {
    if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this);
    this.parent = null;
  }
  addEventListener(type, fn) { this.listeners[type] = fn; }
  click() { return this.disabled || !this.listeners.click ? undefined : this.listeners.click(); }
  querySelector(selector) {
    const direct = selector.startsWith(':scope > ');
    const cls = selector.replace(':scope > ', '').replace(/^\./, '');
    const walk = (node) => {
      for (const child of node.children) {
        if (child.classList.contains(cls)) return child;
        if (!direct) {
          const found = walk(child);
          if (found) return found;
        }
      }
      return null;
    };
    return walk(this);
  }
  all(cls) {
    const out = [];
    const walk = (node) => node.children.forEach((c) => { if (c.classList.contains(cls)) out.push(c); walk(c); });
    walk(this);
    return out;
  }
  inputs(type) {
    const out = [];
    const walk = (node) => node.children.forEach((c) => { if (c.tagName === 'INPUT' && c.type === type) out.push(c); walk(c); });
    walk(this);
    return out;
  }
}

const document = { createElement: (tag) => new Element(tag) };
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

/** syncQuestionActions with the functions of the module it reaches for. */
function loadSync(postJSON) {
  const sendAnswer = load('sendAnswer', { postJSON });
  const questionBox = load('questionBox', { document, sendAnswer });
  return load('syncQuestionActions', { questionBox });
}

function recorder() {
  const posted = [];
  return { posted, postJSON: async (url, body) => { posted.push([url, body]); return { status: 'ok' }; } };
}

/** The model's question as ask_user asks it: its form. */
function asking(form) {
  return {
    type: 'status', phase: 'progress', request_id: 'r1_003', server: 'ask_user.ask_user()',
    message: 'Question: Which database?',
    meta: { ask_user: {
      id: 'a1b2c3d4e5f60718', answer_url: '/plugins/ask_user/answer',
      form: Object.assign({
        prompt: 'Which database?', detail: null, warning: null,
        choices: [{ value: 'Postgres', label: 'Postgres' }, { value: 'SQLite', label: 'SQLite' }],
        multi_select: false, text: { label: 'Or answer in your own words', alone: true, max_chars: 4000 },
      }, form || {}),
    } },
  };
}

function options(...names) {
  return names.map((name) => ({ value: name, label: name }));
}

function boxOf(row) {
  const box = row.querySelector(':scope > .question-actions');
  assert.ok(box, 'the question got no box');
  return box;
}

async function testAClickOnAnOptionSendsItWithWhatWasTyped() {
  const { posted, postJSON } = recorder();
  const sync = loadSync(postJSON);
  const row = new Element('div');

  sync(row, asking());
  sync(row, asking());   // asked again: no second box

  assert.strictEqual(row.all('question-actions').length, 1, 'the question got its box twice');
  const box = boxOf(row);
  assert.strictEqual(box.querySelector('.question-prompt').textContent, 'Which database?');
  const choices = box.all('question-choice');
  assert.deepStrictEqual(choices.map((b) => [b.tagName, b.textContent]), [['BUTTON', 'Postgres'], ['BUTTON', 'SQLite']]);
  assert.deepStrictEqual(box.inputs('checkbox'), [], 'a single choice got boxes to tick');
  assert.strictEqual(box.querySelector('.question-text').placeholder, 'Or answer in your own words');
  assert.strictEqual(box.querySelector('.question-text').maxLength, 4000);

  box.querySelector('.question-text').value = '  for now ';
  await choices[1].listeners.click();

  assert.deepStrictEqual(posted, [['/plugins/ask_user/answer',
    { question_id: 'a1b2c3d4e5f60718', choices: ['SQLite'], text: 'for now' }]]);
  assert.ok(choices.every((b) => b.disabled), 'the options stayed live after the answer was taken');
  assert.ok(box.querySelector('.question-text').disabled && box.querySelector('.question-send').disabled);
  assert.strictEqual(box.querySelector('.question-note').textContent, 'Answered: SQLite, for now');
}

async function testSeveralTickedOptionsGoTogether() {
  const { posted, postJSON } = recorder();
  const sync = loadSync(postJSON);
  const row = new Element('div');
  sync(row, asking({ choices: options('linux', 'mac', 'windows'), multi_select: true }));
  const box = boxOf(row);
  const ticks = box.inputs('checkbox');
  assert.deepStrictEqual(ticks.map((t) => t.value), ['linux', 'mac', 'windows']);
  assert.deepStrictEqual(box.all('pk-btn').map((b) => b.textContent), ['Send'], 'options were buttons that send one');

  ticks[2].checked = true;
  ticks[0].checked = true;
  await box.querySelector('.question-send').listeners.click();

  assert.deepStrictEqual(posted.map(([, body]) => body.choices), [['linux', 'windows']]);
  assert.strictEqual(box.querySelector('.question-note').textContent, 'Answered: linux, windows');
}

async function testAnOpenQuestionTakesTextAndNothingIsNotSent() {
  const { posted, postJSON } = recorder();
  const sync = loadSync(postJSON);
  const row = new Element('div');
  sync(row, asking({ choices: [], prompt: 'What should the file be called?', text: { label: 'Your answer', alone: true } }));
  const box = boxOf(row);
  assert.strictEqual(box.querySelector('.question-choices'), null, 'an open question got options');
  const send = box.querySelector('.question-send');
  const text = box.querySelector('.question-text');

  await send.listeners.click();
  assert.deepStrictEqual(posted, [], 'an empty answer was sent');
  assert.ok(!send.disabled && box.querySelector('.question-note').textContent.includes('Write an answer'));

  text.value = 'report.md';
  let prevented = false;
  await text.listeners.keydown({ key: 'Enter', isComposing: false, preventDefault() { prevented = true; } });
  assert.ok(prevented, 'Enter reached the page');
  assert.deepStrictEqual(posted.map(([, body]) => body), [{ question_id: 'a1b2c3d4e5f60718', choices: [], text: 'report.md' }]);
}

async function testAnyKindIsDrawnFromItsForm() {
  // a kind the chat has no code for: a state machine that waits for an event (stategraph)
  const { posted, postJSON } = recorder();
  const sync = loadSync(postJSON);
  const row = new Element('div');
  sync(row, { type: 'status', phase: 'progress', request_id: 'r1_wait_a1', message: 'm waits',
              meta: { stategraph: { id: 'f00df00df00df00d', answer_url: '/plugins/stategraph/answer', form: {
                prompt: "m waits for an event in 'review'", detail: 'review: Is the draft fine?', warning: null,
                choices: options('approve', 'reject'), multi_select: false,
                text: { label: 'Data to send with it (JSON or text)', alone: false } } } } });
  const box = boxOf(row);
  assert.strictEqual(box.querySelector('.question-detail').textContent, 'review: Is the draft fine?');
  assert.strictEqual(box.querySelector('.question-send'), null, 'words that answer nothing alone got a Send');
  const text = box.querySelector('.question-text');
  text.value = '{"why": "too long"}';
  let prevented = false;
  text.listeners.keydown && text.listeners.keydown({ key: 'Enter', isComposing: false, preventDefault() { prevented = true; } });
  assert.ok(!prevented && posted.length === 0, 'Enter sent words that answer nothing alone');

  await box.all('question-choice')[1].listeners.click();
  assert.deepStrictEqual(posted, [['/plugins/stategraph/answer',
    { question_id: 'f00df00df00df00d', choices: ['reject'], text: '{"why": "too long"}' }]]);
}

async function testTheRowsLastLineTakesTheBoxDown() {
  const sync = loadSync(async () => ({}));
  for (const phase of ['end', 'error']) {
    const row = new Element('div');
    sync(row, asking());
    boxOf(row);
    sync(row, { type: 'status', phase, request_id: 'r1_003', message: 'answered by alice: SQLite', meta: {} });
    assert.strictEqual(row.querySelector(':scope > .question-actions'), null, `a ${phase} line left the box`);
  }
}

async function testARowLeftOpenAtTheRunsEndLosesItsBox() {
  const sync = loadSync(async () => ({}));
  const row = new Element('div');
  const line = new Element('div');
  line.className = 'progress-line';
  const icon = new Element('span');
  icon.className = 'progress-icon';
  row.append(icon, line);
  sync(row, asking());
  boxOf(row);
  const mark = load('markOpenScopesUnfinished', { activeOperations: new Map([['r1_003', row]]), document });
  mark();
  assert.strictEqual(row.querySelector(':scope > .question-actions'), null, 'the box outlived the run');
}

async function testARefusedAnswerLeavesASecondTryAndOnlyA404IsFinal() {
  let status = 422;
  const sync = loadSync(async () => { const e = new Error('pick an option or write an answer'); e.status = status; throw e; });
  const row = new Element('div');
  sync(row, asking());
  const box = boxOf(row);
  const [postgres] = box.all('question-choice');

  await postgres.listeners.click();
  assert.ok(!postgres.disabled, 'a 422 left no second try');
  assert.ok(box.querySelector('.question-note').textContent.includes('pick an option'));

  status = 404;
  await postgres.listeners.click();
  assert.ok(postgres.disabled, 'a question nobody waits on could still be answered');
}

async function testOnlyAnAnswerPathOnThisServerIsTaken() {
  const sync = loadSync(async () => ({}));
  for (const url of ['https://elsewhere.example/steal', '/plugins/../api/requests/r1/cancel', '/plugins/ask_user/answer?x=1']) {
    const row = new Element('div');
    const ev = asking();
    ev.meta.ask_user.answer_url = url;
    sync(row, ev);
    assert.strictEqual(row.querySelector(':scope > .question-actions'), null, `a box that posts to ${url}`);
  }
}

async function testTheModelsTextIsShownAsTextNeverAsMarkup() {
  const sync = loadSync(async () => ({}));
  const row = new Element('div');
  const sneaky = '<img src=x onerror="alert(1)">';
  sync(row, asking({ prompt: sneaky, detail: sneaky, warning: sneaky, choices: options(sneaky, 'b'), multi_select: true }));
  const box = boxOf(row);
  for (const part of ['.question-prompt', '.question-detail', '.question-warning']) {
    const shown = box.querySelector(part);
    assert.strictEqual(shown.textContent, sneaky, part);
    assert.strictEqual(shown.innerHTML, '', `${part} went in as markup`);
  }
  const [first] = box.all('question-choice');
  assert.ok(first.children.every((c) => c.innerHTML === ''), 'an option went in as markup');
}

async function testAPasteCutAtTheLimitIsSaid() {
  const sync = loadSync(async () => ({}));
  const row = new Element('div');
  sync(row, asking({ text: { label: 'Say', alone: true, max_chars: 5 } }));
  const box = boxOf(row);
  const text = box.querySelector('.question-text');
  const note = box.querySelector('.question-note');

  text.value = 'abcde';   // what the browser leaves of a longer paste
  text.listeners.input();
  assert.strictEqual(note.textContent, 'At most 5 characters: longer text is cut.');
  text.value = 'abcd';
  text.listeners.input();
  assert.strictEqual(note.textContent, '', 'the note stayed after the text got shorter');
  note.textContent = 'Write an answer first.';
  text.listeners.input();
  assert.strictEqual(note.textContent, 'Write an answer first.', 'typing wiped another note');
}

async function testWordsAnswerAloneWhereThereIsNothingToPick() {
  const { posted, postJSON } = recorder();
  const sync = loadSync(postJSON);
  const row = new Element('div');
  sync(row, asking({ choices: [], text: { label: 'Say', alone: false } }));   // as the CLI: no choice, words answer
  const box = boxOf(row);
  const send = box.querySelector('.question-send');
  assert.ok(send, 'nothing to pick and no Send: the question cannot be answered');

  box.querySelector('.question-text').value = 'later';
  await send.listeners.click();

  assert.deepStrictEqual(posted, [['/plugins/ask_user/answer',
    { question_id: 'a1b2c3d4e5f60718', choices: [], text: 'later' }]]);
}

(async () => {
  const tests = [testAClickOnAnOptionSendsItWithWhatWasTyped, testSeveralTickedOptionsGoTogether,
    testAnOpenQuestionTakesTextAndNothingIsNotSent, testAnyKindIsDrawnFromItsForm, testTheRowsLastLineTakesTheBoxDown,
    testARowLeftOpenAtTheRunsEndLosesItsBox, testARefusedAnswerLeavesASecondTryAndOnlyA404IsFinal,
    testOnlyAnAnswerPathOnThisServerIsTaken, testTheModelsTextIsShownAsTextNeverAsMarkup,
    testAPasteCutAtTheLimitIsSaid, testWordsAnswerAloneWhereThereIsNothingToPick];
  for (const test of tests) {
    await test();
    await settle();
  }
  console.log(`${tests.length} passed`);
})().catch((error) => {
  console.error(String(error && error.message ? error.message : error), error && error.stack);
  process.exit(1);
});
