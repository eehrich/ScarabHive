# Tools nachladen (`tools.deferred`)

Ein Agent schickt mit **jedem** LLM-Aufruf die Schemas aller Tools mit, die
seine Allowlist freigibt. Beim coder waren das 51 Tools mit rund 49 KB, bei
jedem Schritt. Die meisten davon braucht ein Lauf nie: forge nur bei Tickets,
das OKF-Schreiben nur am Ende einer Arbeit.

Mit `tools.deferred` schickt der Agent seltene Tools nur noch als Name und
Einzeiler mit. Das volle Schema kommt erst dazu, wenn das Modell danach fragt.
Claude Code macht es genauso (Deferred Tools plus `ToolSearch`).

## Konfiguration

```yaml
tools:
  allowed:
    - "coder_fs/*"
    - "forge/*"
  deferred:
    - "forge/*"      # dieselben Muster wie allowed/blocked
```

- `deferred` ändert nichts daran, **was** der Agent darf. Die Allowlist bleibt
  die einzige Instanz. Ein zurückgestelltes Tool bleibt erlaubt, nur sein
  Schema wird zurückgehalten.
- Die Muster gleicht derselbe Matcher ab wie allowed/blocked
  (`tool_matches_patterns`).
- Leer oder nicht gesetzt: Alles bleibt wie bisher, und `tool_search` gibt es
  dann nicht.
- Die Merge-Syntax (`+`/`!`) gilt auch hier.

## Ablauf

1. **Laufstart:** Die zurückgestellten Schemas werden aus der Tool-Liste
   genommen. An ihre Stelle tritt das Kern-Tool `tool_search`. Dessen
   Beschreibung listet sie mit Namen und erstem Satz.
2. **Laden:**
   - `tool_search(query="select:a,b")` lädt genau diese Tools.
   - Jede andere `query` lädt die besten Stichwort-Treffer, höchstens fünf.
     Ein Treffer im Namen wiegt schwerer als einer in der Beschreibung.
   - Die Schemas werden **hinten** an die Tool-Liste angehängt und gelten für
     den Rest des Laufs.
3. **Aufruf ohne Laden:** Das Tool läuft **nicht**, denn seine Argumente wären
   geraten. Stattdessen wird sein Schema geladen, und das Modell bekommt einen
   Fehler (`ToolNotLoaded`) mit der Bitte, den Aufruf zu wiederholen.
   - Abgewiesen wird jeder Aufruf eines Tools, das **in diesem Schritt** geladen
     wurde. Das gilt für einen zweiten Aufruf ebenso wie für einen Aufruf direkt
     hinter dem `tool_search`, das das Tool geladen hat. Das Schema bekommt das
     Modell erst im nächsten Schritt zu sehen.
   - Ein solcher Aufruf ist nie gelaufen. Deshalb zählt er wie ein von einem
     Hook blockierter Aufruf nicht zur Fehlerserie der Auto-Eskalation.
4. **Nächster Lauf derselben Session:** Was die Historie schon geladen hatte,
   ist sofort wieder da (`restore`), und zwar in der Reihenfolge, in der der
   Lauf es geladen hat. Gemeint sind Tools, die eine `tool_search`-Antwort
   nennt oder die schon aufgerufen wurden. So beginnt der Lauf mit der
   Tool-Liste (und dem Cache-Prefix), mit der der vorige endete.

Code: `src/agent_system/servers/agent/deferred_tools.py`. Verdrahtet in
`Agent._initialize_request_and_conversation` (Aufteilen und `restore`) und in
`ToolExecutionManager.execute_tools_streaming` (Parameter `intercept`). Pre-
und Post-Tool-Hooks sehen `tool_search` nicht, ebenso wenig wie einen
abgewiesenen Aufruf.

`/context` zählt, was ein Lauf der Session tatsächlich schickt. Die API reicht
dafür die gespeicherten Nachrichten durch, deshalb stimmt die Zahl auch für
eine Session, die in einem anderen Prozess lief. `/tools` und
`list_available_tools` zeigen alle Tools, die der Agent aufrufen darf,
zurückgestellte eingeschlossen.

Ein `deferred`-Muster, das kein erlaubtes Tool trifft, bewirkt nichts. Der
Kern schreibt dazu einmal pro Prozess eine Warnung ins Log, und der
agent_editor zeigt es als Problem „Deferred matches no allowed tool“. Bei
einem externen MCP-Server-Eintrag (`mcp_servers`) wird das Feld ignoriert.

**Grenze:** Ruft ein `tool_script` ein zurückgestelltes, noch nicht geladenes
**externes** MCP-Tool auf, bekommt es „Unknown tool“ statt des Hinweises, das
Tool direkt aufzurufen. Diese Kombination kommt in keiner Konfiguration vor.
Ein Fix müsste die Prüfung in `tool_script`, die statisch und synchron ist, an
den Zustand des laufenden Requests anbinden.

## Prompt-Cache

Die Tool-Liste steht vorn im gecachten Prefix. Ein Nachladen verwirft den
Cache deshalb ab der Tool-Liste, einmal pro Nachladen. Danach trifft der Cache
wieder. Das ist billiger, als bei jedem Schritt alles mitzuschicken. Das
Nachladen im Folgelauf (Punkt 4) hält den Prefix über Läufe hinweg stabil.

## Gemessen (30.09.2026, coder, `or-deepseek-flash`)

Gepaart, gleiche Aufgabe, lokal:

| | Tools | Schema | Prompt 1. Aufruf |
|---|---|---|---|
| ohne `deferred` | 52 | 46,8 KB | 16.793 Token |
| mit `deferred` (forge, coder_okf, coding_cli, datetime, sequential_thinking) | 20 | 27,0 KB | 11.581 Token |

Das sind rund 5.200 Token weniger **pro Aufruf** (−31 % des ersten Prompts).
Die Einsparung wiederholt sich mit jedem Schritt des Laufs.

Verhalten in drei Läufen:
- Eine Aufgabe, die ein zurückgestelltes Tool braucht (OKF durchsuchen): Das
  Modell rief `tool_search` auf, lud zwei Tools und benutzte sie. Das Nachladen
  kostete einen Cache-Miss, danach lag die Cache-Trefferquote bei 96 %.
- Eine Aufgabe ohne Bedarf: Es wurde nichts nachgeladen, richtige Antwort in
  zwei Schritten.
- Fortsetzung der ersten Session: Die geladenen Tools waren ab dem ersten
  Aufruf da, ohne `tool_search`, Antwort in einem Schritt.

Nicht gemessen ist die Fehlerquote bei der Tool-Wahl über viele Läufe. Offen
ist auch, ob stärkere oder schwächere Modelle ein zurückgestelltes Tool
übersehen, das sie brauchen würden. Beides zeigt sich erst im Betrieb.

## Wann zurückstellen

Zurückstellen lohnt sich für Tools, die ein typischer Lauf **nicht** braucht
und die viel Schema mitbringen. Nicht zurückstellen sollte man, was fast jeder
Lauf braucht (Dateien lesen, Shell): Das kostet sonst bei jedem Lauf einen
Aufruf und einen Cache-Miss. Die Größen pro Tool liefert eine Abfrage auf
`data/message_debugger/debugger.db` (`llm_requests.payload_json` → `tools`).
