---
name: panel-authoring
description: How to build a panel in ScarabHive or move an existing plugin panel onto the UI kit — the web_ui.panel block in schema.yaml, the template on kit/panel_base.html, panel-kit.js (api, html, dialogs, session, auto-refresh), the host protocol, the prohibitions and how to check. Load as soon as a panel (plugin or core), a template under templates/ or a script under static/ is built, migrated or repaired, and before a plugin gets a web_ui block.
---

# Building panels

Code, UI text and commit messages are English. A further plugin root
(`src/plugins_<name>/`) may keep its panels in another language (`<html lang>`).

This skill is the craft.
**`/ui/kit` shows the components themselves** — each in every state, rendered
from the same files a panel loads. Look there instead of copying markup from
old panels.

## What a panel is

An HTML page the shell shows in an **iframe**: docked as a tab next to the chat
or detached as a floating window. Each panel is open once; opening it again
brings it to the front. It is opened through the **catalog**
(`GET /api/ui/catalog`): the panel launcher, the command palette and the entry
points in the chat all read the same list. Opened directly in a browser tab the
same page works too — it then shows dialogs and toasts itself. The button
"Open in a new browser tab" on the tab and the window bar takes it there. A tab
of its own has no chat to follow: a panel with a `session` context gets the
chat's session pinned as `?session_id=` and so shows the same as in the frame.
If `<pk-session all>` is set to "all sessions", the tab starts there
(`?session_scope=all`) and stays pinned all the same: the choice "this session"
then names one. The way back: in its own tab the kit puts "Open in ScarabHive"
into the head — the shell opens with this panel on this page
(`/?panel=<path of the page>`; docked, or where it is already open), on "all
sessions" if it is set there. A pinned session opens in the chat as well; the
pin stays, because a panel can hang more on it than the session it follows
(`message_debugger` filters by it).

The iframe is a boundary for style, crashes and life cycle, **not** a security
boundary: panels run with `allow-same-origin` and the shell's cookie. Do not
embed foreign code as a panel.

## 1. The catalog entry (`schema.yaml`)

```yaml
web_ui:
  panel:
    endpoint: "/plugins/{{ name }}/"
    title: "Message Debugger"
    description: "Captured message turns and raw API requests and responses"
    icon: bug
    category: debug
    keywords: [requests, responses, llm]
    window: {width: 1000, height: 700}
    contexts:
      request: "/plugins/{{ name }}/?request_id={request_id}"
  endpoints:
    - path: "/"
      method: "GET"
      handler: "render_panel"
      response_type: "html"
```

| Field | Required | Meaning |
|---|---|---|
| `endpoint` | yes | URL of the panel page. `{{ name }}` is the plugin instance. |
| `title` | yes | Name in the launcher, the tab, the window bar. Two instances with the same title get the instance name appended. |
| `icon` | yes | Symbol id from `static/kit/icons.svg` (list: `/ui/kit`). |
| `category` | yes | `session`, `writer`, `context`, `agents`, `debug`, `system`, `admin` — fixed, not extensible. |
| `description` | | one sentence, shown in the launcher and searched. |
| `keywords` | | search words that are not in the title. |
| `window` | | size of the detached window. |
| `contexts` | | entry points from context: `session` (a session's info button, a click on the session title in the head) and `request` (the request id under an answer). The URL **must start with `endpoint`**; the shell inserts `{session_id}` or `{request_id}` URL-encoded. Only add one when the panel really reads the parameter. **A `session` context pins a panel that otherwise follows the chat** — it then no longer follows it until the user releases the pin. Whoever adds it therefore shows the pinned session visibly and releasably: with `<pk-session>` in the toolbar, or in a field that can be cleared (as `message_debugger` does). Pinning silently is the mistake. |

**Who sees the panel is not in the block:** the catalog shows it to the roles
that both layers of the route security in `config/security.yaml` let open the
`endpoint`: `auth.endpoint_security` (app-wide) and `auth.plugin_security`.
If a panel is for admins only, a rule for its routes belongs in the
configuration — then visibility and access match automatically.

`web_ui` knows exactly these two keys: `panel` and `endpoints`. Other keys
(`button`, `menu`, `requires_auth`, `roles`, …) are an error.
`src/agent_system/ui/catalog.py` (`plugin_panel`) is the parser — the
validator uses the same one; at run time a broken entry drops out of the
catalog with an error log.

The plugin has to be registered as a web plugin and hand out its schema: it
provides `get_web_router()` **and** `get_schema_data()` — the registry reads
`web_ui` from `get_schema_data()`; without the method the panel is missing
without a message. Schema-based plugins inherit both from
`SchemaBasedPluginWebInterface` and build the router with
`SchemaRouterGenerator` from `web_ui.endpoints`. The `endpoint` lives under
`/plugins/<instance>/` — the schema router serves it there, and only there may
the shell frame it.

## 2. The page

**Route:** render the template through `ui_templates()` — it finds the
plugin's templates first and the kit's after them:

```python
from agent_system.ui.resources import ui_templates

templates = ui_templates(Path(__file__).parent / "templates")

async def render_panel(self, request: Request):
    return templates.TemplateResponse(request, "panel.html", {"plugin": self.name})
```

**Template:**

```jinja
{% extends "kit/panel_base.html" %}
{% from "kit/macros.html" import icon %}
{% block title %}Message Debugger{% endblock %}
{% block toolbar %}<pk-refresh interval="5"></pk-refresh>{% endblock %}
{% block content %}
<div class="pk-tabs" role="tablist" data-pk-tabs>
  <button class="pk-tab" role="tab" aria-selected="true" aria-controls="tab-turns" data-tab="turns">Turns</button>
  <button class="pk-tab" role="tab" aria-selected="false" aria-controls="tab-raw" data-tab="raw">Raw requests</button>
</div>
<section id="tab-turns" role="tabpanel"><div id="turns"></div></section>
<section id="tab-raw" role="tabpanel" hidden></section>
{% endblock %}
{% block scripts %}<script type="module" src="/plugins/{{ plugin }}/static/panel.js"></script>{% endblock %}
```

Script and own CSS live in the plugin's `static/` folder; if the plugin serves
it through `get_static_assets()`, it is reachable under
`/plugins/<name>/static/`.

`panel_base.html` sets the theme from the cookie (no flash), loads `kit.css`
and `panel-kit.js`, and in the frame hides its own title — the tab or the
window bar carries it there; the toolbar stays. Further blocks: `head`, `lang`
(`de` for a German panel).

Icons in the template: `{{ icon('refresh-cw') }}`, `{{ icon('bug', size='sm', label='Bug') }}`.

## 3. `panel-kit.js`

```js
import { api, html, render, icon, session } from '/static/kit/panel-kit.js';
```

| Export | What for |
|---|---|
| `api(path, {method, json, body, headers, quiet, raw, latest})` | every server call. Cookie auth, JSON in and out, errors as `ApiError(status, detail)` **plus a toast** (unless `quiet: true`), aborted when the panel goes. Whoever reacts themselves (404 → empty state) takes `quiet` and catches. **`latest: 'name'`** aborts the previous call of the same name — an overtaken answer can never be drawn; the aborted one throws an `AbortError` without a toast, recognisable with `isAborted(error)`. That replaces every hand-made sequence counter. |
| `abandon(name)` | drop the running `latest` call of this name, for instance when the user clears what it would fill. |
| `pluginBase(import.meta.url)` | the plugin's address for a script from its `static/` folder (`/plugins/<name>`). |
| `errorText(error)` | the text the toast would show ("404: Not Found") — for a page that shows the error itself. |
| `update(el, content)` | `render`, but only when the markup changed (returns whether it drew). An unchanged answer leaves scroll, focus, selection and open `<details>` alone. Draw an element either with `update` or with `render`. |
| `notice(el, text, {kind})` | a message in place as a `.pk-callout` (`danger` by default, `warn`, `info`, `ok`); an empty text hides it. For "Refresh failed, shown is the state of …". |
| `emptyState(icon, title, text)`, `skeleton(lines)` | empty state and loading placeholder; in templates the macros `empty()` and `skeleton()` from `kit/macros.html`. |
| `localTime(ts, {relative, seconds})` | a stored timestamp in local time and the page's language. Values without a zone (that is how all our databases write them) count as UTC; `relative` gives "5 minutes ago". |
| `formQuery(form)`, `setQuery(params)` | filter form → query (empty fields drop out); write the query into the URL (`replaceState`) and tell the shell. |
| `withBusy(controls, fn)` | `fn` one at a time: the buttons are locked, a double click starts nothing. Afterwards **all** are free again — whoever has lock states of their own sets them again after `withBusy`. A button that a redraw replaces meanwhile is no longer the locked one: rows a tick redraws remember their lock themselves (a set of ids the markup reads, for instance). |
| `copyText(text)` | to the clipboard, with a toast. |
| `<pk-pager page pages>` | back/next with "Page 2 of 7", fires `page` (`detail.page`), hidden with one page. |
| `<table data-pk-select>`, `selectRow(table, id)` | rows with `data-id` and `tabindex="0"` are selectable by click or Enter (`aria-selected`, event `rowselect` with `detail.id`); controls in the row stay their own. `selectRow` marks again after a redraw. |
| `selectTab(list, name)` | select a tab (`data-tab`) without a click — from the URL, for instance. |
| `isDark()` | whether the page is dark right now (for canvas/SVG that read no tokens). |
| ``html`…` ``, `render(el, content)` | build markup: every inserted value is escaped, nested ``html`` `` and arrays stay markup. `trusted(str)` only for HTML that is already safe (sanitised by the server, say). `false`, `null` and `undefined` yield nothing — so `${cond && html`…`}` works; in an attribute therefore `aria-pressed="${String(on)}"`, or it says `""`. `render` gives the focus back: if it was inside the element, the new element with the same `data-key` gets it afterwards. |
| `escapeHtml(v)`, `jsonView(value)`, `icon(name, {size, label})` | helpers for the same. `jsonView` shows JSON for reading: all levels open, strings without quotes and with their line breaks, arrays as a list. The panel itself offers the raw JSON (copy, toggle). |
| `yamlCode(text)` | YAML text coloured (`.pk-yaml-*`), the text itself unchanged: for a `<pre class="pk-code">` or as a copy under a textarea. `\|`/`>` blocks keep their colour across blank lines, an unquoted `!…` shows as a tag. No Prism needed. |
| `alert(msg)`, `confirm(msg, {title, confirmLabel, danger})`, `prompt(msg, {title, value, placeholder, confirmLabel})`, `dialog({title, message, actions, input})` | dialogs — a promise with the result (`confirm` → `true/false`, `prompt` → text or `null`). In the shell above the whole application, otherwise in the panel. |
| `toast(msg, {kind})` | `info`, `ok`, `warn`, `error`. |
| `session.id`, `session.current`, `session.onChange(fn)` | the session open in the **chat**: its id or `null`, `current` with its title as well; `onChange` gets `{id, title}` or `null`. |
| `session.shown`, `session.scope`, `session.pinned`, `session.follow()` | the session the **panel** shows (see `<pk-session>` below): `shown` is the id it is about right now (`null` for all sessions and when none is open), `scope` is `'session'` or `'all'`, `pinned` the id from a context link, `follow()` releases the pin — **the page reloads**. A panel with unsaved input (`setDirty`) is not among those with `<pk-session>` today: the shell learns the released path before the browser can ask. |
| `<pk-session>`, `<pk-session all>` | into the toolbar. Says **which** session the panel shows and offers the way back to the chat; with `all` also the choice between this session and all. Fires `sessionscope` on the `document` as soon as what the panel should show changes — the chat switches, the user picks a scope, the pin falls. The panel then reads `session.shown`/`session.scope` and reloads; an own `session.onChange` next to it loads twice. If it follows the chat and there is nothing to choose, the element keeps out of the way (`hidden`). |
| `autoRefresh(fn, ms)` → `{start, stop, running}`; `<pk-refresh interval="s">` | reloading, paused while the panel is not visible. `<pk-refresh>` fires `refresh` on the `document`; `event.detail.auto` says whether the tick (true) or a click (false) asks — an expensive reload may skip the tick. With `auto` the tick runs from the start (otherwise only after a click). `interval` and `auto` are only the **page's default**: at the button the user picks the tick (5 s, 10 s, 30 s, 1 min, the default, or off), and that choice holds for this panel path from then on — stored in localStorage under `pk.refresh:<pathname>`. |
| `isVisible()` | whether the panel can be seen — not with the tab in the background, the window closed, the browser tab covered. `autoRefresh` asks it itself. |
| `initTabs(root)` | make `[data-pk-tabs]` usable (click, arrow keys, event `tabchange`). Runs by itself on load; calling it again after a re-render does no harm. Each tab is as wide as its text while all fit; when it gets tight they give way down to five characters (with "…"; a shorter one stays as wide as its text — in Chromium, elsewhere padded to the minimum width —, a `pk-icon` or `pk-dot` before the text gets its room on top, `--pk-tab-min` sets a minimum width of your own), after that the row scrolls sideways — the mouse wheel turns it, and the selected tab moves into view; a shortened tab shows its whole text on hover (an own `title` stays). A bar of page links (`nav` with `a.pk-tab`) behaves the same, its current page (`aria-current="page"`) is in view on load. In a flex row where the tabs should give way sideways, `.pk-tabs` needs `flex: 1 1 auto` (on its own the row does not shrink, so that a narrow column cannot squash it to height 0). |
| `wheelScrollsAcross(row)`, `keepInSight(row, item)` | for a sideways scrolling row of your own: the mouse wheel turns it (the browser takes only Shift or a trackpad for that); `keepInSight` moves an element fully into view, and while a font is still loading, once more as soon as it is there — not `scrollIntoView`, which drags every scrolling box around it along, up into the shell. |
| `initSidebars(root)` | give every `.pk-sidebar` the width it was last dragged to and remember the next one (localStorage `pk.sidebar:<pathname>`, with `#id` when the bar has one). A button with `data-pk-sidebar-toggle` and `aria-controls="<id of the bar>"` folds it away and back, like the chat's session bar; that is remembered too (`…:open`). Runs by itself on load; only a bar rendered later needs the call. |
| `placeMenu(menu, box, {matchWidth})` | position a menu of your own (`matchWidth`: as wide as the field it belongs to — suggestion lists). A `.pk-menu[popover]` with an `id` opened through `popovertarget` is placed by the kit itself. |
| `setTitle(text)`, `navigate(path)` | title in the tab, remember the path in the panel (opened on restore and counted as the panel's own path: unlike a context link the launcher does not reset it; without an argument the path the page shows right now). |
| `openSession(id)` | open a session in the chat (the shell switches there, on a narrow screen the panel steps aside). `false` where there is no chat — in a browser tab of its own; the panel then says itself what to do. |
| `setDirty(bool)` | report unsaved input: while `true`, the shell asks before closing, detaching or docking, or a link that reloads the panel, and the browser tab of its own before leaving. After saving or discarding, `setDirty(false)`. |
| `setTheme(theme)`, `currentTheme()`, `onThemeChange(fn)`, `THEMES` | theme choice and change (settings, kit page); `onThemeChange` also reports a change from the shell and, with `system`, a change of the system colours. |
| `announcePreferences(preferences)` | tell the shell that settings are stored on the account (after the PUT to `/auth/me/preferences`); the chat shows them right away. |

**The pattern of a loader** — the newest answer wins, what did not change
stays, an error keeps what is shown and says so:

```js
async function load() {
  try {
    const data = await api(`${base}/jobs?${formQuery(filters)}`, { latest: 'jobs', quiet: true });
    notice(stale, '');
    update(list, data.jobs.length ? rows(data.jobs) : emptyState('list-todo', 'No jobs'));
  } catch (error) {
    if (isAborted(error)) return;
    notice(stale, `Not refreshed (${errorText(error)}) — shown is the last state.`);
  }
}
```

Language: the kit speaks the language of `<html lang>` (`de` for a German
panel) — dialog buttons, refresh menu, pager, copy toast.

Example:

```js
async function load() {
  const turns = await api(`/plugins/message_debugger/turns?session_id=${encodeURIComponent(session.id)}`);
  render(document.getElementById('turns'), turns.length
    ? html`<table class="pk-table">${turns.map((t) => html`<tr><td>${t.agent}</td><td class="pk-num">${t.tokens}</td></tr>`)}</table>`
    : html`<div class="pk-empty">${icon('history')}<div class="pk-empty-title">Nothing captured yet</div></div>`);
}
document.addEventListener('refresh', load);
session.onChange(load);
load();
```

**Sortable tables** need no script of their own: after `render()` the kit sorts
every `<table class="pk-table" data-pk-sort="<name>">` with a `<thead>` that it
rendered (the enclosing one too, when only the `tbody` is new).

- A click anywhere in the header cell sorts, a `.pk-num` column largest first,
  any other A–Z; the second click reverses. On the column the table is sorted
  by right now (also through `aria-sort` in the markup), the first click
  already reverses. The kit puts the header content into a
  `<button class="pk-sort" data-key="sort:<name>:<header text>">` (keyboard);
  through the `data-key` it gets the focus back after a redraw — a selector of
  your own on `[data-key]` therefore hits it as well. Headers without text,
  with a control of their own (link, button, input, select, label, `summary`,
  anything with `tabindex`) or with `data-pk-nosort` stay silent;
  `data-pk-nosort` also where the values have no order (a row of badges).
- The choice holds across every redraw (auto-refresh) and is tied to the
  **header text**, not the column number; a column that is only sometimes
  there does not shift it. The name separates the tables of a page. It also
  survives a reload: the kit remembers it per panel path in localStorage under
  `pk.sort:<pathname>` (table name → header text and direction), like the
  refresh tick. Only a **plain name** is remembered (`[\w.:-]`, at most 40
  characters) and only the last twelve per panel — a name a panel builds from
  an input (the SQL panel names its table after the query) stays in the page
  and does not go to disk. A broken entry counts as none.
- Sorting goes by `data-sort-value`, else by the text. If all values of a
  column are numbers, as numbers; all ISO timestamps, as points in time
  (without a zone as UTC, as our databases write them); otherwise the whole
  column naturally as text ("B9" before "B10"). Empty or a single dash always
  comes last. **Every cell whose text is not its value** (`toLocaleString()`
  with a thousands separator, "1.2 s", "0.300¢", a badge, a shortened text)
  gets `data-sort-value="${raw ?? ''}"`.
- `aria-sort="descending"` in the markup is the order until the user chooses.
  If the panel sorts its rows by a column today, that mark belongs on the
  header and the JS sorting goes.
- Do not make sortable: key-value tables, tables with pairs of rows (a detail
  row under each entry) and lists loaded page by page on the server — the
  server sorts those. A list "the newest N" is sorted by the kit within
  those N.
- One header row, one `<tbody>` (only the first is sorted), no `colspan`. Rows
  keep their attributes when re-sorted; handlers through
  `data-id`/`data-index` keep working, code that reads the order in the DOM
  does not.

## 4. Look

- **Components** instead of your own: `pk-btn` (`--primary`, `--danger`,
  `--ghost`, `--icon`, `--sm`), `pk-input`/`pk-select`/`pk-textarea`/`pk-check`/`pk-switch`
  in `pk-field` and `pk-form`, `pk-tabs`, `pk-dialog`, `pk-menu`, `pk-table` in
  `pk-table-wrap`, `pk-card`, `pk-stats`/`pk-stat`, `pk-badge`, `pk-dot`,
  `pk-empty`, `pk-skeleton`, `pk-spinner`, `pk-progress` (`--ok/--warn/--danger`),
  `pk-code`, `pk-kv`, `pk-json`, `pk-callout` (`--danger/--warn/--info/--ok`),
  `pk-details` (on a `<details>`), `pk-prose` (reading text; `pk-prose--breaks`
  keeps stored line breaks), `pk-text--ok/--warn/--danger/--info` (a value in
  a tone colour), `pk-code--full` (code without a height cap),
  `pk-menu-item[aria-selected]` (the chosen suggestion); layout with
  `pk-stack`, `pk-row`, `pk-grow`, `pk-toolbar`, `pk-filters` (filter row
  above a list), `pk-split` (list on the left, detail on the right, the detail
  stays put), `pk-sidebar-layout` with `pk-sidebar` (sidebar next to the main
  area: width draggable at the corner, the kit remembers the width per panel,
  stacked when narrow — **every sidebar takes this one**, none of your own),
  a panel's pages as `<nav class="pk-tabs">` with `<a class="pk-tab"
  aria-current="page">`.
- **Colours, spacing, radii, type only through tokens**: `--surface-0…3`,
  `--text-primary/secondary/muted`, `--border-subtle/default/strong`,
  `--accent*`, `--ok/--warn/--danger/--info` (+ `-subtle`), `--space-1…8`,
  `--radius-sm/md/lg`, `--text-xs…xl`, `--font-ui`, `--font-mono`. Every value
  holds in light and dark. A hex value in a panel is an error — if a token is
  missing, it belongs in the kit.
- **Own CSS** only for what is really the panel's own (a timeline, a graph),
  in a file next to the script, built from tokens.
- `hidden` hides reliably, whatever `display` a class sets.

## 5. Prohibitions

- **No `alert`/`confirm`/`prompt`** — the kit replaces them; the native call
  fails loudly (console + toast). Test: `tests/ui/test_kit.py`.
- **No `innerHTML` with data** except through ``html`…` ``/`render`. Server
  data neither: agent names, tool arguments, titles come from models.
- **No `window.parent`, no `window.top`** — the shell speaks only through the
  protocol. Session, theme and dialogs come through the kit.
- **No raw `fetch`** in panel code.
- **No buttons, tabs, modals, toasts, refresh buttons of your own**, no emoji
  as icons.
- **No CDN** — libraries go to `static/vendor/`.

## 6. The protocol (for the curious and the shell)

Every message carries `type`. The kit speaks it; a panel only calls the
functions above.

| Direction | `type` | Content |
|---|---|---|
| panel → shell | `pk:ready` | on load |
| shell → panel | `pk:init` | `theme`, `visible`, `session` — only then does the panel count as "in the shell" |
| shell → panel | `pk:theme` / `pk:session` / `pk:visibility` | `theme` / `session` / `visible` |
| panel → shell | `pk:dialog` → `pk:dialog-result` | `id`, `dialog: {title, message, actions, input}` → `id`, `value` |
| panel → shell | `pk:toast` | `message`, `kind` |
| panel → shell | `pk:title` / `pk:navigate` / `pk:set-theme` | `text` / `path` / `theme` |
| panel → shell | `pk:dirty` | `dirty` (unsaved input; a new page in the panel counts as clean) |
| panel → shell | `pk:open-session` | `session_id` (the chat loads this session; `openSession`) |
| panel → shell | `pk:scope` | `scope` (`'session'`/`'all'`, from `<pk-session all>` when choosing and when loading with `?session_scope=all`; a tab the shell opens starts in the same scope) |
| panel → shell | `pk:preferences` | `preferences` (already stored; the shell fires `preferences:changed` for the chat) |

What a panel says before `pk:init` (title, path) the kit holds back and sends
after the handshake. Without a shell (a browser tab of its own, a foreign
frame) no `pk:init` comes, and the kit shows dialogs and toasts itself.

## 7. Checking

1. `python src/scripts/validate_plugin.py src/plugins/<name>` — the `web_ui`
   block goes through the catalog parser.
2. `pytest tests/ui -q` — among other things every icon named in the code
   exists, no kit user calls native dialogs (covered are templates on
   `panel_base.html` and scripts that import `panel-kit.js`), catalog rules,
   the shell in a real browser.
3. **Visual check in both themes**: open the panel in the shell, docked and
   detached, switch the theme with the button in the head. Open it directly
   under its URL in a browser tab — it has to work there too.
4. Panel logic with branches gets a browser test after the pattern of
   `tests/ui/test_shell_browser.py` (`run_app_test_page` with a stub app) or
   `tests/ui/test_kit_js.py` — and a mutation probe: change the served file in
   memory, the test has to go red.

## 8. Migrating an existing panel

One plugin at a time.

1. **Inventory:** which endpoints, which states (empty, error, loading), which
   actions (delete → `confirm` with `danger`), which parameters does it read
   (`session_id`, `request_id`)? Only what it really reads becomes `contexts`.
   If it reads `session_id`, `<pk-session>` belongs in the toolbar and
   `session.shown` in place of `pinned || session.id`.
2. Route on `ui_templates()`, template on `kit/panel_base.html`.
3. Delete the embedded `<style>`; move what remains onto tokens. Own `.btn`,
   `.modal`, `.tab`, `.toast`, `.badge` → kit classes.
4. Inline script into a module next to the template (`static/panel.js`),
   `fetch` → `api`, string HTML → ``html`…` ``, delete the `escapeHtml` copy,
   `toggleAutoRefresh` → `<pk-refresh>`, guessing the session through
   `window.parent` → `session`.
5. Emoji → `icon()`. Not in the sprite? Add it to `static/kit/icons.svg` from
   Lucide (same stroke width), do not leave it as an emoji.
6. Remove dead parts of the old panel (branches never reached, duplicate
   handlers, unused endpoints) instead of carrying them along.
7. Check as in § 7, then the visual check — the old and the new panel side by
   side once: if a function is missing, the migration is not done.
