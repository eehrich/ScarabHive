// The chat's tool detail: what a tool was asked and answered, as something to read (toolPartNodes).
//
// The function is taken out of chat_module.js as it is and run against a small DOM stub, with the cap
// (TOOL_DETAIL_CHARS) made small, so its bounds can be looked at exactly.
//
// Run: node tests/js/tool_detail_parts.test.js   (driven by test_tool_detail_parts.py)
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');

// CHAT_MODULE: a copy to read instead (a mutation probe)
const SOURCE = fs.readFileSync(process.env.CHAT_MODULE || path.join(__dirname, '..', '..', 'static', 'js', 'chat_module.js'), 'utf8')
  .replace(/\r\n/g, '\n');

function functionSource(name) {
  const start = SOURCE.indexOf(`\n  function ${name}(`);
  assert.ok(start >= 0, `${name} is not in chat_module.js`);
  const end = SOURCE.indexOf('\n  }\n', start);
  assert.ok(end > start, `the end of ${name} was not found`);
  return SOURCE.slice(start, end + 4);
}

const fields = SOURCE.match(/\n  const EDIT_FIELDS = (\[.*\]);\n/);
assert.ok(fields, 'EDIT_FIELDS is not in chat_module.js');

class Element {
  constructor(tag) { this.tagName = tag.toUpperCase(); this.children = []; this.className = ''; this._text = ''; }
  appendChild(child) { this.children.push(child); return child; }
  get firstChild() { return this.children[0]; }
  set textContent(value) { this._text = String(value); this.children = []; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
}
const document = { createElement: (tag) => new Element(tag) };

function load(cap) {
  // eslint-disable-next-line no-new-func
  return new Function('document', 'TOOL_DETAIL_CHARS', 'EDIT_FIELDS',
    `${functionSource('toolPartNodes')}\nreturn toolPartNodes;`)(document, cap, new Function(`return ${fields[1]};`)());
}
const text = (nodes) => nodes.map((n) => n.textContent).join('|');
const lines = (nodes, cls) => nodes.flatMap(function walk(n) {
  return [...(n.className === cls ? [n.textContent] : []), ...n.children.flatMap(walk)];
});

let failed = 0;
function check(name, fn) {
  try { fn(); console.log(`ok   ${name}`); } catch (error) { failed += 1; console.log(`FAIL ${name}\n     ${error.message}`); }
}

check('an empty object is shown as {}, not as nothing', () => {
  assert.strictEqual(text(load(100)({})), '{}');
});

check('many fields are cut as one line, counting the fields not shown', () => {
  const payload = Object.fromEntries(Array.from({ length: 50 }, (_, i) => [`k${i}`, 'v'.repeat(8)]));
  const nodes = load(30)(payload);
  assert.ok(nodes.length < 6, `${nodes.length} nodes for a cap of 30`);
  const shown = nodes.length - 1;
  assert.strictEqual(nodes[nodes.length - 1].textContent, `… ${50 - shown} more fields`);
});

check("an edit's second half is not counted among the fields not shown", () => {
  const nodes = load(20)({ filePath: 'a.txt', oldString: 'x'.repeat(30), replaceAll: false, newString: 'y' });
  assert.strictEqual(nodes[nodes.length - 1].textContent, '… 1 more field');
});

check('an edit cut in its first half draws no empty line put in', () => {
  const nodes = load(20)({ oldString: 'x'.repeat(30), newString: 'new' });
  assert.deepStrictEqual(lines(nodes, 'tool-diff-add'), [], 'an empty "+ " line stands for the cut new half');
  assert.ok(text(nodes).includes('… 3 more characters'), text(nodes));
});

check('a key longer than the cap is cut like a value', () => {
  const nodes = load(10)({ ['k'.repeat(40)]: 'v' });
  assert.strictEqual(lines(nodes, 'tool-field-key')[0], `${'k'.repeat(10)}…`);
});

process.exitCode = failed ? 1 : 0;
