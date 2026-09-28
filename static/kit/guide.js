// <pk-guide>: the kit's AmigaGuide viewer -- a node of a guide with the viewer's buttons, links, Retrace and search.
//
//   <pk-guide></pk-guide>                                   the ScarabHive manual, its main node
//   <pk-guide guide="my_plugin" node="config"></pk-guide>   a plugin's own guide, inside the plugin's panel
//   <pk-guide guide="my_plugin" file="docs/design.md">       a documentation file the guide links to, Markdown rendered
//   <pk-guide address search></pk-guide>                    the Help panel: the page IS the viewer
//
// The server lays every node out (GET /api/help/node, see agent_system/ui/help.py); this element only draws it.
// `address`: the node on screen is the page's address (?guide=&node=&line=, or ?q= for a search) and its title, and
// the address opens it -- one viewer per page. `search`: a search field over every guide. Setting `guide`, `node` or
// `file` opens that page, also when set to the value it has (the reader may have moved on). Event `guidechange`
// (bubbles) after each page shown: detail is the node, or {query} for a search.
// Give the element a height (or let a flex column give it one) and it scrolls itself; without one, the nearest
// scrolling box around it does -- never anything outside this document, which in the shell would be the shell.
import { api, html, trusted, render, icon, emptyState, skeleton, notice, errorText, isAborted, setQuery, setTitle } from './panel-kit.js';

const MANUAL = { guide: 'scarabhive', node: 'main' };
// Where the buttons lead before a node has said so (a search as the first page): the manual.
const MANUAL_NAV = { contents: MANUAL, index: { guide: 'scarabhive', node: 'index' }, help: { guide: 'scarabhive', node: 'help' } };
// The AmigaGuide window's buttons, in its order, each with an icon from the kit sprite.
const BUTTONS = [['contents', 'Contents', 'book-open'], ['index', 'Index', 'scroll-text'], ['help', 'Help', 'circle-help'],
  ['retrace', 'Retrace', 'history'], ['prev', 'Browse', 'chevron-left'], ['next', 'Browse', 'chevron-right']];
let instances = 0;

// What a page is: a node of a guide, or a documentation file next to it (a link in a README opens one).
function place(to) {
  return to.file ? { guide: to.guide, file: to.file } : { guide: to.guide, node: to.node };
}

function href(to) {
  const params = place(to);
  if (to.line) params.line = to.line;
  return `?${new URLSearchParams(params)}`;
}

// A button to a page: a node, or (to.file) a documentation file next to the guide.
const nodeLink = (to, label, cls = '') => html`<a class="pk-guide-button ${cls}" href="${href(to)}" data-guide="${to.guide}" ${to.file ? html`data-file="${to.file}"` : html`data-node="${to.node}"`} data-line="${to.line || 0}">${label}</a>`;

function span(s) {
  const cls = s.style.map((c) => `pk-guide-${c}`).join(' ');
  if (s.link) return nodeLink(s.link, s.text, cls);
  if (s.file) return nodeLink(s.file, s.text, cls);
  // the server lets only http, https and mailto through as a web link
  if (s.url) {
    return html`<a class="pk-guide-button pk-guide-web ${cls}" href="${s.url}" target="_blank" rel="noopener noreferrer" title="${s.url}">${s.text}${icon('external-link', { size: 'sm' })}</a>`;
  }
  if (s.image) return html`<img class="pk-guide-image" src="${s.image}" alt="${s.text}" loading="lazy">`;
  if (s.broken) return html`<span class="pk-guide-button pk-guide-broken ${cls}" title="${`Leads nowhere: ${s.broken}`}">${s.text}</span>`;
  if (s.inert) return html`<span class="pk-guide-button pk-guide-inert ${cls}" title="${`“${s.inert}” runs something on an Amiga; not here`}">${s.text}</span>`;
  return cls ? html`<span class="${cls}">${s.text}</span>` : s.text;
}

const spans = (list) => list.map(span);

// A line of the node, by its kind (see layout() in agent_system/ui/amigaguide.py). No whitespace between the tags:
// every character inside a line is shown as written. A Markdown file comes rendered and sanitised by the server
// (agent_system/utils/markdown_render.py), its links already made local.
function line(l) {
  if (l.html !== undefined) return html`<div class="pk-guide-md pk-prose" data-n="${l.n}">${trusted(l.html)}</div>`;
  switch (l.kind) {
    case 'rule':
      return html`<hr class="pk-guide-rule" data-n="${l.n}">`;
    case 'code':
      return html`<pre class="pk-guide-codeblock" data-n="${l.n}" data-language="${l.language}"><code class="language-${l.language}">${l.rows.map((row, i) => html`${i ? '\n' : ''}${spans(row)}`)}</code></pre>`;
    case 'table':
      return html`<div class="pk-guide-table-wrap" data-n="${l.n}"><table class="pk-guide-table">
        <thead><tr>${l.rows[0].map((cell) => html`<th>${spans(cell)}</th>`)}</tr></thead>
        <tbody>${l.rows.slice(1).map((row) => html`<tr>${row.map((cell) => html`<td>${spans(cell)}</td>`)}</tr>`)}</tbody>
      </table></div>`;
    case 'h1': case 'h2': case 'h3':
      return html`<div class="pk-guide-line pk-guide-${l.kind} pk-guide-${l.align}" role="heading" aria-level="${l.level}" data-n="${l.n}">${spans(l.spans)}</div>`;
    case 'bullet': case 'number': case 'quote':
      return html`<div class="pk-guide-line pk-guide-${l.kind} pk-guide-level-${l.level}" data-n="${l.n}">${l.marker && html`<span class="pk-guide-marker" aria-hidden="true">${l.marker}</span>`}${spans(l.spans)}</div>`;
    default:
      return html`<div class="pk-guide-line pk-guide-${l.align}${l.wrap ? '' : ' pk-guide-pre'}" data-n="${l.n}">${spans(l.spans)}</div>`;
  }
}

const hit = (h) => html`<div class="pk-guide-hit">${nodeLink(h, ` ${h.title} `)} <span class="pk-guide-fg-shadow">${h.database}</span><div class="pk-guide-snippet">${h.snippet}</div></div>`;

/** The box that scrolls `element` into view: the nearest one around it that scrolls, else this document. */
function scrollerOf(element) {
  for (let node = element.parentElement; node; node = node.parentElement) {
    const { overflowY } = getComputedStyle(node);
    if ((overflowY === 'auto' || overflowY === 'scroll') && node.scrollHeight > node.clientHeight) return node;
  }
  return document.scrollingElement;
}

class GuideViewer extends HTMLElement {
  static observedAttributes = ['guide', 'node', 'file'];

  connectedCallback() {
    if (this.page) return;  // moved in the DOM: keep what it shows
    this.key = `pk-guide-${++instances}`;
    this.trail = [];   // what Retrace returns to: {guide, node} or {query}
    this.shown = null; // on screen: a laid-out node, or {query}
    this.nav = MANUAL_NAV;  // where the buttons lead from the last node shown
    render(this, html`<div class="pk-guide-bar" role="toolbar" aria-label="Guide">
        ${BUTTONS.map(([name, label, glyph]) => html`<button type="button" class="pk-btn pk-btn--sm" data-nav="${name}" aria-label="${name === 'prev' ? 'Browse back' : name === 'next' ? 'Browse forward' : label}" disabled>${name === 'next' ? html`${label}${icon(glyph)}` : html`${icon(glyph)}${label}`}</button>`)}
        ${this.hasAttribute('search') && html`<form class="pk-guide-search" role="search"><input class="pk-input" type="search" name="q" placeholder="Search all guides" aria-label="Search all guides"></form>`}
      </div>
      <div class="pk-guide-stale" hidden></div>
      <div class="pk-guide-scroll">
        <article class="pk-guide-page" tabindex="-1">${skeleton(6)}</article>
        <div class="pk-guide-problems pk-prose--breaks" hidden></div>
      </div>`);
    this.page = this.querySelector('.pk-guide-page');
    this.stale = this.querySelector('.pk-guide-stale');
    this.problems = this.querySelector('.pk-guide-problems');
    this.buttons = Object.fromEntries([...this.querySelectorAll('[data-nav]')].map((b) => [b.dataset.nav, b]));
    for (const [name, button] of Object.entries(this.buttons)) {
      button.addEventListener('click', () => (name === 'retrace' ? this.retrace() : this.nav[name] && this.open(this.nav[name])));
    }
    this.page.addEventListener('click', (event) => {
      const link = event.target.closest('a[data-guide]');
      // a modified click keeps the browser's meaning: a new tab, a new window
      if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      const { guide, node, file, line: at } = link.dataset;
      this.open({ guide, node, file, line: Number(at) || 0 });
    });
    this.querySelector('.pk-guide-search')?.addEventListener('submit', (event) => {
      event.preventDefault();
      const query = new FormData(event.target).get('q').trim();
      if (query) this.search(query);
    });
    const params = new URLSearchParams(this.hasAttribute('address') ? location.search : '');
    if (params.get('q')) this.search(params.get('q'));
    else this.open(this.start(params));
  }

  // Also for the value the attribute already has: the reader may have moved on since, and the page asks again.
  attributeChangedCallback(name) {
    if (!this.page) return;
    const to = this.start(new URLSearchParams());
    if (name !== 'file') delete to.file;  // the node asked for, not a file set before it
    this.open(to);
  }

  start(params) {
    return {
      guide: params.get('guide') || this.getAttribute('guide') || MANUAL.guide,
      node: params.get('node') || this.getAttribute('node') || MANUAL.node,
      file: params.get('file') || this.getAttribute('file') || undefined,
      line: Number(params.get('line')) || 0,
    };
  }

  where() {
    if (!this.shown) return null;
    return this.shown.query !== undefined ? { query: this.shown.query } : place(this.shown);
  }

  async open(to, { retrace = false } = {}) {
    try {
      const node = await api(`/api/help/node?${new URLSearchParams(place(to))}`, { latest: this.key, quiet: true });
      const focused = this.contains(document.activeElement);  // before the link that has it is drawn away
      this.step(retrace);
      this.shown = node;
      this.nav = node.nav;
      notice(this.stale, '');
      render(this.page, node.lines.map(line));
      this.anchor();
      const mistakes = node.problems.length ? `Mistakes in this node of ${node.guide}:\n${node.problems.join('\n')}` : '';
      notice(this.problems, mistakes, { kind: 'warn' });
      this.showing({ browse: true, focused }, node.title === node.database ? node.title : `${node.title} · ${node.database}`,
        { ...place(node), ...(to.line ? { line: to.line } : {}) }, node);
      this.scrollToLine(to.line);
    } catch (error) {
      if (!isAborted(error)) this.failed(`${to.guide}/${to.file || to.node}`, error);
    }
  }

  async search(query, { retrace = false } = {}) {
    try {
      const result = await api(`/api/help/search?${new URLSearchParams({ q: query })}`, { latest: this.key, quiet: true });
      const focused = this.contains(document.activeElement);
      this.step(retrace);
      this.shown = { query };
      notice(this.stale, '');
      notice(this.problems, '');
      const more = result.total > result.hits.length ? `, the first ${result.hits.length} shown` : '';
      render(this.page, result.hits.length
        ? html`<p class="pk-guide-line pk-guide-fg-shadow">${result.total} ${result.total === 1 ? 'node contains' : 'nodes contain'} “${query}”${more}.</p>${result.hits.map(hit)}`
        : emptyState('search', 'Nothing found', `No guide contains “${query}”.`));
      this.anchor();
      this.showing({ browse: false, focused }, `Search: ${query}`, { q: query }, { query });
      this.scrollToLine(0);
    } catch (error) {
      if (!isAborted(error)) this.failed(`the search for “${query}”`, error);
    }
  }

  /** The trail after a page arrived: a retrace takes its station off, anything else puts the page left behind on. */
  step(retrace) {
    if (retrace) this.trail.pop();
    else if (this.shown) this.trail.push(this.where());
  }

  // The station stays on the trail until its page arrives: a failed or overtaken load loses nothing.
  retrace() {
    const back = this.trail[this.trail.length - 1];
    if (!back) return;
    if (back.query !== undefined) this.search(back.query, { retrace: true });
    else this.open(back, { retrace: true });
  }

  // A button's address (?guide=...) is relative: right in the Help panel, which reads it. In an embedded viewer the
  // page around it would get it, and ignore it -- a new tab opened from there opens the Help panel instead.
  anchor() {
    if (this.hasAttribute('address')) return;
    for (const link of this.page.querySelectorAll('a[data-guide][href^="?"]')) {
      link.setAttribute('href', `/ui/panels/help${link.getAttribute('href')}`);
    }
  }

  /** Buttons, the focus, and -- for the page's own viewer -- its title and address; then tell whoever listens
   *  (last: a listener that moves the focus has the last word). */
  showing({ browse, focused }, title, query, detail) {
    for (const [name, button] of Object.entries(this.buttons)) {
      button.disabled = name === 'retrace' ? !this.trail.length
        : !this.nav[name] || (!browse && (name === 'prev' || name === 'next'));
    }
    if (this.hasAttribute('address')) {
      setTitle(title);
      setQuery(query);
    }
    this.keepFocus(focused);
    this.dispatchEvent(new CustomEvent('guidechange', { bubbles: true, detail }));
  }

  // The link that had the focus is gone, a Browse button at the end is disabled: the page takes it, so a keyboard
  // or a screen reader goes on from here and not from the top of the document.
  keepFocus(focused) {
    const now = document.activeElement;
    if (focused && (!this.contains(now) || now.disabled)) this.page.focus({ preventScroll: true });
  }

  failed(what, error) {
    notice(this.stale, `Could not open ${what} (${errorText(error)}).`);
    if (this.shown) return;  // the page on screen stays; the notice says why nothing changed
    render(this.page, html`${emptyState('circle-help', 'Nothing to show', 'This page of the help does not exist.')}
      <p class="pk-guide-line pk-guide-center">${nodeLink(MANUAL, ' ScarabHive manual ')}</p>`);
    this.anchor();
  }

  // Scrolls only boxes inside this document: scrollIntoView would scroll the shell around the frame as well.
  scrollToLine(number) {
    this.page.parentElement.scrollLeft = 0;  // a new page starts at its left edge, too
    const target = number ? [...this.page.querySelectorAll('[data-n]')].filter((el) => Number(el.dataset.n) <= number).pop() : null;
    const box = scrollerOf(this.page);
    const top = box === document.scrollingElement ? 0 : box.getBoundingClientRect().top;
    const offset = (target || this.page).getBoundingClientRect().top - top;
    if (target || offset < 0) box.scrollTop += offset;  // a new page starts at its top; one below the fold stays put
  }
}

customElements.define('pk-guide', GuideViewer);
