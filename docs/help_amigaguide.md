# Hilfe im AmigaGuide-Format

Stand 28.09.2026. Das Help-Panel zeigt das ScarabHive-Handbuch und die Doku
jedes Plugins in einem AmigaGuide-Viewer. Das Format ist das Hypertext-Hilfe-
format von AmigaOS, **erweitert** um das, was Markdown kann -- in AmigaGuides
eigener Syntax. Nutzer-Entscheid 28.09.2026: das Format wächst, es wechselt nicht
auf Markdown (ein `@markdown`-Modus war kurz gebaut und ist wieder raus). Markdown
gerendert wird nur, was eine Markdown-Datei *ist*: README, `docs/*.md`, `@embed x.md`.

## Wo was liegt

| Teil | Datei |
|---|---|
| Parser, Layout, Markdown-Umschreiben | `src/agent_system/ui/amigaguide.py` |
| Bibliothek (welche Guides es gibt), Routen | `src/agent_system/ui/help.py` |
| Das Handbuch | `src/agent_system/ui/guides/scarabhive.guide` (jede `*.guide` dort gehört dazu; Id = Dateiname) |
| Der Viewer (Kit-Element `<pk-guide>`) | `static/kit/guide.js`, Stile am Ende von `static/kit/kit.css` |
| Das Panel | `templates/panels/help.html` (nur `<pk-guide address search>`), Kern-Panel `help` in `ui/catalog.py` |
| Tests | `tests/ui/test_help.py`, `tests/ui/test_help_panel_browser.py` (+ `help_panel_tests.html`, `help_embed_probe.html`) |

## Woher die Guides kommen

1. Das Handbuch aus `ui/guides/`.
2. Ein Plugin mit eigener Guide: `<plugin-ordner>/<ordnername>.guide`, Id = Ordnername
   (= Plugin-Typ). Link von außen: `sub_agent_manager/main`.
3. Ein Plugin ohne Guide, aber mit `README.md`: die README, als Markdown gerendert.
   Ihre Links auf Doku-Dateien öffnen im Viewer als Datei-Seite (siehe Sicherheit).
4. `plugins`: erzeugt, eine Tabelle aller Plugins aus `plugins.plugin_dirs`.

Eine Guide wird neu gelesen, sobald sie, eine mit `@embed` eingebettete Datei (auch
eine, die beim Lesen noch fehlte) oder -- bei einer README-Seite -- die `plugin.toml`
sich ändert (mtime + Größe, gestempelt vor dem Lesen). Kein Neustart.

## Format

AmigaOS-3.1-Teilmenge: `@database @author @(c) @$VER: @node @endnode @title @toc
@prev @next @index @help @wordwrap @smartwrap @remark @embed`, inline `b ub i ui u uu
plain fg bg jleft jcenter jright pard line par tab code body amigaguide`, Buttons
`@{"label" link node [zeile]}` (auch `guide/node`, `x.guide/node`, `HELP:x.guide/node`).
`system`, `rx`, `rxs`, `beep`, `close`, `quit` werden als tote Buttons gezeigt, nie
ausgeführt. Escapes `\@`, `\\`.

Erweiterungen (was Markdown hat, in AmigaGuide-Syntax):

| Schreibweise | Wirkung |
|---|---|
| `@{h1}Titel`, `@{h2}`, `@{h3}` | Überschrift, endet mit ihrer Zeile |
| `@{bullet [ebene]}`, `@{number [ebene]}` | Listenpunkt; Nummern zählen selbst -- über Leerzeilen, Code-Blöcke und Tabellen hinweg; ein normaler Absatz, eine Überschrift, ein Zitat, eine Linie oder ein Bullet derselben Ebene beginnt neu |
| `@{quote}` | Zitat-Absatz |
| `@{rule}` | Trennlinie |
| `@{tt}`..`@{utt}`, `@{s}`..`@{us}` | Inline-Code, durchgestrichen (wie `b`/`ub`) |
| `@{code sprache}` .. `@{body}` | Code-Block, eingefärbt (siehe unten); `@{code}` ohne Sprache bleibt AmigaOS (Zeilen wie geschrieben) |
| `@{table}` .. `@{body}` | Tabelle: eine Zeile pro Reihe, Zellen mit `\|`, erste Reihe = Kopf; `\|` ist ein Pipe; eine Trennzeile (`---`, drei Striche je Zelle) einmal, direkt unter dem Kopf; Umbruch, Block oder Ausrichtung in einer Zelle ist ein Fehler, kein Verlust |
| `@{"label" link https://…}` | Web-Link (nur http/https/mailto), neuer Tab |
| `@{"label" link docs/x.md}` | Doku-Datei als Seite (wie auf dem Amiga: ein Link darf eine Datei nennen) |
| `@{image datei.png "alt"}` | Bild aus dem Guide-Ordner |
| `@embed datei` | Markdown gerendert, alles andere als Code-Block in der Sprache der Endung |

Was der Leser nicht einordnen kann, steht unter der Seite (`problems`), statt zu
verschwinden: ein `@{` ohne schließendes `}` (als Text gezeigt; die Befehle danach
wirken weiter), ein Block-Befehl in einem Code-Block, ein toter Link.

Syntaxfarben: `<pk-guide>` lädt beim ersten Code-Block das Prism des Chats
(`static/vendor/prism/prism.js`, `data-manual`: es färbt nur, was es bekommt) und färbt
jeden Block, dessen Sprache es kennt (python, yaml, json, toml, bash, sql, js/ts, css,
html/xml, diff, markdown, c/cpp, java) -- in Guides wie in gerendertem Markdown. Ein
Block mit einem Button, Bild oder Textattribut darin (auch einem, das von vorher noch
eingeschaltet ist, etwa ein offenes `@{b}`) bleibt ungefärbt: Prism schreibt das Markup
neu, der Button wäre weg. Gefärbt werden höchstens 100 000 Zeichen je Seite; ein Block,
der nicht mehr hineinpasst, bleibt ungefärbt (512 KB JSON kosteten 230 ms und 4,5 MB
Markup bei jedem Öffnen). Schlägt das Laden von Prism fehl, fragt die
nächste Seite neu. Das `tabindex`, das Prism an den Block hängt, nimmt der Viewer wieder
weg: sonst wäre nur jeder gefärbte Block ein Tab-Halt. Farben: Kit-Tokens, dieselbe
Palette wie im Chat.

Markdown-Dateien rendert `markdown_to_html` (der sanitisierte Renderer des Chats) mit
`line_breaks=False`: ein Zeilenumbruch ist ein Leerzeichen, die Listen-Rettung des
Chats bleibt aus, und eine Liste direkt unter einer Absatzzeile bekommt die Leerzeile,
die Python-Markdown braucht (wie GitHub).

## Sicherheit (bewusste Entscheidungen)

- Jeder Dateizugriff läuft über `inside(ordner, relativ)`: absolute, Laufwerks- und
  UNC-Pfade werden abgelehnt, **bevor** das Dateisystem gefragt wird (`//host/x`
  aufzulösen ist unter Windows schon ein SMB-Zugriff), danach `resolve()` +
  `is_relative_to()`; NUL und überlange Pfade ergeben None statt eines 500ers.
- Die Asset-Route liefert nur Bildtypen (`png jpg gif webp svg`), nur oben im Ordner
  oder unter `docs/`, mit `Content-Security-Policy: default-src 'none'; …; sandbox` --
  ein SVG, direkt geöffnet, führt nichts aus.
- Datei-Seiten (`document()`) nur für `.md`/`.markdown`/`.txt`, und nur die `README.md`
  oben im Ordner oder Dateien unter `docs/`. Das Review hat gemessen, was ohne diese
  Grenze jeder angemeldete Nutzer las: Agent-Prompts (`agents/prompts/*.md`),
  gitignorierte Arbeitsdateien, Doku admin-only Plugins. Ein Plugin-Ordner enthält
  auch Konfiguration (n8n: eine getrackte `secrets.env`).
- Und nur, was die Doku **verlinkt** (`linked_document()`): ein Button eines Knotens,
  ein Link der README, ein Link einer so erreichten Datei-Seite. Das dritte Review fand
  in `docs/` Unverlinktes, das nicht für jeden Leser ist (ein Betriebs-Runbook mit
  Host-IP und `root@`-Befehl). Die Links kommen aus der gerenderten Seite selbst
  (`data-file`): was der Viewer als Link zeigt, öffnet sich, sonst nichts -- ein Pfad
  in einem Code-Block schließt nichts auf.
- Eine Datei über 512 KB wird genannt, nicht gerendert: 2,8 MB Markdown hielten den
  Lock des Renderers vier Sekunden (Debatten-Forum und Formatter-Hook warteten mit).
- Eine unlesbare Datei (Speichern eines Editors genau dazwischen, Sperre) kostet ihre
  eigene Guide, nicht alle Routen.
- Entfernte Bilder werden nie geladen (der Alt-Text steht dafür) -- dieselbe Linie wie
  der Chat-Sanitizer.
- Markdown-HTML kommt sanitisiert vom Server; das Umschreiben von `a`/`img` baut jedes
  Attribut neu und escapet es.
- Routen: `/api/help/*` und `/ui/panels/help` fallen unter die Default-Policy
  `require_auth`; eigene Regeln braucht es nicht.

## `<pk-guide>` in einem Plugin-Panel

```html
<script type="module" src="/static/kit/guide.js"></script>
<pk-guide guide="my_plugin" node="config"></pk-guide>
```

Attribute: `guide`, `node`, `file` (nur eine Datei, die die Guide verlinkt) -- setzen öffnet die Seite, auch mit dem Wert, den
das Attribut schon hat (der Leser kann inzwischen weitergeklickt haben); `search`
(Suchfeld); `address` (das Element ist die Seite: Adresse und Titel gehören ihm -- nur
einmal pro Seite, so im Help-Panel).
Event `guidechange` nach jeder gezeigten Seite. Das Element scrollt sich selbst (mit
Höhe) oder den nächsten scrollenden Kasten -- nie etwas außerhalb seines Dokuments.
`scrollIntoView` war der Fehler, der die Shell mitscrollte.

## Prüfen

- `pytest tests/ui/test_help.py -k repository`: jede Guide im Repo ohne Strukturfehler,
  toten Link, unbekanntes Attribut. Nach jeder Änderung am Handbuch fahren.
- Parser, Bibliothek und `guide.js` sind mutationsgeprüft (Mutanten nur im Speicher,
  `guide.js` über eine vorgeschaltete Route ausgeliefert).

## Grenzen (bewusst)

- `pyproject.toml` liefert `*.guide` als package-data mit; Bilder für das Handbuch
  bräuchten einen eigenen Eintrag, sobald es welche gibt.
- Plugin-Guides und READMEs sieht jeder angemeldete Nutzer, auch die eines Plugins,
  dessen Panel nur Admins öffnen -- Doku gilt als lesbar; wer das anders will, hängt
  die Guides an die Panel-Rollen des Plugins.
- Kein CLI-Viewer und kein Agent-Tool zum Lesen der Hilfe (Nutzer 28.09.2026: ein
  Agent-Tool braucht es nicht) -- beides ließe sich auf `Library.page()`/`search()` setzen.
