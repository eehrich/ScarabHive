# Session-Archiv

Alte Unterhaltungen ziehen aus `data/sessions/` in ZIP-Archive um — ganze
Bäume auf einmal, und sie kommen genauso wieder zurück.

## Warum

`data/sessions/` wuchs ohne Grenze. Ein Buchlauf hinterlässt eine
Root-Session und mehrere hundert Sub-Agent-Sessions, und nichts hat sie je
weggeräumt. Gemessen am 20.09.2026 auf dieser Maschine:

| | Dateien | Größe | älter als 30 Tage |
|---|---|---|---|
| Sub-Agent-Sessions | 59.970 | 5,10 GB | 59.960 |
| Root-Sessions | 227 | 0,05 GB | 202 |
| Sub-Indizes (`.subs.*`) | 1.304 | 0,05 GB | — |

Das zahlt jeder, der den Store durchläuft: ein Verzeichnis-Listing, der
`iterdir` in `delete_session`, ein Index-Rebuild. Der teuerste Fall war
gemessen **7 min 31 s** — so lange hing `create_session` beim ersten
Sub-Agenten eines Requests, weil ein fehlender Sub-Index als „Index verloren"
gelesen wurde und alle 60k Dateien las (gefixt, siehe
`services/session_manager.py`). Solange das Verzeichnis so groß bleibt, bleibt
jede solche Stelle eine Falle.

## Was umzieht

**Die Einheit ist ein Baum**: eine Root-Session mit allen Sub-Agent-Sessions
darunter, transitiv. Eine halbe Unterhaltung ist nichts wert — ein archivierter
Elternteil mit verwaisten Kindern wäre schlimmer als gar nicht aufzuräumen.

Ein Baum zieht um, wenn **jede** Session darin älter ist als
`retention_days` und **keine** davon läuft. Eine einzige frische Session hält
den ganzen Baum fest.

Das Alter ist der `updated_at`-Wert aus der Index-Zeile, ersatzweise die
mtime der Datei. Beide sind billig zu lesen; keine Session-Datei wird dafür
geöffnet.

Eine Session, die **kein Index kennt** (Index-Drift — auf dieser Maschine 7 von
60.196), wird zu ihrer eigenen Wurzel und damit trotzdem erfasst. Ebenso ein
Kind, dessen Elterndatei fehlt.

## Wo es liegt

```
data/session_archive/
  <user>/
    index.json                  eine Zeile je archiviertem Baum
    2026-08/<root_id>.zip       der Baum als ZIP
```

**Ein ZIP pro Baum**, nicht pro Monat: der Baum ist die Einheit, die archiviert,
zurückgeholt und gelöscht wird, also ist er auch die Einheit, die eine Datei
ist. Das hält das Zurückholen bei einem einzigen `open`.

**Ein vorhandenes Archiv wird nie ersetzt, nur ergänzt.** Ob ergänzt oder neu
geschrieben wird, entscheidet die **Datei**, nicht das Manifest — ein verlorenes
oder unlesbares `index.json` würde sonst zu überschriebenen Archiven, und das
ist der einzige Fehler hier, den man nicht rückgängig machen kann. Ein Restore
nimmt sein ZIP mit; ein zurückgeholter Baum fängt also ein frisches an.

Jedes ZIP trägt sein eigenes `_manifest.json`. Geht `index.json` verloren,
sind die Archive trotzdem lesbar und der Index daraus wieder aufbaubar.

Das Archiv liegt **neben** `data/sessions/`, nicht darin: der `SessionManager`
behandelt jedes Unterverzeichnis von `data/sessions/` als Benutzerverzeichnis.

## Die Reihenfolge

Immer: **Archiv schreiben → zurücklesen und prüfen → erst dann die
Live-Dateien löschen.** Ein Absturz dazwischen kostet eine Dublette, nie eine
Unterhaltung. Ein ZIP, das sich nicht wieder öffnen lässt oder dem Mitglieder
fehlen, wird verworfen — die `.tmp` bleibt nicht liegen.

Der Manifest-Eintrag wird geschrieben, **bevor** die erste Datei gelöscht
wird: ab da ist das Archiv die einzige Kopie, und ein fertiges Archiv, das
kein Index kennt, würde der nächste Durchgang überschreiben.

Gelöscht wird **von unten nach oben**, die Wurzel zuletzt. Schlägt ein Löschen
fehl (unter Windows durchaus real: die Datei ist in einem anderen Prozess
offen), macht der Durchgang mit den übrigen weiter und meldet die
Nachzügler. Der **nächste** Durchgang ergänzt dasselbe Archiv, statt es zu
ersetzen — ein Ersetzen würde die Geschwister herauswerfen, die nur noch dort
existieren.

Gelöscht wird ausschließlich über `SessionManager.delete_session(...,
create_backup=False)`. Damit bleiben Index-Partitionen, Cache und die
Grabsteine (`_deleted`) stimmig — das Archiv hält keine eigene Invariante des
Live-Stores.

## Wer geschützt ist

Zwei Wächter, weil keiner allein alles sieht:

* **Laufende Jobs** — der `BackgroundJobManager` des API-Prozesses
  (`active_sessions()`). Fällt diese Auskunft aus, wird **gar nichts**
  archiviert: „ich weiß nicht, welche laufen" darf nicht als „keine läuft"
  gelesen werden. Diese Frage wird **pro Baum neu** gestellt: ein erster
  Sweep über den Rückstand läuft rund 18 Minuten, und eine Unterhaltung, die
  in Minute 12 wieder aufgenommen wird, darf nicht archiviert werden, weil sie
  in Minute 0 still war.
* **Session-Presence** — die `<sid>.lock`-Dateien. Das ist der Wächter, der
  **prozessübergreifend** wirkt, und der einzige, den ein CLI-Prozess hat.
  Er wird einmal pro Benutzer gelesen (ein `scandir` über 60k Einträge
  wiederholt man nicht 200-mal). Sein Ausfall stoppt den Sweep **nicht** —
  anders als oben, und mit Absicht: `session_presence.enabled: false` ist eine
  unterstützte Einstellung, ein Sweep, der ohne Presence verweigert, würde auf
  jeder solchen Maschine verweigern. Sind **beide** Wächter weg (CLI ohne
  Presence), sagt die Kommandozeile das vor dem Lauf.

  ⚠️ Gelesen werden die Lock-Dateien **direkt**, nicht über
  `SessionPresence.list_for_user`: die Methode lässt Sub-Agent-Sessions
  bewusst weg („die gehören dem Lauf, der sie gestartet hat") — und das sind
  59.970 von 60.196. Ein Wächter auf dieser Methode hätte fast alles
  übersehen, wofür es ihn gibt. `get()` filtert nicht.

## Zurückholen

`restore(user, root_id)` schreibt jede Session wieder an ihren Platz —
`updated_at` und Nachrichtenzahl **unverändert**. Eine Unterhaltung kommt als
die zurück, die sie war; würde der Restore sie auf „heute" stempeln, wäre sie
einen Monat lang vor dem nächsten Sweep sicher, ohne dass jemand sie angefasst
hätte.

Dafür gibt es `SessionManager.reinstate_session()` — das Gegenstück zu
`delete_session`: es schreibt unverändert und trägt die Index-Zeile nach. Den
Grabstein aus `_deleted` hebt es **nicht** auf; das muss es auch nicht, denn
`is_deleted` verwirft den Eintrag von selbst, sobald die Datei wieder da ist.
Vorher aufzuheben würde genau das Loch öffnen, das der Grabstein schließt.

Es **verweigert**, wenn die Session wieder live ist: zwei Sessions unter einer
ID sind Korruption, keine Dublette. Nach einem erfolgreichen Restore
verschwindet das ZIP.

Drei Dinge, die ein Restore zusätzlich abfängt:

* **Ein Restore, der auf halber Strecke abgebrochen ist, lässt sich
  fortsetzen.** Eine Session, die als *genau die archivierte Kopie* wieder
  live ist (gleicher `updated_at`), wird übersprungen statt als Konflikt
  gemeldet — sonst wäre ein halb zurückgeholter Baum für immer blockiert.
  Eine Session, die als etwas **anderes** unter derselben ID live ist, bleibt
  eine Ablehnung.
* **Ein Archiv mit fremdem `user_id` wird abgelehnt**, denn
  `reinstate_session` schreibt an die `user_id` *im Dokument*.
* **Der Pfad aus dem Manifest wird geprüft.** `index.json` ist eine Datei auf
  Platte, keine vertrauenswürdige Eingabe — und `forget` löscht, was dort
  steht.

`forget(user, root_id)` löscht ein Archiv endgültig. Danach gibt es keine
Kopie mehr.

## Bedienung

**Panel** „Session Archive" (Kategorie *session*, Plugin `session_archive`):
Liste, *Restore*, *Delete*, *Archive now*. Jeder Endpoint antwortet nur über
das Archiv des **anfragenden** Benutzers; es gibt keinen Parameter für fremde.

Die Liste ist über jede Spalte sortierbar (zuletzt archiviert zuerst, bis man
etwas anderes wählt; die Wahl überlebt den Refresh-Takt und einen Reload).
Vier Spalten sortieren nach dem Wert **hinter** der Zelle, nicht nach dem
Angezeigten: der Titel ohne die ID darunter, die Größe in Bytes statt auf
`0.0 MB` gerundet, und beide Daten nach dem vollen Zeitstempel — ein Sweep
legt einen ganzen Schwung innerhalb derselben Minute ab, und die zeigen alle
denselben Tag.

**CLI**:

```bash
agent-cli run --session-user admin --list-archived
agent-cli run --session-user admin --archive-sessions --dry-run
agent-cli run --session-user admin --archive-sessions 90
agent-cli run --session-user admin --restore-session <root_id>
```

**Automatisch**: der Sweep im API-Prozess, `first_sweep_delay_seconds` nach
dem Start und dann alle `sweep_interval_hours`.

## Konfiguration (`config/config.yaml`)

```yaml
session_archive: # type SessionArchiveConfig
  enabled: true
  retention_days: 30            # ein Baum zieht um, wenn JEDE Session älter ist
  sweep_interval_hours: 24.0
  first_sweep_delay_seconds: 300.0
  max_trees_per_sweep: 200      # hält einen Durchgang begrenzt (nicht den Trockenlauf)
  # archive_path: null          # Vorgabe: data/session_archive
```

Änderungen brauchen einen **Neustart** — der Dienst wird beim Start gebaut.

## Was es nicht tut

* **Die `.backup_*.json`-Dateien** aus `delete_session` räumt es nicht weg. Das
  ist eine eigene Aufbewahrungsfrage (auf dieser Maschine bisher 3 Dateien).
* **Den Haupt-Index heilen.** Fehlt `index.json` ganz, baut der
  `SessionManager` ihn weiterhin aus einem Vollscan neu auf — das ist der
  ehrliche Reparaturweg bei echtem Verlust, und er läuft im Normalbetrieb nie.
* **Über Benutzer hinweg aufräumen.** Jeder Benutzer hat sein eigenes Archiv.

## Messung und Tests

```bash
pytest tests/session/test_session_archive.py -q      # der Dienst
pytest src/plugins/session_archive/tests -q          # das Panel
```

Panel im Browser (Chromium, läuft einzeln):

```bash
pytest src/plugins/session_archive/tests/test_plugin_session_archive_panel.py -q
```

Trockenlauf gegen den echten Store (ändert nichts):

```bash
agent-cli run --session-user admin --archive-sessions --dry-run
```

Gemessen am 20.09.2026:

* Der Waldlauf über 60.196 Dateien dauert rund **eine Sekunde** (`scandir`
  plus 1.304 Index-Dateien, keine Session-Datei geöffnet). Der Trockenlauf
  meldete 200 Bäume mit 58.949 Sessions, 25 Bäume waren zu jung.
* Ein Baum mit 296 Sessions und 26 MB: **5,4 s** archiviert, **3,8 s**
  zurückgeholt. Der Rückstand von 59.960 Sessions ist damit rund
  **18 Minuten** Hintergrundarbeit — einmalig; danach ist ein täglicher Sweep
  im Sekundenbereich.
* Der Deckel `max_trees_per_sweep` begrenzt, was ein Durchgang **schreibt**.
  Ein Trockenlauf zählt alles: ein Bericht, der bei 200 aufhört zu zählen,
  läse sich wie „mehr ist nicht da". Aus demselben Grund nennt ein gedeckelter
  Durchgang in Log, CLI und Panel, **wie viele** noch warten (`remaining`) —
  „Archived 200" sieht sonst aus wie fertig, gerade im Panel, dessen Liste
  sich darunter aktualisiert.
* `skipped_young` zählt **alle** zu jungen Bäume des Benutzers, nicht die, an
  denen ein Durchgang zufällig vorbeikam, bevor der Deckel ihn stoppte. Vorher
  wuchs die Zahl mit jedem Durchgang, obwohl sich die Menge nicht änderte
  (gemessen am 20.09.2026 auf `cli_user`: 328, 667, 970).
