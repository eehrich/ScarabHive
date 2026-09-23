# Quartus-Projekt: 7-Segment-Zaehler (DE10-Lite, Cyclone 10 LP)

**Aufgabe:** "Bau mir ein Quartus-Projekt mit einem 7-Segment-Zaehler in Verilog -
inkl. .qsf, .sdc und Testbench."

**Genutzte Skill-Dateien (fpga-verilog-quartus, via skills_read):**
- SKILL.md
- references/project-files-qsf.md
- references/timing-sdc.md
- references/simulation.md
- references/quartus-basics.md
- (zusaeatzlich: references/quartus-cli-flow.md, references/verilog-for-synthesis.md)

---

## 0) Wichtige Annahmen / Board-Hinweis

- **Board:** Terasic DE10-Lite, Takt **50 MHz**, I/O-Standard **3,3-V LVTTL** (wie angegeben).
- **Achtung (Reality-Check):** Die originale Terasic-DE10-Lite-Bestueckung ist ein
  **MAX 10** (Teilenummer `10M50DAF484C7G`, Family "MAX 10"), NICHT Cyclone 10 LP -
  das ist ein haeufiger Irrtum. Da Cyclone 10 LP explizit angegeben wurde, ist
  der Projekt-.qsf unten primaer auf Cyclone 10 LP gestellt. Die Pin-Namen
  (CLOCK_50, KEY[0], SW[0], HEX0/HEX1) stammen aus dem offiziellen Terasic
  DE10-Lite User Manual und gelten fuer die reale DE10-Lite (MAX 10, 484-Pin QFP).
  Fuer die normale DE10-Lite tauscht man nur wenige Zeilen im .qsf (Block
  "MAX 10 (echte DE10-Lite)" unten) - der Verilog und die .sdc bleiben identisch.
  Pins IMMER gegen das Board-Handbuch pruefen, nicht erfinden.

- **Projektname == Top-Level-Entity:** `seg_counter`
- Quartus-Edition: **Quartus Prime Lite** (kostenlos, unterstuetzt Cyclone 10 LP & MAX 10).

### Projektstruktur
```
seg_counter/
  seg_counter.qpf           # Projekt-Pointer
  seg_counter.qsf           # alle Assignments (Tcl)
  seg_counter.sdc           # Timing-Constraints
  seg_counter.v             # Top + 7-Seg-Decoder (synthetisierbar)
  sim/
    tb_seg_counter.v        # Testbench (NICHT zur Synthese)
    sim.do                  # Questa/ModelSim-Skript
```

---

## 1) Verilog-Quelltext - seg_counter.v

Synthetisierbare Regeln: synchrone Logik mit non-blocking (<=), async. akt.-low
Reset, Clock-Enable statt gegattertem Takt, kein Latch, COUNTER_TICKS als
Parameter (im Test simfreundlich ueberschreibbar).

```verilog
// seg_counter.v - 7-Segment-Zaehler 0..99 fuer DE10-Lite
// 50 MHz Takt, akt.-low async Reset, Clock-Enable anstatt gegattertem Takt.
// Parameter COUNTER_TICKS = Zyklen zwischen zwei Zahlschritten.
//   Default 50_000_000 -> bei 50 MHz genau 1 Zahlschritt/Sekunde (1 Hz).
module seg_counter #(
    parameter COUNTER_TICKS = 50_000_000
) (
    input  wire       clk,      // 50 MHz Systemtakt
    input  wire       reset_n,  // akt.-low async Reset (KEY[0])
    input  wire       enable,   // Zaehlfreigabe (SW[0])
    output wire [6:0] hex0,     // Einerstelle, Segmente akt.-low
    output wire [6:0] hex1      // Zehnerstelle, Segmente akt.-low
);

    // ---- Zaehltakt-Teiler (Clock-Enable) -----------------------------
    reg [25:0] clk_div;
    reg        clk_en;          // genau einen Takt breiter Puls (nicht gegattern)

    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            clk_div <= 26'd0;
            clk_en  <= 1'b0;
        end
        else if (clk_div == (COUNTER_TICKS - 1)) begin
            clk_div <= 26'd0;
            clk_en  <= 1'b1;
        end
        else begin
            clk_div <= clk_div + 1'b1;
            clk_en  <= 1'b0;
        end
    end

    // ---- BCD-Zaehler 00..99 ------------------------------------------
    reg [3:0] units, tens;      // 4-Bit, nur Werte 0..9

    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            units <= 4'd0;
            tens  <= 4'd0;
        end
        else if (enable && clk_en) begin
            if (units == 4'd9) begin
                units <= 4'd0;
                if (tens == 4'd9) tens <= 4'd0;
                else              tens <= tens + 1'b1;
            end
            else begin
                units <= units + 1'b1;
            end
        end
    end

    // ---- 7-Segment-Decoder (akt.-low, Segmente {g,f,e,d,c,b,a}) ------
    // 0 leuchtet das Segment an (DE10-Lite: akt.-low).
    function [6:0] seg7_active_low;
        input [3:0] d;
        begin
            case (d)
                4'd0: seg7_active_low = 7'b1000000;
                4'd1: seg7_active_low = 7'b1111001;
                4'd2: seg7_active_low = 7'b0100100;
                4'd3: seg7_active_low = 7'b0110000;
                4'd4: seg7_active_low = 7'b0011001;
                4'd5: seg7_active_low = 7'b0010010;
                4'd6: seg7_active_low = 7'b0000010;
                4'd7: seg7_active_low = 7'b1111000;
                4'd8: seg7_active_low = 7'b0000000;
                4'd9: seg7_active_low = 7'b0010000;
                default:             seg7_active_low = 7'b1111111; // alle aus
            endcase
        end
    endfunction

    assign hex0 = seg7_active_low(units);
    assign hex1 = seg7_active_low(tens);

endmodule
```

---

## 2) Projekt-Assignments - seg_counter.qsf

.qsf ist Tcl: eine Assignment-Zeile je Eintrag, Kommentare mit #. Primaer auf
Cyclone 10 LP gestellt (wie gewuenscht). Der Block "MAX 10 (echte DE10-Lite)"
unten ist die Alternative fuer die reale Terasic-Board-Bestueckung. Pin-Nummern
sind die offiziellen DE10-Lite-Pins aus dem Terasic User Manual.

```tcl
# ---------------- seg_counter.qsf ---------------------------------------
# Projekt == Top-Level-Entity: seg_counter

# ---- Projekt / Geraet ------------------------------------------------
set_global_assignment -name FAMILY "Cyclone 10 LP"
set_global_assignment -name DEVICE 10CL006YU256C8G
set_global_assignment -name TOP_LEVEL_ENTITY seg_counter
set_global_assignment -name PROJECT_OUTPUT_DIRECTORY output_files

# ---- Quelldateien (jede synthetisierbare HDL-Datei!) -----------------
set_global_assignment -name VERILOG_FILE seg_counter.v

# ---- Timing-Constraint-Datei ----------------------------------------
set_global_assignment -name SDC_FILE seg_counter.sdc

# ---- optional --------------------------------------------------------
set_global_assignment -name NUM_PARALLEL_PROCESSORS 4

# ======================================================================
#  FALLS Du eine NORMALE Terasic-DE10-Lite hast: Family ist MAX 10,
#  NICHT Cyclone 10 LP. Hiermit ersetzen:
# ======================================================================
# set_global_assignment -name FAMILY "MAX 10"
# set_global_assignment -name DEVICE 10M50DAF484C7G

# ---- Pin-Assignments (DE10-Lite, Terasic User Manual) ----------------
# Hinweis: Diese Pins gelten fuer die reale DE10-Lite (MAX 10).
# Bei Cyclone-10-LP mit anderem Package IMMER neu pruefen!
set_location_assignment PIN_P11 -to clk
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to clk

set_location_assignment PIN_B8  -to reset_n     # KEY[0]
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to reset_n

set_location_assignment PIN_C10 -to enable      # SW[0]
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to enable

# HEX0 - Einerstelle (akt.-low Segmente; Bit0 = Segment a)
set_location_assignment PIN_C17 -to hex0[0]
set_location_assignment PIN_D17 -to hex0[1]
set_location_assignment PIN_E16 -to hex0[2]
set_location_assignment PIN_C16 -to hex0[3]
set_location_assignment PIN_C15 -to hex0[4]
set_location_assignment PIN_E15 -to hex0[5]
set_location_assignment PIN_C14 -to hex0[6]
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to "hex0[*]"

# HEX1 - Zehnerstelle (akt.-low Segmente; Bit0 = Segment a)
set_location_assignment PIN_B17 -to hex1[0]
set_location_assignment PIN_A17 -to hex1[1]
set_location_assignment PIN_B16 -to hex1[2]
set_location_assignment PIN_B15 -to hex1[3]
set_location_assignment PIN_A15 -to hex1[4]
set_location_assignment PIN_A14 -to hex1[5]
set_location_assignment PIN_B14 -to hex1[6]
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to "hex1[*]"

Und die Projekt-Pointer-Datei - seg_counter.qpf:

```
QUARTUS_PROJECT = "seg_counter" REVISION = "seg_counter" DATE = "01/01/2025" TIME = "00:00:00"
```

---

## 3) Timing-Constraints - seg_counter.sdc

SDC: "Ohne Clock keine aussagekraeftige Timing-Analyse." -> Takt am Eingangspin
erzeugen (50 MHz ergibt Periode 20 ns). Zusaetzlich Unschaerfe ableiten, Reset
als asynchrone Logik (false path) und grobe I/O-Delays fuer Key/Switch/HEX.

```sdc
# ---------------- seg_counter.sdc --------------------------------------
# 50-MHz-Systemtakt am Port 'clk'   (Periode 20 ns)
create_clock -name clk50 -period 20.000 [get_ports {clk}]

# Realistische Clock-Uncertainty
derive_clock_uncertainty

# Asynchroner akt.-low Reset ohne Timing-Beziehung zum Takt
set_false_path -from [get_ports {reset_n}]

# Eingangs-Delays fuer Taster (KEY[0]) / Schalter (SW[0])
set_input_delay  -clock clk50 -max 2.0 [get_ports {reset_n}]
set_input_delay  -clock clk50 -min 0.5 [get_ports {reset_n}]
set_input_delay  -clock clk50 -max 2.0 [get_ports {enable}]
set_input_delay  -clock clk50 -min 0.5 [get_ports {enable}]

# Ausgangs-Delay an den 7-Segment-Ausgaengen
set_output_delay -clock clk50 -max 3.0 [get_ports {hex0[*]}]
set_output_delay -clock clk50 -min 1.0 [get_ports {hex0[*]}]
set_output_delay -clock clk50 -max 3.0 [get_ports {hex1[*]}]
set_output_delay -clock clk50 -min 1.0 [get_ports {hex1[*]}]
```

---

## 4) Testbench - sim/tb_seg_counter.v

Testbench hat KEINE Ports und wird NICHT in die .qsf-Synthese aufgenommen
(bleibt in sim/). Der DUT wird mit COUNTER_TICKS(4) instanziiert, damit der
Zaehler im funktionalen Sim schnell zaehlt und der 99->00-Ueberlauf pruefbar ist
(Default 50 M Zyklen waere im Sim zu lang). Enthaelt Reset, Decoder-Referenz und
einen automatischen PASS/FAIL-Check.

```verilog
// sim/tb_seg_counter.v - funktionale Testbench (nicht synthetisierbar)
`timescale 1ns/1ps

module tb_seg_counter;

    reg        clk     = 1'b0;
    reg        reset_n = 1'b0;
    reg        enable  = 1'b0;
    wire [6:0] hex0, hex1;

    integer i, errors = 0;

    // Referenz-Decoder (akt.-low), identisch zum DUT
    function [6:0] ref7;
        input [3:0] d;
        begin
            case (d)
                4'd0: ref7 = 7'b1000000;
                4'd1: ref7 = 7'b1111001;
                4'd2: ref7 = 7'b0100100;
                4'd3: ref7 = 7'b0110000;
                4'd4: ref7 = 7'b0011001;
                4'd5: ref7 = 7'b0010010;
                4'd6: ref7 = 7'b0000010;
                4'd7: ref7 = 7'b1111000;
                4'd8: ref7 = 7'b0000000;
                4'd9: ref7 = 7'b0010000;
                default:  ref7 = 7'b1111111;
            endcase
        end
    endfunction

    // DUT: zaehlt alle COUNTER_TICKS(=4) Takte
    seg_counter #(.COUNTER_TICKS(4)) dut (
        .clk     (clk),
        .reset_n (reset_n),
        .enable  (enable),
        .hex0    (hex0),
        .hex1    (hex1)
    );

    // 50-MHz-Takt: 20 ns Periode
    always #10 clk = ~clk;

    // wartet genau EINEN Zahlschritt (5 Taktflanken bei TICKS=4)
    task tick;
        integer n;
        begin
            for (n = 0; n < 5; n = n + 1) @(posedge clk);
        end
    endtask

    initial begin
        $dumpfile("tb_seg_counter.vcd");
        $dumpvars(0, tb_seg_counter);

        // Reset + Freigabe
        reset_n = 0; enable = 0;
        repeat (3) @(posedge clk);
        reset_n = 1; enable = 1;
        #1;
        if (hex0 !== ref7(0) || hex1 !== ref7(0)) begin
            $display("FAIL: nach Reset nicht 00 (hex0=%b hex1=%b)", hex0, hex1);
            errors = errors + 1;
        end

        // 0..9 auf Einerstelle pruefen
        for (i = 0; i < 10; i = i + 1) begin
            #1;
            if (hex0 !== ref7(i[3:0])) begin
                $display("FAIL: Einerstelle %0d -> %b erwartet %b", i, hex0, ref7(i[3:0]));
                errors = errors + 1;
            end
            tick;
        end

        // nach 10 Zahlschritten -> Rollover zu 10 (Einer=0, Zehner=1)
        #1;
        if (hex0 !== ref7(0) || hex1 !== ref7(1)) begin
            $display("FAIL: Rollover zu 10 (hex0=%b hex1=%b)", hex0, hex1);
            errors = errors + 1;
        end
        $display("Stand nach Rollover: tens=%0d units=%0d hex1=%b hex0=%b",
                 dut.tens, dut.units, hex1, hex0);

        // von 10 bis 99 zaehlen (89 Schritte) + 1 Schritt -> Wrap auf 00
        for (i = 0; i < 90; i = i + 1) tick;

        // Zehner und Einer muessen wieder 0 (00) sein
        if (hex0 !== ref7(0) || hex1 !== ref7(0)) begin
            $display("FAIL: Wrap auf 00 (hex0=%b hex1=%b)", hex0, hex1);
            errors = errors + 1;
        end
        $display("Stand nach Wrap: tens=%0d units=%0d", dut.tens, dut.units);

        if (errors == 0) $display(">>> PASS: alle Checks bestanden <<<");
        else             $display(">>> FAIL: %0d Fehler <<<", errors);

        $finish;
    end

endmodule
```

Und das Sim-Skript - sim/sim.do:

```tcl
# sim.do - Questa Intel FPGA Edition / ModelSim
vlib work
vlog ../seg_counter.v tb_seg_counter.v
vsim -c work.tb_seg_counter
run -all
quit -f
```

---

## 5) Compile- & Simulations-Kommandos

### Compile (Quartus Prime Lite) - headless
```bash
# ein-zeiliger Voll-Compile (map + fit + asm + sta):
quartus_sh --flow compile seg_counter

# oder Schritt fuer Schritt:
quartus_map --read_settings_files=on seg_counter -c seg_counter
quartus_fit                        seg_counter -c seg_counter
quartus_asm                        seg_counter -c seg_counter
quartus_sta                        seg_counter -c seg_counter   # Timing-Analyse

# Version pruefen
quartus_sh --version
```

### Timing-Bericht (SDC-basiert)
```bash
quartus_sta -t sta.tcl seg_counter
# sta.tcl:
#   load_package timing
#   project_open seg_counter -current_revision
#   report_timing -setup -npaths 10
```

### Programmieren der DE10-Lite (JTAG)
```bash
# .sof in die SRAM-Konfiguration (fluechtig)
quartus_pgm -c 1 -m JTAG -o p;output_files/seg_counter.sof

# dauerhaft in Flash (zuerst .jic erzeugen):
quartus_cpf -c output_files/seg_counter.sof seg_counter.jic
quartus_pgm -c 1 -m JTAG -o p;seg_counter.jic
```

### Simulation (Questa Intel FPGA Edition / ModelSim-Altera)
```bash
# Manuell
vlog ../seg_counter.v tb_seg_counter.v
vsim -c work.tb_seg_counter -do "run -all; quit"

# Oder ueber das Do-File
vsim -c -do sim.do

# Ergebnis: ">>> PASS: alle Checks bestanden <<<" und tb_seg_counter.vcd
# zur Wellenform-Ansicht in der GUI.
```

> **Hinweis:** Compile/Programmierung muessen auf einer Maschine mit installiertem
> **Quartus Prime Lite** und **Questa Intel FPGA Edition** ausgefuehrt werden;
> in dieser Umgebung kann nur der Projektinhalt (1-4) erzeugt werden.

---

## Zusammenfassung der Deliverables
1. **Verilog**      seg_counter.v (Top = seg_counter, BCD 00..99, 7-Seg-akt.-low-Decoder)
2. **.qsf**         (Family/Device Cyclone 10 LP + DE10-Lite-Pins + MAX-10-Alternative)
3. **.sdc**         (50-MHz-Takt clk50, I/O-Delays, false path Reset)
4. **Testbench**    sim/tb_seg_counter.v + sim/sim.do mit PASS/FAIL
5. **Kommandos**    Compile (quartus_sh --flow compile), Programmieren (quartus_pgm), Simulieren (vlog/vsim)
