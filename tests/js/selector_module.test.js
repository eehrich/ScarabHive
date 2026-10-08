// The composer's selector (static/js/selector_module.js), against stubbed /agents and /llm/profiles:
// the model follows the agent, only a person's choice goes out, a session's restore is no choice.
//
// Run: node tests/js/selector_module.test.js   (driven by test_selector_module.py)
'use strict';

const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const MODULE = path.join(__dirname, '..', '..', 'static', 'js', 'selector_module.js');

const AGENTS = {
  agents: ['coder', 'writer', 'bare'],
  default: 'coder',
  details: [{ name: 'coder', llm_profile: 'fast' }, { name: 'writer', llm_profile: 'deep' },
    { name: 'bare', llm_profile: null }],
};
const PROFILES = {
  profiles: [{ name: 'deep' }, { name: 'fast' }, { name: 'turbo' }],
  default: 'turbo',
  thinking_levels: ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'],
};

/** A window with the selector loaded; *hold* keeps a list from answering until released. */
function load({ hold = {}, fail = {} } = {}) {
  const events = [];
  const releases = {};
  const window = {
    console,
    document: { getElementById: () => null, createElementNS: () => ({ setAttribute() {}, append() {} }) },
    CustomEvent: function (type, init) { this.type = type; this.detail = init.detail; },
    dispatchEvent(event) { events.push(event.detail); },
    fetch(url) {
      const data = url === '/agents' ? AGENTS : PROFILES;
      const answer = fail[url] ? { ok: false, status: 500 } : { ok: true, json: async () => data };
      if (hold[url]) return new Promise((resolve) => { releases[url] = () => resolve(answer); });
      return Promise.resolve(answer);
    },
  };
  window.window = window;
  vm.runInNewContext(fs.readFileSync(MODULE, 'utf8'), Object.assign(window, { fetch: window.fetch }),
    { filename: MODULE });
  return { selector: window.selectorModule, events, releases };
}

// objects made in the module's context have its prototypes: compare their JSON
const plain = (value) => JSON.parse(JSON.stringify(value));

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test('the chat runs on the agent\'s own profile and sends nothing until a person picks', async () => {
  const { selector } = load();
  await selector.init();
  assert.strictEqual(selector.getCurrentAgent(), 'coder');
  assert.strictEqual(selector.getCurrentLLMProfile(), 'fast', 'not the agent\'s own (the global default is turbo)');
  assert.strictEqual(selector.getProfileOverride(), null);
  assert.deepStrictEqual(plain(selector.getLLMParams()), {});
});

test('a picked profile is an override, the agent\'s own one is none', async () => {
  const { selector } = load();
  await selector.init();
  assert.ok(selector.setLLMProfile('turbo'));
  assert.strictEqual(selector.getProfileOverride(), 'turbo');
  assert.ok(selector.setLLMProfile('fast'));
  assert.strictEqual(selector.getProfileOverride(), null);
  assert.strictEqual(selector.setLLMProfile('nope'), false);
});

test('a person\'s agent pick drops their choice and names it; the new agent runs on its own', async () => {
  const { selector, events } = load();
  await selector.init();
  selector.setLLMProfile('turbo');
  selector.setThinking('high');
  selector.setAgent('writer');
  assert.strictEqual(selector.getCurrentLLMProfile(), 'deep');
  assert.strictEqual(selector.getProfileOverride(), null);
  assert.deepStrictEqual(plain(selector.getLLMParams()), {});
  assert.deepStrictEqual(plain(events.pop().dropped), { profile: 'turbo', params: { thinking_level: 'high' } });
});

test('a pick of the same agent keeps the choice', async () => {
  const { selector, events } = load();
  await selector.init();
  selector.setLLMProfile('turbo');
  selector.setAgent('coder');
  assert.strictEqual(selector.getProfileOverride(), 'turbo');
  assert.strictEqual(events.pop().dropped, null);
});

test('a restore takes the session\'s profile only where it is not the agent\'s own, and only known params', async () => {
  const { selector } = load();
  await selector.init();
  selector.restore({ agent: 'writer', profile: 'deep', params: { thinking_level: 'low', max_tokens: 5 } });
  assert.strictEqual(selector.getCurrentAgent(), 'writer');
  assert.strictEqual(selector.getProfileOverride(), null, 'the stored own profile became an override');
  assert.deepStrictEqual(plain(selector.getLLMParams()), { thinking_level: 'low' },
    'a key the server refuses would fail every message of the session');
  selector.restore({ agent: 'writer', profile: 'turbo', params: { thinking_level: 'ultra' } });
  assert.strictEqual(selector.getProfileOverride(), 'turbo');
  assert.deepStrictEqual(plain(selector.getLLMParams()), {});
});

test('a restore that comes before the lists is applied once both are there', async () => {
  const { selector, releases } = load({ hold: { '/llm/profiles': true } });
  const started = selector.init();
  await new Promise((resolve) => setTimeout(resolve, 0));
  selector.restore({ agent: 'writer', profile: 'turbo', params: { thinking_level: 'max' } });
  releases['/llm/profiles']();
  await started;
  assert.strictEqual(selector.getCurrentAgent(), 'writer');
  assert.strictEqual(selector.getProfileOverride(), 'turbo');
  assert.deepStrictEqual(plain(selector.getLLMParams()), { thinking_level: 'max' });
});

test('a restore while the profiles load takes the agent at once: a message then must not go to another', async () => {
  const { selector, releases } = load({ hold: { '/llm/profiles': true } });
  const started = selector.init();
  await new Promise((resolve) => setTimeout(resolve, 0));
  selector.restore({ agent: 'writer', profile: 'turbo' });
  assert.strictEqual(selector.getCurrentAgent(), 'writer', 'the session would run under the default agent');
  releases['/llm/profiles']();
  await started;
  assert.strictEqual(selector.getProfileOverride(), 'turbo');
});

test('a person\'s pick while a restore waits wins, and the session\'s choice does not follow', async () => {
  const { selector, releases } = load({ hold: { '/llm/profiles': true } });
  const started = selector.init();
  await new Promise((resolve) => setTimeout(resolve, 0));
  selector.restore({ agent: 'writer', profile: 'turbo', params: { thinking_level: 'high' } });
  selector.setAgent('coder');
  releases['/llm/profiles']();
  await started;
  assert.strictEqual(selector.getCurrentAgent(), 'coder');
  assert.strictEqual(selector.getProfileOverride(), null);
  assert.deepStrictEqual(plain(selector.getLLMParams()), {});
});

test('a stored level stays when the levels could not be read: the server judges it', async () => {
  const { selector } = load({ fail: { '/llm/profiles': true } });
  await selector.init();
  selector.restore({ agent: 'writer', params: { thinking_level: 'high' } });
  assert.deepStrictEqual(plain(selector.getLLMParams()), { thinking_level: 'high' });
});

test('a message sent while a restore waits carries the session\'s choice, so its save keeps it', async () => {
  const { selector, releases } = load({ hold: { '/llm/profiles': true } });
  const started = selector.init();
  await new Promise((resolve) => setTimeout(resolve, 0));
  selector.restore({ agent: 'writer', profile: 'turbo', params: { thinking_level: 'high', max_tokens: 9 } });
  assert.strictEqual(selector.getProfileOverride(), 'turbo');
  assert.deepStrictEqual(plain(selector.getLLMParams()), { thinking_level: 'high' });
  releases['/llm/profiles']();
  await started;
});

test('a failed profile list keeps the session\'s pick: the server judges it', async () => {
  const { selector } = load({ fail: { '/llm/profiles': true } });
  await selector.init();
  selector.restore({ agent: 'writer', profile: 'turbo' });
  assert.strictEqual(selector.getProfileOverride(), 'turbo');
});

test('an agent without a known own profile: a stored profile stays the session\'s choice', async () => {
  const { selector } = load();
  await selector.init();
  selector.restore({ agent: 'bare', profile: 'deep' });
  assert.strictEqual(selector.getProfileOverride(), 'deep');
});

test('a person\'s profile and level set while a restore waits for the agents are what goes out, then and after', async () => {
  const { selector, releases } = load({ hold: { '/agents': true } });
  const started = selector.init();
  await new Promise((resolve) => setTimeout(resolve, 0));
  selector.restore({ agent: 'writer', profile: 'deep', params: { thinking_level: 'low' } });
  assert.ok(selector.setLLMProfile('turbo'));
  assert.ok(selector.setThinking('high'));
  assert.strictEqual(selector.getProfileOverride(), 'turbo', 'the next message sends the session\'s choice');
  assert.deepStrictEqual(plain(selector.getLLMParams()), { thinking_level: 'high' });
  releases['/agents']();
  await started;
  assert.strictEqual(selector.getProfileOverride(), 'turbo', 'the restore put the session\'s choice over it');
  assert.deepStrictEqual(plain(selector.getLLMParams()), { thinking_level: 'high' });
});

test('Keep puts back a pick the failed profile list could not confirm, as the restore took it', async () => {
  const { selector, events } = load({ fail: { '/llm/profiles': true } });
  await selector.init();
  selector.restore({ agent: 'coder', profile: 'turbo' });
  selector.setAgent('writer');
  const { dropped } = events.pop();
  selector.setChoice({ override: dropped.profile, params: dropped.params });
  assert.strictEqual(selector.getProfileOverride(), 'turbo', 'the note says "Kept" and the pick is gone');
});

test('setThinking takes only what the server takes', async () => {
  const { selector } = load();
  await selector.init();
  assert.strictEqual(selector.setThinking('ultra'), false);
  assert.deepStrictEqual(plain(selector.getLLMParams()), {});
  assert.ok(selector.setThinking('none'));
  assert.deepStrictEqual(plain(selector.getLLMParams()), { thinking_level: 'none' });
  assert.ok(selector.setThinking(null));
  assert.deepStrictEqual(plain(selector.getLLMParams()), {});
});

(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try {
      await fn();
      console.log('  ok   ' + name);
    } catch (error) {
      failed += 1;
      console.log('  FAIL ' + name + '\n       ' + String(error.message).split('\n').join('\n       '));
    }
  }
  console.log(failed ? failed + ' failed' : 'all ' + tests.length + ' passed');
  process.exit(failed ? 1 : 0);
})();
