# stategraph: Backlog aus der Review vom 27.09.2026

Sechs Prüfer, je eine Richtung: Laufzeit-Bugs, Modell/Panel-Bugs, fehlende Features,
Bedienung, Debug-Fähigkeit, Nutzung durch Nutzer und Agent. Die Bug-Befunde sind per Skript
belegt (Skripte im Scratchpad der Session, `review_a/`, `review_b/`, `review_e/`) oder am Code
nachgesehen. Der Nutzer hat alles freigegeben ("alle Sachen angehen").

Stand Phase 1: gebaut, Review der Fix-Runde eingearbeitet (Abbruch über den Aufrufer schützt jetzt auch
`finally`/`close` -- `CancellationManager.protect`; Steps pro Frame; eine Watchpoint-Pause beantwortet ein offenes
„pause“; Breakpoints/`run_to` auf States ohne diesen Hook: 422; das Panel nimmt Breakpoints beim Umbenennen mit und
verwirft veraltete beim Start; die Fassaden-Prüfung läuft in einem Worker-Thread; `expected_versions` als
JSON-String). Offene Lücke, benannt: dass das Panel `keepingChoices` aufruft, sieht das Fake-DOM nicht -- der
Helfer selbst ist im Browser getestet.

Arbeitsweise: Phase für Phase. Je Phase: bauen, jeder neue Test mit Mutation geprüft,
adversarielles Review des Diffs, Befunde fixen, pfadbegrenzt committen. Ein erledigter Punkt
bekommt `[x]` und den Commit.

## Phase 1: Bugs und Lücken

### Laufzeit und Fassade

- [x] **R1 Terminate schneidet ein laufendes `finally`/`close` ab.** `server.py` `_cancel_requests` bricht
  jeden `<run>_NNN`-Token ab, auch den des Aufräum-Agenten, der gerade läuft (§3.10 verspricht das
  Gegenteil). Fix: laufende finally/close-Request-IDs im RunContext merken und auslassen.
- [x] **R2 `step` in parallelen Frames geht verloren.** `engine/debugger.py` `pause()` setzt `mode = "run"`,
  sobald irgendein Frame hält; der Step des einen Frames wird vom anderen verbraucht. Fix: Step an den
  Frame binden, der ihn bekam.
- [x] **R3 `run_key` ist nicht an Maschine, `mock_only` und Params gebunden.** `service.start_run` /
  `journal.latest_by_key`: ein Mock-Lauf mit Key k beantwortet später einen Live-Lauf mit k. Fix: bei
  abweichender Maschine, mock_only oder Params-Hash 409 "neuen Key nehmen".
- [x] **R4 Tool-Argumente ungeprüft.** `params` als JSON-String gibt eine Unsinnsmeldung, `mocks` als
  String hinterlässt eine `running`-Zeile (create_run vor der Prüfung), `max_wait: "abc"` und `files`
  als String laufen an `_run_tool` vorbei. Fix: Typen/Bereiche in den Tool-Bodies prüfen, JSON-Strings
  parsen, Mocks vor create_run prüfen.
- [x] **R5 Debug-Namen ungeprüft.** Breakpoints, `run_to` und Mock-Pfade werden nicht gegen die Maschine
  geprüft; ein Mock-Tippfehler lässt still den echten Agenten laufen. Fix: State-Namen prüfen (422),
  am Laufende `unused_mocks` melden.
- [x] **R6 Fassade: fremde Session-ID liefert fremden Lauf.** `facade.py` Continue-Pfad prüft `sees_run`
  nicht. Fix: vor der Antwort `sees_run` prüfen.
- [x] **R7 Fassade: dieselbe Anfrage in derselben Session nach transientem Fehler abgelehnt.** Fix: ist
  `row.run_key == <agent>:<request id>`, über `_start` gehen.
- [x] **R8 Fassaden-Konfig erst beim ersten Aufruf geprüft.** Fix: beim Start prüfen (Maschine existiert
  und validiert, `task_param` deklariert, Pflicht-Params gedeckt), Log-Error.

### Modell und Panel

- [x] **M1 Event-Auswahl springt beim Polling zurück** (`panel.js` drawDebugPane): gewählter Wert wird
  beim Neuzeichnen verloren, "Send" schickt das falsche Event. Gleiches für eventFrame, runToState,
  forkStep. Fix: Auswahl erhalten.
- [x] **M2 Transition-Formular schickt alle Felder**; ein mehrzeiliger Guard wird im `<input>` zu einer
  Zeile. Fix: über `field()`/`changedFields`, mehrzeilig als Textarea.
- [x] **M3 Zahlenfeld mit Tippfehler löscht still** (im Browser liefert `type=number` dann `""`).
  Fix: `validity.badInput` → FormError.
- [x] **M4 Flow-Stil `states: {x: …}`**: YAML-Abschnitt zeigt die Geschwister-Map, Apply schachtelt sie
  in den State. Fix: Fragment sperren, wenn der State nicht am Zeilenanfang steht; Server verweigert.
- [—] **M5 Skalar-Anker nicht gesperrt** — WIDERLEGT im Review der Fix-Runde: ohne Sperre ändert ein Edit nur
  den eigenen State; ruamel verlegt den Anker auf den Alias, dessen Wert bleibt gleich (Probe `probe_m5.py`). Die
  Sperre hätte nur einen harmlosen Weg genommen; zurückgebaut.
- [x] **M6 `graph_view` umgeht `yaml_bounds`** (Alias-Bombe blockiert die API). Fix: bei `doc is None`
  leerer Graph, Grenzen auch auf den Re-Parse.
- [x] **M7 Validator übersieht State mit `do` ohne Completion-Transition** (sicher `no_transition`).
  Gebaut als Warnung SG109, nicht als Fehler: eine Aktivität, die nur über ihre Fehler-Transitionen weiterführt,
  ist eine Maschine, die läuft (die Semantik-Tests fahren genau solche).
- [x] **M8 Reservierte Namen `finally`/`resources`** als State-Namen erlaubt. Fix: `check_state_name` in
  `_new_name`, dieselbe Liste im Panel.
- [x] **M9 YAML-Datum als Param-Default** validiert, scheitert zur Laufzeit. Fix: nicht-JSON-Skalare im
  Loader als SG001.
- [x] **M10 Windows-Pfade mit Backslash** in `files` (store.relative): Problem-Sprung in Unterordner
  scheitert. Fix: `relative()` liefert `/`.

### Doku

- [x] **D1 `get_run`-Felder** in `debugging.md`/Design §8.1 falsch (`status` statt `run_status`,
  `view.frames` statt `frames`); SKILL-Zeile zu SG005 (enum-Param ist erlaubt).
- [x] **D2 Fassaden-README ohne `metadata.visibility`** → privat, für SAM und Chat unsichtbar.
- [x] **D3 403 beim Speichern einer mitgelieferten Maschine** ohne Hinweis "unter neuer id speichern".

## Phase 2: Debug-Fähigkeit

Stand: gebaut, Review der Fix-Runde eingearbeitet (Traceback behält sein Ende; ein angehaltener Fork hält am
Fork-Punkt, nicht an einem State ohne `do` davor; `waiting_since` einmal pro Eintritt, über Resume und in der
gespeicherten Ansicht; `failures` überleben einen Absturz im Backoff; Guards auch bei Initial-Choice und
verworfenem Event; `full_output` und ein Gesamtdeckel von 200.000 Zeichen für `get_run`; ein Toast beim Kopieren).
N3 (gedeckelte Antworten) ist hier mit erledigt.

- [x] **G1 Gerenderte Eingabe bleibt im Journal** (Task, Tool-Args, Call-Args, Decide-Input) — heute
  überschreibt die End-Zeile `inputs`. Anzeige im Panel.
- [x] **G2 Guard-Auswertung** `[{owner, index, guard, result|error}]` in die Transition-Trace und in
  `error.data` von `no_transition`.
- [x] **G3 Traceback** (gekürzt, Frames im Companion-Modul) in `error.data.traceback`; `logger.warning`
  für activity_failed/agent_failed/internal.
- [x] **G4 Fehlgeschlagene Retry-Versuche** in `meta.failures`.
- [x] **G5 Fork mit `pause` und `mocks`** (RunManager.fork kann es schon; Service/Tool/Panel reichen es
  durch); `pause_at_start` auch am Tool `run_machine`.
- [x] **G6 `get_run` mit `after`/`kinds`/`key`** und Kappung langer Felder; Panel-History mit Zeitstempel
  und Filter.
- [x] **G7 Warten sichtbar**: `waiting_since`, `deadline` in der Frame-View, `inbox` in der Tool-Antwort.
- [x] **G8 Panel zeigt `error.data`/`cause`** bei Aktivitätsfehlern; Request-ID kopierbar.

## Phase 3: Nutzung durch Nutzer und Agent

Stand: gebaut; Zwischencommit 8b016ff6b, danach die Befunde des Reviews: ein abgebrochenes `get_run` beendet
den Lauf nicht mehr und wartet auch auf Läufe anderer Prozesse; eine Antwort nach Neustart (oder aus agent-cli,
ein Prozess je Nachricht) setzt den Lauf fort und beantwortet seinen Warte-State; als Tool eines anderen Agents
blockiert `ask`; Frames, die dasselbe Event nehmen, werden genannt und per `frame` gewählt; ein vom Guard
verworfenes Event wird mit den Guards gemeldet; Daten nach dem Event-Namen bleiben ganz; `key=value` liest nach
dem deklarierten Typ und behält Backslashes; ungültiges `on_wait` zeigt auch das Panel.

- [x] **N1 Tool `stategraph_list_runs`** (read-only, per `sees_run` gefiltert); Slash-Befehle
  `/stategraph-runs`, `/stategraph-stop`.
- [x] **N2 Weiterwarten**: `wait`/`max_wait` an `get_run`; Antwort bei `running` sagt, wie es weitergeht;
  `max_wait` gedeckelt.
- [x] **N3 Ergebnisse gedeckelt**: `out`/ctx-Werte in Tool-Antworten gekappt, mit Längenangabe.
- [x] **N4 `/stategraph-run` mit Params** (`<id> {json}`).
- [x] **N5 `catalog` liefert echte Tools** mit Beschreibung und Parametern statt Allowlist-Mustern.
- [x] **N6 Fassade: Warte-State im Gespräch** — wartet der Lauf auf ein Event, beendet die Fassade die
  Runde mit der Frage (Beschreibung, erlaubte Events, Schema); die nächste Nachricht in der Session wird
  das Event. `on_wait: ask | block` (v6_story_machine bleibt `block`, writer_jobs zählt jede Antwort als
  Erfolg).
- [x] **N7 Freigabe als Agent in der Maschinendatei** (`agent:`-Block) statt eigener Config — erst prüfen,
  ob die Registry Plugin-Agenten zur Laufzeit annimmt; sonst Knopf, der den YAML-Eintrag zeigt.
  Geprüft: sie nimmt keine an (neue Agents beim Reload sind ausdrücklich nicht unterstützt; dafür bräuchte der
  Kern ein `Runtime.declare(name, ToolServerConfig)` samt Materialisierung -- Entscheidung des Nutzers, anderes
  Ressort). Gebaut ist der Ausweg: das Panel zeigt die Agents einer Maschine, ihre Probleme und einen Eintrag
  zum Kopieren. Nachtrag 28.09. (Nutzer: bauen): `agent:`-Block in der Maschinendatei; der Kern bekam
  `Runtime.declare` und fragt `offered_servers` jeder Plugin-Factory in jedem Prozess vor dem Bau ab
  (angekündigt im comm.txt); ein neuer Block zählt ab dem nächsten Start, das Panel sagt es. Standard
  `visibility: private` (sonst böte jede SAM mit `allowed_agents: ['*']` eine frisch gespeicherte Maschine
  ihrem LLM an); ein Config-Reload behält die angebotenen Agents. Probleme des Blocks sind Warnungen (SG111):
  die Maschine selbst läuft weiter.
- [x] **N8 Params in der Fassaden-Beschreibung**, `input: json` nimmt ein Objekt.

## Phase 4: Bedienung

Stand: gebaut, Review eingearbeitet (Duplizieren schreibt keine importierten Dateien in die beschreibbare Wurzel,
sondern verweist per Maschinen-id, und ersetzt die ganze `python:`-Zeile; Undo/Redo einer nach dem anderen, auch
per Taste; ein Formular bekommt sein Getipptes nur zurück, wenn es noch dasselbe zeigt; ein umbenannter State bleibt
gezeigt; „Neues Event…“ deklariert und wählt aus, statt das Formular zu übergehen; `events:` ohne Wert; Löschen
vergisst Params und Undo-Schritte; ein Maschinenwechsel beginnt die Runs-Liste neu). Redo auf Wunsch des Nutzers.

- [x] **U1 Mehrere Apply-Formulare** eines States: Änderungen in einem anderen Formular überleben das
  Apply (oder "Apply all").
- [x] **U2 Agent/Tool/`by`/Profil/Maschine als Auswahl** (`<datalist>` aus dem Katalog, neue Route);
  Feldbeschreibungen sichtbar statt nur im Tooltip.
- [x] **U3 Undo** für Graph-Edits (letzten Dateitext zurückschreiben); Auto-Layout mit Rückfrage.
- [x] **U4 Warte-State beantworten**: Knöpfe der angenommenen Events in der Debug-Leiste, Event
  vorausgewählt, Beschreibung sichtbar.
- [x] **U5 Runs-Tab**: Result-Karte sichtbar (über der Liste oder Liste im Scroller); ältere Läufe
  (Cursor), Status-Filter.
- [x] **U6 Params/Events**: Settings-Formular der Maschine oben, doppelte Tabellen weg, Form als Hilfe;
  "Neues Event…" im Trigger-Select.
- [x] **U7 State-Suche** in der Graph-Leiste; Mausrad schwenkt, Strg+Rad zoomt.
- [x] **U8 Duplizieren** einer (auch mitgelieferten) Maschine unter neuer id.
- [x] **U9 Schmale Breite**: Maschinen-Liste klappt ein, sobald eine Maschine offen ist; Palette als Menü.
- [x] **U10 "Nochmal mit diesen Eingaben"** auf der Result-Karte; Params pro Maschine gemerkt.
- [x] **U11 Namensdialoge** behalten die Eingabe beim Fehler; unveränderter Name ist kein Fehler.
- [x] **U12 Fehler-Badge klickbar**; Übersicht zeigt alle Probleme.

## Phase 5: Features

Stand: gebaut (Tests in test_plugin_stategraph_backlog_features.py). F1: die End-Reihenfolge einer Join-Politik
schreibt der Join selbst ins Journal (`<kind>:joined`), weil eine Aktivitätszeile die seq ihres Starts behält.
F8: die Route `/plugins/<instanz>/callback*` liegt unter der Admin-Regel des Plugins -- öffentlich wird sie erst,
wenn der Nutzer sie in BEIDEN Schichten öffnet, `auth.endpoint_security` und `auth.plugin_security` (seine
Sicherheitsentscheidung; Beispiel in der README unter Security). Der Token steht in der Query
(`/callback?token=...`), weil `network.remote_paths` nur exakte Pfade durchlässt -- ein Token im Pfad wäre von
einem anderen Rechner aus nie erreichbar; URLs mit dem Token im Pfad (vor dem Umzug ausgegeben) gelten weiter.
Die Logs maskieren den Token in beiden Formen (Kern, `loggable_path`: 7378256f5 fuer den Pfad, die Query-Form mit
diesem Umzug, angekuendigt im comm.txt).
F9: ein Slot läuft nach transientem Fehlschlag höchstens dreimal, fünf Minuten nach dem letzten Ende; die Lease gilt pro
Instanz und wird beim Stop freigegeben.

- [x] **F1 Join-Politik** `join: first | {count: n}` für `parallel`, `until:` für `map`.
- [x] **F2 Prüf-Funktion in der Agent-Aktivität** (`check:` mit `sg`, darf async sein, Feedback an dieselbe
  Instanz).
- [x] **F3 Lokale Submaschinen** in derselben Datei (`machines:`), teilen das Companion-Modul.
- [x] **F4 Timer-State** `after: 10m` (Ablauf = Completion).
- [x] **F5 `limits.concurrency`** für Blatt-Aktivitäten eines Laufs.
- [x] **F6 `emit`**: Zwischenstand an Aufrufer und Lauf-Session.
- [x] **F7 Operator-Reparatur**: `set` auf `out` am exit-/error-Breakpoint.
- [x] **F8 Callback-URL pro Event** (einmalig, auf Lauf und Event begrenzt) — braucht eigenes
  Sicherheitsreview.
- [x] **F9 Zeitpläne** (`schedules:` in der Plugin-Config, `run_key` pro Slot).

## Nachträge

- [x] **W1 Fork-Hook sieht den ctx am Fork-Punkt** (aus dem v6-Review der Writer-Session, Nutzer 28.09.: bauen):
  die Ressourcen der Wurzel eines Forks öffnen am Fork-Punkt, nicht am Start; davor lesen sie den Wert der Quelle
  (Hashes: ein Token), danach tauscht ctx jeden Wert der Quelle -- und der Läufe davor -- gegen den des Forks, vars
  neu. Journalzeile `fork_resources` {sources, chain [{values, until}], at}: ein Fork eines Forks spielt jeden
  Schritt mit den Werten, die sein Lauf dort hatte. Der Punkt liegt vor der Arbeit des Schritts (auch vor dem
  finally des verlassenen States) und nie hinter dem Journal der Quelle; eine Fork-Wurzel ohne Live-Teil
  (vor dem Punkt geendet, oder ihre Fork-Hooks scheiterten) führt kein finally aus -- die Quelle hat es schon --
  und schließt nur, was sie selbst geöffnet hat; ein Fork bei Schritt 0 öffnet am Start. Nebenbei: ein Fork eines Forks divergierte am HEAD, wenn ctx einen Wert der
  Ur-Quelle aus einem Output trug (gemessen).

## Für andere Ressorts

- Writer (`writer_pipeline_v6/machines/v6_story.yaml` `chapter_plan`): jede Runde startet einen neuen
  Planer ohne `continue`, der Task sagt aber "Dein letzter Plan wurde nicht übernommen" — der neue
  kennt den Plan nicht. Gemeldet über `tmp/comm.txt`.
