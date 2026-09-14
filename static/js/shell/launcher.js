// The panel launcher: every catalogue panel by category, with search, pins and recents.
import { html, render, icon } from '/static/kit/panel-kit.js';

const PINS_KEY = 'scarabhive.pinned';
const RECENT_KEY = 'scarabhive.recent';

function readList(key) {
  let list = null;
  try { list = JSON.parse(localStorage.getItem(key) || '[]'); } catch { /* unreadable: an empty list */ }
  return Array.isArray(list) ? list : [];
}

function writeList(key, list) {
  try { localStorage.setItem(key, JSON.stringify(list)); } catch { /* storage unavailable */ }
}

/** Case-insensitive match of every word of the query against title, description and keywords. */
export function matches(panel, query) {
  const haystack = [panel.title, panel.description, ...(panel.keywords || [])].join(' ').toLowerCase();
  return query.toLowerCase().split(/\s+/).filter(Boolean).every((word) => haystack.includes(word));
}

export class Launcher {
  constructor({ catalog, open }) {
    this.catalog = catalog;
    this.openPanel = open;
    this.popover = document.getElementById('launcher');
    this.search = document.getElementById('launcherSearch');
    this.list = document.getElementById('launcherList');
    this.search.addEventListener('input', () => this.render());
    // beforetoggle runs before the first paint of the open popover, toggle only after it
    this.popover.addEventListener('beforetoggle', (event) => {
      if (event.newState !== 'open') return;
      this.search.value = '';
      this.render();
    });
    this.popover.addEventListener('toggle', (event) => {
      if (event.newState === 'open') this.search.focus();
    });
    this.list.addEventListener('click', (event) => {
      const pin = event.target.closest('[data-pin]');
      if (pin) {
        this.togglePin(pin.dataset.pin);
        return;
      }
      const item = event.target.closest('[data-panel]');
      if (item) {
        this.popover.hidePopover();
        this.open(item.dataset.panel);
      }
    });
    this.list.addEventListener('keydown', (event) => {
      if ((event.key !== 'Enter' && event.key !== ' ') || !event.target.matches('[data-panel]')) return;
      event.preventDefault();
      event.target.click();
    });
  }

  open(panelId, options) {
    writeList(RECENT_KEY, [panelId, ...readList(RECENT_KEY).filter((id) => id !== panelId)].slice(0, 5));
    this.openPanel(panelId, options);
  }

  togglePin(panelId) {
    const pins = readList(PINS_KEY);
    writeList(PINS_KEY, pins.includes(panelId) ? pins.filter((id) => id !== panelId) : [...pins, panelId]);
    this.render();
  }

  item(panel, pins) {
    const pinned = pins.includes(panel.id);
    return html`
      <div class="launcher-item" role="button" tabindex="0" data-panel="${panel.id}">
        <span class="launcher-icon">${icon(panel.icon)}</span>
        <span class="launcher-text">
          <div class="launcher-title">${panel.title}</div>
          <div class="launcher-desc">${panel.description}</div>
        </span>
        <button type="button" class="pk-btn pk-btn--ghost pk-btn--icon pk-btn--sm launcher-pin" data-pin="${panel.id}"
                aria-pressed="${String(pinned)}" title="${pinned ? 'Unpin' : 'Pin to the top'}">${icon('pin', { size: 'sm' })}</button>
      </div>`;
  }

  /** Instances of one plugin (same group) fold into one row; the rest stay single rows. */
  folded(items, pins) {
    const groups = new Map();
    items.filter((p) => p.group).forEach((p) => groups.set(p.group, [...(groups.get(p.group) || []), p]));
    return items.map((p) => {
      if (!p.group) return this.item(p, pins);
      const members = groups.get(p.group);
      if (members[0] !== p) return '';
      return html`
        <details class="launcher-fold">
          <summary class="launcher-item">
            <span class="launcher-icon">${icon(p.icon)}</span>
            <span class="launcher-text">
              <div class="launcher-title">${p.group} <span class="pk-badge">${members.length}</span></div>
              <div class="launcher-desc">${p.description}</div>
            </span>
            ${icon('chevron-right', { size: 'sm' })}
          </summary>
          ${members.map((member) => this.item(member, pins))}
        </details>`;
    });
  }

  render() {
    const { categories, panels } = this.catalog();
    const query = this.search.value.trim();
    const pins = readList(PINS_KEY);
    const byId = new Map(panels.map((p) => [p.id, p]));
    const sections = [];
    if (query) {
      sections.push(['Results', panels.filter((p) => matches(p, query)), false]);
    } else {
      sections.push(['Pinned', pins.map((id) => byId.get(id)).filter(Boolean), false]);
      sections.push(['Recent', readList(RECENT_KEY).map((id) => byId.get(id)).filter((p) => p && !pins.includes(p.id)), false]);
      categories.forEach((c) => sections.push([c.label, panels.filter((p) => p.category === c.id), true]));
    }
    const shown = sections.filter(([, items]) => items.length);
    if (this.catalog().failed) {
      render(this.list, html`<div class="pk-empty">${icon('circle-alert')}<div>The panels could not be loaded. Reload the page to try again.</div></div>`);
      return;
    }
    if (!shown.length) {
      render(this.list, html`<div class="pk-empty">${icon('search')}<div>No panel matches "${query}"</div></div>`);
      return;
    }
    render(this.list, shown.map(([label, items, fold]) => html`
      <div class="launcher-group">${label}</div>${fold ? this.folded(items, pins) : items.map((p) => this.item(p, pins))}`));
  }
}
