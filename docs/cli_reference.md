# Agent CLI Reference

Complete command-line interface reference for AgentSystem.

## Installation

After installing the package (`pip install -e .`), the `agent-cli` command is available globally:

```bash
agent-cli --help
```

## Global Options

Available for all commands:

```bash
agent-cli [OPTIONS] COMMAND [ARGS]...

Options:
  --config PATH              Path to config file (default: AGENT_CONFIG_PATH, else config/config.yaml)
  -v, --verbose             Enable verbose logging
  --color {auto,always,never}  Color output mode (default: auto)
  --no-color                Disable colored output
  --show-tools                Show tool calls and their results
  --no-status               Disable status event output
  --raw                     Output raw JSON (machine-readable)
  -h, --help                Show help message
```

## Commands Overview

| Command | Description |
|---------|-------------|
| `run` | Execute an agent task (default command) |
| `chat` | Interactive chat with an agent (stays in the session) |
| `plugins` | Inspect discovered plugins (read-only) |
| `mcp` | Inspect external MCP servers (read-only) |
| `hooks` | Inspect registered hooks |
| `users` | User management (direct database access) |
| `reload` | Reload the running server's config |

---

## `agent-cli run` - Execute Agent Tasks

Run an agent with a given prompt.

### Usage

```bash
agent-cli run [OPTIONS] PROMPT
agent-cli PROMPT                     # `run` may be left out

# Default agent (default_agent in config.yaml)
agent-cli run "What is the weather in Berlin?"

# Specify agent by name
agent-cli run "Check system status" --agent sysadmin_agent

# Override LLM profile
agent-cli run --llm turbo "Fast question about Python"

# Multimodal: the task comes FIRST -- --attach takes every path that
# follows it, so a task behind the flag is read as a file name
agent-cli run "What's in this image?" --attach screenshot.png
```

### Options

```bash
--agent TEXT                    Agent name to use (plugin or config-based)
--llm TEXT                      LLM profile to use (overrides agent's default)
--llm-params KEY=VALUE ...      Override LLM parameters for this run, e.g.
                                thinking_level=max max_tokens=16384
--attach PATH ...               File(s) to attach -- images, audio or text;
                                the kind is read from the file, like /attach
                                in the chat (--images/--audio/--text still
                                work and are sorted the same way). Put the
                                request FIRST (see above); behind the flag it
                                is read as one more path, and the CLI says so
                                instead of reporting a missing request
--max-steps N                   Step budget for this run (overrides the
                                agent's max_steps; this process only)
--session ID|TITEL              Continue an existing session -- ihre ID oder der
                                Titel, den ihr `/title` gegeben hat (unbekannt:
                                legt eine Session mit dieser ID an)
--session-title TEXT            Title for the new session
--list-sessions [COUNT|all]     List this user's sessions, one line each
                                (default 20, 0 = no limit; no sub-agent sessions).
                                Nur Agenten, die für den Chat gedacht sind
                                (visibility ui/both) -- dazu immer der Agent
                                dieses Aufrufs und die mit --session genannte
                                Session. Was Pipelines unter demselben User
                                gestartet haben, zählt eine Fußzeile; `all`
                                listet alles. `agent-run --list-sessions` liest
                                keine Config und listet deshalb immer alles.
                                A task that follows the flag is ignored, as
                                before -- the listing runs and nothing else
--vars KEY=VALUE ...            Template variables for the agent's prompt
```

These are global and come BEFORE the subcommand:

```bash
--no-status                     Disable status event streaming
--raw                           Output raw JSON instead of human-readable
--color auto|always|never|ansi|html|text
--config PATH                   Path to config
```

`--max-steps` changes the budget for THIS process only — nothing is written to
the YAML, and the agent keeps its configured value everywhere else. Without
the flag the budget comes from `max_steps` in the agent's YAML, as before.
It works on `chat` too, which shares the resolved agent.

### Examples

```bash
# Quick query with default agent
agent-cli run "What time is it in Tokyo?"

# Use a specialized agent
agent-cli run "Review this PR: https://github.com/..." --agent coder_reviewer

# Override the agent and its model for one task
agent-cli run --agent research_agent --llm or-gpt-full \
  "Research the latest AI developments in 2026"

# Vision task with image (task first -- see above)
agent-cli run "Explain this architecture diagram" --attach diagram.png

# Machine-readable output for scripting
agent-cli run --raw "List top 3 tech stocks" | jq '.summary'
```

---

## `agent-cli chat` - Interactive Chat

REPL mode: stay in the session and keep talking to the agent, like `ollama run`.
The session is saved after every turn and can be resumed later (`--session`).

```bash
# Chat with the default agent
agent-cli chat

# Chat with a specific agent and LLM profile
agent-cli chat --agent amiga_coder --llm deepseek-chat

# Send a first message immediately
agent-cli chat "Wie ist der Stand?" --agent sysadmin_agent

# Resume an earlier session (/sessions and /session print this line for you)
agent-cli chat --session a1b2c3d4 --agent amiga_coder

# List sessions without entering the chat
agent-cli chat --list-sessions
```

Nimmt dieselben Optionen wie `run`: `--agent`, `--llm`, `--llm-params`,
`--attach`, `--max-steps`, `--session`, `--session-user`, `--session-title`,
`--force`, `--list-sessions` und `--vars`, dazu die globalen `--color` und
`--no-status`. Im Chat heißt das:

- `--attach` hängt die Dateien an die erste Nachricht, wie `/attach` es tut,
  samt Prüfung, ob das Modell sie lesen kann. Ohne mitgegebene erste
  Nachricht warten sie auf die erste getippte.
- `--llm-params` gelten auch für jedes Profil, auf das `/model` wechselt.
- `--session-title` benennt nur die Session, mit der der Chat startet —
  nicht die nach `/new` oder `/resume`.
- `--raw` und `--show-tools` wirken im Chat nicht; den Tool-Verkehr zeigt
  `/last`.

**In-chat commands:**

| Command | Effect |
|---------|--------|
| `/exit`, `/quit`, `/q`, `/bye` | End the chat (Ctrl-D / Ctrl-Z+Enter work too) |
| `/new` | Start a fresh session (the current one stays saved) |
| `/session` | Show the current session and the command that resumes it |
| `/sessions [count\|all]` | List this user's sessions, one line each (default 20, `0` = no limit). Sub-agent sessions are left out — they outnumber the real ones ten to one. Ebenso die Läufe von Agenten, die nicht für den Chat gedacht sind (visibility weder `ui` noch `both`; gemessen 25.09.: 4562 von 5054, fast alles Pipeline-Bewerter) — der Agent dieses Chats und die laufende Session bleiben immer drin. Eine Fußzeile zählt den Rest, `/sessions all` zeigt ihn. Der Browser-Chat liest dieselbe Liste über `GET /api/sessions/listing?count=&agent=&current=` — Zählung, `all`, Filter und Fußzeile rechnet der Server mit denselben Funktionen wie das Terminal |
| `/resume [id\|titel]` | Continue an earlier session without leaving the chat; ohne Argument die letzte, die dieser Nutzer verlassen hat (im Browser die jüngste aus `/sessions` außer der offenen, von jedem Chat-Agenten: der Browser wechselt den Agenten mit der Session, das Terminal bleibt bei seinem). Statt der ID geht auch der Titel: IDs sind maschinell (`2332j2kj22k`) und lassen sich **nicht** umbenennen — sie sind der Schlüssel, unter dem Usage-Tracker, Message-Debugger, Kontext-Speicher, Sub-Session-Indizes und Presence-Locks ihre Zeilen führen. Mehrere Sessions mit demselben Titel: die zuletzt benutzte; ein Präfix reicht. Wie `--session <id>`: die Session läuft auf ihrem eigenen LLM weiter. Im Terminal wird eine Session eines anderen Agenten abgelehnt, mit dem Befehl, der sie fortsetzt (der Browser wechselt stattdessen den Agenten) — in diesem Chat liefe sie mit fremden Tools und fremdem Prompt, und das nächste Speichern schriebe diesen Agenten in ihren Datensatz. Dasselbe, wenn sich ihr LLM hier nicht starten lässt (fehlender Schlüssel): sonst liefe sie auf dem Profil dieses Chats, und das Speichern überschriebe ihre eigene Wahl |
| `/title [text]` | Der Session einen Namen geben — den, den `/sessions` zeigt, und unter dem `/resume` und `--session` sie wiederfinden; ohne Text zeigt es den aktuellen. Titel werden nur unter den Top-Level-Sessions gesucht (Sub-Agent-Titel sind ihr Auftragstext); eine ID erreicht jede Session. Tragen mehrere Sessions denselben Titel, nimmt `/resume` die jüngste und sagt, dass es noch andere gibt. Eine Session ohne ersten Turn hat noch keinen Datensatz; dort geht der Titel mit dem ersten Speichern mit |
| `/agent [name]` | Agent dieses Chats — ohne Argument listet es die Agenten der Konfiguration, mit Argument wird gewechselt. Der Wechsel startet **immer eine neue Session**: eine Session trägt den Agenten, mit dem sie lief, und unter einem anderen liefe sie mit fremden Tools und fremdem Prompt. Der neue Agent läuft auf seinem eigenen LLM, ein `/model` davor gilt für ihn nicht |
| `/vars [KEY=VALUE ...]` | Template variables of this session — bare lists them, `unset KEY` removes one, `clear` empties. The same variables `--vars` fills. A change reaches the agent on its next step and is written to the session file at once, so a removal survives `/resume` |
| `/model [profile]`, `/llm` | LLM dieser Session — ohne Argument listet es die Profile und markiert das laufende, mit Argument wird gewechselt. Gilt ab der nächsten Nachricht und wird sofort in die Session geschrieben, ein späteres `--session <id>` startet also darauf — auch wenn der Chat gleich danach endet. Eine Session ohne erste Nachricht hat noch keinen Datensatz; dort landet die Wahl mit dem ersten Speichern. `--llm-params` gehen mit |
| `/tools [filter]` | Tools the agent really has, grouped by server (optionally filtered) |
| `/skills` | Skill bundles it loads, `always` vs `on_demand` |
| `/costs` | Session cost so far **including sub-agents** (needs `context_usage_tracker`) |
| `/context`, `/ctx` | Was das Kontextfenster füllt. Zwei Blöcke, die nie vermischt werden: was der Anbieter beim **letzten Call gezählt** hat (aus `context_usage_tracker`, mit dem Fenster, gegen das er gezählt wurde — und dem Hinweis „stale", wenn seither kompaktiert wurde), und was das Gespräch **jetzt** enthält, geschätzt und nach Art aufgeschlüsselt: Tool-Ergebnisse, Antworten, deine Nachrichten, System-Prompt, Tool-Schemas. Größtes zuerst, denn das ist die Antwort auf „warum ist mein Fenster voll" — in einer langen Session sind es fast immer die Tool-Ergebnisse. Keine Kategorie wird als „Messung minus Schätzung" gerechnet: das sähe exakt aus und trüge den Fehler von beidem |
| `/history [n]` | Last `n` exchanges (default 6); tool traffic condensed to one line each |
| `/last` | The last turn's tool calls and results in full, formatted |
| `/copy` | Die letzte Antwort in die Zwischenablage — den **Text**, wie das Modell ihn geschrieben hat, nicht das, was das Terminal daraus gemacht hat (die Live-Region bricht auf die Fensterbreite um und kürzt Tool-Zeilen). Unter Windows über `clip` in UTF-16LE, sonst `wl-copy`, `xclip`, `xsel` in dieser Reihenfolge; eines, das installiert ist und trotzdem scheitert, hält das nächste nicht auf |
| `/undo` | Die letzte Frage und alles, was sie beantwortet hat, aus der Session nehmen. Der Datensatz wird sofort mitgeschnitten, sonst holt `--session <id>` den Turn zurück — auch dann, wenn die Session danach leer ist. Lässt sich der gekürzte Stand nicht schreiben, sagt der Chat es, statt den Turn als weg auszugeben |
| `/retry` | Dasselbe, und die Frage gleich noch einmal stellen — mit den Anhängen, mit denen sie gestellt wurde. Das Modell sieht seinen ersten Versuch dabei **nicht** mehr, genau darum wird geschnitten statt angehängt |
| `/export [path]` | Das Gespräch als Markdown schreiben: Fragen, Antworten, die Tool-Aufrufe und ihre Ergebnisse gekürzt (`/last` zeigt sie ganz). Ohne Pfad `chat-<session>.md` im aktuellen Verzeichnis; eine vorhandene Datei wird nie überschrieben |
| `/edit [text]` | Die nächste Nachricht in `$VISUAL`/`$EDITOR` schreiben (ohne beides: `notepad` bzw. `vi`), das Argument steht schon drin. Für das, wofür eine Prompt-Zeile die falsche Form hat — eine Spezifikation, ein eingefügter Diff mit einem Absatz drumherum. Eine leere Datei schickt nichts, und ein Editor, der mit einem Fehler endet, auch nicht: wer abbricht, will den Turn nicht bezahlen |
| `/help`, `/h`, `/?` | List the commands |
| ↑ / ↓ | Walk the input history; Ctrl-R searches it |
| Tab | Vervollständigt, was zur Zeile passt: am `/` die Kommandos, Plugin-Kommandos und Skills, hinter `/model` die Profile, hinter `/agent` die Agenten, hinter `/vars` die Variablen dieser Session, hinter `/attach` Pfade (auch mit Backslash). Hinter `/resume` die Sessions, die der Prozess schon gesehen hat — `/sessions` oder ein leeres `/resume` füllen die Liste. In einer Nachricht wird nichts angeboten |
| Ctrl-C | Cancel the **running turn**; twice at the prompt exits. Bricht auch ein laufendes Kommando ab (`/sessions`, `/resume`, `/vars`, `/tools`, ein Plugin-Kommando), ohne den Chat zu beenden; ein laufendes Speichern wird erst zu Ende gebracht, ein zweites Ctrl-C lässt es fallen. Nach Ctrl-C laufen vorgemerkte Zeilen nie als neue Turns — auch dann nicht, wenn die Antwort schneller war |

**Der Chat wacht von selbst auf.** Während er am Prompt wartet, läuft sein
Event-Loop weiter — ein im Hintergrund gestarteter Sub-Agent (`blocking=false`)
arbeitet also auch dann, wenn gerade niemand tippt. Trifft Eingabe für
seine Session ein — ein Sub-Agent, der mit `wake_when_done` fertig geworden ist —,
dann bricht er das Warten ab und startet den Zug selbst, statt darauf zu warten,
dass jemand zufällig etwas tippt. Was schon getippt ist, bleibt unangetastet;
der Weckruf wartet dann auf die nächste halbe Sekunde, und bis dahin hat die
eigene Zeile ohnehin einen Zug gestartet. Anhänge aus `/attach` gehen mit einem
geweckten Zug **nicht** mit: sie gehören der Nachricht, die gerade geschrieben
wird. Umgeleitete Eingabe (`… | agent-cli chat`) weckt nicht — dort gibt es
keinen Zeileneditor, den man unterbrechen könnte, und niemanden, der wartet.

Plugins add their own, listed under *Plugin commands* in `/help` — but only
those whose tool this agent may call, so the list differs per agent. They run
the plugin directly, without an LLM turn: `/compact` (context_engineer) shrinks
the conversation on the spot. Two plugins claiming the same name are both
reachable as `/<plugin>:<command>`. See `docs/plugin_commands_design.md`.

**In the browser** the same commands run, from the same catalogue and the same
parser — `/sessions`, `/resume`, `/tools`, `/costs`, `/history`, `/last`,
`/vars`, `/title`, `/agent`, `/undo`, `/retry`, `/export` und `/context`
answer from the API (`/agents/<name>/tools`, `/api/sessions`, `/chat/vars`,
`/chat/undo`, `/chat/transcript`, `/chat/context`) instead of from the local
agent. `/vars`
sends the line as typed, so the grammar is read by the same parser the
terminal uses; it needs a session, which in the browser exists from the first
message on. It reads the persisted variables merged with the live ones — the
browser can open a session the running process has never loaded, and listing
only the live half would report "none" for a session whose file is full, then
overwrite it.

Fünf sind terminal-eigen: `/exit` (kein Terminal zum Verlassen), `/attach`
(der Browser hat seinen Upload-Knopf), `/model` (der Browser wählt das Profil
in seinem eigenen Selektor), `/edit` (ein Textfeld IST schon ein Editor, und
`$EDITOR` läuft auf der Maschine, auf der die CLI läuft) und `/copy` (dort ist
die Antwort markierbarer Text, ein gescrolltes Terminal gibt sie gar nicht
mehr her). Alles andere gibt es in **beiden** Oberflächen — ein Kommando, das
der Nutzer im Terminal findet und im Browser nicht, liest sich wie ein Defekt.

Wo beide dasselbe tun, tun sie es auch durch dieselbe Stelle: der Schnitt von
`/undo` und `/retry` ist `chat_actions.split_off_last_exchange`, das Markdown
von `/export` ist `chat_actions.transcript_markdown`. Was sich unterscheidet,
ist nur, worauf sie angewandt werden: das Terminal kürzt die Nachrichtenliste
des Agenten und lässt das nächste Speichern folgen, der Browser lässt den
Server den **Datensatz** kürzen (`POST /chat/undo`) und lädt ihn neu — er zeigt
ja den Datensatz. Eine Session, in der gerade ein Lauf arbeitet, wird dabei
abgelehnt (409), nicht unter ihm weggeschnitten — im Browser `/undo force`,
für den Fall, dass das Schloss die Leiche eines abgestürzten Prozesses ist.

`/title` und `/agent` gehen im Browser durch die Widgets, die es schon hat —
die Session-Liste und den Agenten-Selektor —, damit das Kommando und der
Knopf daneben nicht auseinanderlaufen. `/export` lädt dort herunter statt zu
schreiben: einen Pfad auf der Platte des Servers kann der Browser nicht
meinen, und er sagt das, statt ihn still zu ignorieren. `/retry` legt die
Frage zurück ins Eingabefeld, statt sie sofort zu senden — eine Datei, die
mitging, liegt auf der Platte des Betrachters, und nur der kann sie erneut
anhängen.

Plugin commands work there as well, and stay per-agent: the browser asks
`/chat/commands?agent=<name>` for the list and `POST /chat/command` runs one.
Both resolve the command against what THAT agent may dispatch, so the browser
names a command and never a tool — a command whose tool the agent may not call
does not exist for it, exactly as in the terminal. Switching the agent in the
selector re-fetches the list.

**Eine Session bringt ihren Agenten und ihr LLM mit.** Beides steht in ihrem
Datensatz, und ein blankes `--session <id>` liest es zurück — die gleiche
Unterhaltung läuft also mit dem Agenten und dem Modell weiter, mit dem sie
begonnen wurde, statt mit den Config-Defaults. `--agent` und `--llm` schlagen
das weiterhin. **`agent-run` verhält sich identisch**, und das ist kein
Komfort, sondern Notwendigkeit: beide Einstiegspunkte *schreiben* denselben
Datensatz, und solange sie die Frage verschieden beantwortet haben, hat jeder
`agent-run`-Aufruf gelöscht, was `agent-cli` dort hinterlegt hatte. Die
Entscheidung liegt deshalb an genau einer Stelle
(`cli_utils/session_defaults.py`). Zwei Einschränkungen mit Grund: ein gespeichertes Profil wird
nur übernommen, wenn auch der Agent der gespeicherte ist (ein Profil, das für
einen anderen Agenten gewählt wurde, gehört nicht in dessen Kette), und wenn
es ohnehin der Default des Agenten ist, passiert nichts — ein zweiter Client
für denselben Wert wäre reine Arbeit.

**Eingabe-History.** Pfeil hoch holt zurück, was in *dieser Session* gefragt
wurde. Sie wird nirgends zusätzlich gespeichert: die Session selbst ist das
Protokoll, ihre User-Nachrichten sind die History. `/resume` und `/new`
tauschen sie deshalb mit aus, und beide Oberflächen zeigen dieselbe.

Zwei Dinge stehen bewusst nicht drin. **Slash-Kommandos** laufen im REPL und
erreichen die Session nie — sie sind bis zum Prozessende abrufbar, auch über
`/new` und `/resume` hinweg, danach
weg. Und **sehr lange Nachrichten** (über 2000 Zeichen) werden übersprungen:
ein `/skill`-Aufruf landet als vollständig *ausgepackter* Skill-Text in der
Session, und niemand will 30 kB SKILL.md über seinem Prompt haben. Eine
Nachricht, die mit `//` abgeschickt wurde, kommt auch wieder mit `//` zurück
— sonst würde Enter darauf das Kommando *ausführen* statt es zu senden.

Im Browser ist das Eingabefeld mehrzeilig, dort gehören die Pfeiltasten
zuerst dem Cursor: sie greifen erst dann auf die History zu, wenn der Cursor
sich nicht mehr bewegen *kann* — also am obersten bzw. untersten Rand des
Textes. Nach einem Rückruf steht der Cursor am **Anfang**, damit weiteres
Zurückblättern einen Tastendruck pro Schritt kostet; der erste Pfeil runter
gehört deshalb noch dem Cursor, erst der zweite geht wieder vorwärts. Nichts
geht dabei verloren: ein angefangener Entwurf kommt zurück, und was man in
einen zurückgeholten Eintrag hineinschreibt, bleibt beim Weiterblättern
erhalten. Escape bricht ab und stellt den Entwurf wieder her.

**Multi-line input.** A plain Enter sends the message, so pasting a block
needs one of:

```
"""
move.w  d0,d1
rts
"""
```

or a trailing backslash to continue on the next line. A message that has to
*start* with a command word is escaped with a doubled slash — `//new ...`
reaches the agent as `/new ...`; anything else beginning with `/` that is not
a known command — a path like `/etc/nginx/nginx.conf`, for instance — is sent
as an ordinary message. The escape only fires where it is needed: a pasted
`// TODO: fix` or `//192.168.1.1/share` keeps both slashes.

Während ein Turn läuft, geht eine getippte Zeile beim nächsten Schritt an den
Agenten. Es gilt dieselbe Regel wie am Prompt, aus derselben Funktion: eine
**einzelne** Zeile, die ein bekanntes Kommandowort ist, wird abgewiesen
(Kommandos gibt es nur am Prompt), alles andere ist eine Nachricht — ein
eingefügter Block also **eine** Nachricht, mit Einrückung und Leerzeilen, und
ein Pfad wie `/etc/nginx/nginx.conf` geht durch. In `/history` und `/last`
steht jede Nachricht, die der Agent bekommen hat, auch eine mit `//`
abgeschickte.

**Display:** tool activity is rendered like the WebUI front panel -- one line
per operation that updates in place and collapses into its `✓`/`✗` end state,
instead of a chronological log. Thinking tokens appear as a live counter
(`✻ Thinking… (~120 tokens · 4s)`), intermediate agent narration between tool
calls is shown dimmed, and the final answer is rendered as markdown. Each turn
ends with a dim usage footer (`↑1.2k ↓830 · $0.0213 · 3m41s`) and the session
total is printed on exit. On a non-ANSI terminal (or when piped) the display
falls back to plain chronological lines.

---

## `agent-cli plugins` — Plugins ansehen

Nur lesend. Ein Plugin wird in `config/plugins.yaml` eingeschaltet, nicht per
Befehl: `enabled: true` am Server-Eintrag, und damit ein Agent die Tools auch
bekommt, gehören sie in dessen Allowlist (`agent_config.tools.allowed`). Die
früheren Befehle `enable`/`disable`/`status` gibt es nicht mehr.

```bash
agent-cli plugins list [--format table|json] [--show-metadata]
agent-cli plugins info NAME [--format table|json]
agent-cli plugins search BEGRIFF
agent-cli --raw plugins info NAME --format json    # zusätzlich Factory-Details
```

`list` zeigt jeden Plugin-**Typ** und darunter seine Instanzen, sobald es mehr
als eine gibt. ENABLED heißt beim Typ: mindestens eine Instanz ist
eingeschaltet. Dieselbe Regel gilt für `info` — `writer_audio_ops` ist eine
Instanz vom Typ `audio_ops`, nicht ein eigener Typ. Nennt `type:` einen anderen
Server (`child: {type: base}`), zählt die Instanz zum Plugin am Ende dieser
Kette.

```
| NAME                              | ENABLED   | DESCRIPTION                                          | VERSION   |
|-----------------------------------|-----------|------------------------------------------------------|-----------|
| audio_ops                         | YES       | Audio file manipulation - cut, merge, mix, and co... | 1.1.0     |
| ├─ audio_ops                      | YES       | Audio file manipulation - cut segments from FLAC/... |           |
| ├─ writer_audio_ops               | YES       | Audio-Manipulation für Writer-System                 |           |
```

JSON-Form eines Eintrags (`instances` bleibt leer, solange es nur eine gibt):

```json
{"name": "audio_ops", "description": "...", "version": "1.1.0", "enabled": true,
 "instances": [{"instance_name": "audio_ops", "enabled": true, "description": "..."},
               {"instance_name": "writer_audio_ops", "enabled": true, "description": "..."}]}
```

`--show-metadata` hängt bei `--format json` die rohen Plugin-Metadaten an.

---

## `agent-cli mcp` — Externe MCP-Server ansehen

Nur lesend. Jeder Aufruf verbindet die in `config/mcp_servers.yaml`
eingeschalteten Server, erledigt die Aktion und trennt wieder — eine Verbindung
überlebt den Prozess nicht. Deshalb gibt es kein `connect`/`disconnect`; ob ein
Server erreichbar ist, beantwortet `test`. Gesperrte Tools stehen in
`mcp_servers.yaml` unter `tools.blocked` — nur diese Liste wirkt bei einem
externen Server; welche Tools ein Agent aufrufen darf, regelt seine Allowlist.
`tool allow/block`,
`enable`/`disable` und `feature` sind entfallen (`allow/block` hatte die Datei
neu geschrieben und dabei alle Kommentare gelöscht).

```bash
agent-cli mcp                                 # Hilfe
agent-cli mcp list [--format table|json]      # konfigurierte Server
agent-cli mcp status [SERVER] [--format ...]  # ohne SERVER wie list
agent-cli mcp test SERVER                     # Verbindung + Grundfunktion, JSON
agent-cli mcp tools SERVER [--format ...]     # Tools, gesperrte markiert
```

`--format` steht **hinter** der Aktion (`mcp list --format json`).

---

## `agent-cli hooks` — Hooks ansehen

Lädt die Plugins, damit sich ihre Hooks registrieren (etwa zwei Sekunden, dazu
der Verbindungsaufbau zu eingeschalteten externen MCP-Servern), und zeigt dann
die Registry. Scheitert das Laden als Ganzes, endet der Befehl mit Exit-Code
1. Ein einzelnes Plugin, das nicht lädt, fehlt in der Liste — wie im Server —
und steht als Fehler auf stderr.

```bash
agent-cli hooks list [--type pre_llm_call] [--format table|json]
agent-cli hooks inspect PLUGIN.HOOK                # alle Angaben als JSON
```

ENABLED ist der Stand der Registry nach `schema.yaml`, `hooks.overrides` und
dem `hook_config` der Instanz — nicht die Überschreibung einzelner Agenten.
`hooks.overrides` kommt aus der geladenen Config (`--config`).
Ausführungsstatistiken gibt es hier nicht: sie liegen im Speicher des
Prozesses, der die Hooks ausführt (API-Server), den ein CLI-Aufruf nie sieht.

---

## `agent-cli users` — Benutzer verwalten

Arbeitet direkt auf der Benutzer-Datenbank (`auth.database_path`, Default
`data/users.db`) und braucht deshalb kein Login. Hilfe mit
`agent-cli users -h` bzw. `agent-cli users BEFEHL -h`. Ohne Befehl läuft
`list` (auch `users --limit 5`). Fehler enden mit Exit-Code 1. Globale Optionen
wie `--config` stehen **vor** `users`.

```bash
agent-cli users list [--limit 100] [--skip 0]
agent-cli users info USERNAME
agent-cli users create USERNAME EMAIL [-p PASSWORT] [-n "Voller Name"] [-r user|admin|guest] [--admin] [--inactive]
agent-cli users update USERNAME [-e EMAIL] [-n NAME] [-p PASSWORT] [-r ROLLE] [--activate | --deactivate]
agent-cli users delete USERNAME [-f]
agent-cli users generate-api-key USERNAME
agent-cli users revoke-api-key USERNAME [-f]
```

Ohne `-p` fragt `create` das Passwort ab; `-f` überspringt die Rückfrage.

---

## `agent-cli reload` — Config des laufenden Servers neu laden

```bash
agent-cli reload [--url http://127.0.0.1:8000] [--api-key KEY] [--format table|json] [--timeout 30]
```

Ruft `POST /admin/reload-config` am laufenden Server auf (kein Neustart). URL
und Schlüssel kommen sonst aus `AGENT_SERVER_URL` bzw. `AGENT_ADMIN_API_KEY` /
`AGENT_API_KEY`; der Schlüssel muss einem Admin gehören.

---

## Umgebungsvariablen

| Variable | Wirkung |
|----------|---------|
| `AGENT_CONFIG_PATH` | Config-Datei, wenn `--config` fehlt (sonst `config/config.yaml`) |
| `AGENT_SERVER_URL` | Server für `reload` |
| `AGENT_ADMIN_API_KEY`, `AGENT_API_KEY` | Schlüssel für `reload` |
| `NO_COLOR` | keine Farben |

API-Schlüssel der LLM-Anbieter stehen in `config/secrets.env` neben der Config.

---

## Exit-Codes

| Code | Bedeutung |
|------|-----------|
| 0 | Erfolg |
| 1 | Fehler: unbekanntes Plugin/Hook/Server, `mcp test` gescheitert, `reload` ohne Erfolg (auch ein einzelner Server), LLM-Profil unbekannt, Anhang unlesbar, Session belegt oder nicht ladbar, `users`-Befehl gescheitert |
| 2 | Aufruf falsch: unbekannter Befehl oder Parameter, ungültige `--llm-params` |

Fehler, die der **Agent** während eines Laufs meldet (`ERROR:`-Zeilen), ändern
den Exit-Code nicht.

---

## Konfiguration

- `config/config.yaml` — u.a. `default_agent` (Agent ohne `--agent`),
  `logging.file_cli` (Logdatei der CLI; ohne den Schlüssel `<logging.file>-cli.log`,
  also `logs/agent-cli.log`).
- `config/plugins.yaml` und die per `includes` geladenen Dateien — Plugins und
  Agenten unter `plugins.servers`; ein Agent ist ein Server-Eintrag mit
  `agent_config`. Siehe [Configuration-Based Agents](config_based_agents.md).
- `config/mcp_servers.yaml` — externe MCP-Server unter
  `external_servers.remote_servers`. Siehe [Tool server configuration](server_configuration.md).

---

## Fehlersuche

**„Agent not found“** — die Fehlermeldung listet die verfügbaren Agenten; der
Name gehört hinter `--agent`, nicht als erstes Wort des Tasks.

**ANSI-Codes in umgeleiteter Ausgabe** — `--no-color` bzw. `NO_COLOR=1`. Bei
`--color auto` (Default) entstehen in Pipes keine Escape-Sequenzen.

**Ein Tool fehlt dem Agenten** — Plugin in `plugins.yaml` eingeschaltet
(`plugins list`)? Tool in der Allowlist des Agenten? Bei externen Servern:
`mcp test SERVER` und `mcp tools SERVER`.

---

## Further Reading

- [Configuration-Based Agents](config_based_agents.md) - Deep dive into agent definitions
- [Tool server configuration](server_configuration.md) - External server setup
- [Plugin Authoring](plugin_authoring.md) - Create custom plugins
- [Authentication Guide](multi_user_authentication.md) - Security and user management
- [Context Management](context_management.md) - Token budget strategies

---

## Kurzreferenz

```bash
agent-cli "Aufgabe"                             # run mit dem Default-Agenten
agent-cli run "Aufgabe" --agent NAME --llm PROFIL
agent-cli chat --agent NAME                     # interaktiv
agent-cli run --list-sessions                   # Sessions dieses Users

agent-cli plugins list | info NAME | search BEGRIFF
agent-cli mcp list | status [SERVER] | test SERVER | tools SERVER
agent-cli hooks list | inspect NAME
agent-cli users list | info | create | update | delete | generate-api-key | revoke-api-key
agent-cli reload

agent-cli --verbose run "Aufgabe"               # Fortschritt
agent-cli --show-tools run "Aufgabe"              # Tool-Aufrufe im Detail
agent-cli --raw run "Aufgabe"                   # Ergebnis als JSON
```
