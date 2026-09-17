// The workspace: panels docked as tabs beside the chat, or detached into
// floating windows -- and the host side of the pk:* protocol every panel speaks.
import { html, render, icon, showToast, showDialog } from '/static/kit/panel-kit.js';

// allow-modals only keeps unmigrated panels' native dialogs working; it goes
// once no panel calls them (docs/webui_konzept.md, section 4.4).
const SANDBOX = 'allow-scripts allow-same-origin allow-forms allow-modals allow-popups allow-downloads';
const MIN_WIDTH = 320;
const MIN_HEIGHT = 200;

/** A stored window rectangle, or null when storage holds something else. */
function storedRect(rect) {
  return rect && ['x', 'y', 'w', 'h'].every((key) => Number.isFinite(rect[key]))
    ? { x: rect.x, y: rect.y, w: rect.w, h: rect.h } : null;
}

export class Workspace {
  /**
   * @param {object} deps
   * @param {() => object} deps.catalog  returns the catalogue ({panels: [...]})
   * @param {() => string} deps.theme    current theme
   * @param {() => object|null} deps.session  active session {id, title}
   * @param {(theme: string) => void} deps.onSetTheme
   * @param {string} deps.layoutKey  where this browser keeps the layout
   * @param {() => boolean} deps.narrow  a narrow screen: panels cover the chat, one at a time
   * @param {() => void} deps.onShow  a panel came to the front
   */
  constructor({ catalog, theme, session, onSetTheme, layoutKey, narrow, onShow }) {
    this.catalog = catalog;
    this.theme = theme;
    this.session = session;
    this.onSetTheme = onSetTheme;
    this.layoutKey = layoutKey;
    this.narrow = narrow;
    this.onShow = onShow;
    this.body = document.querySelector('.app-body');
    this.dock = document.getElementById('dock');
    this.resizer = document.getElementById('dockResizer');
    this.tabBar = this.dock.querySelector('.app-dock-tabs');
    this.frameHost = this.dock.querySelector('.app-dock-frames');
    this.layer = document.getElementById('floatingLayer');
    this.floatingList = document.getElementById('floatingList');
    /**
     * key -> {key, panelId, path, linked, place: 'dock'|'float', frame, element, title, rect}
     * linked: the path is where a link sent the panel, not where it navigated itself
     */
    this.items = new Map();
    this.active = null;
    this.draggedTab = null;
    this.tabDropped = false;
    this.zTop = 1;
    this.dockWidth = 520;
    window.addEventListener('message', (event) => this.onMessage(event));
    this.wireResizer();
    this.wireTabBar();
    document.getElementById('dockCollapse').addEventListener('click', () => this.stepAside());
    let pending = false;
    window.addEventListener('resize', () => {
      if (pending) return;
      pending = true;
      requestAnimationFrame(() => {
        pending = false;
        this.fitWindows();
        this.renderDock();  // crossing the narrow breakpoint shows or hides the dock
      });
    });
    this.floatingList.addEventListener('click', (event) => {
      const chip = event.target.closest('[data-key]');
      if (chip) this.focus(chip.dataset.key);
    });
  }

  panel(panelId) {
    return this.catalog().panels.find((p) => p.id === panelId) || null;
  }

  // ------------------------------------------------------------ opening

  /**
   * Open a panel (or focus it if it is open). place: 'dock' (default) or 'float'.
   * path: where a link sends the panel. Opened again without one, a panel a
   * link sent somewhere starts over; a path it navigated to itself stays.
   */
  open(panelId, { path = '', place = 'dock', rect = null, linked = Boolean(path), quietly = false } = {}) {
    const panel = this.panel(panelId);
    if (!panel) {
      showToast(document, `Panel "${panelId}" is not available`, 'warn');
      return;
    }
    const existing = [...this.items.values()].find((item) => item.panelId === panelId);
    if (existing) {
      if (path || existing.linked) {
        const moved = path !== existing.path;
        existing.path = path;
        existing.linked = linked;
        if (moved) this.mount(existing);
        this.save();
      }
      this.focus(existing.key, { quietly });
      return;
    }
    const item = { key: `${panelId}:${Date.now().toString(36)}`, panelId, path, linked, place, rect, frame: null, element: null };
    this.items.set(item.key, item);
    this.mount(item);
    this.focus(item.key, { quietly });
    this.save();
  }

  /** The URL of a panel from its catalogue entry and a path relative to it (query or sub path). */
  url(item) {
    const base = this.panel(item.panelId).url;
    if (!item.path) return base;
    if (item.path.startsWith('?')) return base + item.path;
    return item.path.startsWith(base) ? item.path : base.replace(/\/?$/, '/') + item.path.replace(/^\//, '');
  }

  createFrame(item) {
    const frame = document.createElement('iframe');
    frame.className = 'panel-frame';
    frame.setAttribute('sandbox', SANDBOX);
    frame.title = item.title;
    frame.src = this.url(item);
    return frame;
  }

  /** (Re)load the panel where it is placed. A fresh document starts with the catalogue title. */
  mount(item) {
    if (item.frame) item.frame.remove();
    if (item.element) item.element.remove();
    item.element = null;
    item.title = this.panel(item.panelId).title;
    item.frame = this.createFrame(item);
    if (item.place === 'dock') {
      this.frameHost.appendChild(item.frame);
    } else {
      this.mountWindow(item);
    }
    this.renderDock();
    this.renderFloatingList();
  }

  close(key) {
    const item = this.items.get(key);
    if (!item) return;
    item.frame.remove();
    if (item.element) item.element.remove();
    this.items.delete(key);
    if (this.active === key) this.active = this.docked().at(-1)?.key || null;
    this.renderDock();
    this.renderFloatingList();
    this.save();
  }

  /** Docked becomes floating and back. Moving an iframe reloads it; the path it reported is kept. */
  move(key, place) {
    const item = this.items.get(key);
    if (!item || item.place === place) return;
    item.place = place;
    if (place === 'dock') this.active = key;
    this.mount(item);
    this.focus(key);
    this.save();
  }

  docked() {
    return [...this.items.values()].filter((item) => item.place === 'dock');
  }

  /** Whether a panel is on screen: the front tab of a shown dock, or a window while the panels are not aside. */
  shown(item) {
    if (item.place === 'float') return getComputedStyle(this.layer).display !== 'none';
    return item.key === this.active && getComputedStyle(this.dock).display !== 'none';
  }

  /** On a narrow screen the panels cover the chat; this steps them back without closing any. */
  stepAside() {
    this.body.dataset.panels = 'aside';
    this.renderDock();
  }

  /** quietly: put back (the layout restore), not brought forward by the viewer. */
  focus(key, { quietly = false } = {}) {
    const item = this.items.get(key);
    if (!item) return;
    delete this.body.dataset.panels;
    if (item.place === 'dock') {
      this.active = key;
    } else {
      item.element.style.zIndex = String(++this.zTop);
      this.layer.querySelectorAll('.app-window').forEach((w) => { w.dataset.focused = String(w === item.element); });
    }
    this.renderDock();
    if (!quietly) this.onShow();
  }

  // --------------------------------------------------------------- dock

  renderDock() {
    const docked = this.docked();
    const open = docked.length > 0;
    this.dock.hidden = !open;
    this.resizer.hidden = !open;
    this.body.style.setProperty('--dock-width', `${this.dockWidth}px`);
    if (open && !docked.some((item) => item.key === this.active)) this.active = docked[0].key;
    // re-rendering the bar keeps the keyboard focus: render() finds the element again by its
    // data-key, and a tab and each of its buttons have one
    // While a tab is dragged the bar is left alone: a replaced drag source never gets its dragend.
    // The drag's end draws it again, titles that came meanwhile included.
    if (!this.draggedTab) render(this.tabBar, docked.map((item) => html`
      <div class="dock-tab" role="tab" tabindex="0" draggable="true" aria-selected="${String(item.key === this.active)}" data-key="${item.key}" title="${item.title}">
        ${icon(this.panel(item.panelId).icon, { size: 'sm' })}
        <span class="dock-tab-title">${item.title}</span>
        <span class="dock-tab-actions">
          <button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm" data-act="float" data-key="${item.key}:float" title="Detach into a window">${icon('picture-in-picture-2', { size: 'sm' })}</button>
          <button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm" data-act="close" data-key="${item.key}:close" title="Close">${icon('x', { size: 'sm' })}</button>
        </span>
      </div>`));
    docked.forEach((item) => { item.frame.hidden = item.key !== this.active; });
    this.items.forEach((item) => this.post(item, 'pk:visibility', { visible: this.shown(item) }));
  }

  wireTabBar() {
    this.tabBar.addEventListener('click', (event) => {
      const tab = event.target.closest('.dock-tab');
      if (!tab) return;
      const act = event.target.closest('[data-act]')?.dataset.act;
      if (act === 'close') this.close(tab.dataset.key);
      else if (act === 'float') this.move(tab.dataset.key, 'float');
      else this.focus(tab.dataset.key);
    });
    this.tabBar.addEventListener('keydown', (event) => {
      if (!event.target.matches('.dock-tab')) return;
      if (event.shiftKey && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) {
        // the keyboard's way to sort the tabs
        event.preventDefault();
        const keys = this.docked().map((item) => item.key);
        const from = keys.indexOf(event.target.dataset.key);
        const to = from + (event.key === 'ArrowLeft' ? -1 : 1);
        if (to < 0 || to >= keys.length) return;
        keys.splice(to, 0, keys.splice(from, 1)[0]);
        this.arrangeDock(keys);
        return;
      }
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      this.focus(event.target.dataset.key);
    });
    // Sorting by drag and drop: the dragged tab moves in the bar as it goes; dropped on the bar, the order is
    // taken, cancelled (Esc) or dropped anywhere else, the bar goes back to the order it had.
    this.tabBar.addEventListener('dragstart', (event) => {
      const tab = event.target.closest?.('.dock-tab');
      if (!tab) return;
      this.draggedTab = tab;
      this.tabDropped = false;
      tab.dataset.dragging = '';
      event.dataTransfer.effectAllowed = 'move';
      // Firefox starts no drag without data; a type of its own, so no text field takes the tab as text
      event.dataTransfer.setData('application/x-scarabhive-tab', tab.dataset.key);
    });
    this.tabBar.addEventListener('dragover', (event) => {
      const dragged = this.draggedTab;
      if (!dragged) return;  // a file or text dragged in from elsewhere is no tab
      event.preventDefault();
      const others = [...this.tabBar.querySelectorAll('.dock-tab')].filter((tab) => tab !== dragged);
      const before = others.find((tab) => {
        const box = tab.getBoundingClientRect();
        return event.clientX < box.left + box.width / 2;
      }) || null;
      if (dragged.nextElementSibling !== before) this.tabBar.insertBefore(dragged, before);
    });
    this.tabBar.addEventListener('drop', (event) => {
      if (!this.draggedTab) return;
      event.preventDefault();
      this.tabDropped = true;
    });
    this.tabBar.addEventListener('dragend', () => {
      if (!this.draggedTab) return;
      this.draggedTab = null;
      if (this.tabDropped) this.arrangeDock([...this.tabBar.querySelectorAll('.dock-tab')].map((tab) => tab.dataset.key));
      else this.renderDock();
    });
  }

  /** Docked panels in the order of these keys, the rest after them; drawn and kept for the next visit. */
  arrangeDock(keys) {
    const sorted = keys.map((key) => this.items.get(key)).filter((item) => item?.place === 'dock');
    const rest = [...this.items.values()].filter((item) => !sorted.includes(item));
    this.items = new Map([...sorted, ...rest].map((item) => [item.key, item]));
    this.renderDock();
    this.save();
  }

  /** Follow a pointer until it is released or lost -- also when it leaves the window or the browser cancels it. */
  track(handle, event, onMove, onEnd) {
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    document.body.classList.add('app-dragging');
    const end = () => {
      document.body.classList.remove('app-dragging');
      handle.removeEventListener('pointermove', onMove);
      handle.removeEventListener('lostpointercapture', end);
      onEnd();
    };
    handle.addEventListener('pointermove', onMove);
    handle.addEventListener('lostpointercapture', end);
  }

  wireResizer() {
    this.resizer.addEventListener('pointerdown', (event) => {
      if (event.button !== 0) return;
      const startX = event.clientX;
      const startWidth = this.dockWidth;
      this.resizer.dataset.dragging = '';
      this.track(this.resizer, event, (e) => {
        this.dockWidth = Math.min(Math.max(startWidth + (startX - e.clientX), MIN_WIDTH), window.innerWidth * 0.7);
        this.body.style.setProperty('--dock-width', `${this.dockWidth}px`);
      }, () => {
        delete this.resizer.dataset.dragging;
        this.save();
      });
    });
  }

  // ------------------------------------------------------------ windows

  mountWindow(item) {
    const panel = this.panel(item.panelId);
    const element = document.createElement('section');
    element.className = 'app-window';
    render(element, html`
      <header class="app-window-bar">
        <button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm app-back" data-act="aside" title="Back to the chat">${icon('arrow-left', { size: 'sm' })}</button>
        ${icon(panel.icon, { size: 'sm' })}
        <span class="app-window-title pk-truncate"></span>
        <button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm" data-act="dock" title="Dock beside the chat">${icon('panel-right', { size: 'sm' })}</button>
        <button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm" data-act="close" title="Close">${icon('x', { size: 'sm' })}</button>
      </header>
      <div class="app-window-body"></div>
      <div class="app-window-resize" aria-hidden="true"></div>`);
    item.element = element;
    this.showTitle(item);
    element.querySelector('.app-window-body').appendChild(item.frame);
    this.place(item);
    this.layer.appendChild(element);

    element.addEventListener('pointerdown', () => this.focus(item.key));
    element.querySelector('[data-act="aside"]').addEventListener('click', () => this.stepAside());
    element.querySelector('[data-act="close"]').addEventListener('click', () => this.close(item.key));
    element.querySelector('[data-act="dock"]').addEventListener('click', () => this.move(item.key, 'dock'));
    this.dragging(element.querySelector('.app-window-bar'), item, (rect, dx, dy) => ({ ...rect, x: rect.x + dx, y: rect.y + dy }));
    this.dragging(element.querySelector('.app-window-resize'), item,
      (rect, dx, dy) => ({ ...rect, w: Math.max(MIN_WIDTH, rect.w + dx), h: Math.max(MIN_HEIGHT, rect.h + dy) }));
  }

  showTitle(item) {
    item.element.querySelector('.app-window-title').textContent = item.title;
    item.element.setAttribute('aria-label', item.title);
  }

  /**
   * Show a window at its rectangle, pulled into reach of the layer as it is now.
   * The rectangle itself stays: a browser window shrunk and grown again gets it back.
   * A window gets its first rectangle on the first wide screen that shows it --
   * on a narrow one it fills the screen and takes none.
   */
  place(item) {
    if (!item.rect) {
      if (this.narrow()) return;
      item.rect = this.firstRect(item);
    }
    const { x, y, w, h } = this.clamp(item.rect, this.layer.getBoundingClientRect());
    Object.assign(item.element.style, { left: `${x}px`, top: `${y}px`, width: `${w}px`, height: `${h}px` });
  }

  /**
   * The panel's own window size near the top right, on the first step down no other
   * window stands on. A window dragged a few pixels below a step still stands on it, one
   * dragged above the first step on that one. A window dragged further down holds a
   * step lower down: the new window may cover its bar, and the window chips bring it forward.
   */
  firstRect(item) {
    const size = this.panel(item.panelId).window;
    const bounds = this.layer.getBoundingClientRect();
    const taken = new Set([...this.items.values()]
      .filter((other) => other.place === 'float' && other.rect)
      .map((other) => Math.max(0, Math.floor((other.rect.y - 20) / 24))));
    let step = 0;
    while (taken.has(step)) step++;
    return {
      w: Math.min(size.width, bounds.width - 40),
      h: Math.min(size.height, bounds.height - 40),
      x: Math.max(20, bounds.width - size.width - 40 - step * 24),
      y: 20 + step * 24,
    };
  }

  /** Keep a window reachable: its bar stays inside the layer. */
  clamp(rect, bounds) {
    const w = Math.min(Math.max(rect.w, MIN_WIDTH), bounds.width);
    const h = Math.min(Math.max(rect.h, MIN_HEIGHT), bounds.height);
    return {
      w, h,
      x: Math.min(Math.max(rect.x, 0), Math.max(0, bounds.width - 80)),
      y: Math.min(Math.max(rect.y, 0), Math.max(0, bounds.height - 36)),
    };
  }

  fitWindows() {
    this.items.forEach((item) => { if (item.place === 'float') this.place(item); });
  }

  /**
   * Moving or resizing starts from where the window is shown and leaves it there.
   * On a narrow screen a window fills the screen: nothing to move, and its own rectangle stays.
   */
  dragging(handle, item, apply) {
    handle.addEventListener('pointerdown', (event) => {
      if (event.button !== 0 || event.target.closest('button') || this.narrow()) return;
      const bounds = this.layer.getBoundingClientRect();
      const start = { x: event.clientX, y: event.clientY, rect: this.clamp(item.rect, bounds) };
      this.track(handle, event, (e) => {
        item.rect = this.clamp(apply(start.rect, e.clientX - start.x, e.clientY - start.y), bounds);
        this.place(item);
      }, () => this.save());
    });
  }

  renderFloatingList() {
    const floating = [...this.items.values()].filter((item) => item.place === 'float');
    render(this.floatingList, floating.map((item) => html`
      <button type="button" class="floating-chip" data-key="${item.key}" title="Bring to front">
        ${icon(this.panel(item.panelId).icon, { size: 'sm' })} ${item.title}</button>`));
  }

  // --------------------------------------------------------- persistence

  save() {
    const layout = {
      dockWidth: this.dockWidth,
      active: this.items.get(this.active)?.panelId || null,
      items: [...this.items.values()].map(({ panelId, path, linked, place, rect }) => ({ panelId, path, linked, place, rect })),
    };
    try { localStorage.setItem(this.layoutKey, JSON.stringify(layout)); } catch { /* storage unavailable */ }
  }

  forgetLayout() {
    try { localStorage.removeItem(this.layoutKey); } catch { /* storage unavailable */ }
  }

  /**
   * Reopen what was open, in this browser. Panels that left the catalogue and
   * entries storage mangled are dropped. On a narrow screen the panels come
   * back behind the chat instead of over it.
   */
  restore() {
    let layout = null;
    try { layout = JSON.parse(localStorage.getItem(this.layoutKey) || 'null'); } catch { /* unreadable: start empty */ }
    if (!layout || !Array.isArray(layout.items)) return;
    if (Number.isFinite(layout.dockWidth)) this.dockWidth = layout.dockWidth;
    const valid = layout.items.filter((saved) => saved && typeof saved.panelId === 'string' && this.panel(saved.panelId));
    // windows without a rectangle of their own last: they take the steps the others leave free
    const rectless = (saved) => saved.place === 'float' && !storedRect(saved.rect);
    for (const saved of [...valid.filter((s) => !rectless(s)), ...valid.filter(rectless)]) {
      const path = typeof saved.path === 'string' ? saved.path : '';
      this.open(saved.panelId, {
        path,
        linked: Boolean(path) && saved.linked === true,
        place: saved.place === 'float' ? 'float' : 'dock',
        rect: storedRect(saved.rect),
        quietly: true,
      });
    }
    const active = [...this.items.values()].find((item) => item.panelId === layout.active && item.place === 'dock');
    if (active) this.focus(active.key, { quietly: true });
    if (this.narrow() && this.items.size) this.stepAside();
  }

  // ------------------------------------------------------------ protocol

  itemFor(source) {
    return [...this.items.values()].find((item) => item.frame && item.frame.contentWindow === source) || null;
  }

  post(item, type, payload = {}) {
    if (item.frame?.contentWindow) {
      item.frame.contentWindow.postMessage({ type, ...payload }, window.location.origin);
    }
  }

  broadcast(type, payload = {}) {
    this.items.forEach((item) => this.post(item, type, payload));
  }

  async onMessage(event) {
    if (event.origin !== window.location.origin) return;
    const item = this.itemFor(event.source);
    if (!item) return;
    const message = event.data || {};
    switch (message.type) {
      case 'pk:ready':
        this.post(item, 'pk:init', { theme: this.theme(), visible: this.shown(item), session: this.session() });
        break;
      case 'pk:dialog': {
        const value = await showDialog(document, message.dialog || {});
        this.post(item, 'pk:dialog-result', { id: message.id, value });
        break;
      }
      case 'pk:toast':
        showToast(document, String(message.message), message.kind);
        break;
      case 'pk:title':
        item.title = String(message.text || this.panel(item.panelId).title);
        if (item.element) this.showTitle(item);
        this.renderDock();
        this.renderFloatingList();
        break;
      case 'pk:navigate':
        item.path = String(message.path || '');
        item.linked = false;
        this.save();
        break;
      case 'pk:set-theme':
        this.onSetTheme(String(message.theme));
        break;
    }
  }
}
