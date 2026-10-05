# CLI-Architektur

Wie `agent-cli` und `agent-run` gebaut sind. Die Befehle selbst stehen in der
[CLI Reference](cli_reference.md); hier steht, was dahinter passiert und
welche Fallen der Code bereits kennt.

Stand: 2026-09-14 (Aufräumrunde: tote Befehle, Config-Schreiber und
Doppel-Implementierungen entfernt).

---

## 1. Einstiegspunkte

| Befehl | Modul | Zweck |
|--------|-------|-------|
| `agent-cli` | `src/agent_system/agent_cli.py:main` | Agent-Läufe (`run`, `chat`) und Inspektion (`plugins`, `mcp`, `hooks`), Benutzer (`users`), `reload` des Servers |
| `agent-run` | `src/agent_system/agent_run.py:main` | schlanker Einmal-Lauf mit dem Default-Agenten; teilt Session-Logik und Anhänge mit `agent-cli` |

Beide sind in `pyproject.toml` unter `[project.scripts]` eingetragen.

**Grundsatz: Die CLI schreibt keine Konfiguration.** Ein Plugin oder Tool-Server
wird eingeschaltet, indem man die YAML bearbeitet — `enabled` allein reicht
auch nicht, der Agent braucht die Tools in seiner Allowlist. Die früheren
Schreib-Befehle (`plugins enable`, `mcp enable`, `mcp tool allow/block`,
`mcp feature set`) sind entfernt; `allow/block` hatte `mcp_servers.yaml` per
`yaml.safe_dump` neu geschrieben und dabei alle Kommentare verloren.

---

## 2. Argumente parsen (`agent_cli.main`)

Drei Stufen, jede aus einem gemessenen Grund:

1. **Vorparser** (`parse_known_args`): holt die globalen Optionen (`--config`,
   `-v`, `--color`, `--no-color`, `--show-tools`, `--no-status`, `--raw`) von
   *überall* aus der Zeile und setzt sie vor das Subcommand. Ist das erste
   übrige Wort kein Subcommand, wird `run` eingefügt — `agent-cli "Frage"`
   funktioniert deshalb ohne `run`.
2. **`users` geht an Typer**, bevor der Hauptparser läuft
   (`cli_utils/users.py`) — mit den *ursprünglichen* Tokens hinter `users`,
   denn der Vorparser liest auch Optionswerte (`-p -vS3cret` kam als
   `-p -S3cret` an). Typer besitzt Argumente, Hilfe und Exit-Codes. Die
   frühere argparse-Kopie war auseinandergelaufen: Optionen landeten bei
   Befehlen, die sie nicht kennen (Traceback), `update USER EMAIL` verwarf die
   E-Mail, jeder Fehler endete mit 0.
3. **Hauptparser** mit Subparsern für den Rest.

**Falle — ein Flag nur an einer Stelle definieren.** Definiert ein Subparser
dieselbe `dest` wie der Elternparser, überschreibt sein *Default* den Wert des
Elternparsers (Python 3.12, gemessen). So las `plugins info --raw` früher
`False`, und ein `mcp --format json list` lieferte eine Tabelle. Deshalb:
`--raw` nur global, `--format` nur an den Aktionen.

**`--config` ohne Default.** Fehlt das Flag, bekommt `load_settings(None)` die
Wahl: `AGENT_CONFIG_PATH`, sonst `config/config.yaml`. Ein Default
`config/config.yaml` im Parser hatte die Umgebungsvariable für alle
Subcommands verdeckt.

---

## 3. Subcommands und was sie hochfahren

| Subcommand | Bootstrap | Hinweis |
|------------|-----------|---------|
| `plugins` | nur `discover_all_plugins` über `plugins.plugin_dirs` | `enabled` roh aus `plugins.servers` — wie `ToolServerIntegration` beim Registrieren; der Typ folgt der `type:`-Kette bis zum Plugin. Ein Plugin gilt als eingeschaltet, wenn eine seiner Instanzen es ist |
| `mcp` | `ToolServerIntegration.initialize` → Aktion → `shutdown` | nur lesend; eine Verbindung überlebt den Prozess nicht, darum kein `connect`/`disconnect` |
| `hooks` | `ToolServerIntegration.initialize` → Registry lesen → `shutdown` (~2 s plus Verbindungsaufbau externer Server) | Hooks registrieren sich beim Laden der Plugins; ohne das war die Registry immer leer. Scheitert `initialize` als Ganzes → Exit 1; ein einzelnes kaputtes Plugin fehlt (Fehler auf stderr), wie im Server. Keine Statistik: die liegt im Speicher des ausführenden Prozesses |
| `users` | nur die Benutzer-Datenbank (`auth.database_path`) | kein Login nötig, direkter DB-Zugriff |
| `reload` | nichts; `POST /admin/reload-config` am laufenden Server | Admin-Schlüssel nötig |
| `run`, `chat` | voll: Logging, `InitializationService.initialize_for_cli`, `initialize_tools`, `init_batch_system` | siehe 4 |

`mcp` nutzt `ToolServerService` (`list_servers`, `get_server_status`, `test_server`)
und `ToolService.list_tools`; beide Services bedient auch die API.

---

## 4. Ablauf von `run` und `chat`

In dieser Reihenfolge, alles in `main`:

1. **Logging** in eine rollenspezifische Datei (`logging.file_cli`, sonst
   `<logfile>-cli.log`), damit CLI und API nicht dieselbe Datei beschreiben.
   Ohne `-v` sieht die Konsole nur Warnungen.
2. **Bootstrap** (Registry, Session-Service, MCP, Batch-System).
3. **Session-Defaults**: wird `--session` fortgesetzt, gelten Agent und
   LLM-Profil, mit denen sie begonnen wurde — `--agent`/`--llm` schlagen sie
   (`cli_utils/session_defaults.py`).
4. **Einstiegs-Agent**: aus der Registry oder gebaut aus der *aufgelösten*
   Config (`get_tool_server_config`); der rohe Eintrag trüge Pydantic-Defaults
   statt geerbter Werte. Eine Fabrik für alle: `servers/agent/entry.py`
   (`entry_agent`) — die API, `/agent` im Chat und `create_and_register_agent`
   (agent-run, Writer-Audio) bauen dort. Ist der Name kein Agent → die Liste
   der Agenten, Exit 1.
5. `--max-steps` (Kopie der `agent_config`, nur dieser Prozess),
   `--list-sessions` (listet und endet, noch vor dem LLM-Override).
6. **LLM-Override** aus `--llm`/`--llm-params` — vor den Anhängen, damit die
   Fähigkeitsprüfung das tatsächlich genutzte Modell sieht. `--llm-params`
   selbst prüft schon der Parser (Exit 2, vor dem Bootstrap).
7. **Anhänge** (`cli_utils/attachments.py`): die Art kommt aus der Datei, nicht
   aus dem Flag. Nachricht und Fähigkeitsprüfung baut
   `message_with_attachments` (`utils/multimodal_processor.py`), dieselbe
   Stelle wie für API und Chat; Fehler → Exit 1.
8. **Session-Presence** (`core/session_presence.py`): die Session wird
   *gehalten, bevor* sie geladen wird. Belegt → Fehler (Exit 1), `--force`
   übergeht einen verwaisten Halt, `--woken` (vom Weck-Befehl gesetzt) tritt
   still zurück. Ctrl+C ist ein Stopp: Die Session wird markiert losgelassen, und
   nichts weckt sie danach von selbst (`session_locking.md` §5).

   Zwei Dinge musste `chat` dafür lernen. Erstens: **der Prompt wartet auf dem
   Event-Loop, nicht daneben.** `PromptSession.prompt()` ist synchron — es
   startet einen eigenen Loop und blockiert den Thread bis Enter, und alles,
   was auf dem Loop des REPL liegt, steht so lange still. Gemessen am
   20.09.2026: der Ein-Schritt-LLM-Call eines Sub-Agenten lag **vier Minuten**
   ungelesen da und wurde 0,3 s nach der ersten Nutzereingabe fertig — Tippen
   war das, was den Loop wieder drehte. Damit konnte `wake_when_done` im Chat
   gar nicht tragen: der Job, der die Marke setzt, war eingefroren, also
   erschien die Marke nie. `_PromptEditor._ask` fährt deshalb
   `prompt_async` unter `run_until_complete` (`cli_utils/chat.py`). Dasselbe
   gilt für `/edit`: der Editor läuft über `run_in_executor`, denn eine
   Nachricht in vim zu schreiben dauert Minuten, und genau dann hätte ein
   Hintergrund-Job am meisten Zeit. Nicht betroffen und weiterhin blockierend
   ist der Fallback-Leser `input()` — der umgeleitete Fall, in dem niemand vor
   dem Prompt sitzt; und `/copy`, wo `clip`/`xclip` Millisekunden brauchen und
   der Riegel teurer wäre als der Schaden.

   Zweitens: `chat` hält seine Session über den **ganzen** REPL, und darum muss
   er den Weckruf selbst abholen: die Marke (`<session>.pending`) wird sonst nur
   *innerhalb* eines Requests genommen (`_presence_step` bei jedem LLM-Call),
   und am Prompt läuft kein Call — ein mit `wake_when_done` fertig gewordener
   Sub-Agent läge da, bis der Nutzer zufällig etwas tippt. Solange der Prompt
   wartet, fragt deshalb ein Wächter-Thread (`_watch_for_wake` in
   `cli_utils/chat.py`) im halben Sekundentakt `presence.pending(...)` und
   schneidet die Eingabe ab; der REPL nimmt die Marke (damit ein Zug, der nie
   zu einem LLM-Call kommt, keine Endlosschleife auslöst) und startet einen
   Zug mit `WAKE_TASK`, so wie eine Eingabe es täte. **Nicht** abgeschnitten
   wird, was schon getippt ist — `exit()` wirft den Puffer weg — und nicht
   ohne Zeileneditor: `input()` lässt sich von keinem Thread unterbrechen, und
   das ist ohnehin der umgeleitete Fall, in dem niemand vor dem Prompt sitzt.
   Anhänge aus `/attach` gehen mit einem geweckten Zug nicht mit: sie gehören
   der Nachricht, die der Nutzer gerade schreibt.
9. **Session öffnen** über `SessionService.open_for_run`, wie `/run` und
   agent-run: eine gespeicherte wird wiederhergestellt, eine neue beginnt mit
   den `template_vars` der Agent-Config; darüber `--vars`.
10. **Lauf**: `chat` übergibt an `cli_utils/chat.py:run_chat_loop`. `--raw` und
    der Stream-Modus sammeln beide über `collect_final_result`. Der
    Stream-Modus zeigt dabei über `on_event` Tool-Aufrufe (`--show-tools`),
    das Denken (grau), Fehler (`ERROR:`) und die Antwort, sobald sie kommen —
    ein Ctrl+C landet meist außerhalb der Event-Loop, und nach dem Lauf
    erscheint dann nur noch die Abbruch-Zeile. Die Statuszeilen kommen über
    `status_bus`.
11. Session speichern (nicht bei Abbruch), im `finally` Halt freigeben und
    Batch-System und MCP herunterfahren.

**Ein Event-Loop für den ganzen Prozess** (`run_async`, `get_cli_loop`).
`asyncio.run` pro Schritt schloss den Loop danach — und mit ihm die Tasks der
externen MCP-Verbindungen aus dem Bootstrap. Der Agent bekam still null
externe Tools, während `mcp test` funktionierte (gemessen 2026-09-01). Der Chat
leiht sich denselben Loop; `close_cli_loop` räumt ihn bei Prozessende ab.

**stdout trägt das Ergebnis.** Meta-Zeilen („Session saved“, Warnungen)
gehen nach stderr. Statuszeilen und gestreamtes Denken stehen bei `agent-cli`
auf stdout; `--no-status` hält stdout sauber. `agent-run` schreibt Status nach
stderr.

---

## 5. `cli_utils/`

| Modul | Inhalt |
|-------|--------|
| `common.py` | Farbmodus (`set_color_mode`, `supports_color`), Windows-VT-Modus, Statuszeilen, `show_answer` (Antwort als Markdown mit Farben, roh in eine Pipe), `render_with_rich` |
| `chat.py` | die REPL: Renderer, Eingabe/Tastatur, Slash-Befehle, Usage-Summen |
| `session_defaults.py` | Agent/LLM einer fortgesetzten Session |
| `session_listing.py` | `--list-sessions` |
| `attachments.py` | Anhänge nach Dateiart sortieren |
| `agent_runner.py` | Agent-Erzeugung für `agent-run` |
| `users.py` | Typer-App für `agent-cli users` |
| `commands/hooks.py` | `hooks list` / `hooks inspect` |

Slash-Befehle des Chats liegen außerhalb: `agent_system/chat_commands.py`
(eingebaut) und `agent_system/plugin_commands.py` (von Plugins deklariert,
laufen immer über `dispatch_tool_call`).

---

## 6. Ausgabe

- `--color auto` (Default) schreibt ANSI nur, wo es gerendert wird;
  `always`/`ansi` erzwingen es, `never`/`text` schalten es ab, `html` für
  eingebettete Anzeige. Windows-Konsolen bekommen Encoding-Fehler ersetzt,
  Pipes UTF-8.
- `--format table|json` gibt es pro Subcommand (`plugins`, `mcp`-Aktionen,
  `hooks`, `reload`).
- Exit-Codes: 0 Erfolg, 1 Fehler (auch fachliche von `plugins`, `mcp`,
  `hooks`, `reload`, `users`), 2 falscher Aufruf. Fehler, die der Agent
  während eines Laufs meldet (`error`-Events), ändern den Code nicht. Die `_mcp_*`-Helfer und
  `handle_hooks_command` melden dafür Erfolg als `bool`.

---

## 7. Tests

`tests/cli/` — gezielt laufen lassen, die Gruppe dauert gut eine Minute.
Einstiege: `test_cli_subcommands.py` (hooks, users, entfernte Befehle),
`test_cli_mcp.py`, `test_cli_plugins_*.py`, `test_cli_event_loop.py`
(ein Loop), `test_cli_session_resume_end_to_end.py`, `test_cli_chat.py`.

---

## 8. Verwandte Dokumente

- [CLI Reference](cli_reference.md) — alle Befehle und Optionen
- [System Architecture](_arch_agent_system_architecture.md)
- [App Architecture](_arch_app_architecture.md) — die API, die `reload` anspricht
- [Plugin Architecture](_arch_plugin_architecture.md), [Plugin Hooks](plugin_hooks.md)
- [Session Management](session_management.md)
- [Tool server configuration](server_configuration.md)
