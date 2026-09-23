# Negative Slack bei 50 MHz — was am .sdc/vom Setup zu aendern ist

**Frage:** *„Mein Projekt wirft negative Slack auf 50 MHz und mein .sdc hat nur create_clock. Was muss ich aendern?"*

Ein negativer Setup-Slack bei 50 MHz ist ein klassisches Timing-Closure-Problem. Ein
`.sdc`, das ausschliesslich `create_clock` enthaelt, ist zu duenn: Es beschreibt nur
den Takt, aber weder PLL-Ausgaenge, noch Clock-Uncertainty, noch I/O-Delays, noch
unrelevante (False/Multi-Cycle) Pfade. Dadurch werden Pfade bemessen, die gar nicht
kritisch sind, und echte kritische Pfade bleiben unversorgt.

> Wichtig: Ohne vollstaendige Constraints ist der Timing-Report nicht aussagekraeftig.
> Negative Slack ist erst dann ein *reales* Problem, wenn die Constraints stimmen.

---

## 1. Erst die Constraints vervollstaendigen (nicht sofort das RTL aendern)

Ein vollstaendiges Start-Datei-Set, das ueber `create_clock` hinausgeht:

```sdc
# 50 MHz Eingangstakt (Period = 1000/50 = 20 ns)
create_clock -name clk50 -period 20.000 [get_ports {clk}]

# Wenn ein PLL verwendet wird: dessen Ausgaenge automatisch ableiten
derive_pll_clocks
derive_clock_uncertainty

# Verschiedene (async) Takt-Domaenen nicht gemeinsam timen
set_clock_groups -asynchronous -group {clk50} -group {clk2}

# E/A-Delays nur setzen, wenn die tatsaechlichen Werte bekannt sind
# set_input_delay  -clock clk50 -max 2.0 [get_ports {data_in}]
# set_output_delay -clock clk50 -max 3.0 [get_ports {data_out}]
```

Schritte in dieser Reihenfolge:
1. `derive_pll_clocks` einbauen (falls PLL) und `derive_clock_uncertainty` ergaenzen.
2. Konsistente Taktgruppen definieren (`set_clock_groups`).
3. Danach neu kompilieren und `report_timing -setup -npaths 10` ausfuehren.
4. Erst jetzt ansehen, **welcher Pfad** wirklich negativ ist — meist der kritische Pfad.

---

## 2. Den kritischen Pfad identifizieren

```tcl
report_timing -setup -npaths 10
```

Punkte, auf die man im Report achten muss:
- **Slack** (negativ = Fehler), **Data Path / Clock Path / Required Time**.
- Welche Register/Logikstaute (`Logic Levels`) liegen zwischen `clk` und der
  Ziel-Registerkante? Je mehr Logikstaute, desto groesser die Verzoegerung.

---

## 3. Eigentliche Timing-Closure-Massnahmen

Die haeufigsten Fixes gegen negative Slack (in aufsteigender Aufwand-Reihenfolge):

| Massnahme | Was/wo | Aufwand |
|-----------|--------|---------|
| **False Path** auf asynchronen Resets / CDC-Handshakes | SDC | niedrig |
| **Multi-Cycle Path** auf langsame Zaehler-Pfade | SDC | niedrig |
| **Pipeline einschieben** auf dem kritischen Pfad | RTL | mittel |
| **Kombinatorische Tiefe reduzieren** (Logik umstrukturieren) | RTL | mittel |
| **Clock-Enable statt gated Clock** | RTL | mittel |
| **Taktgruppen korrigieren** (unnötige Synchronisation vermeiden) | SDC | niedrig |

Konkrete SDC-Beispiele:

```sdc
# Asynchroner Reset: keine Timing-Relation
set_false_path -from [get_ports {reset_n}]

# Cross-Domain-Handshake nach Synchronisation
set_false_path -from [get_clocks clk50] -to [get_clocks clk2]

# Pfad darf 2 Takte brauchen (z.B. langsamer Zaehler -> Ausgang)
set_multicycle_path 2 -setup -from [get_pins {cnt_reg*}] -to [get_pins {out_reg*}]
```

Und im RTL (Beispiel Pipeline, um die Logikstaute auf dem kritischen Pfad zu
halbiert/splitten):

```verilog
// statt:  y <= a * b + c * d;   // eine lange kombinatorische Stufe
// besser:
wire s1 = a * b;   // Stufe 1
reg  r1;
always @(posedge clk) r1 <= s1;
y <= r1 + (c * d); // Stufe 2, getrennt durch ein Register
```

Regel aus dem Skill: **“Reduce logic depth on the critical path: insert pipeline
registers, split combinational functions across more FF stages.”**

---

## 4. Was NICHT zu tun ist (typische Fallstricke)

- **Nicht einfach den Takt kuerzer/laenger machen**, um den Slack kuenstlich zu
  verschwinden. Das verfaelscht das Ergebnis.
- **Nicht gated Clocks bauen** — sie verschlechtern das Timing (Skew) und sollten
  durch Clock-Enables ersetzt werden.
- **Keine kombinatorischen Schleifen** im RTL lassen — sie brechen die Synthese
  und das Timing.
- **Nicht erst das RTL umbauen**, bevor die Constraints vollstaendig sind. Oft
  verschwindet der “Fehler” schon nach korrekten `derive_pll_clocks` /
  `derive_clock_uncertainty` / `set_clock_groups`.

---

## 5. Zusammenfassung — konkrete TODO-Liste

1. `.sdc` vervollstaendigen: `derive_pll_clocks` + `derive_clock_uncertainty`
   (falls PLL) und Taktgruppen via `set_clock_groups`.
2. Neu kompilieren, `report_timing -setup -npaths 10` ausfuehren.
3. Den tatsaechlich negativen Pfad ansehen (Logic Levels, beteiligte Register).
4. False-/Multi-Cycle-Pfade setzen, die fachlich erlaubt sind.
5. Falls immer noch negativ: Pipeline auf dem kritischen Pfad einbauen bzw.
   kombinatorische Tiefe reduzieren.
6. Board-Infos (Device, Taktfrequenz, I/O-Standard) verifizieren — falsche
   Annahmen fuehren zu falschen Constraints.

---

**Konventionen aus dem Skill (zur Referenz):**
- Periode in ns: `period = 1000 / freq_MHz` → 50 MHz = 20.000 ns.
- Synthesierbares Verilog, 2-FF-Synchronizer ueber Takt-Domaenen, kein gated Clock.
- SDC wird im `.qsf` ueber `SDC_FILE` referenziert.
