// The chat's half of tool_approval, and the line for a call that did not run.
//
// A pre_tool_call hook that asks the person watching the run sends a status line
// with `meta.tool_approval`; the chat puts the answer buttons on that line's row
// and takes them down with the row's last line. A call a hook blocked sends a
// `tool_error` event and nothing else -- no status scope, no tool_call -- so the
// chat draws a line of its own for it, or the call is nowhere on the page.
//
// The functions are taken out of chat_module.js as they are and run against a
// small DOM stub: the module as a whole needs a page, these need a row.
//
// Run: node tests/js/tool_approval_ui.test.js   (driven by test_tool_approval_ui.py)
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

/** syncQuestionActions, with the functions of the module it reaches for (they moved out
 * of it when ask_user got a box of its own), handed the page's `document` and `postJSON`. */
function loadSync(scope) {
  const sendAnswer = load('sendAnswer', { postJSON: scope.postJSON });
  const approvalBox = load('approvalBox', { document: scope.document, sendAnswer });
  const askUserBox = load('askUserBox', { document: scope.document, sendAnswer });
  return load('syncQuestionActions', { approvalBox, askUserBox });
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
}

const document = { createElement: (tag) => new Element(tag) };
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

function question(extra) {
  return Object.assign({
    id: 'a1b2c3d4e5f60718', tool: 'terminal_execute', arguments: '{"command": "ls"}',
    answer_url: '/plugins/tool_approval/answer', decisions: ['allow_once', 'allow_session', 'deny'],
  }, extra || {});
}

function asking(extra) {
  return { type: 'status', phase: 'progress', request_id: 'r1_approval_a1b2c3d4e5f60718',
           message: 'Approve terminal_execute?', meta: { tool_approval: question(extra) } };
}

async function testTheQuestionGetsItsButtonsOnce() {
  const posted = [];
  const sync = loadSync({ document, postJSON: async (url, body) => { posted.push([url, body]); return {}; } });
  const row = new Element('div');

  sync(row, asking());
  sync(row, asking());   // asked again: no second set

  assert.strictEqual(row.all('approval-actions').length, 1, 'the question got its buttons twice');
  const box = row.querySelector(':scope > .approval-actions');
  assert.strictEqual(box.querySelector('.approval-arguments').textContent, '{"command": "ls"}');
  const buttons = box.all('pk-btn');
  assert.deepStrictEqual(buttons.map((b) => b.textContent), ['Allow once', 'Allow for this session', 'Deny']);

  box.querySelector('.approval-reason').value = 'only this once';
  await buttons[1].listeners.click();
  assert.deepStrictEqual(posted, [['/plugins/tool_approval/answer',
    { question_id: 'a1b2c3d4e5f60718', decision: 'allow_session', reason: 'only this once' }]]);
  assert.ok(buttons.every((b) => b.disabled), 'the buttons stayed live after the answer was taken');
  assert.strictEqual(box.querySelector('.approval-note').textContent, 'Answered: Allow for this session');
}

async function testTheRowsLastLineTakesTheButtonsDown() {
  const sync = loadSync({ document, postJSON: async () => ({}) });
  for (const phase of ['end', 'error']) {
    const row = new Element('div');
    sync(row, asking());
    assert.ok(row.querySelector(':scope > .approval-actions'), 'fixture: no buttons to take down');
    sync(row, { type: 'status', phase, request_id: 'r1_approval_x', message: 'terminal_execute: denied', meta: {} });
    assert.strictEqual(row.querySelector(':scope > .approval-actions'), null, `a ${phase} line left the buttons`);
  }
}

async function testAShortenedPreviewSaysSo() {
  const sync = loadSync({ document, postJSON: async () => ({}) });
  const cut = new Element('div');
  sync(cut, asking({ arguments: 'content: "yy … 9000 characters … yy"\npath: "/x"', arguments_cut: true }));
  assert.ok(cut.querySelector('.approval-cut'), 'a shortened preview was shown as the whole call');
  const whole = new Element('div');
  sync(whole, asking());
  assert.strictEqual(whole.querySelector('.approval-cut'), null, 'a whole preview was called shortened');
}

async function testOnlyTheOfferedAnswersGetButtons() {
  const sync = loadSync({ document, postJSON: async () => ({}) });
  const script = new Element('div');
  sync(script, asking({ decisions: ['allow_once', 'deny'] }));
  assert.deepStrictEqual(script.all('pk-btn').map((b) => b.textContent), ['Allow once', 'Deny'],
    'a script was offered for the session');
}

async function testASpawnWithoutApprovalsIsAskedWithItsWarning() {
  const sync = loadSync({ document, postJSON: async () => ({}) });
  const row = new Element('div');
  sync(row, asking({ warning: "agent 'coder' runs WITHOUT tool approvals: the rules of this run do not apply to its calls." }));
  const warning = row.querySelector('.approval-warning');
  assert.ok(warning, 'the warning was not shown');
  assert.ok(warning.textContent.includes('WITHOUT tool approvals'));
  const plain = new Element('div');
  sync(plain, asking());
  assert.strictEqual(plain.querySelector('.approval-warning'), null, 'a warning where none was sent');
}

async function testARowLeftOpenAtTheRunsEndLosesItsButtons() {
  // the question's last line never came (the run was torn down, its stream ended)
  const sync = loadSync({ document, postJSON: async () => ({}) });
  const row = new Element('div');
  const line = new Element('div');
  line.className = 'progress-line';
  const icon = new Element('span');
  icon.className = 'progress-icon';
  row.append(icon, line);
  sync(row, asking());
  assert.ok(row.querySelector(':scope > .approval-actions'), 'fixture: no buttons');
  const activeOperations = new Map([['r1_approval_a1b2c3d4e5f60718', row]]);
  const mark = load('markOpenScopesUnfinished', { activeOperations, document });
  mark();
  assert.strictEqual(row.querySelector(':scope > .approval-actions'), null, 'the buttons outlived the run');
  assert.ok(row.classList.contains('unfinished'));
}

async function testARefusedAnswerSaysWhyAndOnlyA404IsFinal() {
  let status = 403;
  const sync = loadSync({
    document,
    postJSON: async () => { const e = new Error('Only the user whose run asks, or an admin, may answer.'); e.status = status; throw e; },
  });
  const row = new Element('div');
  sync(row, asking());
  const box = row.querySelector(':scope > .approval-actions');
  const [allow] = box.all('pk-btn');

  await allow.listeners.click();
  assert.ok(!allow.disabled, 'a 403 left no second try');
  assert.ok(box.querySelector('.approval-note').textContent.includes('may answer'));

  status = 404;
  await allow.listeners.click();
  assert.ok(allow.disabled, 'a question nobody waits on could still be answered');
}

async function testOnlyAnAnswerPathOnThisServerIsTaken() {
  const sync = loadSync({ document, postJSON: async () => ({}) });
  for (const url of ['https://elsewhere.example/steal', '//elsewhere.example/x', '/api/requests/r1/cancel',
                     '/plugins/../api/requests/r1/cancel', '/plugins/tool_approval/answer?x=1']) {
    const row = new Element('div');
    sync(row, asking({ answer_url: url }));
    assert.strictEqual(row.querySelector(':scope > .approval-actions'), null, `buttons that post to ${url}`);
  }
  const plain = new Element('div');
  sync(plain, { type: 'status', phase: 'progress', request_id: 'r1_003', message: 'reading', meta: {} });
  assert.strictEqual(plain.children.length, 0, 'an ordinary status line got buttons');
}

async function testABlockedCallGetsALineOfItsOwn() {
  const host = new Element('div');
  const made = [];
  const rows = new Set(['r1_004']);   // request ids that have a status row on the page
  const line = load('toolErrorLine', {
    chatContainer: { querySelector: (sel) => ([...rows].some((id) => sel.includes(`"${id}"`)) ? {} : null) },
    CSS: { escape: (s) => s },
    statusBodyFor: () => host,
    createTreeOperationDiv: (key, ev) => {
      made.push(ev);
      const row = new Element('div');
      const icon = new Element('span');
      icon.className = 'progress-icon';
      row.appendChild(icon);
      return row;
    },
  });

  line({ step: 2 }, { type: 'tool_error', tool: 'terminal', error: 'The user denied the call.', blocked: true });
  line({ step: 2 }, { type: 'tool_error', tool: 'nope', error: 'Unknown tool: nope' });
  // a tool that ran and raised: its own row already ends in the error
  line({ step: 2 }, { type: 'tool_error', tool: 'probe', error: 'boom', request_id: 'r1_004' });

  assert.deepStrictEqual(made.map((ev) => [ev.server, ev.message]),
    [['terminal', 'blocked: The user denied the call.'], ['nope', 'failed: Unknown tool: nope']]);
  assert.strictEqual(host.children.length, 2);
  assert.ok(host.children[0].classList.contains('error') && host.children[0].classList.contains('blocked'));
  assert.ok(!host.children[1].classList.contains('blocked'));
  assert.ok(host.children[0].querySelector('.progress-icon').innerHTML.includes('error-mark'));
}

async function testAToolErrorReachesItsLine() {
  // the run's own stream, and a sub-run's (handleSubRunEvent hands the rest to renderRunEvent)
  const drawn = [];
  const render = load('renderRunEvent', { toolErrorLine: (view, data) => drawn.push([view, data.tool]) });
  const view = { openStep: 1 };
  render(view, { type: 'tool_error', tool: 'terminal', error: 'x', blocked: true });
  assert.deepStrictEqual(drawn, [[view, 'terminal']]);

  const handed = [];
  const handle = load('handleSSEEvent', {
    pendingAppendRebind: false, chatContainer: null, scrollBottom: () => {},
    renderRunEvent: (blk, data) => handed.push(data.type),
  });
  handle({ type: 'tool_error', tool: 'terminal', error: 'x' }, { row: null, steps: new Element('div') });
  assert.deepStrictEqual(handed, ['tool_error'], 'the stream dropped the tool_error event');
}

(async () => {
  const tests = [testTheQuestionGetsItsButtonsOnce, testTheRowsLastLineTakesTheButtonsDown,
    testAShortenedPreviewSaysSo, testARowLeftOpenAtTheRunsEndLosesItsButtons,
    testASpawnWithoutApprovalsIsAskedWithItsWarning, testOnlyTheOfferedAnswersGetButtons,
    testARefusedAnswerSaysWhyAndOnlyA404IsFinal, testOnlyAnAnswerPathOnThisServerIsTaken,
    testABlockedCallGetsALineOfItsOwn, testAToolErrorReachesItsLine];
  // That the chat marks the runs it starts as attended: chat_commands_web.test.js,
  // which sees the bodies the chat sends.
  for (const test of tests) {
    await test();
    await settle();
  }
  console.log(`${tests.length} passed`);
})().catch((error) => {
  console.error(String(error && error.message ? error.message : error), error && error.stack);
  process.exit(1);
});
