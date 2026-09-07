# Panel-Kit — eine Oberfläche statt einundzwanzig

**Status:** Konzept. Nichts davon ist gebaut.
**Bestandsaufnahme:** gemessen am 07.09.2026 über alle 29 Panel-Templates.
**Ressort:** Kern-UI (`static/`, `templates/`, `src/agent_system/plugins/web_adapter.py`).

Nachbardokumente: [schema_based_web_plugin.md](schema_based_web_plugin.md) und
[schema_based_web_routing.md](schema_based_web_routing.md) beschreiben die
**Server**seite eines Panels — wie seine Routen entstehen. Dieses Dokument
beschreibt die **Client**seite: wie es aussieht.

---

## 1. Das Problem in einem Satz

Jedes Plugin, das ein Panel anbietet, baut das Design von vorne — und jedes
kommt bei etwas anderem heraus.

---

## 2. Bestandsaufnahme

29 Panels in 21 Plugins, zusammen **22.050 Zeilen HTML**.

| Größe | Wert |
|---|---|
| eingebettetes CSS (`<style>`-Blöcke) | **8.507 Zeilen — 38 % des HTML** |
| hartcodierte Farbwerte | **1.552 Vorkommen, 209 verschiedene** |
| `var(--token)`-Nutzungen | **60** |
| eigenes JS (`<script>` ohne `src`) | 25 Panels, **98 rohe `fetch()`** |

Dieselbe Sache, mehrfach neu geschrieben:

| Klasse | in wie vielen Panels neu definiert |
|---|---|
| `.form-group` | 25 |
| `.stat-card` | 24 |
| `.modal` | 18 |
| `.tab` | 18 |
| `.badge` | 16 |
| `.btn` | 15 |
| `.toast` | 13 |

Dasselbe im JavaScript: `escapeHtml()` 11×, `toggleAutoRefresh()` 7×,
`showToast()` 6×, `showError()` 5×, `closeModal()` / `switchTab()` je 4×.

### 2.1 Der Refresh-Knopf

Das Beispiel, an dem das Problem am deutlichsten wird: **29 Refresh-Knöpfe in
15 Panels, in 6 verschiedenen Schreibweisen.**

| Beschriftung | Vorkommen |
|---|---|
| `🔄` | 12 |
| `⏱️` | 9 |
| `🔄 Refresh` | 5 |
| `⏱ Auto` | 1 |
| `⟳` | 1 |
| `Neu laden` | 1 |

Derselbe Knopf, dieselbe Funktion, sechs Gesichter. Dahinter liegt jedes Mal
eine eigene `toggleAutoRefresh()`-Kopie mit eigenem Intervall.

### 2.2 Buttons

**11 verschiedene Formkombinationen** aus Polsterung, Radius und Schriftgröße:

| padding | radius | font-size | Vorkommen |
|---|---|---|---|
| `8px 16px` | `4px` | `14px` | **13** |
| `10px 20px` | `6px` | `0.9rem` | 2 |
| `5px 10px` | `4px` | `12px` | 1 |
| `5px 12px` | `3px` | `12px` | 1 |
| `8px 20px` | `4px` | `13px` | 1 |
| `12px 30px` | `8px` | — | 1 |
| `6px 10px` | `6px` | `13px` | 1 |
| `8px 16px` | `4px` | `13px` | 1 |

Bemerkenswert: **es gibt bereits einen De-facto-Standard** — `8px 16px`,
Radius `4px`, `14px` Schrift, in 13 von 23 Deklarationen. Er ist nur nirgends
aufgeschrieben, also weicht ein Drittel davon ab.

### 2.3 Dialoge

**11 Panels bauen einen eigenen `div.modal`, kein einziges benutzt das native `<dialog>`.** Jedes bringt
eigenen Backdrop, eigenes Schließen, eigenes Escape-Handling mit — oder eben
nicht. Fokusfalle hat keines.

### 2.4 Toolbars

**16 verschiedene Klassennamen für dieselbe Leiste:** `.controls` (11×),
`.header-actions` (4×), `.modal-actions` (4×), `.toolbar` (2×), dazu
`.refresh-controls`, `.form-actions`, `.memory-actions`, `.agent-actions`,
`.filter-actions`, `.task-actions`.

### 2.5 Ikonografie

**28 von 29 Panels setzen Emoji direkt in den Text.** Kein Icon-System, keine
gemeinsame Bedeutung: 🔄 und ⟳ und ⏱️ meinen dasselbe.

### 2.6 Der Nebenbefund, der alles erklärt

Die häufigsten Farben in den Panels sind `#1e1e1e`, `#252526`, `#d4d4d4`,
`#569cd6` — die **VS-Code-Dark+-Palette**. Die Shell benutzt `#111820`,
`#1a2129`, `#1e242d`. Die Panels malen ein anderes Theme als die Anwendung,
in der sie stecken.

---

## 3. Ursache — drei Schichten, keine davon Nachlässigkeit

**1. Panels liegen in `<iframe>`.** `plugin_manager.js` setzt
`iframe.src = plugin.panel_endpoint`. Ein iframe erbt kein CSS vom
Eltern-Dokument. Jeder Autor *musste* alles selbst mitbringen.

**2. Es gibt kein gemeinsames Basis-Template.** 0 von 29 Panels benutzen
`{% extends %}`; 15 Plugins bauen je ihre eigene `Jinja2Templates` auf ihr
eigenes Verzeichnis. Jedes Panel beginnt bei `<!DOCTYPE html>`.

**3. `static/css/base.css` hat sieben Tokens** — vier Hintergründe, drei
Rahmenfarben. Keine Textfarbe, kein Akzent, kein Zustand, kein Abstand, kein
Radius, keine Schrift. Wer sich in Tokens ausdrücken wollte, *könnte* es nicht.

**Und der Befund, der den Weg zeigt:** 17 der 29 Panels binden bereits eine
Shell-CSS ein — `scrollbar.css`. Der Weg aus dem iframe nach `/static/…` ist
also längst befahren, gleiche Origin, funktioniert. Er wurde nur nie für das
Design benutzt.

---

## 4. Wie andere Systeme das lösen

Zwei Lager, und die Trennlinie ist genau unsere Frage: iframe oder nicht.

### Lager A — kein iframe, dafür SDK-Zwang

**Hermes Desktop Plugin SDK.** Ein Plugin ist eine einzige ESM-Datei, läuft
**im selben Dokument** mit voller App-Autorität und registriert sich über
`ctx.register({ area: 'panes', render })`. Beitragspunkte sind benannte Flächen
(`PANES_AREA`, `ROUTES_AREA`, `SIDEBAR_NAV_AREA`, `STATUSBAR_AREAS`,
`PALETTE_AREA`). Stil-Isolation gibt es **nicht** — Einheitlichkeit wird
erzwungen, indem nur drei Import-Spezifizierer überhaupt auflösen
(`@hermes/plugin-sdk`, `react`, `react/jsx-runtime`) und die App ihr eigenes
UI-Kit mitliefert. Hausregel wörtlich: *„Style with theme variables, never
hardcoded colors."*

**Grafana.** Dasselbe als npm-Paket `@grafana/ui`, 87 Komponenten in Storybook.
Setzt eine Build-Kette im Plugin voraus.

### Lager B — iframe behalten, Tokens hineinreichen

**VS Code.** Webviews sind iframes. VS Code **injiziert alle Theme-Farben als
CSS-Variablen** ins `<html>` des Webviews (`--vscode-button-background` usw.)
und aktualisiert sie live bei jedem Theme-Wechsel. Das Lehrreiche daran: das
begleitende Komponenten-Toolkit wurde zum **1.1.2025 eingestellt** — übrig
geblieben und heute empfohlen ist genau der Variablen-Weg.

**Home Assistant.** Custom Cards stylen über `var(--primary-color)` aus dem
Theme; die Konsistenz kommt aus den Variablen, nicht aus einer Bibliothek.

---

## 5. Entscheidung: Lager B

Drei Gründe, in dieser Gewichtung:

1. **Wir sind schon dort.** iframes, gleiche Origin, und 17 Panels linken
   bereits Shell-CSS. Es braucht keinen Umbau, um anzufangen.
2. **Isolation ist hier Wert, nicht Ballast.** 21 Plugins, über lange Zeit mit
   schwächeren Modellen gewachsen. Hermes' „volle App-Autorität, keine
   Isolation" trägt nur mit hartem SDK-Zwang und einem Lade-Mechanismus, den
   wir nicht haben. Ein Panel mit kaputtem JS nähme sonst die Shell mit.
3. **Das einzige System, das beides versucht hat, hat die Bibliothek beerdigt
   und die Variablen behalten.**

---

## 6. Der Plan

### Stufe 1 — `static/css/panel-kit.css`

Eine Datei, ein `<link>` pro Panel, sonst nichts. Alte Panels ignorieren sie;
es kann nichts brechen.

**Teil A: Tokens** (von 7 auf ~25). Die vorhandenen vier Hintergründe und drei
Rahmenfarben bleiben, dazu kommen die Gruppen, die heute fehlen und deshalb
1.552-mal ausgeschrieben wurden:

| Gruppe | Tokens |
|---|---|
| Text | `--text-primary`, `--text-secondary`, `--text-muted` |
| Akzent | `--accent`, `--accent-hover` |
| Zustand | `--ok`, `--warn`, `--error`, `--info` (je Grund- und Rahmenton) |
| Abstand | `--space-1` … `--space-6` |
| Form | `--radius`, `--radius-lg` |
| Schrift | `--font-ui`, `--font-mono`, `--text-sm/base/lg` |

**Teil B: Komponenten**, nach gemessener Häufigkeit. Für jede gilt: die
Variante, die heute schon Mehrheit ist, wird die Norm — es geht um
Aufschreiben, nicht um Neuerfinden.

| Komponente | ersetzt heute | Norm |
|---|---|---|
| `.pk-btn` + `--primary/--danger/--ghost/--icon` | 23 Deklarationen, 11 Formen | `8px 16px`, Radius `4px`, `14px` (13× Mehrheit) |
| `.pk-refresh` | 29 Knöpfe, 6 Schreibweisen | **ein** Zeichen, **ein** Verhalten |
| `.pk-dialog` auf nativem `<dialog>` | 11 handgebaute `div.modal` | Fokusfalle, Escape, `::backdrop` gratis |
| `.pk-toolbar` | 16 Klassennamen | ein Name |
| `.pk-card` / `.pk-stat` | 24 `.stat-card` | |
| `.pk-badge` | 16 | Zustandsfarben aus Tokens |
| `.pk-toast` | 13 | |
| `.pk-tabs` | 18 `.tab` | |
| `.pk-field` | 25 `.form-group` | |
| `.pk-table`, `.pk-empty`, `.pk-loading` | verstreut | |

Präfix `pk-`, damit die Datei nichts umstylt, was ein Panel schon selbst
definiert hat. Die Migration eines Panels ist dann ein Umbenennen, kein Risiko.

### Stufe 2 — `templates/panel_base.html`

Damit Kopf, Link und Grundgerüst nicht 29× getippt werden:

```jinja
{% extends "panel_base.html" %}
{% block title %}Message Debugger{% endblock %}
{% block toolbar %}{{ pk_refresh() }}{% endblock %}
{% block content %} … {% endblock %}
```

Hier steckt die **einzige echte Code-Änderung** des ganzen Plans: die
`Jinja2Templates` der Plugins brauchen das gemeinsame Verzeichnis im Suchpfad
(Jinja-`ChoiceLoader`, gesetzt an einer Stelle in `web_adapter.py`). Heute
zeigt jede auf genau ein eigenes Verzeichnis.

### Stufe 3 — `static/js/panel-kit.js`

Getrennt vom CSS, eigener Nutzen, eigene Messung:

- `escapeHtml`, `showToast`, `showError` (heute 11 + 6 + 5 Kopien)
- `autoRefresh(fn, ms)` — ein Intervall-Verhalten statt sieben
- `api(path, opts)` als Hülle um die **98 rohen `fetch()`**: Auth-Header,
  Fehlerbehandlung und Abbruch an einer Stelle
- `dialog(...)` auf `<dialog>`

### Stufe 4 — Migration

Panel für Panel, opt-in, in der Reihenfolge der Größe. Das Erfolgsmaß steht
schon fest und ist heute gemessen:

| | heute | Ziel |
|---|---|---|
| eingebettetes CSS | 8.507 Zeilen | < 1.000 |
| verschiedene Farbwerte | 209 | ~0 (alles Tokens) |
| Refresh-Schreibweisen | 6 | 1 |
| Button-Formen | 11 | 1 + Varianten |
| handgebaute Modals | 11 | 0 |

---

## 7. Was ausdrücklich nicht gebaut wird

- **Keine Komponenten-Bibliothek mit Build-Kette** (Grafana-Weg). Die UI hat
  kein npm-Setup, und der Nutzen käme erst nach der Umstellung aller Panels.
  Microsoft hat diesen Weg für Webviews gebaut und wieder eingestellt.
- **Kein Umbau auf same-document / Web Components.** Das kostet die Isolation,
  die bei diesem Bestand gerade Geld wert ist, und verlangt einen SDK-Zwang,
  den wir nicht durchsetzen können.
- **Kein Anfassen fertiger Panels „nebenbei".** Migration ist ein eigener
  Schritt pro Plugin, mit Sichtprüfung.

---

## 8. Offene Entscheidungen

Keine davon blockiert Stufe 1.

1. **Hell/Dunkel?** `base.css` setzt heute fest `color-scheme: dark`. Tokens
   machen ein zweites Theme später möglich — ob es gewollt ist, ist offen.
2. **Emoji oder SVG?** 28 Panels benutzen Emoji. Billigste Norm: Emoji
   behalten, aber ein festgelegter Satz. Sauberste: ein Inline-SVG-Sprite.
3. **Wer migriert?** Die Panels liegen in zwei fremden Ressorts
   (`src/plugins/`, `src/plugins_writer/`). Das Kit kommt aus dem Kern, die
   Umstellung berührt fremde Bäume — das muss abgesprochen werden.
4. **Fortschritt messen.** Das Inventar-Skript, das die Zahlen oben erzeugt
   hat, könnte als `src/scripts/panel_inventory.py` mitlaufen. Sinnvoll erst,
   wenn Stufe 1 steht.

---

## 9. Risiken

- **Ein Panel-Kit kann Bestehendes umstylen.** Deshalb Präfix `pk-` und keine
  nackten Element-Selektoren außer im Basis-Template.
- **Migration über Ressort-Grenzen.** Siehe offene Entscheidung 3.
- **Der De-facto-Standard könnte falsch sein.** `8px 16px / 4px / 14px` ist
  die Mehrheit, nicht notwendigerweise die beste Wahl. Vor dem Festschreiben
  einmal ansehen, nicht nur auszählen.
