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
Blöcke: `head`, `lang` (Writer: `de`).

Icons im Template: `{{ icon('refresh-cw') }}`, `{{ icon('bug', size='sm', label='Bug') }}`.

## 3. `panel-kit.js`

```js
import { api, html, render, icon, session } from '/static/kit/panel-kit.js';
```

| Export | Wofür |
|---|---|
| `api(path, {method, json, body, headers, quiet, raw, latest})` | jeder Server-Aufruf. Cookie-Auth, JSON rein und raus, Fehler als `ApiError(status, detail)` **plus Toast** (außer `quiet: true`), abgebrochen, wenn das Panel geht. Wer selbst reagiert (404 → leerer Zustand), nimmt `quiet` und fängt. **`latest: 'name'`** bricht den vorigen Aufruf gleichen Namens ab — eine überholte Antwort kann nie gezeichnet werden; der abgebrochene wirft einen `AbortError` ohne Toast, erkennbar mit `isAborted(error)`. Das ersetzt jeden eigenen Sequenz-Zähler. |
| `abandon(name)` | den laufenden `latest`-Aufruf dieses Namens verwerfen, etwa wenn der Nutzer leert, was er füllen würde. |
| `pluginBase(import.meta.url)` | die Adresse des Plugins für ein Skript aus seinem `static/`-Ordner (`/plugins/<name>`). |
| `errorText(error)` | der Text, den der Toast zeigen würde („404: Not Found“) — für eine Seite, die den Fehler selbst anzeigt. |
| `update(el, content)` | `render`, aber nur bei geändertem Markup (Rückgabe: gezeichnet ja/nein). Eine unveränderte Antwort lässt Scroll, Fokus, Auswahl und offene `<details>` stehen. Ein Element entweder mit `update` oder mit `render` zeichnen. |
| `notice(el, text, {kind})` | Meldung an Ort und Stelle als `.pk-callout` (`danger` Standard, `warn`, `info`, `ok`); leerer Text versteckt sie. Für „Aktualisieren fehlgeschlagen, gezeigt ist der Stand von …“. |
| `emptyState(icon, title, text)`, `skeleton(lines)` | leerer Zustand und Lade-Platzhalter; in Templates die Makros `empty()` und `skeleton()` aus `kit/macros.html`. |
| `localTime(ts, {relative, seconds})` | ein gespeicherter Zeitstempel in Ortszeit und Seitensprache. Werte ohne Zone (so schreiben es alle unsere Datenbanken) gelten als UTC; `relative` ergibt „vor 5 Minuten“. |
| `formQuery(form)`, `setQuery(params)` | Filterformular → Query (leere Felder fallen weg); Query in die URL schreiben (`replaceState`) und der Shell melden. |
| `withBusy(controls, fn)` | `fn` einmal zur Zeit: die Knöpfe sind gesperrt, ein Doppelklick startet nichts. Danach sind **alle** wieder frei — wer eigene Sperrzustände hat, setzt sie nach `withBusy` neu. Ein Knopf, den ein Neuzeichnen währenddessen ersetzt, ist nicht mehr der gesperrte: Zeilen, die ein Takt neu zeichnet, merken sich ihre Sperre selbst (etwa eine Menge von IDs, die das Markup liest). |
| `copyText(text)` | in die Zwischenablage, mit Toast. |
| `<pk-pager page pages>` | Zurück/Weiter mit „Seite 2 von 7“, feuert `page` (`detail.page`), versteckt bei einer Seite. |
| `<table data-pk-select>`, `selectRow(table, id)` | Zeilen mit `data-id` und `tabindex="0"` sind per Klick oder Enter wählbar (`aria-selected`, Event `rowselect` mit `detail.id`); Bedienelemente in der Zeile bleiben ihre eigenen. `selectRow` markiert nach dem Neuzeichnen wieder. |
| `selectTab(list, name)` | einen Tab (`data-tab`) wählen, ohne Klick — etwa aus der URL. |
| `isDark()` | ob die Seite gerade dunkel ist (für Canvas/SVG, die keine Tokens lesen). |
| ``html`…` ``, `render(el, content)` | Markup bauen: jeder eingesetzte Wert wird escapet, verschachteltes ``html`` `` und Arrays bleiben Markup. `trusted(str)` nur für schon sicheres HTML (z. B. vom Server sanitisiert). `false`, `null` und `undefined` ergeben nichts — damit `${cond && html`…`}` geht; in einem Attribut darum `aria-pressed="${String(on)}"`, sonst steht dort `""`. `render` gibt den Fokus zurück: lag er im Element, bekommt ihn danach das neue Element mit demselben `data-key`. |
| `escapeHtml(v)`, `jsonView(value)`, `icon(name, {size, label})` | Hilfen für dasselbe. `jsonView` zeigt JSON zum Lesen: alle Ebenen offen, Strings ohne Anführungszeichen und mit ihren Zeilenumbrüchen, Arrays als Liste. Das Roh-JSON bietet das Panel selbst an (Kopieren, Umschalter). |
| `yamlCode(text)` | YAML-Text eingefärbt (`.pk-yaml-*`), der Text selbst unverändert: für ein `<pre class="pk-code">` oder als Kopie unter einer Textarea. `\|`/`>`-Blöcke behalten ihre Farbe über Leerzeilen, ein unquotiertes `!…` erscheint als Tag. Kein Prism nötig. |
| `alert(msg)`, `confirm(msg, {title, confirmLabel, danger})`, `prompt(msg, {title, value, placeholder, confirmLabel})`, `dialog({title, message, actions, input})` | Dialoge — Promise mit dem Ergebnis (`confirm` → `true/false`, `prompt` → Text oder `null`). In der Shell über der ganzen Anwendung, sonst im Panel. |
| `toast(msg, {kind})` | `info`, `ok`, `warn`, `error`. |
| `session.id`, `session.onChange(fn)` | die Session, die im Chat offen ist: ihre ID oder `null`; `onChange` bekommt `{id, title}` oder `null`. |
| `autoRefresh(fn, ms)` → `{start, stop, running}`; `<pk-refresh interval="s">` | Nachladen, pausiert, solange das Panel nicht sichtbar ist. `<pk-refresh>` feuert `refresh` am `document`; `event.detail.auto` sagt, ob der Takt (true) oder ein Klick (false) fragt — teures Nachladen darf den Takt auslassen. Mit `auto` läuft der Takt von Anfang an (sonst erst nach Klick). `interval` und `auto` sind nur die **Voreinstellung der Seite**: Am Knopf wählt der Nutzer den Takt (5 s, 10 s, 30 s, 1 min, die Voreinstellung, oder aus), und diese Wahl gilt ab dann für diesen Panel-Pfad — gespeichert im localStorage unter `pk.refresh:<pathname>`. |
| `isVisible()` | ob das Panel zu sehen ist — nicht bei Tab im Hintergrund, geschlossenem Fenster, verdecktem Browser-Tab. `autoRefresh` fragt es selbst. |
| `initTabs(root)` | `[data-pk-tabs]` bedienbar machen (Klick, Pfeiltasten, Event `tabchange`). Läuft beim Laden von selbst; nach dem Nachrendern erneut aufrufen ist unschädlich. |
| `initSidebars(root)` | jeder `.pk-sidebar` die zuletzt gezogene Breite geben und die nächste merken (localStorage `pk.sidebar:<pathname>`, mit `#id`, wenn die Leiste eine hat). Läuft beim Laden von selbst; nur eine später gerenderte Leiste braucht den Aufruf. |
| `placeMenu(menu, box, {matchWidth})` | eigenes Menü positionieren (`matchWidth`: so breit wie das Feld, zu dem es gehört — Vorschlagslisten). Ein `.pk-menu[popover]` mit `id`, das per `popovertarget` geöffnet wird, platziert das Kit selbst. |
| `setTitle(text)`, `navigate(path)` | Titel im Tab, Pfad im Panel merken (wird beim Wiederherstellen geöffnet und gilt als eigener Pfad: anders als ein Kontext-Link setzt der Starter ihn nicht zurück; ohne Argument der Pfad, den die Seite gerade zeigt). |
| `setDirty(bool)` | ungespeicherte Eingaben melden: solange `true`, fragt die Shell vor Schließen, Ab- oder Andocken oder einem Link, der das Panel neu lädt, und der eigene Browser-Tab vor dem Verlassen. Nach dem Speichern oder Verwerfen `setDirty(false)`. |
| `setTheme(theme)`, `currentTheme()`, `onThemeChange(fn)`, `THEMES` | Theme-Wahl und -Wechsel (Einstellungen, Kit-Seite); `onThemeChange` meldet auch einen Wechsel aus der Shell und, bei `system`, einen Wechsel der Systemfarben. |

**Das Muster eines Laders** — neueste Antwort gewinnt, Unverändertes bleibt
stehen, ein Fehler behält das Gezeigte und sagt es:

```js
async function load() {
  try {
    const data = await api(`${base}/jobs?${formQuery(filters)}`, { latest: 'jobs', quiet: true });
    notice(stale, '');
    update(list, data.jobs.length ? rows(data.jobs) : emptyState('list-todo', 'Keine Jobs'));
  } catch (error) {
    if (isAborted(error)) return;
    notice(stale, `Nicht aktualisiert (${errorText(error)}) — gezeigt ist der letzte Stand.`);
  }
}
```

Sprache: das Kit spricht die Sprache aus `<html lang>` (Writer-Panels: `de`)
— Dialogknöpfe, Refresh-Menü, Pager, Kopier-Toast.

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

**Sortierbare Tabellen** brauchen kein eigenes Skript: `render()` sortiert
danach jede `<table class="pk-table" data-pk-sort="<name>">` mit `<thead>`, die
es gerendert hat (auch die umschließende, wenn nur der `tbody` neu kommt).

- Ein Klick irgendwo in die Kopfzelle sortiert, eine `.pk-num`-Spalte größte
  zuerst, jede andere A–Z; der zweite Klick dreht um. Auf der Spalte, nach der
  die Tabelle gerade sortiert ist (auch per `aria-sort` im Markup), dreht schon
  der erste Klick um. Das Kit legt den Kopfinhalt in einen
  `<button class="pk-sort" data-key="sort:<name>:<Kopftext>">` (Tastatur); über
  den `data-key` bekommt er nach dem Neuzeichnen den Fokus zurück — ein eigener
  Selektor auf `[data-key]` trifft ihn also mit. Köpfe ohne Text, mit eigenem
  Bedienelement (Link, Button, Eingabefeld, Auswahl, Label, `summary`, alles
  mit `tabindex`)
  oder mit `data-pk-nosort` bleiben stumm; `data-pk-nosort` auch dort, wo die
  Werte keine Ordnung haben (eine Reihe Badges).
- Die Wahl hält über jedes Neuzeichnen (Auto-Refresh) und hängt am
  **Kopftext**, nicht an der Spaltennummer; eine Spalte, die nur manchmal da
  ist, verschiebt sie also nicht. Der Name trennt die Tabellen einer Seite.
  Nach einem Reload ist sie weg.
- Sortiert wird nach `data-sort-value`, sonst nach dem Text. Sind alle Werte
  einer Spalte Zahlen, als Zahlen; alle ISO-Zeitstempel, als Zeitpunkte (ohne
  Zone als UTC, wie unsere Datenbanken schreiben); sonst
  die ganze Spalte natürlich als Text („B9" vor „B10"). Leer oder ein
  einzelner Strich steht immer am Ende. **Jede Zelle, deren Text nicht ihr
  Wert ist** (`toLocaleString()` mit Tausenderpunkt, „1.2 s", „0.300¢", ein
  Badge, ein gekürzter Text), bekommt `data-sort-value="${roh ?? ''}"`.
- `aria-sort="descending"` im Markup ist die Reihenfolge, bis der Nutzer
  wählt. Sortiert das Panel seine Zeilen heute nach einer Spalte, gehört
  diese Markierung an den Kopf und die JS-Sortierung weg.
- Nicht sortierbar machen: Schlüssel-Wert-Tabellen, Tabellen mit Zeilenpaaren
  (Detailzeile unter jedem Eintrag) und serverseitig seitenweise geladene
  Listen — dort sortiert der Server (`writer_admin`). Eine Liste „die neuesten
  N" sortiert das Kit innerhalb dieser N.
- Eine Kopfzeile, ein `<tbody>` (nur das erste wird sortiert), kein
  `colspan`. Zeilen behalten beim Umsortieren ihre
  Attribute; Handler über `data-id`/`data-index` funktionieren weiter, Code,
  der die Reihenfolge im DOM liest, nicht.

## 4. Aussehen

- **Komponenten** statt eigener: `pk-btn` (`--primary`, `--danger`, `--ghost`,
  `--icon`, `--sm`), `pk-input`/`pk-select`/`pk-textarea`/`pk-check`/`pk-switch`
  in `pk-field` und `pk-form`, `pk-tabs`, `pk-dialog`, `pk-menu`, `pk-table` in
  `pk-table-wrap`, `pk-card`, `pk-stats`/`pk-stat`, `pk-badge`, `pk-dot`,
  `pk-empty`, `pk-skeleton`, `pk-spinner`, `pk-progress` (`--ok/--warn/--danger`),
  `pk-code`, `pk-kv`, `pk-json`, `pk-callout` (`--danger/--warn/--info/--ok`),
  `pk-details` (auf einem `<details>`), `pk-prose` (Lesetext; `pk-prose--breaks`
  hält gespeicherte Zeilenumbrüche), `pk-text--ok/--warn/--danger/--info`
  (ein Wert in einer Tonfarbe), `pk-code--full` (Code ohne Höhendeckel),
  `pk-menu-item[aria-selected]` (der gewählte Vorschlag); Layout mit `pk-stack`, `pk-row`,
  `pk-grow`, `pk-toolbar`, `pk-filters` (Filterzeile über einer Liste),
  `pk-split` (Liste links, Detail rechts, das Detail bleibt stehen),
  `pk-sidebar-layout` mit `pk-sidebar` (Seitenleiste neben dem Hauptbereich:
  an der Ecke in der Breite ziehbar, das Kit merkt die Breite pro Panel,
  schmal gestapelt — **jede Seitenleiste nimmt diese**, keine eigene), Seiten
  eines Panels als `<nav class="pk-tabs">` mit `<a class="pk-tab"
  aria-current="page">`.
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

Alle Nachrichten tragen `type`. Das Kit spricht es; ein Panel ruft nur die
Funktionen oben.

| Richtung | `type` | Inhalt |
|---|---|---|
| Panel → Shell | `pk:ready` | beim Laden |
| Shell → Panel | `pk:init` | `theme`, `visible`, `session` — erst danach gilt das Panel als „in der Shell" |
| Shell → Panel | `pk:theme` / `pk:session` / `pk:visibility` | `theme` / `session` / `visible` |
| Panel → Shell | `pk:dialog` → `pk:dialog-result` | `id`, `dialog: {title, message, actions, input}` → `id`, `value` |
| Panel → Shell | `pk:toast` | `message`, `kind` |
| Panel → Shell | `pk:title` / `pk:navigate` / `pk:set-theme` | `text` / `path` / `theme` |
| Panel → Shell | `pk:dirty` | `dirty` (ungespeicherte Eingaben; eine neue Seite im Panel gilt als sauber) |

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
