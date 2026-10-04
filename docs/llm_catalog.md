# Der Modellkatalog — die Entscheidungen dahinter

`config/llm.yaml` und `config/llm_openrouter.yaml` sind **Daten**: Modellnamen,
Fenster, Knöpfe. Was ein Feld bedeutet, steht in `agent_system/config/models.py`
am Feld selbst. Was hier steht, ist das, was man den Werten nicht ansieht.

Die Vererbung zwischen den Einträgen (`extends:`) beschreibt
[llm_model_inheritance_konzept.md](llm_model_inheritance_konzept.md).

## Was ein Endpunkt spricht, sagt sein Eintrag — nicht sein Name

Die Clients kennen keine Modellnamen. Eigenheiten eines Endpunkts stehen als
Schlüssel am Modell, und wer einen neuen Anbieter aufnimmt, setzt Zeilen statt
Code. Welche Route welchen Schlüssel auswertet:

| Schlüssel | `openai_httpx` | `openai_responses` | `openrouter_sdk` | `anthropic` | `gemini*`, `openai`, `ollama` |
|---|---|---|---|---|---|
| `tool_schema_dialect` | ja | ja | ja | – | – |
| `reasoning_details_mode` | ja | ja | ja | ja | – |
| `assistant_reasoning_field` | ja | – | – | – | – |
| `thinking_request_shape` | – | – | – | ja | – |
| `stream_silence_timeout` | ja | ja | – | – | – |
| `provider_affinity_minutes` | ja | ja | ja | – | – |
| `prompt_cache_marker_style` | ja | ja | verweigert `anthropic` | eigener Weg | – |
| `safety_settings` | ja | ja | verweigert | – | nur `gemini*` (nativ) |

Die ersten sechs Zeilen sind die **Dialekt-Schlüssel**: steht einer auf einer
Route, die ihn nicht auswertet, protokolliert der Bau des Clients das („is not
wired for provider=…"). Die letzten beiden Zeilen haben diesen Wächter nicht.
Ein unbekannter **Wert** lässt den Bau in allen Fällen scheitern. Beides mit Absicht: still ignoriert zu werden ist der
Fall, den man erst Wochen später am Rechnungsbetrag merkt.

## Structured Output: die Route hat das Feld, der Eintrag sagt, ob das Modell es kann

Ein Aufrufer, der die Schlussantwort als JSON will (`ResponseFormat`,
`agent_system/llm/structured_output.py`), bekommt das native Feld nur, wenn
**beides** stimmt: die Route hat ein Feld dafür (`response_format_kinds` am
Client) und der Modelleintrag erklärt es.

| Route | Feld | JSON-Schema | JSON-Objekt |
|---|---|---|---|
| `openai`, `openai_httpx` (auch `ollama` im `openai_compat`-Modus) | `response_format` | ja | ja |
| `openai_responses`, `openrouter_sdk` | `text.format` | ja | ja |
| `anthropic` | `output_config.format` | ja | – (die Messages-API hat keinen JSON-Modus) |
| `gemini`, `gemini_sdk` | `responseMimeType` + `responseJsonSchema` | ja | ja (nur der MIME-Typ) |
| `ollama` nativ | `format` | ja | ja (`"json"`) |
| Batch, Realtime | – | – | – |

- `capabilities.structured_output: true` heißt: das Modell hält sich an ein
  Schema — **auch in einem Request mit Tools**, denn ein Agent schickt das Feld
  auf jedem Schritt. Gemini vor 3 lehnt diese Kombination ab: dort nicht setzen.
  Über OpenRouter hat es jedes Modell einzeln (`supported_parameters` enthält
  `structured_outputs`).
- `capabilities.json_mode` wird nicht gelesen. Natives „irgendein
  JSON-Objekt" bekommt nur ein Modell mit `structured_output: true`: die
  `json_mode`-Werte in den Katalogdateien las bis F11 niemand und sind
  ungeprüft (Claude-Einträge tragen `true`, obwohl die Anthropic-Route gar
  keinen JSON-Modus hat). Ein Modell nur mit `json_mode` bekommt das Format
  als Hinweis im Gespräch, die Antwort wird geprüft wie jede andere.
- Das Schema geht so raus, wie es die strikte Teilmenge durchlaufen hat
  (`agent_system/llm/schema_worker.py`: Schlüsselwort-Whitelist, `$ref` nur
  auf `#/$defs/…`, Annotationen wie `default` entfernt). Gemini bekommt es als
  `responseJsonSchema` (JSON Schema), nicht durch den Sanitizer der Function
  Declarations. Anthropic und OpenAI im `strict`-Modus lehnen Schemas ab,
  deren Objekte kein `additionalProperties: false` haben — laut, als 400,
  statt still umgeschrieben.
- Warum das Feld auf jedem Schritt steht und nicht nur auf dem letzten: OpenAI
  rendert das Schema in den gecachten Kontext, Anthropic verwirft den Cache des
  Gesprächs, wenn sich `output_config.format` ändert. Konstant über den Lauf
  bleibt der Präfix gleich (gemessen am Payload:
  `tests/agent/test_agent_structured_output.py`); nur auf dem letzten Call
  wäre genau dieser Call ein Cache-Fehlgriff.

Stand 29.09.2026 erklärt kein Eintrag in `config/llm*.yaml`
`structured_output`. Bis ein Betreiber das tut, läuft jeder strukturierte
Aufruf — Schema wie JSON-Objekt — über den Fallback (Hinweis im Gespräch +
Prüfung) oder scheitert, wenn der Aufrufer keinen Fallback erlaubt.

## Wie lange ein Stream nur Keep-alives schicken darf

OpenRouter hält einen wartenden Stream mit Kommentarzeilen offen, etwa alle
halbe Sekunde. Jede davon setzt den `read`-Timeout zurück — der feuert dort
also nie. `stream_silence_timeout` zählt nur echte Events: kommt so lange
keins, wird der Versuch wiederholt. Ohne Eintrag gibt es keine solche Grenze.
Ist das Endereignis eines Laufs schon da, bleibt die Antwort stehen, egal wie
der Stream danach endet (Keep-alives, Stille, abgerissene Verbindung).

Gemessen am 16.09.2026 (Produktions-Payload, Flex-Tier): zwischen zwei Events
lagen höchstens 9,2 s, auch über 38.000 Tokens verdeckten Denkens — OpenRouter
meldet dabei alle paar Sekunden ein `output_item`. Die 900 s der Basis liegen
bei OpenAIs Empfehlung für Flex (15 Minuten Wartezeit sind dort normal).
DeepSeek direkt bekommt keinen Eintrag: die API schickt unter Last ebenfalls
nur Keep-alives, schließt eine Anfrage aber nach 10 Minuten Wartezeit selbst.

Nicht verwechseln: ein Call, der 26 Minuten lang **generiert** (gemessen: ein
Kontinuitäts-Pass mit 55.742 Tokens), ist nicht still und wird nicht
abgebrochen. Dagegen hilft die Output-Grenze des Agenten, kein Timeout.

## OpenRouter: warum die Backends gepinnt sind

Der Prompt-Cache bei OpenRouter ist **backend-lokal**. Wer die Anbieterwahl dem
Preis überlässt (`sort: price`), verteilt dieselbe Konversation über mehrere
Backends — und ein Cache-Miss kostet bei Langkontext leicht mehr, als der
günstigere Anbieter spart. Deshalb setzen die Einträge `provider_routing.order`
und bleiben beim selben Backend.

`allow_fallbacks: false` steht dort, wo ein Ausweichen teuer wäre: fällt der
gepinnte Anbieter aus, übernimmt die **Agent-Kette** (llm_profile), nicht ein
stiller 2x-Preissprung bei OpenRouter.

### Wer wirklich geliefert hat: `openrouter_metadata`

Die Clients schicken auf OpenRouter-Endpunkten den Header
`X-OpenRouter-Metadata: enabled`. Ohne ihn nennt **keine** Antwort das
Backend — auf der Responses-Route gibt es kein anderes Feld dafür. Mit ihm
landet in jedem `post_llm_response`-Hook ein Feld `routing`:

```json
{"selected": "DeepInfra", "available": ["DeepInfra", "StreamLake"],
 "attempt": 1, "strategy": "latest", "region": "FRA"}
```

Damit ist zum ersten Mal nachvollziehbar, ob `provider_routing.order`
gehalten hat. **Der Client urteilt darüber nicht selbst:** die Config nennt
Gateway-Slugs, die Metadaten Anzeigenamen, und übersetzen kann nur die
Anbieterliste des Gateways (`GET /api/v1/providers`, `name` → `slug`) — ein
naiver Vergleich schlüge ausgerechnet bei den härtesten Pins Fehlalarm.
Gemeldet, nicht gerichtet.

**Anbieter-Pin:** Jeder Assistant-Turn merkt sich als `served_by`, welches
Backend geliefert hat. Der nächste Request geht als **harter Pin** darauf
raus: `order: [dieses eine]` plus `allow_fallbacks: false`. Den Slug zum
Anzeigenamen liefert die Anbieterliste; im Code steht kein Anbietername.

Warum hart: `allow_fallbacks: false` hält nur Anbieter **außerhalb** der Liste
draußen. Eine Liste mit zwei Einträgen rotiert trotzdem — bei DeepSeek in 34
von 38 Backend-Wechseln innerhalb eines Laufs —, und ein Eintrag ganz ohne
`order` wird vom Gateway frei verteilt: ein `coder`-Aufruf am 22.09.2026 landete
so auf einem Backend mit kaltem Cache und kostete das Vierfache seiner
Nachbarn. Deshalb ein Eintrag ohne Ausweichweg; ein Modelleintrag ohne
`order` wird genauso gepinnt.

Der Eintrag entscheidet weiter, **welche** Backends überhaupt dürfen: steht
das gelieferte nicht in seiner `order`, bleibt der Eintrag unverändert.

**Lehnt das gepinnte Backend ab** (429, 5xx, oder 404 „No endpoints found",
wenn es das Modell nicht mehr führt), geht derselbe Aufruf sofort noch einmal
raus — ohne Pin, also so, wie der Modelleintrag konfiguriert ist, und ohne
Rate-Limit-Wartezeit; dort darf das Gateway wieder wählen. Der Agent-Typ
merkt sich das abgelehnte Backend nicht weiter. Was das Backend dagegen
beantwortet hat (etwa ein 400 wegen eines defekten Reasoning-Items), ist keine
Ablehnung: die Heilung bleibt auf demselben Backend, sonst scheitert der
zurückgespielte Verlauf erneut.

**Der erste Aufruf eines Laufs** hat noch keinen Assistant-Turn. Er startet
auf dem Backend, das den **Agent-Typ** zuletzt bedient hat — gleich welche
Instanz, welcher Lauf, welcher Client (`agent_system/llm/backend_affinity.py`,
Schlüssel Agent-Name + Modell, gesetzt über `set_app_title`). Dort liegt der
Prompt, den alle Läufe des Typs teilen. Das gilt nur innerhalb von
`provider_affinity_minutes` nach dem letzten Aufruf des Typs (Default: die
Cache-Dauer des Modells `prompt_cache_ttl_minutes` — Claude und Gemini 5,
GPT 30 —, ohne die 30; `0` = aus, pro Modelleintrag oder per `llm_params`
pro Agent). Danach ist
der Cache kalt, und es gilt wieder die konfigurierte `order`. Gemessen am
22.09.2026 an den ersten Aufrufen von v4/v6-Läufen (72 h):

| Abstand zum letzten Aufruf des Typs | gleiches Backend | anderes Backend |
|---|---|---|
| < 5 min | 57,8 % aus dem Cache | 27,8 % |
| 5–30 min | 22,5 % | 9,0 % |
| 30–60 min | 1,1 % | 8,3 % |

Die eigene Historie eines Laufs schlägt diesen Wert immer: nur sie sagt,
welches Backend das zurückgespielte Reasoning prüfen kann. Lehnt das
vorgezogene Backend ab (429, 5xx), fällt das Gateway im selben Request auf
die übrigen `order`-Einträge weiter (gemessen: 34 von 38 Wechseln innerhalb
eines Laufs waren genau das, die übrigen 4 Requests trugen keine `order`).
Der Speicher lebt im Prozess; nach einem Neustart folgt der erste Aufruf
jedes Typs wieder der `order`.

**Modell-Pin für Aliase (Responses-Route):** Ein `~author/familie-latest`
wird pro Request aufgelöst, und OpenRouter steigt still auf ein älteres Modell
der Familie ab, wenn das neueste scheitert (429/5xx; undokumentiert,
OpenRouterTeam/docs#601). Gemessen an einem `shorts_producer`-Lauf am
30.09.2026:
- 23 von 69 Aufrufen gingen nach einem 504 von gemini-3.8-flash an 3.7.
- Die Aufrufe wechselten bunt zwischen beiden Modellen, und jeder Wechsel traf
  auf einen kalten Cache: 20 % gelesen statt 74 %.
- Jeder dieser Aufrufe wartete vorher rund 25 s auf den 504.

Deshalb bleibt ein Lauf auf dem Modell, das seinen letzten Turn beantwortet
hat. Das steht als `served_model` im Replay-Block. Der Request nennt den
konkreten Slug, und den kann OpenRouter nicht mehr abstufen (`available=1`).
Das gilt für jeden `~`-Alias auf der Responses-Route; DeepSeek stuft genauso
ab, nach 429ern. Die Claude-Aliase laufen über Chat Completions und sind nicht
gepinnt, dort wurde kein Abstieg beobachtet.

Eine Ablehnung (429, 5xx, 404) schickt die Wiederholung wieder an den Alias,
wie beim Anbieter-Pin; der Lauf folgt dann dem Modell, das geantwortet hat,
bis dieses ablehnt. Ein Lauf ohne eigene Historie beginnt beim Alias, also
beim neuesten Modell; eine fortgesetzte Session bleibt auf ihrem Modell, bis es
ablehnt. Der Preis: Scheitert das gepinnte Modell, wartet derselbe
Aufruf zweimal, weil der Alias es vor dem Abstieg noch einmal versucht.
Dafür bleibt ein kurzer Aussetzer ohne Modellwechsel.

Der Replay-Block eines anderen Modells desselben Alias gilt dabei als fremd:
Sein verschlüsseltes Reasoning prüft nur das Modell, das es geschrieben hat.
Früher trug der Block nur den Alias, und die Prüfung verglich Alias mit Alias.
Ist ein Block einer Nachricht fremd, oder fehlt einer ihrer Tool-Aufrufe im
Replay, wird die ganze Nachricht neu aufgebaut.
Der Message-Validator fasst aufeinanderfolgende Turns zusammen; nur halb
zurückgespielt, fehlten die Aufrufe der fremden Hälfte, ihre Ergebnisse
standen aber im Request.

### `session_id`: Cache-Lokalität ohne harten Pin

OpenRouters Prompt-Cache ist backend-lokal (siehe oben). `session_id` ist der
Sticky-Routing-Schlüssel des Gateways: gleiche Session → gleiches Backend.
Gemessen am 01.09.2026: **6/6** Aufrufe auf einem Anbieter mit `session_id`,
ohne ihn verteilten sich 6 Aufrufe auf **4** Anbieter.

Die Clients senden dafür den **aufgelösten `prompt_cache_key`** — der bedeutet
schon „gleicher stabiler Präfix", ist also genau die richtige Gruppierung, und
braucht keine Verdrahtung, die es nicht gibt. Nur an OpenRouter; ein fremder
OpenAI-Endpunkt lehnt unbekannte Parameter mit 400 ab.

Das ersetzt `provider_routing.order` nicht, macht es aber entbehrlicher: wer
den harten Pin lockert, behält mit `session_id` die Cache-Treffer und gewinnt
zurück, dass ein ausgefallener Anbieter nicht mehr den ganzen Modelleintrag
kostet.

### Drei Felder, die bereitstehen und aus gutem Grund leer sind

`plugins`, `prompt_cache_options` und `safety_identifier` reichen die Clients
unverändert durch, gesetzt wird keines davon:

| Feld | was es könnte | warum ungesetzt |
|---|---|---|
| `plugins` | `context-compression` (Prompt automatisch kürzen), `response-healing`, `moderation`, `file-parser`, `auto-router` | jedes ändert, was das Modell sieht oder kostet — nicht ohne Messung |
| `prompt_cache_options` | `{"mode": "explicit"}` schaltet OpenAIs **eigene** Breakpoints ab, sodass nur unsere Marker zählen (GPT-5.6+) | welche der beiden Varianten besser cacht, ist hier ungemessen |
| `safety_identifier` | stabiles Pseudonym pro Endnutzer; ohne es trägt der Request die **Konto**-Identität, ein Policy-Block trifft also alles | ein Wert pro Lauf müsste vom Aufrufer kommen, den Weg gibt es noch nicht (Responses-Route only) |

### Kein `usage: {include: true}` mehr

Das Feld ist bei OpenRouter deprecated und wirkungslos — Kosten, Cache-Treffer
und `cost_details` kommen ohnehin in jeder Antwort. Am 01.09.2026 gegengeprüft,
**streamend wie nicht-streamend**: mit und ohne Feld identische
`usage`-Schlüssel und derselbe `cost`. Deshalb schicken die Clients es nicht
mehr; wieder einbauen bringt nichts. `stream_options: {include_usage: true}`
bleibt dagegen stehen — das braucht OpenAI direkt, nicht OpenRouter.

## DeepSeek via OpenRouter: fp8, nicht fp4

Preistabelle vom 2026-08-20 (Input / Output / Cache-Read je 1M Token):

| Endpunkt | in | out | cache | Quantisierung |
|---|---|---|---|---|
| open-inference | 0,065 | 0,14 | 0,014 | fp4 |
| relace | 0,07 | 0,14 | 0,014 | fp4 |
| decart | 0,0765 | 0,153 | 0,0153 | fp4 |
| **streamlake** | 0,0784 | 0,1568 | 0,0157 | **fp8** ← `order[0]` |
| **baidu** | 0,0798 | 0,1596 | 0,0160 | **fp8** |
| **deepinfra** | 0,08 | 0,18 | 0,0160 | **fp8** |
| deepseek (direct) | 0,22 | 0,66 | 0,007 | — |

Die drei billigsten sind **fp4-quantisiert**. Für Prosa ist das ein
Qualitätsrisiko, das ~15 % Ersparnis nicht wert ist — deshalb beginnt `order`
beim günstigsten fp8. Wer fp4 probieren will: `open-inference` auf Position 0
setzen und **am Buch** messen, nicht an der Rechnung.

**baidu steht bewusst hinten**: es deckelt die Ausgabe bei 131072 Token,
streamlake und deepinfra erlauben 384000. Drei v4-Scorer fordern 262144 über
`llm_params` an und würden bei baidu mit 400 abbrechen (Review-Befund B4).

`quantizations: ["fp8"]` ist **erzwungen**, nicht bevorzugt: ohne das Feld
routet ein ausgelasteter Pin preis-sortiert weiter — genau auf die fp4-Endpunkte,
die die Tabelle für Prosa ausschließt.

Kontextfenster: an den gepinnten Endpunkten gemessen (2026-08-20) — streamlake
1,024M, baidu/deepinfra 1,048M. Die älteren or-deepseek-Einträge deklarierten
100k; **die** sind die Untertreibung, nicht der große Wert. Ein zu kleines
Fenster ließe den Summarizer zehnmal zu früh feuern.

`deepseek-v4-pro` ist auf den Slug **0813** gepinnt: neuere Generation *und*
billiger als der undatierte (1,60/3,20 gegen 1,205/3,614). Einen
`latest`-Alias gibt es für Pro nicht, für Flash schon.

## Warum manche Einträge kein `max_tokens` setzen

Die OpenRouter-DeepSeek-Einträge ersetzen Direct-Profile, die ohne Cap liefen.
Ein hartes Limit dort hätte lange Szenen und JSON still abgeschnitten — und das
Reasoning teilt sich dieses Budget. Deshalb: kein `max_tokens`, der Anbieter
entscheidet. Bei den `-unlimited`-Varianten steht `max_tokens: null`
ausdrücklich da, weil sie es sonst von ihrem Elterneintrag erben würden.

## Gemini via OpenRouter: die Responses-Route

Die Gemini-Einträge laufen über `provider: openai_responses`, nicht über die
Chat-Completions-Brücke. Auf dieser Route reisen die Output-Items des Modells
**wortgleich** hin und zurück; die Brücke musste sie rekonstruieren, was die
`"encrypted content ... could not be verified"`-400er erzeugte.

**Die `openrouter-gemini*`-Einträge setzen kein `safety_settings`** — das Feld
wirkt auf dieser Route nicht. Derselbe Unsinns-Wert
(`category: HARM_CATEGORY_NOT_A_REAL_THING`) wird auf `/chat/completions` mit
HTTP 400 samt Enum-Liste abgelehnt, auf `/responses` mit HTTP 200
stillschweigend geschluckt: OpenRouter lässt es dort fallen, bevor es Google
erreicht (gemessen 01.09.2026).

Es fehlt dadurch nichts. `debug.echo_upstream_body` zeigt auf der Chat-Route,
dass OpenRouter ohne eigene Angabe alle fünf Kategorien auf `OFF` setzt —
freizügiger als die Schwellen, die hier früher standen (drei auf
`BLOCK_ONLY_HIGH`, und `HARM_CATEGORY_CIVIC_INTEGRITY` fiel ganz aus dem
Upstream-Body). Für Prosa war unsere Angabe die *strengere*.

Wer wirklich eigene Schwellen braucht, nimmt die nativen `gemini-3-*`-Einträge
(`provider: gemini_sdk`): die reden direkt mit Google, dort greift das Feld.

`service_tier: flex` ist Googles Flex Processing: billiger, dafür längere
Warteschlange. Bei einer 429 auf dem Flex-Tier lässt der Client das Feld einmal
fallen und wiederholt auf Standard.

### AI Studio vor Vertex

`openrouter-gemini` fragt Google AI Studio zuerst, Vertex danach. Gemessen am
01.10.2026 mit den ersten 30 Aufrufen eines `shorts_producer`-Laufs. Sie wurden
je zweimal pro Anbieter nachgespielt, mit einer eigenen Nonce, sodass kein
Durchgang den Cache eines anderen trifft:

| | Standard | Flex |
|---|---|---|
| AI Studio: aus dem Cache | 80–84 % | 82–84 % |
| AI Studio: Kosten | 0,25–0,27 $ | 0,12–0,14 $ |
| AI Studio: je Aufruf | 3,5 s | 4,5 s (max. 11 s) |
| Vertex: aus dem Cache | 59–71 % | 77–83 % |
| Vertex: Kosten | 0,32–0,40 $ | 0,13–0,14 $ |
| Vertex: je Aufruf | 7–12 s | 19 s (max. 78 s) |

Der Präfix war in allen Fällen byte-gleich. Vertex (auf OpenRouter nur sein
`global`-Endpunkt) verfehlt seinen impliziten Cache öfter und ist langsamer. Auf
Flex braucht Vertex 20–50 s pro Aufruf; die Messung vom 30.09. mit „Flex
14–315 s“ lag auf Vertex. AI Studio ist mit Flex fast so schnell wie mit Standard,
kostet aber nur die Hälfte. Ein echter Lauf bestätigt das: dieselbe Produktion
mit Flex auf AI Studio kostete 0,35 $ bei 89 % Cache-Anteil.

Fällt AI Studio aus (429/5xx), geht der Aufruf an Vertex. Der Pin auf den Anbieter
(`served_by`) hält den Rest des Laufs dort, wo er angefangen hat.

## GPT-5.6 via OpenRouter

`prompt_cache_key: "auto"` ist ab GPT-5.6 **Pflicht** für zuverlässiges
Cache-Matching — ohne Key cacht das Modell praktisch nie (belegt 2026-07-21:
byte-identischer 10k-Prefix, `cached_tokens=0`). Der Key gehört zum Modell, nicht
in die Agent-Dateien; dort stand er bis zum 2026-08-22 34-mal.

Reasoning-Round-Trip: Die Items der Reasoning-Modelle bilden eine
verschlüsselte Kette, die jeder Turn vollständig zurückgeben muss. Das
Config-Feld `reasoning_details_mode` sagt pro Modell, wie viel davon
zurückreist, und wird auf `openai_httpx`, `openai_responses`,
`openrouter_sdk` und `anthropic` ausgewertet. Der Default hängt an der Route:
`keep_last` auf der Chat-Route (dort gilt eine Thought-Signature nur für den
laufenden Turn), `keep_all` dort, wo ganze Item-Ketten verbatim zurückgehen.

⚠️ Wer den Cache eines Modells nutzt, das seinen Prefix byteweise vergleicht
(Claude), braucht `keep_all` — mit `keep_last` verliert die vorige Runde ihre
Blöcke, das Prefix ändert sich vor dem Anker, und jeder Schritt schreibt den
Cache neu statt ihn zu lesen. Gemessen am `book_launcher` (43 Calls): 19-mal
Rückfall auf 25.878 gelesene Tokens, nie mehr. `openrouter-claude` setzt es
deshalb.

## `openrouter_sdk`: dieselbe Route über das offizielle SDK

Seit 2026-09-01 gibt es einen zweiten Weg zum selben `/responses`-Endpunkt:
`provider: openrouter_sdk` (Plugin `plugins/llm_openrouter`) schickt den
Request über OpenRouters offizielles Python-SDK. Er ist ein **A/B-Kandidat**,
kein Ersatz: der Client erbt vom `openai_responses`-Client und tauscht nur
`_post` — Payload-Bau, Cache-Breakpoints, Parser, Heilungsschleife und Hooks
sind dieselben. Was in einem Vergleich abweicht, ist der Transport.

Umschalten ist eine Zeile; die Einträge erben ohnehin von `openrouter-base`:

```yaml
    mein-modell:
      extends: openrouter-base
      provider: openrouter_sdk    # statt openai_responses
```

Voraussetzung: `pip install openrouter` (steht in `requirements/all.txt`).
Ohne installiertes Paket ist nur dieser eine Eintrag betroffen — das Plugin
wird erst importiert, wenn ein Modell den Provider nennt.

**Was gemessen ist** (openrouter 1.1.108, 2026-09-01):

* Die Antwort wird **aus dem rohen Body** gelesen, nicht aus dem typisierten
  Ergebnis. Hauptgrund ist baulich: die geerbte Heilungsschleife entscheidet
  am Statuscode, erkennt Body-Fehler in einer HTTP 200 und prüft
  Reasoning-Ablehnungen am Body-**Text** — sie braucht den Rohtext ohnehin.
  Dazu kommt eine bekannte Zerbrechlichkeit: `usage` ist `OptionalNullable`,
  und `UsageCostDetails` verlangt
  `upstream_inference_input_cost`/`…output_cost`. Fehlen die, fällt das
  **ganze** `usage`-Objekt still auf `Unset()` — Tokens, `cost` und
  Cache-Treffer weg, ohne Fehler. **Nicht live beobachtet**: die am
  01.09.2026 gemessenen Antworten (deepseek-v4-flash, gemini-3.5-flash-lite)
  trugen alle Pflichtfelder, das typisierte Modell hätte sie korrekt
  geparst. Der rohe Body ist also Vorsorge, kein Reparaturfall.
* Der Request geht typisiert raus und trägt alles, was diese Route braucht:
  `provider`-Routing, `reasoning`, `service_tier`, `prompt_cache_key` und den
  Cache-Breakpoint `prompt_cache_breakpoint`.
* **Zwei Felder kann er nicht**, und er verweigert deshalb beim Bauen statt
  sie zu verlieren: `safety_settings` (kein SDK-Parameter — damit fällt die
  Gemini-Route aus) und Anthropic-`cache_control` pro Content-Part
  (`prompt_cache_marker_style: anthropic`).
* Das SDK ergänzt drei Felder von sich aus: `store: false`, `stream: false`
  und `service_tier: "auto"`. Das letzte heißt: der Flex-Drop schickt den
  Standard-Tier *explizit*, wo die httpx-Route das Feld weglässt.
* Preis der Abhängigkeit: `pydantic<2.13`. Das deckelt die ganze Anwendung
  eine Minor unter dem aktuellen Stand.

## Profile

`turbo-batch` ist **kein Zwilling von `turbo` mehr.** `turbo` zeigt seit
2026-08-18 auf gemini-3.5-flash-lite über OpenRouter, und einen
OpenRouter-Batch-Pfad gibt es nicht (die Factory kennt nur
gemini/openai/anthropic als `batch_provider`). Wer von `turbo` nach
`turbo-batch` wechselt, um zu sparen, wechselt die Modellfamilie:
gpt-5.4-nano statt gemini-flash-lite, 272k statt 400k Kontext, ohne
`safety_settings` und ohne `provider_routing`.

`default_profile: or-deepseek-flash` (seit 2026-08-20, vorher `chat`):
dieselbe Modellfamilie, aber über OpenRouter — `chat` zeigte auf die
Direct-API, deren Konto fast leer ist. Greift nur als letzte Rückfallebene,
wenn kein Profil auflösbar ist.

## Was NICHT im Katalog steht

* **Keine Feld-Erklärungen.** Die stehen am Pydantic-Feld in
  `agent_system/config/models.py`.
* **Kein Änderungsprotokoll.** Wer wissen will, wann ein Wert warum gesetzt
  wurde, liest `git log -p config/llm*.yaml` — dort steht es vollständig und
  ohne die Datei zu verstopfen.
