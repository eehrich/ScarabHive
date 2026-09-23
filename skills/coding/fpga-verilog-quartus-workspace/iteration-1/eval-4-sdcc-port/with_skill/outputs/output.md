# VHDL-Entwurf von Xilinx nach Intel (Quartus) migrieren — die Unterschiede

> Frage: *„Ich migriere einen VHDL-Entwurf von Xilinx nach Intel. Wo liegen die Unterschiede?"*
> Antwortgrundlage: Skill `fpga-verilog-quartus` (SKILL.md + References `project-files-qsf`, `verilog-for-synthesis`, `quartus-basics`, `timing-sdc`).

Die gute Nachricht zuerst: **Der VHDL-/RTL-Kern (Entity, Architecture, `ieee.std_logic_1164`, `numeric_std`) ist zu ~90 % portabel.** Die Unterschiede stecken fast alle im Tool, in den Projekt-Dateien, den Constraints, den IP-Cores/Primitiven und der Simulation — nicht im eigentlichen VHDL-Code.

---

## 1. Werkzeug- und Projektdaten (größter Unterschied)

| Thema | Xilinx (Vivado/ISE) | Intel (Quartus Prime) |
|------|---------------------|------------------------|
| Projektdatei | `.xpr` (XML) | `.qpf` (Zeiger) + `.qsf` (Tcl-Settings) |
| Constraint-Datei | `.xdc` (SDC-Superset) | `.sdc` (reines SDC) |
| HDL-Auflistung | in `.xpr`/Sources | **jede Datei explizit** per `set_global_assignment -name VHDL_FILE ...` im `.qsf` |
| Compilerteile | synth, `place_design`, `bitgen` | `quartus_map`, `quartus_fit`, `quartus_asm`, `quartus_sta` |
| VHDL-Kopf | — | in Lite/Standard oft nötig, Pro eigenständig |

**Merkregel (Skill-„golden rule"):** Projektname == Top-Level-Entity; `TOP_LEVEL_ENTITY` muss exakt mit einer Entity übereinstimmen. Im `.qsf` muss **jede** synthetisierbare HDL-Datei gelistet werden, sonst wird sie ignoriert („can't find entity").

Minimales Quartus-Projekt statt `.xpr`:

```tcl
# myproj.qpf
QUARTUS_PROJECT = "myproj" REVISION = "myproj"

# myproj.qsf
set_global_assignment -name FAMILY "Cyclone V"
set_global_assignment -name DEVICE 5CEBA4F23C7
set_global_assignment -name TOP_LEVEL_ENTITY top
set_global_assignment -name PROJECT_OUTPUT_DIRECTORY output_files
set_global_assignment -name VHDL_FILE src/top.vhd
set_global_assignment -name VHDL_FILE src/sub.vhd
set_global_assignment -name SDC_FILE constraints.sdc
```

---

## 2. Pins & I/O-Standards (Constraint-Syntax unterscheidet sich)

Pin-/IO-Assignments sind board-spezifisch (aus dem Board-Handbuch, nie raten!) und die Syntax ist je Tool anders:

```tcl
# Xilinx (XDC)
set_property PACKAGE_PIN N8  [get_ports clk]
set_property IOSTANDARD  LVCMOS33 [get_ports clk]

# Intel (QSF: Location + IO_STANDARD als getrennte Assignments)
set_location_assignment PIN_N8 -to clk
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to clk
```

Die **Pin-Namen selbst** (`PIN_N8`) stammen vom jeweiligen Package und unterscheiden sich zwischen Xilinx- und Intel-Bauteilen — eine Pin-Zuordnung ist **nie 1:1 übernehmbar** und muss komplett neu aus dem Intel-Board-Handbuch gezogen werden. Gleiches gilt für die I/O-Standard-Namen (`LVCMOS33` ↔ `3.3-V LVTTL`).

---

## 3. Timing-Constraints (SDC — weitgehend kompatibel!)

Weil **XDC ein SDC-Superset** und Quartus reines SDC verwendet, sind Timing-Constraints großteils wörtlich portabel:

```sdc
create_clock -name clk50 -period 20.000 [get_ports {clk}]
# bei PLL:
derive_pll_clocks
derive_clock_uncertainty
set_clock_groups -asynchronous -group {clk50} -group {clk2}
set_false_path -from [get_ports {reset_n}]
```

Unterschiede:
- `get_ports`/`get_pins` funktionieren in beiden; Xilinx-spezifische Filter (`get_cells`, `get_nets`) und `set_property`-Timing-Kommandos existieren in Quartus nicht.
- Skill-Regel: **Ohne `create_clock` ist jedes Timing-Report wertlos** — zuerst die Clock anlegen.

---

## 4. VHDL-Sprachunterschiede (nur an wenigen Stellen)

- **Standard-Bibliotheken**: `ieee.std_logic_1164` + `ieee.numeric_std` sind in beiden Tools voll unterstützt → der portable Kern.
- **Vendor-Packages ersetzen**: Xilinx `UNISIM` (Primitive-Simulation), `XilinxCoreLib` → Intel `ALTERA` / `altera_mf`. `ieee.std_logic_arith`/`std_logic_unsigned` unterstützt Quartus zwar auch, sauberer Port heißt aber: **auf `numeric_std` umstellen**.
- **VHDL-2008**: in Quartus **Pro** gut unterstützt, in **Lite/Standard** je Release eingeschränkter als Vivado. 2008-Konstrukte (`if generate` mit `else`, unconstrained Records, `condition ? a : b`) ggf. zurückportieren.
- Konfigurationen / `work`-Library funktionieren in beiden, müssen aber ggf. an Quartus angepasst werden.

---

## 5. Primitive & IP-Cores (hier liegt der meiste Umbau)

Primitive/FPGA-Komponenten heißen und funktionieren bei Intel anders:

| Funktion | Xilinx | Intel (Quartus) |
|----------|--------|-----------------|
| PLL/Clock | DCM / MMCM / Clocking Wizard | `altpll` / `altera_pll` |
| Clock-Buffer | BUFG / IBUFG | `altclkctrl` |
| IO-Buffer | IBUF / OBUF / IBUFDS / OBUFDS / IOBUF | `ALT_INBUF` / `ALT_OUTBUF` (oder per IO_STANDARD inferiert) |
| Block-RAM | Block RAM / BRAM-Generator | `altsyncram`, inferierte M9K/M10K/MLAB |
| FIFO | FIFO Generator | `scfifo` / `dcfifo` |
| DSP | DSP48 | DSP-Blöcke (via `*`-RTL) |
| Transceiver | GT-Kerne | Transceiver Native PHY |

- Jede instanziierte Xilinx-Primitive (**`IBUFDS`, `BUFG`, `MMCME2`, ...**) muss durch das **Intel-Pendant** ersetzt werden — das ist die aufwändigste Handarbeit.
- **IP-Cores** werden über den **IP Catalog** bzw. **Platform Designer (Qsys)** neu generiert; Xilinx-`.xci`-IP ist nicht übernehmbar. Generierte Intel-IP liefert eine `.qip` (Dateiliste) + Wrapper, die im `.qsf` referenziert wird.
- **Generierte Dateien nie von Hand editieren** — bei Intel regenerieren statt patchen.
- RAM/DSP via RTL inferieren statt Primitive instanziieren = deutlich portabler.

---

## 6. Simulation (Bibliotheken!)

- **Xilinx**: Vivado Simulator (`xsim`) oder ModelSim/Questa; vorkompilierte Libraries `xilinx_*` / `UNISIM`.
- **Intel**: ModelSim/Questa **Intel FPGA Edition**; VHDL mit den Libraries **`altera_mf` / `altera_lnsim`** vorkompilieren, sonst schlagen IP-Instanzen in der Simulation fehl.
- Die Testbench selbst (Entity + `process`/`wait`) ist portabel; nur die Vendor-Library für IP-Modelle von Xilinx- auf Intel-Library umstellen.

---

## 7. Bitstrom erzeugen & programmieren

- **Xilinx**: `.bit` / `.bin` / `.mcs`; Programmierung über Vivado Hardware Manager (JTAG).
- **Intel**: **`.sof`** (flüchtige SRAM-Konfig) und **`.jic`/`.pof`** (für Flash/EPCS); Programmierung via **`quartus_pgm`** oder GUI mit USB-Blaster. `.sof`→`.jic`-Konvertierung mit `quartus_cpf`.

---

## 8. Geräte-/Edition-Wahl (zuerst klären!)

Es entscheidet sich zuerst, **welche Intel-Familie** und damit **welche Quartus-Edition** gilt:

- **Quartus Lite** (kostenlos): Cyclone IV/V, Cyclone 10 LP, MAX 10, MAX II/V → üblich für DE10-Lite/DE0.
- **Quartus Standard** (kostenpflichtig): Arria 10/V, Cyclone IV/V/10 LP, Stratix V, MAX.
- **Quartus Pro**: Agilex 3/5/7/9, Stratix 10, Arria 10, Cyclone 10 GX.

Ein Xilinx-Ziel (Artix/Kintex/Virtex/Spartan/Zynq) muss auf ein **Intel-Pendant** (Cyclone/Arria/Stratix/MAX/Agilex) abgebildet werden — Größe, ALMs, RAM, DSP und Pins weichen ab.

---

## Checkliste für die Migration

1. **Edition/Familie/Bauteil** klären (Quartus Lite/Standard/Pro ↔ Zielbaustein).
2. **Projekt** als `.qpf`+`.qsf` neu aufbauen, **alle** VHDL-Dateien listen, `TOP_LEVEL_ENTITY` prüfen.
3. **Pins & IO-Standards** neu aus dem Intel-Board-Handbuch ziehen (nicht 1:1 übernehmen).
4. **Timing-Constraints** als `.sdc` übernehmen (weitgehend 1:1), `create_clock` zuerst.
5. **Xilinx-Primitive/`UNISIM`** durch Intel-Pendants / inferred RTL ersetzen; `XilinxCoreLib` → `altera_mf`.
6. **IP-Cores** über IP Catalog/Platform Designer neu generieren; `.qip` referenzieren; Generiertes **nicht** handeditieren.
7. **VHDL-2008**-Features auf die Edition-Unterstützung prüfen.
8. **Simulations-Libraries** (`altera_mf`/`altera_lnsim`) in der Intel-ModelSim/Questa vorkompilieren.
9. Kompilieren: `quartus_map` → `quartus_fit` → `quartus_asm` (CLI: Reference `quartus-cli-flow`), dann `.sof`/`.jic` programmieren.

> Hinweis zum Skill-Umfang: Dieses Skill-Dokument ist primär für **Verilog/SystemVerilog + Quartus** geschrieben. Für die VHDL-Migration gelten dieselben Quartus-/Constraint-/IP-/Projekt-Regeln; die obige Liste überträgt sie auf den VHDL-Fall.
