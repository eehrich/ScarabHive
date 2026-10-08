// A minimal DOM for panel_smoke.js in jsc: elements with attributes, classes, children, listeners, forms, and the
// few globals the panel reads (location, localStorage, URLSearchParams where jsc has none).
class ClassList {
  constructor(el) { this.el = el; }
  get set() { return new Set((this.el.getAttribute('class') || '').split(/\s+/).filter(Boolean)); }
  write(set) { this.el.setAttribute('class', [...set].join(' ')); }
  add(...names) { const s = this.set; names.forEach((n) => s.add(n)); this.write(s); }
  remove(...names) { const s = this.set; names.forEach((n) => s.delete(n)); this.write(s); }
  toggle(name, on) { const s = this.set; const want = on === undefined ? !s.has(name) : Boolean(on); if (want) s.add(name); else s.delete(name); this.write(s); return want; }
  contains(name) { return this.set.has(name); }
}
function matches(el, selector) {
  return selector.split(',').map((s) => s.trim()).some((one) => {
    let m;
    if ((m = one.match(/^\.([\w-]+)$/))) return el.classList.contains(m[1]);
    if ((m = one.match(/^\[([\w-]+)(?:="([^"]*)")?\]$/))) return m[2] === undefined ? el.hasAttribute(m[1]) : el.getAttribute(m[1]) === m[2];
    if ((m = one.match(/^([\w-]+)$/))) return el.tagName === m[1].toUpperCase();
    return false;
  });
}
class FakeElement {
  constructor(tag = 'div', id = '') {
    this.tagName = tag.toUpperCase(); this.attrs = {}; this.children = []; this.parentNode = null; this.listeners = {};
    this.dataset = new Proxy({}, {
      get: (_, key) => this.attrs[`data-${String(key).replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`],
      set: (_, key, value) => { this.attrs[`data-${String(key).replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`] = String(value); return true; },
      deleteProperty: (_, key) => { delete this.attrs[`data-${String(key).replace(/[A-Z]/g, (c) => `-${c.toLowerCase()}`)}`]; return true; },
    });
    this.classList = new ClassList(this); this.style = {}; this.value = ''; this.checked = false; this.disabled = false;
    this.readOnly = false; this.hidden = false; this.innerHTML = ''; this._text = ''; this.id = id;
    this.selectionStart = 0; this.selectionEnd = 0;
  }
  get textContent() { return this._text; }
  set textContent(v) { this._text = String(v); this.children = []; }
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === 'id') this.id = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  hasAttribute(k) { return k in this.attrs; }
  removeAttribute(k) { delete this.attrs[k]; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  replaceChildren(...cs) { this.children = []; cs.forEach((c) => this.appendChild(c)); }
  remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter((c) => c !== this); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  removeEventListener() {}
  dispatchEvent(event) { event.target ||= this; for (const fn of this.listeners[event.type] || []) fn(event); let p = this.parentNode; while (event.bubbles && p) { for (const fn of p.listeners[event.type] || []) fn(event); p = p.parentNode; } return true; }
  async fire(type, init = {}) { const event = { type, bubbles: true, preventDefault() {}, stopPropagation() {}, target: this, ...init }; const results = (this.listeners[type] || []).map((fn) => fn(event)); await Promise.all(results); return event; }
  descendants() { const out = []; const walk = (n) => n.children.forEach((c) => { out.push(c); walk(c); }); walk(this); return out; }
  querySelectorAll(sel) { return this.descendants().filter((d) => matches(d, sel)); }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  closest(sel) { for (let n = this; n; n = n.parentNode) if (n instanceof FakeElement && matches(n, sel)) return n; return null; }
  contains(other) { for (let n = other; n; n = n.parentNode) if (n === this) return true; return false; }
  getBoundingClientRect() { return { width: 900, height: 600, left: 0, top: 0, right: 900, bottom: 600 }; }
  focus() {} setPointerCapture() {} setSelectionRange() {}
  setRangeText(t) { this.value += t; }
  get elements() {
    const self = this;
    const fields = self._fields ||= {};
    return new Proxy(fields, {
      get(target, key) {
        if (key === Symbol.iterator) return function* () { yield* Object.values(target); };
        if (typeof key !== 'string') return undefined;
        return target[key] ||= new FakeElement('input');
      },
    });
  }
}
const byId = new Map();
globalThis.FakeElement = FakeElement;
globalThis.document = {
  getElementById(id) { if (!byId.has(id)) byId.set(id, new FakeElement(id === 'canvas' ? 'svg' : 'div', id)); return byId.get(id); },
  createElementNS(ns, tag) { return new FakeElement(tag); },
  createElement(tag) { return new FakeElement(tag); },
  addEventListener(type, fn) { (globalThis.DOC_LISTENERS[type] ||= []).push(fn); },
  elementFromPoint() { return null; },
};
globalThis.window = globalThis;
globalThis.DOC_LISTENERS = {};
globalThis.location = { search: '?machine=review', pathname: '/plugins/stategraph/' };
globalThis.localStorage = { store: {}, getItem(k) { return this.store[k] ?? null; }, setItem(k, v) { this.store[k] = String(v); }, removeItem(k) { delete this.store[k]; } };
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0);
globalThis.Event = class { constructor(type) { this.type = type; } };
globalThis.console = { log: print, warn: print, error: (...a) => { globalThis.ERRORS.push(a.map(String).join(' ')); print('console.error', ...a); }, err: print };
globalThis.ERRORS = [];
if (typeof URLSearchParams === 'undefined') {
  globalThis.URLSearchParams = class {
    constructor(init = '') {
      this.pairs = [];
      if (typeof init === 'string') {
        for (const part of init.replace(/^\?/, '').split('&').filter(Boolean)) {
          const [k, v = ''] = part.split('=');
          this.pairs.push([decodeURIComponent(k), decodeURIComponent(v)]);
        }
      } else if (init) {
        for (const [k, v] of Object.entries(init)) this.pairs.push([k, String(v)]);
      }
    }
    get(key) { const found = this.pairs.find(([k]) => k === key); return found ? found[1] : null; }
    toString() { return this.pairs.map(([k, v]) => `${encodeURIComponent(k)}=${encodeURIComponent(v)}`).join('&'); }
  };
}
