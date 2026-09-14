// The command palette (Ctrl+K): panels, sessions, agents and actions in one search.
import { html, render, icon } from '/static/kit/panel-kit.js';
import { matches } from './launcher.js';

export class Palette {
  /**
   * @param {() => Array<{group, icon, label, hint, keywords, run, refine, instance}>} entries
   *   An entry with `refine` stands for several `instance` entries: without a
   *   query only it is listed, and choosing it types `refine` into the search.
   */
  constructor(entries) {
    this.entries = entries;
    this.dialog = document.getElementById('palette');
    this.input = document.getElementById('paletteInput');
    this.list = document.getElementById('paletteList');
    this.selected = 0;
    this.shown = [];
    this.input.addEventListener('input', () => { this.selected = 0; this.render(); });
    this.input.addEventListener('keydown', (event) => this.onKey(event));
    this.list.addEventListener('click', (event) => {
      const row = event.target.closest('[data-index]');
      if (row) this.run(Number(row.dataset.index));
    });
    this.dialog.addEventListener('click', (event) => {
      if (event.target === this.dialog) this.dialog.close();
    });
  }

  open() {
    this.input.value = '';
    this.selected = 0;
    this.render();
    this.dialog.showModal();
    this.input.focus();
  }

  onKey(event) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const step = event.key === 'ArrowDown' ? 1 : -1;
      this.selected = (this.selected + step + this.shown.length) % Math.max(this.shown.length, 1);
      this.render();
    } else if (event.key === 'Enter') {
      event.preventDefault();
      this.run(this.selected);
    }
  }

  run(index) {
    const entry = this.shown[index];
    if (!entry) return;
    if (entry.refine) {
      this.input.value = entry.refine;
      this.selected = 0;
      this.render();
      this.input.focus();
      return;
    }
    this.dialog.close();
    entry.run();
  }

  render() {
    const query = this.input.value.trim();
    this.shown = this.entries()
      .filter((e) => (query ? !e.refine : !e.instance))
      .filter((e) => !query || matches({ title: e.label, description: e.hint, keywords: e.keywords }, query))
      .slice(0, 60);
    if (!this.shown.length) {
      render(this.list, html`<div class="pk-empty">${icon('search')}<div>Nothing matches "${query}"</div></div>`);
      return;
    }
    let group = null;
    render(this.list, this.shown.map((entry, index) => {
      const heading = entry.group !== group ? html`<div class="palette-group">${entry.group}</div>` : '';
      group = entry.group;
      return html`${heading}<div class="palette-item" role="option" data-index="${index}" aria-selected="${String(index === this.selected)}">
        ${icon(entry.icon)}<span class="pk-truncate">${entry.label}</span><span class="palette-hint">${entry.hint || ''}</span></div>`;
    }));
    this.list.querySelector('[aria-selected="true"]')?.scrollIntoView({ block: 'nearest' });
  }
}
