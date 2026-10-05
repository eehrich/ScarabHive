# Wie eine Antwort angezeigt wird

Agenten antworten in Markdown. Gespeichert, gestreamt und über die API
ausgeliefert wird genau dieser Text; **dargestellt** wird er erst dort, wo ihn
jemand liest. Ein Plugin oder Hook dafür gibt es nicht mehr: der
`markdown_formatter` und der Hook-Punkt `format_output` sind am 30.09.2026
entfallen. Bis dahin renderte der Hook im Server nach HTML — pro Agent
eingeschaltet, sodass die meisten Agenten im Chat als Rohtext erschienen, und
wo er lief, bekamen auch API-Clients und aufrufende Agenten HTML statt Markdown
(B57, 05.08.2026: zwölf Befunde stumm verloren; der Schutz dafür steht noch in
`core/agent_caller.py`).

## Web-Chat

`static/js/chat_module.js` (`formatContent`) zeichnet jede Antwort mit
markdown-it (`static/vendor/markdown-it/`):

- **Roh-HTML bleibt Text** (`html: false`): eine Antwort trägt, was ein Tool
  geholt hat. `<script>`, `<img onerror=…>`, `<b>` erscheinen als Zeichen.
  Einzige Ausnahme ist `<br>`: in einer Tabellenzelle der einzige Zeilenumbruch,
  den Markdown hat, und Modelle schreiben ihn dort (eigene Inline-Regel).
- **Keine Bilder**: ein fremdes Bild ist eine Anfrage an eine Adresse, die der
  Text gewählt hat — so leitet eine präparierte Seite Daten aus.
- **Links** nur `http`, `https`, `mailto` oder relativ (ohne Schema, wie beim
  Sanitizer im Server); sie öffnen in einem neuen Tab
  (`rel="noopener noreferrer"`), nicht an der Stelle des Chats.
- **Ein einfacher Zeilenumbruch bleibt einer** (`breaks: true`), wie das
  Modell ihn gemeint hat.
- **JSON** (strukturierte Ausgabe, oder ein Prompt verlangt JSON) erscheint
  als Code-Block, so wie es ist.
- Eine Antwort, die ganz in einem ```` ```markdown ````-Zaun steckt, ist die
  Antwort, nicht ein Beispiel davon: der Zaun fällt weg.
- Code-Blöcke färbt Prism (`language-<sprache>`).

Während des Streamens zeigt der Chat den Text roh; die fertige Antwort
(`thinking_complete`, `final`, Verlauf) wird gezeichnet.

## Terminal

`agent-cli`, `agent-run` und der Chat im Terminal geben die Antwort über
`cli_utils/common.show_answer` aus, gesteuert von `--color`:

| `--color` | Ausgabe |
|---|---|
| `auto` (Standard) | Markdown mit Farben (Rich), wenn stdout ein Terminal ist; sonst wie `text` |
| `always`, `ansi` | Markdown mit Farben, immer |
| `html` | HTML aus `utils/markdown_render.markdown_to_html` |
| `never`, `text` — und `auto` unter `NO_COLOR` oder `TERM=dumb` | der Text, wie das Modell ihn schrieb |

`always`/`ansi` schreiben ANSI-Codes auch in eine Pipe (unter Windows auch dort,
wo Rich sonst die Konsolen-API nähme) und auch unter `NO_COLOR` oder `TERM=dumb`:
ein ausdrückliches `--color` gewinnt. JSON wird immer so gedruckt, wie es ist. Ein
einfacher Zeilenumbruch bleibt auch hier einer, `<br>` ebenso (auch allein auf
einer Zeile: HTML-Blöcke gibt es hier wie im Chat nicht), und anderes HTML bleibt
als Text stehen — Rich allein würde es verschlucken, und mit ihm Platzhalter wie
`--agent <name>`. Bilder und Links wie im Chat: kein Bild, Links nur `http`,
`https`, `mailto` oder relativ (ein Terminal macht sie anklickbar); unter
`TERM=dumb` steht ein Link als „Text (URL)", ein Autolink nur einmal, wenn sein
Text seine Adresse ist.

## Server-Renderer

`utils/markdown_render.markdown_to_html` (Python-Markdown mit eigenem
Allowlist-Sanitizer) bleibt für die Seiten, die auf dem Server rendern: das
Debate-Forum-Panel, den Hilfe-Viewer (AmigaGuide-Markdown-Knoten) und
`--color html`.

## Tests

- `tests/ui/shell_tests.html` — „an answer is drawn from its Markdown…“: was
  das Modell schrieb, erscheint; Roh-HTML, Skripte, Bilder und
  `javascript:`-Links nicht; JSON bleibt JSON.
- `tests/app/test_app_sub_run_answers.py` — Stream und `POST /run` liefern jede
  Antwort als den Text des Modells, auch die eines Sub-Agenten.
- `tests/agent/test_agent_structured_output.py` —
  `test_an_answer_leaves_the_run_as_the_model_wrote_it`.
- `tests/cli/test_cli_show_answer.py` — die Ausgabe je `--color`.
