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
`"encrypted content ... could not be verified"`-400er erzeugte. `safety_settings`
gehen unverändert durch.

`service_tier: flex` ist Googles Flex Processing: billiger, dafür längere
Warteschlange. Bei einer 429 auf dem Flex-Tier lässt der Client das Feld einmal
fallen und wiederholt auf Standard.

## GPT-5.6 via OpenRouter

`prompt_cache_key: "auto"` ist ab GPT-5.6 **Pflicht** für zuverlässiges
Cache-Matching — ohne Key cacht das Modell praktisch nie (belegt 2026-07-21:
byte-identischer 10k-Prefix, `cached_tokens=0`). Der Key gehört zum Modell, nicht
in die Agent-Dateien; dort stand er bis zum 2026-08-22 34-mal.

`reasoning_details_mode: keep_all` bei den Reasoning-Modellen: deren Items
bilden eine verschlüsselte Kette, die jeder Turn vollständig zurückgeben muss.

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
