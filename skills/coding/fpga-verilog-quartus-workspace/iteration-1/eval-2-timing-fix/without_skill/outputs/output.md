# Timing-Fix bei 50 MHz: negative Slack und eine .sdc mit nur `create_clock`

## Das Problem in einem Satz

`create_clock` allein ist das absolute Minimum einer Timing-Constraint. Eine .sdc, die nur
`create_clock -name clk50 -period 20.000 [get_ports {clk}]` enthält, analysiert die Timing-
Pfade nur **unvollständig**. Der gemeldete negative Slack ist dann oft **nicht vertrauenswürdig**
und/oder entsteht, weil beteiligte Takte nicht korrekt zusammenhängen. Zwei getrennte Dinge
müssen geklärt werden:

1. Ist der negative Slack **echt** (echte Logik-Tiefe zu lang für 50 MHz) oder
2. ist er ein **Artefakt der fehlenden Constraints** (Takte werden als zusammenhängend
   behandelt, obwohl sie asynchron sind, PLL-Ausgänge unconstrained, I/O-Pfade ohne
   Delay-Angaben, false paths fehlen).

Nur wenn die Constraints vollständig und korrekt sind, kann man sagen, was wirklich im RTL
zu ändern ist.

---

## Schritt 1 – Die .sdc komplett machen (Constraints)

Der übliche, minimale, aber vollständige Startblock für 50 MHz:

```sdc
# 50-MHz-Eingangstakt auf dem Port 'clk'
create_clock -name clk50 -period 20.000 [get_ports {clk}]

# Falls ein PLL im Design steckt: Ausgangstakte automatisch ableiten
derive_pll_clocks

# Realistische Clock-Uncertainty zwischen unabhängigen Takten (wichtig fürs Closuen)
derive_clock_uncertainty

# Asynchrone Takt-Domänen klar trennen (sonst werden unabhängige Takte
# miteinander getimt und erzeugen falsche negative Slack)
set_clock_groups -asynchronous -group {clk50} -group {clk_derived}
```

### Was fehlt typischerweise (und wozu) – die Check-Liste:

| Fehlende Zeile | Folge | Konkretes Beispiel |
|---|---|---|
| `derive_pll_clocks` | PLL-Ausgänge liegen im Timing (hohe Fmax auf "ungestreämtem" Takt) | mit PLL zwingend nötig |
| `derive_clock_uncertainty` | fehlende Jitter/Skew-Reserve → Slack zu optimistisch/realistisch falsch | immer empfehlenswert |
| `set_clock_groups -asynchronous` | unabhängige Takte werden falsch als zusammenhängend getimt → falsche negative Slack an der Domänen-Grenze | CDC zwischen zwei Takten |
| `set_input_delay` / `set_output_delay` | I/O-Pfade falsch oder gar nicht getimt | Daten von/von externen Bausteinen |
| `set_false_path` (Reset, CDC-Handshake, async.) | nicht relevante Pfade ziehen an Slack | `reset_n`, synchronisierte Handshakes |
| `set_multicycle_path` | zu strenge Single-Cycle-Annahme für langsame Zähler/Logik | `set_multicycle_path 2 -setup …` |

### Konkretes Beispiel mit I/O und False/Multicycle:

```sdc
# I/O: Daten kommen 2 ns nach der Taktflanke
set_input_delay  -clock clk50 -max 2.0 [get_ports {data_in}]
set_input_delay  -clock clk50 -min 0.5 [get_ports {data_in}]
# Ausgang wird 3 ns vor der nächsten Flanke abgegriffen
set_output_delay -clock clk50 -max 3.0 [get_ports {data_out}]
set_output_delay -clock clk50 -min 1.0 [get_ports {data_out}]

# Asynchrones Reset: keine Timing-Beziehung
set_false_path -from [get_ports {reset_n}]

# Pfad mit 2 Zyklen Zeit (z. B. langsamer Zähler -> Ausgaberegister)
set_multicycle_path 2 -setup -from [get_pins {cnt_reg*}] -to [get_pins {out_reg*}]
```

> Erst wenn das drin ist, den Timing-Report neu erzeugen:
> ```tcl
> report_timing -setup -npaths 10   # in der Timing Analyzer Tcl-Console
> ```
> Und den **kritischsten Pfad** (niedrigster Slack) anschauen, nicht irgendeinen.

---

## Schritt 2 – Wenn der Slack nach korrekten Constraints **immer noch** negativ ist

Dann ist es ein echtes RTL-Timing-Problem bei 50 MHz. Die üblichen Fixes, in
Reihenfolge der Wirksamkeit:

1. **Logik-Tiefe auf dem kritischen Pfad reduzieren** – die häufigste Ursache:
   - Pipeline-Register in die lange Kombinatorik einfügen (FF-Stufe in der Mitte).
   - Große Kombinationen (lange Addierer, große Multiplexer, tiefe Case-Ketten)
     auf mehrere FF-Stufen aufteilen.
2. **Clock-Enables statt Gated Clocks verwenden** – gegatete Takte (z. B.
   `and` auf `clk`) verschlechtern Timing und erhöhen Skew. Statt
   `clk_en` auf den Takt zu legen, den Takt immer laufen lassen und das
   Enable an die FF-Clock-Enable-Pins legen.
3. **Synthese-/Fitter-Einstellungen**: Timing-Driven Synthesis / Optimierung
   aktiviert lassen, ggf. `fmax`-Optimierung priorisieren.
4. **Hold-Slack prüfen**: negative **Hold**-Slack ist meist ein
   Constraint-/Skew-Thema (Uncertainty, Clock-Groups), nicht ein
   Logik-Tiefen-Thema. Getrennt behandeln:
   `report_timing -hold -npaths 10`.
5. **Metastabilität an CDC korrekt behandeln**: Daten nie direkt über eine
   asynchrone Takt-Domänen-Grenze; 2-FF-Synchronisierer + `set_false_path`
   zwischen den Domänen.

---

## Empfohlene Reihenfolge zum Vorgehen

1. **Constraints quartieren**: erst die .sdc prüfen/ergänzen (PLL-Takte,
   Uncertainty, Clock-Groups, I/O-Delays, false paths). → Timing neu berichten.
2. **Echten kritischen Pfad identifizieren**: `report_timing -setup` auf den
   schlechtesten Slack-Pfad.
3. **RTL-Fix wählen**: Pipeline-Stufen / Logik-Tiefe reduzieren ODER
   Multicycle (nur wenn die Funktion tatsächlich mehr als einen Takt braucht).
4. **Neu kompilieren + berichten** und gegenprüfen.

---

## Kurzfassung (TL;DR)

- Eine .sdc mit **nur** `create_clock` ist unvollständig → der negative Slack ist
  zunächst nicht vertrauenswürdig.
- **Erst ergänzen**: `derive_pll_clocks`, `derive_clock_uncertainty`,
  `set_clock_groups -asynchronous` (asynchrone Domänen), `set_input_delay` /
  `set_output_delay`, `set_false_path` (Reset/CDC), ggf. `set_multicycle_path`.
- **Dann erst das RTL fixen**: kritischen Pfad mit `report_timing` suchen und die
  Logik-Tiefe durch Pipeline-Register reduzieren (oder Multicycle, falls fachlich
  zulässig); Clock-Enables statt Gated Clocks verwenden.
- **Hold** und **Setup** getrennt prüfen — negative Hold-Slack ist meist ein
  Constraint-/Skew-Thema, kein Logik-Tiefen-Thema.
