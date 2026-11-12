/**
 * The web chat's slash menu (static/js/slash_commands.js), driven the way the
 * page drives it: attached to the input, fed an "input" event per word, read
 * back from the rows it renders. The catalogue is the server's own answer to
 * GET /chat/commands?surface=web, written to the file named in argv[2] by
 * tests/app/test_app_chat_commands.py -- not one rebuilt here.
 *
 * Exit code 0 when every case holds; each failure is printed.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const MODULE = path.join(__dirname, '..', '..', 'static', 'js', 'slash_commands.js');
const catalogue = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

function makeElement() {
  return {
    style: {}, children: [], listeners: {}, value: '', textContent: '', className: '',
    set innerHTML(_) { this.children = []; },
    appendChild(child) { this.children.push(child); return child; },
    addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); },
    setAttribute() {}, focus() {}, closest() { return null; }, get parentNode() { return makeElement(); },
    dispatchEvent(event) { (this.listeners[event.type] || []).forEach((fn) => fn(event)); },
  };
}

const window = { addEventListener() {}, setTimeout() {} };
const sandbox = {
  window, document: { createElement: makeElement },
  fetch: () => Promise.resolve({ ok: true, json: () => Promise.resolve(catalogue) }),
  Event: function (type) { this.type = type; },
};
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(MODULE, 'utf8'), sandbox);
const slash = window.slashCommands;

const input = makeElement();
let box = null;
input.closest = () => null;
Object.defineProperty(input, 'parentNode', { get: () => ({
  style: {}, appendChild(child) { box = child; return child; } }) });

/** The rows the menu shows for *word*, as their label texts, first one first. */
function menu(word) {
  input.value = word;
  input.listeners.input.forEach((fn) => fn({}));
  return box ? box.children.map((row) => row.children[0].textContent) : [];
}

const failures = [];
function check(name, ok, detail) { if (!ok) failures.push(`${name}: ${detail}`); }

slash.attach(input);
slash.load().then(() => {
  // Enter takes the first row: an alias of another command must not stand before a name.
  let rows = menu('/res');
  check('"/res" offers /resume first', rows[0] && rows[0].startsWith('/resume'), JSON.stringify(rows));
  rows = menu('/cont');
  check('"/cont" offers /context first', rows[0] && rows[0].startsWith('/context'), JSON.stringify(rows));
  check('"/cont" still offers /resume through its alias', rows.some((r) => r.startsWith('/resume')),
    JSON.stringify(rows));
  // A spelling typed in full leads, though another command's name starts with it too.
  rows = menu('/h');
  check('"/h" offers /help first', rows[0] && rows[0].startsWith('/help'), JSON.stringify(rows));
  // The row says why it is offered when only an alias matched.
  rows = menu('/restore');
  check('"/restore" names the alias on /rewind\'s row',
    rows.length === 1 && rows[0].startsWith('/rewind') && rows[0].includes('restore'), JSON.stringify(rows));
  // ...and repeats nothing the usage line already names.
  const help = catalogue.commands.find((c) => c.name === 'help');
  rows = menu('/hel');
  check('"/help" lists no alias twice', rows[0] === help.display, JSON.stringify(rows));

  if (failures.length) {
    failures.forEach((f) => console.log('FAIL', f));
    process.exit(1);
  }
  console.log('ok');
});
