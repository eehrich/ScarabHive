# Help in AmigaGuide format

As of 2026-09-28. The help panel shows the ScarabHive manual and the docs of
each plugin in an AmigaGuide viewer. The format is AmigaOS's hypertext help
format, **extended** by what Markdown can do -- in AmigaGuide's
own syntax. User decision 2026-09-28: the format grows, it does not switch
to Markdown (a `@markdown` mode was briefly built and has been removed again). Only what a Markdown file
*is* is rendered as Markdown: README, `docs/*.md`, `@embed x.md`.

## Where things are

| Part | File |
|---|---|
| Parser, layout, Markdown rewriting | `src/agent_system/ui/amigaguide.py` |
| Library (which guides exist), routes | `src/agent_system/ui/help.py` |
| The manual | `docs/guides/scarabhive.guide` (every `*.guide` there belongs to it; id = file name; found like `templates/` and `static/`) |
| The viewer (kit element `<pk-guide>`) | `static/kit/guide.js`, styles at the end of `static/kit/kit.css` |
| The panel | `templates/panels/help.html` (only `<pk-guide address search>`), core panel `help` in `ui/catalog.py` |
| Search by meaning (index of every node, all-MiniLM-L6-v2; kept in `data/help/search_index.json`) | `src/agent_system/ui/help_index.py`, tests `tests/ui/test_help_index.py` |
| The terminal viewer (`/help <topic>` in `agent-cli chat`) | `src/agent_system/cli_utils/help_viewer.py`, tests `tests/cli/test_cli_help_viewer.py` |
| Tests | `tests/ui/test_help.py`, `tests/ui/test_help_panel_browser.py` (+ `help_panel_tests.html`, `help_embed_probe.html`) |

## Where the guides come from

1. The manual from `docs/guides/`.
2. A plugin with its own guide: `<plugin-folder>/<folder-name>.guide`, id = folder name
   (= plugin type). Link from outside: `sub_agent_manager/main`.
3. A plugin without a guide but with a `README.md`: the README, rendered as Markdown.
   Its links to doc files open in the viewer as a file page (see Security).
4. `plugins`: generated, a table of all plugins from `plugins.plugin_dirs`.

A guide is re-read as soon as it, a file embedded with `@embed` (even
one that was still missing at read time) or -- for a README page -- the `plugin.toml`
changes (mtime + size, stamped before reading). No restart.

## Format

AmigaOS 3.1 subset: `@database @author @(c) @$VER: @node @endnode @title @toc
@prev @next @index @help @wordwrap @smartwrap @remark @embed`, inline `b ub i ui u uu
plain fg bg jleft jcenter jright pard line par tab code body amigaguide`, buttons
`@{"label" link node [line]}` (also `guide/node`, `x.guide/node`, `HELP:x.guide/node`).
`system`, `rx`, `rxs`, `beep`, `close`, `quit` are shown as dead buttons, never
executed. Escapes `\@`, `\\`.

Extensions (what Markdown has, in AmigaGuide syntax):

| Notation | Effect |
|---|---|
| `@{h1}Title`, `@{h2}`, `@{h3}` | Heading, ends with its line |
| `@{bullet [level]}`, `@{number [level]}` | List item; numbers count on their own -- across blank lines, code blocks and tables; a normal paragraph, a heading, a quote, a rule or a bullet of the same level starts over |
| `@{quote}` | Quote paragraph |
| `@{rule}` | Horizontal rule |
| `@{tt}`..`@{utt}`, `@{s}`..`@{us}` | Inline code, strikethrough (like `b`/`ub`) |
| `@{code language}` .. `@{body}` | Code block, syntax-highlighted (see below); `@{code}` without a language stays AmigaOS (lines as written) |
| `@{table}` .. `@{body}` | Table: one line per row, cells separated by `\|`, first row = header; `\|` is a pipe; a separator line (`---`, three dashes per cell) once, directly under the header; a line break, block or alignment in a cell is an error, not a loss |
| `@{"label" link https://…}` | Web link (only http/https/mailto), new tab |
| `@{"label" link docs/x.md}` | Doc file as a page (as on the Amiga: a link may name a file) |
| `@{image file.png "alt"}` | Image from the guide folder |
| `@embed file` | Markdown rendered, everything else as a code block in the language of the extension |

What the reader cannot place is shown below the page (`problems`) instead of
disappearing: a `@{` without a closing `}` (shown as text; the commands after it
keep working), a block command inside a code block, a dead link.

Syntax colours: on the first code block `<pk-guide>` loads the chat's Prism
(`static/vendor/prism/prism.js`, `data-manual`: it only colours what it is given) and colours
every block whose language it knows (python, yaml, json, toml, bash, sql, js/ts, css,
html/xml, diff, markdown, c/cpp, java) -- in guides as in rendered Markdown. A
block with a button, image or text attribute inside it (even one that is still
switched on from before, such as an open `@{b}`) stays uncoloured: Prism rewrites the markup,
the button would be gone. At most 100,000 characters per page are coloured; a block
that no longer fits stays uncoloured (512 KB of JSON cost 230 ms and 4.5 MB of
markup on every open). If loading Prism fails, the
next page asks again. The viewer removes the `tabindex` that Prism attaches to the block
again: otherwise only every coloured block would be a tab stop. Colours: kit tokens, the same
palette as in the chat.

Markdown files are rendered by `markdown_to_html` (the sanitized server renderer; the chat
draws its answers in the browser) with `line_breaks=False`: a line break is a
space, the list rescue for answers stays off, and a list directly under a paragraph line gets the blank line
that Python-Markdown needs (like GitHub).

## Security (deliberate decisions)

- Every file access goes through `inside(folder, relative)`: absolute, drive and
  UNC paths are rejected **before** the file system is asked (resolving `//host/x`
  is already an SMB access on Windows), then `resolve()` +
  `is_relative_to()`; NUL and overlong paths yield None instead of a 500.
- The asset route serves only image types (`png jpg gif webp svg`), only at the top of the folder
  or under `docs/`, with `Content-Security-Policy: default-src 'none'; …; sandbox` --
  an SVG opened directly executes nothing.
- File pages (`document()`) only for `.md`/`.markdown`/`.txt`, and only the `README.md`
  at the top of the folder or files under `docs/`. The review measured what every logged-in
  user read without this limit: agent prompts (`agents/prompts/*.md`),
  gitignored working files, docs of admin-only plugins. A plugin folder also contains
  configuration (n8n: a tracked `secrets.env`).
- And only what the docs **link** (`linked_document()`): a button of a node,
  a link of the README, a link of a file page reached that way. The third review found
  unlinked material in `docs/` that is not for every reader (an operations runbook with
  host IP and a `root@` command). The links come from the rendered page itself
  (`data-file`): what the viewer shows as a link opens, nothing else -- a path
  in a code block unlocks nothing.
- A file over 512 KB is named, not rendered: 2.8 MB of Markdown held the
  renderer's lock for four seconds (debate forum and formatter hook waited along).
- An unreadable file (an editor saving at exactly that moment, a lock) costs its
  own guide, not all routes.
- Remote images are never loaded (the alt text stands in for them) -- the same line as
  the chat sanitizer.
- Markdown HTML comes sanitized from the server; the rewriting of `a`/`img` rebuilds every
  attribute and escapes it.
- Routes: `/api/help/*` and `/ui/panels/help` fall under the default policy
  `require_auth`; no rules of their own are needed.

## Help buttons in the shell

- Top right in the header (`#helpButton`, `templates/index.html`): opens the help panel
  with the manual. If it already shows the manual (a page of it, a search), the
  button only brings it to the front -- page and retrace stay; on a plugin guide or the
  plugin list it starts at the manual again (`workspace.openManual()`).
- A plugin panel with a guide or README: a `?` at the end of the dock bar (for the tab
  in front, `#dockHelp`) and in the window bar of a detached panel. It opens
  the help panel at `?guide=<type>&node=main`; if it already shows this guide, it stays on
  the page the reader is currently reading (`workspace.openHelp()`). If it has to
  change place for that (dock ↔ window, the dock button brings a help window into the dock), the
  frame reloads there: the page stays, retrace starts empty. Not in every dock tab: they
  already have three buttons at a minimum width of 150 px.
- The source is the catalogue: `Panel.help` carries the guide id, `panel_guides()` in
  `ui/help.py` sets it via the plugin type that the loader resolves for the instance
  (`settings._resolve_server_inheritance`: the `type` of the entry, followed through other entries;
  without an entry the instance name; if inheritance fails, as with the loader
  the entry's own `type`) -- `skills_sam` finds the README of
  `sub_agent_manager`. A plugin folder the API cannot read costs only its
  own plugins (`plugin_docs`), not the catalogue.
- The dock bar button becomes invisible (`data-idle`) on a tab without a guide, not
  removed: its space stays, otherwise the shrinking tabs would change
  their width on every switch. From a window, the help opens as a window on top (docked it lay
  below). The help panel reports every page shown as its own path (`pk:navigate`);
  that is how the shell sees whether it is currently showing the manual.
- A panel in its own browser tab has no button (there is no shell there).

## `<pk-guide>` in a plugin panel

```html
<script type="module" src="/static/kit/guide.js"></script>
<pk-guide guide="my_plugin" node="config"></pk-guide>
```

Attributes: `guide`, `node`, `file` (only a file that the guide links) -- setting opens the page, even with the value
the attribute already has (the reader may have clicked on in the meantime); `search`
(search field); `address` (the element is the page: address and title belong to it -- only
once per page, as in the help panel).
Event `guidechange` after every page shown. The element scrolls itself (with
height) or the nearest scrolling box -- never anything outside its document.
`scrollIntoView` was the bug that scrolled the shell along.

## Checking

- `pytest tests/ui/test_help.py -k repository`: every guide in the repo without structure errors,
  dead links, unknown attributes. Run after every change to the manual.
- Parser, library and `guide.js` are mutation-tested (mutants only in memory,
  `guide.js` served through a route placed in front).

## Limits (deliberate)

- `MANIFEST.in` includes `docs/guides/*.guide`, like `templates/` and `static/`; images
  for the manual would need an entry of their own once there are any.
- Plugin guides and READMEs are seen by every logged-in user, even those of a plugin
  whose panel only admins open -- docs count as readable; whoever wants it otherwise attaches
  the guides to the plugin's panel roles.
- No agent tool for reading the help (user 2026-09-28: an agent tool is not needed).
- The search by meaning is English only (user 2026-10-06): all-MiniLM-L6-v2 found the right
  guide for 3 of 8 German questions; two multilingual models measured worse overall.
