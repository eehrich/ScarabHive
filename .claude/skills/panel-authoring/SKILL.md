---
name: panel-authoring
description: Wie man in ScarabHive ein Panel baut oder ein bestehendes Plugin-Panel auf das UI-Kit umstellt — der web_ui.panel-Block in schema.yaml, das Template auf kit/panel_base.html, panel-kit.js (api, html, Dialoge, Session, Auto-Refresh), das Host-Protokoll, die Verbote und wie man prüft. Laden, sobald ein Panel (Plugin oder Kern), ein Template unter templates/ oder ein Skript unter static/ gebaut, migriert oder repariert wird, und bevor ein Plugin einen web_ui-Block bekommt.
---

# Panels bauen

Deutsch mit dem Nutzer; Code, Oberflächentexte und Commit-Messages Englisch.
Ausnahme: die Writer-Panels (`src/plugins_writer/`) sind bewusst deutsch.

Hintergrund und Entscheidungen: `docs/webui_konzept.md`. Diese Skill ist das
Handwerk. **Die Komponenten selbst zeigt `/ui/kit`** — jede in jedem Zustand,
gerendert aus denselben Dateien, die ein Panel lädt. Dort nachsehen statt
Markup aus alten Panels zu kopieren.

## Was ein Panel ist

Eine HTML-Seite, die die Shell im **iframe** zeigt: als Tab neben dem Chat
angedockt oder herausgelöst als schwebendes Fenster. Jedes Panel ist einmal
offen; ein zweites Öffnen holt es nach vorn. Geöffnet wird es über den
**Katalog** (`GET /api/ui/catalog`): Panel-Starter, Befehlspalette und die
Einstiege im Chat lesen alle dieselbe Liste. Direkt im Browser-Tab geöffnet
funktioniert dieselbe Seite auch — dann zeigt sie Dialoge und Toasts selbst.

Das iframe ist Stil-, Absturz- und Lebenszyklus-Grenze, **keine**
Sicherheitsgrenze: Panels laufen mit `allow-same-origin` und dem Cookie der
Shell. Fremden Code nicht als Panel einbinden.

## 1. Der Katalog-Eintrag (`schema.yaml`)

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

| Feld | Pflicht | Bedeutung |
|---|---|---|
| `endpoint` | ja | URL der Panel-Seite. `{{ name }}` ist die Plugin-Instanz. |
| `title` | ja | Name im Starter, im Tab, in der Fensterleiste. Zwei Instanzen mit gleichem Titel bekommen den Instanznamen angehängt. |
| `icon` | ja | Symbol-ID aus `static/kit/icons.svg` (Liste: `/ui/kit`). |
| `category` | ja | `session`, `writer`, `context`, `agents`, `debug`, `system`, `admin` — fest, nicht erweiterbar. |
| `description` | | ein Satz, erscheint im Starter und wird durchsucht. |
| `keywords` | | Suchwörter, die nicht im Titel stehen. |
| `window` | | Größe des herausgelösten Fensters. |
| `contexts` | | Einstiege aus dem Zusammenhang: `session` (Info-Knopf einer Session, Klick auf den Session-Titel im Kopf) und `request` (Request-ID unter einer Antwort). Die URL **muss mit `endpoint` beginnen**; `{session_id}` bzw. `{request_id}` setzt die Shell URL-kodiert ein. Nur eintragen, wenn das Panel den Parameter wirklich liest. |

**Wer das Panel sieht, steht nicht im Block:** der Katalog zeigt es den Rollen,
die beide Schichten der Route-Security in `config/config.yaml` den `endpoint`
öffnen lassen: `auth.endpoint_security` (app-weit) und `auth.plugin_security`.
Soll ein Panel nur für
Admins sein, gehört eine Regel für seine Routen in die Konfiguration — dann
passen Sichtbarkeit und Zugriff automatisch zusammen.

`web_ui` kennt genau diese zwei Schlüssel: `panel` und `endpoints`. Andere
Schlüssel (`button`, `menu`, `requires_auth`, `roles`, …) sind ein Fehler.
`src/agent_system/ui/catalog.py` (`plugin_panel`) ist der Parser — der
Validator benutzt denselben, zur Laufzeit fällt ein kaputter Eintrag mit
Fehlerlog aus dem Katalog.

Das Plugin muss als Web-Plugin registriert sein und sein Schema herausgeben:
es liefert `get_web_router()` **und** `get_schema_data()` — die Registry liest
`web_ui` aus `get_schema_data()`, fehlt die Methode, fehlt das Panel ohne
Meldung. Schema-basierte Plugins erben beides von
`SchemaBasedPluginWebInterface` und erzeugen den Router mit `SchemaRouterGenerator`
aus `web_ui.endpoints`. Der `endpoint` liegt unter `/plugins/<instanz>/` — dort
bedient ihn der Schema-Router, und nur dort darf die Shell ihn einrahmen.

## 2. Die Seite

**Route:** das Template über `ui_templates()` rendern — es findet die
Templates des Plugins zuerst und das Kit danach:

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

Skript und eigenes CSS liegen im `static/`-Ordner des Plugins; liefert das
Plugin ihn über `get_static_assets()`, ist er unter `/plugins/<name>/static/`
erreichbar.

`panel_base.html` setzt das Theme aus dem Cookie (kein Aufblitzen), lädt
`kit.css` und `panel-kit.js`, und im Frame blendet es den eigenen Titel aus —
den trägt dort der Tab oder die Fensterleiste; die Toolbar bleibt. Weitere
Blöcke: `head`, `header` (ganz ersetzen), `lang` (Writer: `de`).

Icons im Template: `{{ icon('refresh-cw') }}`, `{{ icon('bug', size='sm', label='Bug') }}`.

## 3. `panel-kit.js`

```js
import { api, html, render, icon, session } from '/static/kit/panel-kit.js';
```

| Export | Wofür |
|---|---|
| `api(path, {method, json, body, headers, quiet, raw})` | jeder Server-Aufruf. Cookie-Auth, JSON rein und raus, Fehler als `ApiError(status, detail)` **plus Toast** (außer `quiet: true`), abgebrochen, wenn das Panel geht. Wer selbst reagiert (404 → leerer Zustand), nimmt `quiet` und fängt. |
| ``html`…` ``, `render(el, content)` | Markup bauen: jeder eingesetzte Wert wird escapet, verschachteltes ``html`` `` und Arrays bleiben Markup. `trusted(str)` nur für schon sicheres HTML (z. B. vom Server sanitisiert). `false`, `null` und `undefined` ergeben nichts — damit `${cond && html`…`}` geht; in einem Attribut darum `aria-pressed="${String(on)}"`, sonst steht dort `""`. |
| `escapeHtml(v)`, `jsonView(value)`, `icon(name, {size, label})` | Hilfen für dasselbe. `jsonView` zeigt JSON zum Lesen: alle Ebenen offen, Strings ohne Anführungszeichen und mit ihren Zeilenumbrüchen, Arrays als Liste. Das Roh-JSON bietet das Panel selbst an (Kopieren, Umschalter). |
| `alert(msg)`, `confirm(msg, {title, confirmLabel, danger})`, `prompt(msg, {title, value, placeholder, confirmLabel})`, `dialog({title, message, actions, input})` | Dialoge — Promise mit dem Ergebnis (`confirm` → `true/false`, `prompt` → Text oder `null`). In der Shell über der ganzen Anwendung, sonst im Panel. |
| `toast(msg, {kind})` | `info`, `ok`, `warn`, `error`. |
| `session.current`, `session.id`, `session.onChange(fn)` | die Session, die im Chat offen ist (`{id, title}` oder `null`). |
| `autoRefresh(fn, ms)` → `{start, stop, running}`; `<pk-refresh interval="s">` | Nachladen, pausiert, solange das Panel nicht sichtbar ist. `<pk-refresh>` feuert `refresh` am `document`; `event.detail.auto` sagt, ob der Takt (true) oder ein Klick (false) fragt — teures Nachladen darf den Takt auslassen. Mit `auto` läuft der Takt von Anfang an (sonst erst nach Klick). |
| `onVisibilityChange(fn)`, `isVisible()` | Tab im Hintergrund, Fenster zu, Browser-Tab verdeckt. |
| `initTabs(root)` | `[data-pk-tabs]` bedienbar machen (Klick, Pfeiltasten, Event `tabchange`). Läuft beim Laden von selbst; nach dem Nachrendern erneut aufrufen ist unschädlich. |
| `placeMenu(menu, box)` | eigenes Menü positionieren. Ein `.pk-menu[popover]` mit `id`, das per `popovertarget` geöffnet wird, platziert das Kit selbst. |
| `setTitle(text)`, `setBadge(count)`, `navigate(path)`, `openPanel(id, path)` | Titel im Tab, Zähler am Tab, Pfad im Panel merken (wird beim Wiederherstellen geöffnet und gilt als eigener Pfad: anders als ein Kontext-Link setzt der Starter ihn nicht zurück), anderes Panel öffnen. |
| `setTheme(theme)`, `currentTheme()`, `onThemeChange(fn)`, `THEMES` | Theme-Wahl und -Wechsel (Einstellungen, Kit-Seite); `onThemeChange` meldet auch einen Wechsel aus der Shell. |

Beispiel:

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

## 4. Aussehen

- **Komponenten** statt eigener: `pk-btn` (`--primary`, `--danger`, `--ghost`,
  `--icon`, `--sm`), `pk-input`/`pk-select`/`pk-textarea`/`pk-check`/`pk-switch`
  in `pk-field` und `pk-form`, `pk-tabs`, `pk-dialog`, `pk-menu`, `pk-table` in
  `pk-table-wrap`, `pk-card`, `pk-stats`/`pk-stat`, `pk-badge`, `pk-dot`,
  `pk-empty`, `pk-skeleton`, `pk-spinner`, `pk-progress`, `pk-code`, `pk-kv`,
  `pk-json`; Layout mit `pk-stack`, `pk-row`, `pk-grow`, `pk-toolbar`.
- **Farben, Abstände, Radien, Schrift nur über Tokens**: `--surface-0…3`,
  `--text-primary/secondary/muted`, `--border-subtle/default/strong`,
  `--accent*`, `--ok/--warn/--danger/--info` (+ `-subtle`), `--space-1…8`,
  `--radius-sm/md/lg`, `--text-xs…xl`, `--font-ui`, `--font-mono`. Jeder Wert
  gilt in hell und dunkel. Ein Hex-Wert im Panel ist ein Fehler — fehlt ein
  Token, gehört er ins Kit.
- **Eigenes CSS** nur für das, was das Panel wirklich eigen hat (eine
  Zeitleiste, ein Graph), in einer Datei neben dem Skript, aus Tokens gebaut.
- `hidden` versteckt zuverlässig, egal welches `display` eine Klasse setzt.

## 5. Verbote

- **Kein `alert`/`confirm`/`prompt`** — das Kit ersetzt sie; der native Aufruf
  scheitert laut (Konsole + Toast). Test: `tests/ui/test_kit.py`.
- **Kein `innerHTML` mit Daten** außer über ``html`…` ``/`render`. Auch
  Server-Daten nicht: Agent-Namen, Tool-Argumente, Titel kommen von Modellen.
- **Kein `window.parent`, kein `window.top`** — die Shell spricht nur über das
  Protokoll. Session, Theme und Dialoge kommen über das Kit.
- **Kein rohes `fetch`** im Panel-Code.
- **Keine eigenen Buttons, Tabs, Modals, Toasts, Refresh-Knöpfe**, keine Emoji
  als Icons.
- **Kein CDN** — Bibliotheken nach `static/vendor/`.

## 6. Das Protokoll (für Neugierige und die Shell)

Alle Nachrichten tragen `type` und `v` (`PROTOCOL_VERSION`). Das Kit spricht
es; ein Panel ruft nur die Funktionen oben.

| Richtung | `type` | Inhalt |
|---|---|---|
| Panel → Shell | `pk:ready` | beim Laden |
| Shell → Panel | `pk:init` | `theme`, `visible`, `session` — erst danach gilt das Panel als „in der Shell" |
| Shell → Panel | `pk:theme` / `pk:session` / `pk:visibility` | `theme` / `session` / `visible` |
| Panel → Shell | `pk:dialog` → `pk:dialog-result` | `id`, `dialog: {title, message, actions, input}` → `id`, `value` |
| Panel → Shell | `pk:toast` | `message`, `kind` |
| Panel → Shell | `pk:title` / `pk:badge` / `pk:navigate` / `pk:open` / `pk:set-theme` | `text` / `count` / `path` / `panel, path` / `theme` |

Was ein Panel vor `pk:init` sagt (Titel, Pfad), hält das Kit zurück und
schickt es nach dem Handschlag. Ohne Shell (eigener Browser-Tab, fremder
Frame) kommt kein `pk:init`, und das Kit zeigt Dialoge und Toasts selbst.

## 7. Prüfen

1. `python src/scripts/validate_plugin.py src/plugins/<name>` — der
   `web_ui`-Block geht durch den Katalog-Parser.
2. `pytest tests/ui -q` — u. a. jedes im Code genannte Icon existiert, kein
   Kit-Nutzer ruft native Dialoge (erfasst werden Templates auf
   `panel_base.html` und Skripte, die `panel-kit.js` importieren),
   Katalog-Regeln, Shell im echten Browser.
3. **Sichtprüfung in beiden Themes**: Panel in der Shell öffnen, angedockt und
   herausgelöst, Theme über den Knopf im Kopf wechseln. Direkt unter seiner URL
   im Browser-Tab öffnen — auch da muss es funktionieren.
4. Panel-Logik mit Verzweigungen bekommt einen Browser-Test nach dem Muster
   `tests/ui/test_shell_browser.py` (`run_app_test_page` mit Stub-App) oder
   `tests/ui/test_kit_js.py` — und die Mutationsprobe (Skill `unit-testing`):
   ausgelieferte Datei im Speicher verändern, Test muss rot werden.

## 8. Ein bestehendes Panel migrieren

Ein Plugin nach dem anderen; Writer-Panels mit der Writer-Session absprechen.

1. **Inventur:** welche Endpoints, welche Zustände (leer, Fehler, lädt), welche
   Aktionen (löschen → `confirm` mit `danger`), welche Parameter liest es
   (`session_id`, `request_id`)? Nur was es wirklich liest, wird zu `contexts`.
2. Route auf `ui_templates()`, Template auf `kit/panel_base.html`.
3. Eingebettetes `<style>` löschen; was bleibt, auf Tokens. Eigene `.btn`,
   `.modal`, `.tab`, `.toast`, `.badge` → Kit-Klassen.
4. Inline-Skript in ein Modul neben dem Template (`static/panel.js`), `fetch` →
   `api`, String-HTML → ``html`…` ``, `escapeHtml`-Kopie löschen,
   `toggleAutoRefresh` → `<pk-refresh>`, Session-Raten über `window.parent` →
   `session`.
5. Emoji → `icon()`. Nicht im Sprite? In `static/kit/icons.svg` aus Lucide
   ergänzen (gleiche Strichstärke), nicht als Emoji lassen.
6. Tote Teile des alten Panels (nie erreichte Zweige, doppelte Handler,
   ungenutzte Endpoints) entfernen statt mitzuschleppen.
7. Prüfen wie in § 7, dann die Sichtprüfung — das alte und das neue Panel
   einmal nebeneinander: fehlt eine Funktion, ist die Migration nicht fertig.
