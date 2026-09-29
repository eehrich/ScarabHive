# Godot-Plugin und Gamedev-Agent — Konzept

Stand 2026-09-04. Die Entscheidungen hier sind gemessen, nicht vermutet; wo
eine Zahl steht, steht daneben, wie sie zustande kam.

## Was gebaut wurde

Drei Dinge, in zwei Plugin-Verzeichnissen:

| | Wo | Was |
|---|---|---|
| **Plugin `godot`** | `src/plugins/godot/` | Zwölf Tools über zwei Kanäle: das Godot-Binary headless, und ein laufender Editor über das gebündelte `godot_mcp`-Addon |
| **Agent `gamedev`** | `src/plugins/godot/agents/` | Erbt den Coder-Harness (`type: coder`), bekommt die Godot-Tools, einen Godot-Tester, Blender und den Bild-Agenten als Sub-Agenten, eigene Skills und ein eigenes Wissensbündel |
| **Agent `image_agent`** | `src/plugins/image_compose/agents/` | Texturen, Sprites, UI als PNG exakter Größe: ComfyUI erzeugt, `image_compose` komponiert, der Agent schaut hin |

Dazu `godot_agent` für Godot-Arbeit ohne Harness, das Gegenstück zu
`blender_agent`.

## Die Entscheidung: fertiger MCP-Server oder eigenes Plugin

Jeder „Godot-MCP" besteht aus zwei Teilen. Ein **Addon** in GDScript lebt
im Editor und macht die Arbeit: Szenenbaum, Nodes, Spiel starten, Eingaben,
Screenshots, Log. Ein **Server** daneben übersetzt MCP in das Protokoll des
Addons. Das Addon ist unverzichtbar, der Server ist ein Übersetzer.

Das ist dieselbe Lage wie beim Blender-Plugin, und die Antwort ist dieselbe:
das Addon übernehmen, den Server durch ein eigenes Plugin ersetzen. Gründe:

- **Das Protokoll ist drei Felder.** `{id, command, params}` als ein
  WebSocket-Textframe, zurück `{id, status, result|error}`. Gemessen gegen
  einen headless gestarteten Editor: Port nach 2,5 s offen, Handshake,
  Szenenbaum, Fehler-Envelope, Reconnect nach sauberem Close.
- **Kein Node-Prozess pro Agentenlauf.** Der Upstream-Server ist Node 20 und
  wird bei jedem Start gespawnt. `websockets` liegt schon im venv als
  direkte Abhängigkeit.
- **Der Headless-Kanal fehlt dem fertigen Server.** Parse-Check, Lauf ohne
  Editor, Export, Projekt anlegen: das kann keiner der Server, weil sie nur
  das Addon sprechen. Für einen Agenten ist es die Hälfte der Arbeit.
- **Zwölf Tools statt 21 mal 88 Aktionen**, plus `godot_command` als
  Escape-Hatch für den Rest.

### Welches Addon

Zwei Kandidaten blieben nach der Recherche übrig.

| | satelliteoflove/godot-mcp | hybridindie/godot-mcp |
|---|---|---|
| Richtung | Addon **lauscht** auf 127.0.0.1:6550, wir wählen pro Aufruf | Addon **wählt raus** zu ws://9080, unser Plugin müsste prozessweit lauschen |
| Playtest | Frozen-Time: `freeze`, `step` N Frames mit Inputs, `step_until` Prädikat | Input-Simulation, keine Zeitsteuerung |
| Godot | 4.5+, Addon 4.1.11, MIT | 4.4+, MIT |

Die Richtung entscheidet: API-Prozess und `agent-cli` können nicht beide
Port 9080 binden. Bei einem lauschenden Addon verbinden wir pro Aufruf wie
bei Blender, ohne Zustand im Plugin. Die Frozen-Time-Steuerung ist das
Zweite: ein Spiel, das zwischen zwei Tool-Aufrufen frei läuft, ist beim
nächsten Blick woanders. `godot_play run` startet deshalb eingefroren.

Das Addon liegt gepinnt unter `addon/godot_mcp` (Commit in
`addon/VENDORED.md`). Kein Submodule: es zöge Node-Server, Tests und Docs
mit, und das Addon muss ohnehin in jedes Spielprojekt kopiert werden.
`godot_setup` erledigt das.

### Was keines der Addons kann

Kein Addon führt GDScript **im Editor** aus; satelliteoflove kann es nur im
laufenden Spiel (`exec_run`). Und keines legt **Nodes an**, nur lesen,
ändern, umhängen. Beides deckt der Headless-Kanal: `godot_script` fährt ein
SceneTree-Skript mit der ganzen Engine-API gegen das Projekt, und neue Nodes
sind eine Textänderung an der `.tscn`. Der Editor liest die Datei nach
`godot_scene reload` neu. Diese Regel steht im Skill, nicht im Code.

## Der Headless-Kanal, gemessen

Mit Godot 4.7.2 auf dieser Maschine:

| Aufruf | Ergebnis |
|---|---|
| `--headless --check-only -s kaputt.gd` | Exit 1, `SCRIPT ERROR: Parse Error … (kaputt.gd:3)` auf stderr |
| SceneTree-Skript mit `quit(3)` | Exit 3, stdout und stderr sauber getrennt |
| Szene mit `push_error` in `_ready`, `--quit-after 5` | **Exit 0**, Fehlerblock auf stderr |
| `--headless --import` in einem Projekt mit aktiviertem Addon | Addon lädt, trägt `autoload/MCPGameBridge` in `project.godot` ein |

Die dritte Zeile ist die, die das Design bestimmt: `godot_run` traut dem
Exit-Code nicht. Es parst stderr in Blöcke (`ERROR:`, `SCRIPT ERROR:`,
`WARNING:` mit `at:` und Backtrace) und nimmt als Ort den ersten
Skript-Frame, nicht die C++-Datei, die `push_error` immer als `at:` meldet.
Ein Parse-Fehler erzeugt zwei Blöcke, der zweite (`Failed to load script`)
wird dedupliziert, damit ein Fehler als einer zählt.

Die vierte Zeile macht `godot_setup` vollständig ohne Editor: Projekt
anlegen, Addon kopieren, in `[editor_plugins]` eintragen, `--import`. Der
Test hält die Reihenfolge fest, denn ein Import vor dem Aktivieren
registriert nichts und meldet trotzdem Erfolg.

`godot_check` ohne Skript-Angabe fährt `scripts/check_scripts.gd`, das jedes
`.gd` unterhalb `res://` lädt (ohne `addons/` und `.godot/`). `load()` gibt
für ein kaputtes Skript **kein** `null` zurück, gemessen; der Zähler prüft
`can_instantiate()`. Ein `@abstract`-Skript besteht diese Prüfung auf 4.7.2
(gemessen, `can_instantiate() == true`), wird also nicht fälschlich gemeldet.
Fällt ein Skript ohne `SCRIPT ERROR`-Block durch, druckt der Walker
`FAILED res://x.gd`, und das Plugin macht daraus einen Fehler mit Datei.

Alles auf stderr, was kein Block ist, bleibt erhalten (`stderr` im Ergebnis):
ein abgestürztes Spiel schreibt seinen Backtrace ohne `ERROR:`-Präfix, und
`printerr()` ebenso. Ohne das wäre ein Crash „exit -1073741819, 0 errors" und
sonst nichts.

## Der Gamedev-Agent

`type: coder` erbt Sandbox, Shell, Modellkette, Explorer, Reviewer und 300
Schritte. Die Datei nennt nur, was ein Spiel ändert:

- `+godot/*`, `+media_ops/*` dazu.
- `!coder_sam/*` → `+gamedev_sam/*` mit `coder_explorer`, `coder_reviewer`,
  `gamedev_tester`, `blender_agent`, `image_agent`.
- `!coder_okf/*` → `+gamedev_okf/*`, Bündel `data/okf/gamedev`. Godot-Fakten
  gehören nicht in die Python-Konventionen des Coders.
- Skills: `gamedev-loop` immer; `godot-conventions`, `asset-pipeline` und
  die Coder-Skills on demand. Der Prompt ist Werkzeuge und Grenzen, das
  Wissen liegt in den Skills.

**Blender und der Bild-Agent sind Sub-Agenten, keine Tools.** Ihre Schleife
ist schauen, ändern, schauen; jeder Schritt trägt ein Bild. Die gehören in
ihren Kontext. Zurück kommt ein Pfad unter `data/workspace/`, den der
Gamedev ins Projekt verschiebt, importiert (`godot_import_assets`) und
referenziert. Der `asset-pipeline`-Skill enthält die Briefing-Tabellen: was
ein Modell-Auftrag nennen muss (Größe in Metern, Polygonbudget, Ursprung,
glb), was ein Bild-Auftrag nennen muss (Pixel, kachelbar, Hintergrund).

**Der Tester** ist `coder_tester` plus die Headless-Tools, ohne
Editor-Tools: zwei Fahrer auf einem Editor wären einer zu viel.

Die Abnahme-Zeilen im Bericht: `Ran:` / `Played:` / `Check:` / `Reviewed:`.
`Played` ist die für ein Spiel neue: gespielt im Editor, geschaut, oder
begründet warum headless reichte.

## Der Bild-Agent

`image_agent` ist der Cover-Artist des Writers ohne Bücher: ComfyUI
(`juggernaut_xl`, `sdxl_txt2img` auf dem 5090-Server) erzeugt, `images_render`
komponiert auf die exakte Leinwand, `media_ops_load` schaut. Der Skill
`image-assets` hält die Regeln, die ein generiertes Bild nicht von selbst
erfüllt: exakte Pixel (komponieren, nicht hoffen), Kachelbarkeit (2×2
rendern und auf die Naht schauen), und dass Alpha **gemacht** wird: ein
generiertes Bild ist opak, der Freisteller entsteht im Kompositions-Schritt
(`cutout: true` auf dem Bild-Layer, rembg mit `isnet-general-use`, für
Pixel-Art dazu `alpha_threshold`). Gemessen am 04.09.2026: eine gemalte
Figur mit Boot kommt sauber heraus (1,4 % weiche Pixel, 0,9 s auf der CPU),
`u2net` lässt das Boot halb durchsichtig; ein 80×110-Pixel-Sprite bekommt
einen dunklen Halo, den der Schwellwert entfernt. Dünne Strukturen
(Schnurrhaare) gehen verloren, und der Bericht sagt es.

`images` ist eine eigene `image_compose`-Instanz mit der neuen Option
`output_directories`: alle drei Schreibpfade eines Renders (Komposit,
Layer-Verzeichnis, Spec) müssen unter `data/workspace/images` liegen. Leer
bleibt die Option unbeschränkt, davon hängt die Cover-Pipeline ab; ein Test
prüft beide Richtungen.

## Ende-zu-Ende, gemessen

Gegen die Kopie des „Classic Shmup" in `data/workspace/shmup` (Godot 4.7.2):

| Schritt | Ergebnis |
|---|---|
| `godot_setup` | Addon 4.1.11 installiert, Plugin aktiviert, Autoload eingetragen, Import ohne Fehler |
| `godot_check` | 9 Skripte, keine Parse-Fehler; 16 Warnungen zu ungültigen UIDs, weil die Kopie ohne `.import`-Dateien angelegt wurde |
| `godot_run` 60 Frames | Verdict `ok`, 0 Fehler |
| Editor-Kanal | Port 4,5 s nach GUI-Start offen; `main.tscn` geöffnet, Baum 7 Nodes, `Player` 44 Properties, Editor-Screenshot 900×378 |
| `godot_play` | `run` eingefroren; `step frames=2`; `step 1500 ms` mit Aktion `left` 1200 ms, Report `Player.position = (8, 256)`; `exec` liest dieselbe Position; `step_until x < 0` läuft bis zum Limit 3000 ms, `predicate_met: false` (der Spieler ist bei x=8 geklemmt, korrekt) |
| `godot_observe` | Spiel-Screenshots 900×1200 vor und nach der Eingabe, das Schiff steht danach am linken Rand; Log mit Cursor 17 |

Zwei Dinge fielen erst hier auf und sind gefixt: Godots Leak-Meldung
„resources still in use at exit" zählte bei `godot_script` als Fehler
(jetzt eigenes Feld `exit_leaks`), und das Addon legt den Text eines
Engine-Fehlers ins Feld `type`, nicht `message` (die Log-Zeile liest beide).

## Review-Runde

Zwei Reviewer, read-only, über den fertigen Stand. 23 Befunde, alle am Code
verifiziert und gebaut; keiner fiel. Die Klasse, die zählte: **Erfolg ohne
Beleg**. Export meldete Erfolg, sobald sich die Zieldatei geändert hatte,
auch nach Timeout oder Exit 1 (Godot schreibt das PCK inkrementell). Setup
las den Import-Timeout nie. `_clip` gab ein Dict ohne Listen ungekürzt
zurück und behauptete `truncated: True`. Base64-Müll wurde vor der Prüfung
geschrieben und löschte so den vorigen Screenshot. `ConnectionClosed` entkam
roh, und `godot_status`, das Werkzeug für genau diesen Fall, warf durch. Bei
image_compose wurde `spec_path` erst nach dem Rendern konfiniert.

Dazu eine **stille falsche Antwort**: `observe state` schickte `specs`, das
`get_runtime_state` im Addon nie liest; die Antwort war ein plausibles
Paket über andere Nodes. Der Fake-Addon im Test antwortet jetzt jedem Befehl
mit einem Echo, sodass ein umbenannter oder falsch parametrisierter Befehl
sofort auffällt.

Der Docstring behauptete, per-Aufruf-Verbinden mache immun gegen die
45-s-Idle-Sperre des Addons. Falsch: die Uhr zählt ab dem letzten
*eingehenden* Paket, während ein Befehl läuft. Die spielseitigen Befehle
kappen sich selbst bei 28–30 s; `rescan_filesystem` (60 s) kann darüber
laufen und kommt dann als `STALE` an, nicht als roher Disconnect.

Alle neuen Riegel sind mutationsgeprüft (31 Mutationen, alle rot). Eine
Mutation blieb zunächst grün, weil der Stub beim Import *vor* dem
Autoload-Eintrag hing und das Setup schon daran scheiterte; der Stub hängt
jetzt danach, wie der echte Import auf einem großen Projekt.

## Offen

- **Freisteller auf der GPU.** rembg auf der CPU reicht für Sprites, und
  für dünne Strukturen liegt BiRefNet als Modellname bereit
  (`birefnet-general-lite`, gemessen 14 s statt 2,6 s, hält die
  Schnurrhaare). Auf die GPU käme es nur mit `rembg[gpu]` auf gpuhost1;
  das Erzeugen mit echtem Alpha (LayerDiffuse) bleibt ein eigener Knoten.
- **Editor-Screenshot headless.** Ohne Fenster liefert der Dummy-Renderer
  `CAPTURE_FAILED`; der Fehler kommt sauber an, nur das Bild fehlt. Für
  CI-Läufe ist `godot_run` der Weg.
- **Projekte unter `E:\Projects`.** Die Sandbox ist `data/workspace`; der
  Umzug ist ein Knopf (`projects_root` plus die Datei-Instanzen), keine
  Änderung am Code.
