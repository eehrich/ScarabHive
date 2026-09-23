# Migration eines VHDL-Entwurfs von Xilinx nach Intel (Altera/Quartus)

**Frage:** Ich migriere einen VHDL-Entwurf von Xilinx nach Intel. Wo liegen die Unterschiede?

**Kurzantwort:** Reines, portables HDL (Standard-VHDL-2008 ohne vendor-spezifische Primitives/Megafunktionen) migriert meist mit wenig Aufwand: Dateien einbinden, Constraints übersetzen, Projektdateien neu anlegen. Der eigentliche Aufwand steckt in den **technologieabhängigen Teilen**: instantierte Primitives, IP-Cores, Timing-/Pin-Constraints und Synthese-Attribute. Diese sind herstellerspezifisch und müssen ersetzt bzw. regeneriert werden.

> Wichtig: Zuerst Ziel-Familie und -Baustein (z. B. Cyclone V, MAX 10, Cyclone 10 LP) bzw. die Quartus-Edition bestimmen. **Lite** (kostenlos) unterstützt nur Cyclone/MAX 10/MAX II-V; **Standard** Arria/Stratix; **Pro** nur für Agilex/Stratix 10/Arria 10. Nicht jede Xilinx-Familie hat ein 1:1-Pendant - Ressourcen (ALM vs. LUT, PLLs, Transceiver, RAM) unterscheiden sich.

---

## 1. Projekt- und Constraints-Dateien

| Aspekt | Xilinx (Vivado / ISE) | Intel (Quartus Prime) |
|--------|------------------------|------------------------|
| Projektdatei | .xpr (Vivado), Tcl-Umgebung | .qpf (Zeiger) + .qsf (alle Settings, Tcl-Syntax) |
| Timing-Constraints | .xdc (Vivado) / .ucf (ISE) | .sdc (SDC-Standard) |
| Pin-Zuweisung | set_property PACKAGE_PIN x [get_ports y] | set_location_assignment PIN_xx -to y |
| IO-Standard | set_property IOSTANDARD LVCMOS33 [...] | set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to y |
| Quellenliste | automatisch über Add Sources | jede .vhd-Datei explizit via set_global_assignment -name VHDL_FILE src/foo.vhd |

Minimales Intel-Projekt in der .qsf (Hinweis: **Top-Entity-Name == Projektname** ist die Quartus-Konvention):

```tcl
set_global_assignment -name FAMILY "Cyclone V"
set_global_assignment -name DEVICE 5CEBA4F23C7
set_global_assignment -name TOP_LEVEL_ENTITY top
set_global_assignment -name VHDL_FILE src/top.vhd
set_global_assignment -name VHDL_FILE src/sub.vhd
set_global_assignment -name SDC_FILE constraints.sdc
set_location_assignment PIN_M9 -to clk
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to led[0]
```

---

## 2. Vendor-Bibliotheken und Primitive (der größte Aufwand)

Xilinx liefert die **UNISIM**-Bibliothek mit technologieabhängigen Primitives. Diese existieren in Quartus so **nicht** - sie müssen durch Intel-Pendants ersetzt bzw. ersatzlos gestrichen werden (Quartus fügt viele Puffer automatisch ein).

| Xilinx-Primitive (VHDL) | Intel-Ersatz (Quartus) | Anmerkung |
|-------------------------|------------------------|-----------|
| BUFG, BUFGP, IBUFG (globale Takte) | ALTCLKCTRL / gar nichts | Quartus fügt Taktrouting automatisch ein; BUFG-Instanzen entfernen, Signal direkt verdrahten |
| IBUF, OBUF, IOBUF | ALTIOBUF / nichts | werden durch Quartus automatisch ergänzt |
| IBUFDS, OBUFDS (differenziell) | ALTIOBUF mit diff-IO-Standard | Ports entsprechend verdrahten |
| DCM, MMCM, PLL (Takt-Management) | altpll / altera_pll (IP-Core) | über IP-Catalog regenerieren; andere Port-Namen |
| BUFGCE (Takt mit Enable) | Clock-Enable-Logik oder ALTCLKCTRL | Xilinx-spezifisch |
| Block-RAM RAMB36E1 / RAMB18E1 | altsyncram bzw. Inferenz | besser: RAM aus Verhaltenscode ableiten lassen |
| SRL16 / SRLC32E | automatische Schieberegister-Inferenz | meist Attribut synthesis shreg_extract |
| DSP48 | DSP-/ALM-Schaltungen bzw. altera_mult_add | via Inferenz |
| SerDes/Transceiver GTP/GTX/GTY | ALT2GXB / ALTXcvr oder Native Phy IP | komplett neu projektiert |
| IDELAY / ODELAY | andere Delay-Ressourcen | nur bei Schnittstellen relevant |

Instantierte Primitives erfordern in Quartus die passende Vendor-Bibliothek, z. B.:

```vhdl
-- Xilinx (nicht mehr gültig):
library UNISIM; use UNISIM.VComponents.all;

-- Intel/Megafunktionen:
library altera_mf;
use altera_mf.altera_mf_components.all;  -- z. B. altpll, altsyncram
library lpm; use lpm.lpm_components.all; -- LPM-Funktionen
```

---

## 3. IP-Cores

- **Xilinx:** IP-Catalog / CoreGen, Datei .xci; generierte HDL mit Xilinx-Bibliotheksreferenzen.
- **Intel:** IP-Catalog und **Platform Designer** (früher Qsys); generierte IP liefert eine .qip-Datei (Dateiliste, in der .qsf referenziert) plus VHDL-/Verilog-Wrapper.
- **IP ist NICHT portierbar** - jedes Core (PLL, FIFO, RAM, Transceiver, SerDes, DDR-Controller) muss **neu generiert** werden. Port-Namen und Parameter sind unterschiedlich. Generierte Dateien nicht von Hand ändern (werden überschrieben).
- Häufigste Cores: altpll (PLL), Intel-Speicher-Cores (RAM/FIFO). Für freie Nutzung den IP-Evaluierungsmodus in den Quartus-Settings deaktivieren.

---

## 4. Takt- und Timing-Constraints (.sdc)

Beide nutzen SDC, aber die Konstrukte prüfen. Minimales Quartus-.sdc:

```sdc
create_clock -name clk50 -period 20.000 [get_ports {clk}]
derive_pll_clocks
derive_clock_uncertainty
set_clock_groups -asynchronous -group {clk50} -group {clk2}
set_input_delay  -clock clk50 -max 2.0 [get_ports {data_in}]
set_output_delay -clock clk50 -max 3.0 [get_ports {data_out}]
set_false_path     -from [get_ports {reset_n}]
```

- derive_pll_clocks erzeugt Takte auf den PLL-Ausgängen automatisch (in Vivado teils implizit).
- Ohne create_clock ist die Quartus-Timing-Analyse (quartus_sta) wertlos.
- XDC-/UCF-Eigenschaften (set_property ...) nach SDC-Befehlen übersetzen.

---

## 5. Synthese-Attribute / -Pragmas in VHDL

| Zweck | Xilinx | Intel/Quartus |
|-------|--------|---------------|
| Signal nicht eliminieren | KEEP / DONT_TOUCH | KEEP, DONT_TOUCH, altera_attribute |
| Max. Fanout | MAX_FANOUT | MAX_FANOUT (teils über QSF) |
| Register/SRL | SHREG_EXTRACT | synthesis shreg_extract |
| RAM-Stil | RAM_STYLE, ROM_STYLE | ramstyle (M512, M4K, MLAB) |

In Quartus werden unterstützte VHDL-Attribute im Paket **altera_syn_attributes** (Bibliothek altera) deklariert, z. B.:

```vhdl
attribute altera_attribute : string;
attribute altera_attribute of label : signal is "-name KEEP 1";
```

Die VHDL-Attribut-Syntax selbst ist Standard; nur die Namen weichen ab.

---

## 6. Sprachunterstützung (VHDL)

- Intel/Quartus unterstützt **VHDL-2008** umfassend; Xilinx/Vivado war beim VHDL-2008-Support historisch nachlässiger (Features spät oder teils nur für Simulation). Für vollständigen VHDL-2008-Support gilt Intel/Microchip als besser.
- Nutzt der Code nur IEEE-Standard-Bibliotheken (std_logic_1164, numeric_std), gibt es meist **keine** Sprachprobleme.
- Unterschiede entstehen erst durch vendor-spezifische Pakete/Bibliotheken.

---

## 7. Simulation / Testbench

| Aspekt | Xilinx | Intel |
|--------|--------|-------|
| Simulator | XSim (Vivado) | Questa/ModelSim - Altera Edition |
| Primitive-Sim-Modelle | UNISIM / UNIMACRO | altera_mf, altera_lnsim u. a. |
| Ablauf | Testbench gegen RTL | vlog / vsim oder .do-Datei |

Kommt das RTL mit Intel-IP (PLL/RAM), müssen für die Simulation die Intel-Sim-Bibliotheken geladen werden (via .qip-Referenz). Der EDA-Netlist-Writer erzeugt für die Post-Synthesis-Simulation .vo + .sdo (SDF).

---

## 8. Typische Fallstricke / Konventionen

- **Top-Level-Entity == Projektname** in Quartus zwingend, sonst Synthesefehler.
- **Pins/I/O-Standards** kommen vom Board (z. B. 3.3-V LVTTL, 2.5-V, LVCMOS33); falscher Standard beschädigt Pins. Aus dem Board-Handbuch übernehmen.
- **Edition** richtig wählen - FAMILY / DEVICE müssen von ihr unterstützt werden.
- **RAM/DSP aus Verhalten ableiten** statt Primitives zu instanziieren macht den Code portabel und die Migration nahezu trivial.
- **Gated Clocks vermeiden** und Clock-Enables nutzen (besseres Timing in Quartus); asynchrone Resets synchronisieren.

---

## Checkliste für die Migration

1. [ ] Ziel-Familie/Baustein und Quartus-Edition wählen (Lite/Standard/Pro).
2. [ ] .qpf + .qsf neu anlegen, alle .vhd-Dateien eintragen (auch Untermodule!).
3. [ ] UNISIM-/Xilinx-Primitives ersetzen (BUFG/IBUF/OBUF streichen, DCM/MMCM in PLL).
4. [ ] IP-Cores (.xci) in Intel-IP (.qip) neu generieren.
5. [ ] .xdc/.ucf-Constraints nach .sdc übersetzen (create_clock, delays, false_paths).
6. [ ] Pin-Zuweisungen und I/O-Standards vom Board (nicht aus Vivado) übernehmen.
7. [ ] Xilinx-Synthese-Attribute durch Intel-Attribute ersetzen (altera_attribute, ramstyle, ...).
8. [ ] Testbench gegen Questa/ModelSim anpassen, Intel-Sim-Bibliotheken laden.
9. [ ] Analysis & Synthesis, Fitter, Assembler, Timing Analyzer; Timing schließen.

> **Merksatz:** Je portabler (IEEE-nur, Verhaltenscode, inferierte RAM/DSP), desto kleiner der Migrationsaufwand. Die VHDL-Sprache selbst ist identisch; die Unterschiede konzentrieren sich auf Tool-/Projektdateien, Libraries, IP, Constraints und Synthese-Attribute.
