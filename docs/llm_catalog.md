# Der Modellkatalog — die Entscheidungen dahinter

`config/llm.yaml` und `config/llm_openrouter.yaml` sind **Daten**: Modellnamen,
Fenster, Knöpfe. Was ein Feld bedeutet, steht in `agent_system/config/models.py`
am Feld selbst. Was hier steht, ist das, was man den Werten nicht ansieht.

Die Vererbung zwischen den Einträgen (`extends:`) beschreibt
[llm_model_inheritance_konzept.md](llm_model_inheritance_konzept.md).

## OpenRouter: warum die Backends gepinnt sind

Der Prompt-Cache bei OpenRouter ist **backend-lokal**. Wer die Anbieterwahl dem
Preis überlässt (`sort: price`), verteilt dieselbe Konversation über mehrere
Backends — und ein Cache-Miss kostet bei Langkontext leicht mehr, als der
günstigere Anbieter spart. Deshalb setzen die Einträge `provider_routing.order`
und bleiben beim selben Backend.

`allow_fallbacks: false` steht dort, wo ein Ausweichen teuer wäre: fällt der
gepinnte Anbieter aus, übernimmt die **Agent-Kette** (llm_profile), nicht ein
stiller 2x-Preissprung bei OpenRouter.

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

## GPT-5.6 via OpenRouter

`prompt_cache_key: "auto"` ist ab GPT-5.6 **Pflicht** für zuverlässiges
Cache-Matching — ohne Key cacht das Modell praktisch nie (belegt 2026-07-21:
byte-identischer 10k-Prefix, `cached_tokens=0`). Der Key gehört zum Modell, nicht
in die Agent-Dateien; dort stand er bis zum 2026-08-22 34-mal.

Reasoning-Round-Trip: Die Items der Reasoning-Modelle bilden eine
verschlüsselte Kette, die jeder Turn vollständig zurückgeben muss. Auf der
`openai_responses`-Route erledigt das der Client selbst (verbatim-Replay der
Items, de facto keep_all). Das Config-Feld `reasoning_details_mode` existiert
im Schema weiterhin, ist aber seit dem Umzug auf die Responses-API in keiner
YAML mehr gesetzt — ausgewertet wird es nur noch vom `openai_httpx`-Client
(Default `keep_last`); andere Provider verwerfen es (anthropic nimmt es an,
liest es nie).

## `openrouter_sdk`: dieselbe Route über das offizielle SDK

Seit 2026-09-01 gibt es einen zweiten Weg zum selben `/responses`-Endpunkt:
`provider: openrouter_sdk` (Plugin `plugins_llm/llm_openrouter`) schickt den
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
