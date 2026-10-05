// The chat's half of ask_user: the model's question on the row that asks it.
//
// ask_user puts its question on the call's status row with `meta.ask_user`
// (question, options, multi_select, answer_url). The chat draws the question, a
// button per option (boxes to tick where several may be picked) and a field for
// an answer in one's own words, posts the answer, and takes the box down with the
// row's last line.
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
  const approvalBox = load('approvalBox', { document, sendAnswer });
  const askUserBox = load('askUserBox', { document, sendAnswer });
  return load('syncQuestionActions', { approvalBox, askUserBox });
}

function recorder() {
  const posted = [];
  return { posted, postJSON: async (url, body) => { posted.push([url, body]); return { status: 'ok' }; } };
}

function asking(extra) {
  return {
    type: 'status', phase: 'progress', request_id: 'r1_003', server: 'ask_user.ask_user()',
    message: 'Question: Which database?',
    meta: { ask_user: Object.assign({
      id: 'a1b2c3d4e5f60718', question: 'Which database?', options: ['Postgres', 'SQLite'],
      multi_select: false, answer_url: '/plugins/ask_user/answer',
    }, extra || {}) },
  };
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
  assert.strictEqual(box.querySelector('.ask-user-question').textContent, 'Which database?');
  const options = box.all('ask-user-option');
  assert.deepStrictEqual(options.map((b) => [b.tagName, b.textContent]), [['BUTTON', 'Postgres'], ['BUTTON', 'SQLite']]);
  assert.deepStrictEqual(box.inputs('checkbox'), [], 'a single choice got boxes to tick');

  box.querySelector('.ask-user-text').value = '  for now ';
  await options[1].listeners.click();

  assert.deepStrictEqual(posted, [['/plugins/ask_user/answer',
    { question_id: 'a1b2c3d4e5f60718', choices: ['SQLite'], text: 'for now' }]]);
  assert.ok(options.every((b) => b.disabled), 'the options stayed live after the answer was taken');
  assert.ok(box.querySelector('.ask-user-text').disabled && box.querySelector('.ask-user-send').disabled);
  assert.strictEqual(box.querySelector('.approval-note').textContent, 'Answered: SQLite, for now');
}

async function testSeveralTickedOptionsGoTogether() {
  const { posted, postJSON } = recorder();
  const sync = loadSync(postJSON);
  const row = new Element('div');
  sync(row, asking({ options: ['linux', 'mac', 'windows'], multi_select: true }));
  const box = boxOf(row);
  const ticks = box.inputs('checkbox');
  assert.deepStrictEqual(ticks.map((t) => t.value), ['linux', 'mac', 'windows']);
  assert.deepStrictEqual(box.all('pk-btn').map((b) => b.textContent), ['Send'], 'options were buttons that send one');

  ticks[2].checked = true;
  ticks[0].checked = true;
  await box.querySelector('.ask-user-send').listeners.click();

  assert.deepStrictEqual(posted.map(([, body]) => body.choices), [['linux', 'windows']]);
  assert.strictEqual(box.querySelector('.approval-note').textContent, 'Answered: linux, windows');
}

async function testAnOpenQuestionTakesTextAndNothingIsNotSent() {
  const { posted, postJSON } = recorder();
  const sync = loadSync(postJSON);
  const row = new Element('div');
  sync(row, asking({ options: [], question: 'What should the file be called?' }));
  const box = boxOf(row);
  assert.strictEqual(box.querySelector('.ask-user-options'), null, 'an open question got options');
  const send = box.querySelector('.ask-user-send');
  const text = box.querySelector('.ask-user-text');

  await send.listeners.click();
  assert.deepStrictEqual(posted, [], 'an empty answer was sent');
  assert.ok(!send.disabled && box.querySelector('.approval-note').textContent.includes('Write an answer'));

  text.value = 'report.md';
  let prevented = false;
  await text.listeners.keydown({ key: 'Enter', isComposing: false, preventDefault() { prevented = true; } });
  assert.ok(prevented, 'Enter reached the page');
  assert.deepStrictEqual(posted.map(([, body]) => body), [{ question_id: 'a1b2c3d4e5f60718', choices: [], text: 'report.md' }]);
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
  const [postgres] = box.all('ask-user-option');

  await postgres.listeners.click();
  assert.ok(!postgres.disabled, 'a 422 left no second try');
  assert.ok(box.querySelector('.approval-note').textContent.includes('pick an option'));

  status = 404;
  await postgres.listeners.click();
  assert.ok(postgres.disabled, 'a question nobody waits on could still be answered');
}

async function testOnlyAnAnswerPathOnThisServerIsTaken() {
  const sync = loadSync(async () => ({}));
  for (const url of ['https://elsewhere.example/steal', '/plugins/../api/requests/r1/cancel', '/plugins/ask_user/answer?x=1']) {
    const row = new Element('div');
    sync(row, asking({ answer_url: url }));
    assert.strictEqual(row.querySelector(':scope > .question-actions'), null, `a box that posts to ${url}`);
  }
}

async function testTheModelsTextIsShownAsTextNeverAsMarkup() {
  const sync = loadSync(async () => ({}));
  const row = new Element('div');
  const sneaky = '<img src=x onerror="alert(1)">';
  sync(row, asking({ question: sneaky, options: [sneaky, 'b'], multi_select: true }));
  const box = boxOf(row);
  const question = box.querySelector('.ask-user-question');
  assert.strictEqual(question.textContent, sneaky);
  assert.strictEqual(question.innerHTML, '');
  const [first] = box.all('ask-user-option');
  assert.ok(first.children.every((c) => c.innerHTML === ''), 'an option went in as markup');
}

(async () => {
  const tests = [testAClickOnAnOptionSendsItWithWhatWasTyped, testSeveralTickedOptionsGoTogether,
    testAnOpenQuestionTakesTextAndNothingIsNotSent, testTheRowsLastLineTakesTheBoxDown,
    testARowLeftOpenAtTheRunsEndLosesItsBox, testARefusedAnswerLeavesASecondTryAndOnlyA404IsFinal,
    testOnlyAnAnswerPathOnThisServerIsTaken, testTheModelsTextIsShownAsTextNeverAsMarkup];
  for (const test of tests) {
    await test();
    await settle();
  }
  console.log(`${tests.length} passed`);
})().catch((error) => {
  console.error(String(error && error.message ? error.message : error), error && error.stack);
  process.exit(1);
});
