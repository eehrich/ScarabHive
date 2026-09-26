// A stand-in for /static/kit/panel-kit.js in panel_smoke.js: the names the panel imports, enough behaviour to run it
// without a shell or a browser. html/render escape like the kit; api() answers from globalThis.SERVER.
class SafeHtml { constructor(value) { this.value = value; } toString() { return this.value; } }
const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
export const escapeHtml = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ESC[c]);
const fragment = (v) => (v instanceof SafeHtml ? v.value : Array.isArray(v) ? v.map(fragment).join('') : v == null || v === false ? '' : escapeHtml(v));
export const html = (strings, ...values) => new SafeHtml(String.raw({ raw: strings }, ...values.map(fragment)));
export const trusted = (m) => new SafeHtml(String(m));
export function render(el, content) { el.innerHTML = fragment(content); globalThis.RENDERS.push([el.id, el.innerHTML.length]); }
export function update(el, content) { const m = fragment(content); if (el._drawn === m) return false; el._drawn = m; render(el, content); return true; }
export const icon = (name) => { globalThis.ICONS.add(name); return new SafeHtml(`<svg><use href="#${escapeHtml(name)}"/></svg>`); };
export const emptyState = (name, title, text = '') => html`<div class="pk-empty">${icon(name)}${title}${text}</div>`;
export const jsonView = (v) => html`<pre>${JSON.stringify(v)}</pre>`;
export const yamlCode = (v) => html`${v ?? ''}`;
export const errorText = (e) => `${e.status}: ${e.message}`;
export const isAborted = (e) => e?.name === 'AbortError';
export const localTime = (v) => String(v ?? '');
export const pluginBase = () => '/plugins/stategraph';
export const notice = (el, text) => { el.textContent = text || ''; el.hidden = !text; };
export const setDirty = (v) => { globalThis.DIRTY = Boolean(v); };
export const setTitle = (t) => { globalThis.TITLE = t; };
export const setQuery = (q) => { globalThis.QUERY = new URLSearchParams(q).toString(); };
export const navigate = () => {};
export const openSession = (id) => { (globalThis.OPENED ||= []).push(id); return true; };
export const selectTab = (list, name) => { globalThis.TABS[list.id] = name; return true; };
export const toast = (m, { kind = 'info' } = {}) => { globalThis.TOASTS.push([kind, String(m)]); };
export const copyText = async () => true;
export async function withBusy(controls, fn) { return fn(); }
export function autoRefresh(fn, ms) { let on = false; return { get running() { return on; }, start() { on = true; }, stop() { on = false; } }; }
export const confirm = async (m) => { globalThis.ASKED.push(['confirm', m]); return globalThis.ANSWERS.confirm ?? true; };
export const prompt = async (m) => { globalThis.ASKED.push(['prompt', m]); return globalThis.ANSWERS.prompt.shift() ?? null; };
export const dialog = async (spec) => { globalThis.ASKED.push(['dialog', spec.title, spec.message]); return globalThis.ANSWERS.dialog ?? null; };
export class ApiError extends Error { constructor(status, detail) { super(String(detail)); this.status = status; this.detail = detail; } }
export async function api(path, { method = 'GET', json } = {}) {
  globalThis.CALLS.push([method, path, json]);
  const answer = globalThis.SERVER(method, path, json);
  if (answer instanceof ApiError) {
    globalThis.TOASTS.push(['error', answer.message]);
    throw answer;
  }
  return JSON.parse(JSON.stringify(answer));
}
export const abandon = (name) => { (globalThis.ABANDONED ||= []).push(name); };
