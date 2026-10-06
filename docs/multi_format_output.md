# How an answer is displayed

Agents answer in Markdown. Exactly this text is stored, streamed and delivered
through the API; it is only **displayed** where someone reads it. A plugin or
hook for this no longer exists: the `markdown_formatter` and the hook point
`format_output` were removed on 30.09.2026. Until then the hook rendered to HTML
in the server — enabled per agent, so most agents appeared in the chat as raw
text, and wherever it ran, API clients and calling agents also got HTML instead
of Markdown (B57, 05.08.2026: twelve findings silently lost; the protection for
that is still in `core/agent_caller.py`).

## Web chat

`static/js/chat_module.js` (`formatContent`) draws every answer with
markdown-it (`static/vendor/markdown-it/`):

- **Raw HTML stays text** (`html: false`): an answer carries what a tool
  fetched. `<script>`, `<img onerror=…>`, `<b>` appear as characters. The only
  exception is `<br>`: in a table cell the only line break Markdown has, and
  models write it there (own inline rule).
- **No images**: a foreign image is a request to an address the text chose —
  that is how a prepared page exfiltrates data.
- **Links** only `http`, `https`, `mailto` or relative (without scheme, as with
  the sanitizer in the server); they open in a new tab
  (`rel="noopener noreferrer"`), not in place of the chat.
- **A single line break stays one** (`breaks: true`), as the model meant it.
- **JSON** (structured output, or a prompt demands JSON) appears as a code
  block, as it is.
- An answer that is entirely inside a ```` ```markdown ```` fence is the
  answer, not an example of one: the fence is dropped.
- Code blocks are colored by Prism (`language-<language>`).

While streaming, the chat shows the text raw; the finished answer
(`thinking_complete`, `final`, history) is drawn.

## Terminal

`agent-cli`, `agent-run` and the chat in the terminal output the answer through
`cli_utils/common.show_answer`, controlled by `--color`:

| `--color` | Output |
|---|---|
| `auto` (default) | Markdown with colors (Rich) if stdout is a terminal; otherwise like `text` |
| `always`, `ansi` | Markdown with colors, always |
| `html` | HTML from `utils/markdown_render.markdown_to_html` |
| `never`, `text` — and `auto` under `NO_COLOR` or `TERM=dumb` | the text as the model wrote it |

`always`/`ansi` write ANSI codes even into a pipe (on Windows also where Rich
would otherwise take the console API) and even under `NO_COLOR` or `TERM=dumb`:
an explicit `--color` wins. JSON is always printed as it is. A single line
break stays one here too, `<br>` likewise (even alone on a line: there are no
HTML blocks here, as in the chat), and other HTML remains as text — Rich alone
would swallow it, and with it placeholders like `--agent <name>`. Images and
links as in the chat: no image, links only `http`, `https`, `mailto` or
relative (a terminal makes them clickable); under `TERM=dumb` a link appears as
"text (URL)", an autolink only once if its text is its address.

## Server renderer

`utils/markdown_render.markdown_to_html` (Python-Markdown with its own
allowlist sanitizer) remains for the pages that render on the server: the
debate forum panel, the help viewer (AmigaGuide Markdown nodes) and
`--color html`.

## Tests

- `tests/ui/shell_tests.html` — "an answer is drawn from its Markdown…": what
  the model wrote appears; raw HTML, scripts, images and `javascript:` links do
  not; JSON stays JSON.
- `tests/app/test_app_sub_run_answers.py` — stream and `POST /run` deliver every
  answer as the model's text, including that of a sub-agent.
- `tests/agent/test_agent_structured_output.py` —
  `test_an_answer_leaves_the_run_as_the_model_wrote_it`.
- `tests/cli/test_cli_show_answer.py` — the output per `--color`.
