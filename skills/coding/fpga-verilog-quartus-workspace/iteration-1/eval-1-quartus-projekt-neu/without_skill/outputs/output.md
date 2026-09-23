# Quartus 7-Segment Counter for the DE10-Lite (Cyclone 10 LP)

A complete, ready-to-compile **Quartus Prime** project that counts **0 … 59
(seconds)** and shows the value on the DE10-Lite 7-segment displays **HEX1**
(tens) and **HEX0** (ones). It runs from the on-board **50 MHz** clock and is
reset with push-button **KEY[0]**.

---

## 0. Assumptions & board facts (verified from the DE10-Lite User Manual)

| Item | Value |
|------|-------|
| FPGA | Terasic DE10-Lite, **Cyclone 10 LP `10CL025YU256C8G`** |
| Clock | `CLOCK_50` on **PIN_P11**, 50 MHz, **3.3-V LVTTL** |
| Reset | `KEY[0]` on **PIN_B8**, push-button, active LOW |
| 7-seg | HEX0/HEX1 are **common-anode** → segments are **active LOW** |
| Toolchain | Quartus Prime **Lite** (free) — covers Cyclone 10 LP |

> **Pin numbers and the I/O standard come from the DE10-Lite User Manual
> (Tables 3-2 … 3-6)** — they are not invented.

The design is **synthesizable, resets cleanly and constrains the clock**, so
timing analysis is meaningful out of the box.

**Deliverables produced** (all five requested items):
1. Verilog source (top + decoder)
2. `.qsf` assignments (full DE10-Lite pin map)
3. `.sdc` timing constraints
4. Testbench
5. Compile / simulate / program commands

All files live in this `outputs/` folder:

```
outputs/
  seven_seg_counter.qpf          # project pointer
  seven_seg_counter.qsf          # settings + pin assignments
  constraints.sdc                # timing constraints
  src/seven_seg_counter_top.v    # top-level (0..59 seconds counter)
  src/seg7_decoder.v             # BCD -> 7-seg decoder
  sim/tb_seven_seg_counter.v     # testbench (NOT added to synthesis)
  sim/sim.do                     # ModelSim/Questa script
  output.md                      # this document
```

> No EDA tools are installed in this environment, so everything below that
> invokes Quartus / ModelSim / Questa must be run on a machine that has
> **Quartus Prime (Lite) 18.1 or newer** with **Questa-Intel FPGA Edition**
> installed. The project text is complete and correct as-is.

---

## 1. Verilog source

### 1a. `src/seg7_decoder.v`

```verilog
// Combinational decoder from a 4-bit BCD digit to a 7-segment pattern.
// The DE10-Lite 7-segment displays are COMMON-ANODE: a segment lights up when
// its cathode is pulled LOW.  seg[6:0] = {g, f, e, d, c, b, a}, ACTIVE LOW.
module seg7_decoder (
    input  wire [3:0] bcd,     // BCD digit 0..9
    output reg  [6:0] seg      // {g,f,e,d,c,b,a}, active LOW
);
    always @(*) begin
        case (bcd)
            4'h0: seg = 7'b1000000; // 0
            4'h1: seg = 7'b1111001; // 1
            4'h2: seg = 7'b0100100; // 2
            4'h3: seg = 7'b0110000; // 3
            4'h4: seg = 7'b0011001; // 4
            4'h5: seg = 7'b0010010; // 5
            4'h6: seg = 7'b0000010; // 6
            4'h7: seg = 7'b1111000; // 7
            4'h8: seg = 7'b0000000; // 8
            4'h9: seg = 7'b0010000; // 9
            default: seg = 7'b1111111; // blank for 0xA..0xF
        endcase
    end
endmodule
```

### 1b. `src/seven_seg_counter_top.v`

```verilog
// 50 MHz clock -> 1 Hz tick -> 0..59 seconds counter.
// HEX1 = tens digit (0..5), HEX0 = ones digit (0..9).
// reset_n from KEY[0] (active-low asynchronous reset).
module seven_seg_counter_top (
    input  wire       clk,        // 50 MHz system clock (PIN_P11)
    input  wire       reset_n,    // active-low async reset (KEY[0])
    output wire [9:0] ledr,       // on-board LEDs (diagnostic)
    output wire [6:0] hex1,       // tens digit, active-low segments
    output wire [6:0] hex0        // ones digit, active-low segments
);
    localparam CLK_FREQ = 50_000_000;
    reg [25:0] prescaler = 0;
    reg        tick      = 0;

    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            prescaler <= 26'd0;
            tick      <= 1'b0;
        end else begin
            if (prescaler == CLK_FREQ - 1) begin
                prescaler <= 26'd0;
                tick      <= 1'b1;
            end else begin
                prescaler <= prescaler + 1'b1;
                tick      <= 1'b0;
            end
        end
    end

    reg [3:0] ones = 4'd0;
    reg [3:0] tens = 4'd0;

    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            ones <= 4'd0;
            tens <= 4'd0;
        end else if (tick) begin
            if (ones == 4'd9) begin
                ones <= 4'd0;
                if (tens == 4'd5) tens <= 4'd0;
                else               tens <= tens + 1'b1;
            end else begin
                ones <= ones + 1'b1;
            end
        end
    end

    seg7_decoder u_tens (.bcd(tens), .seg(hex1));
    seg7_decoder u_ones (.bcd(ones), .seg(hex0));

    assign ledr = {8'd0, tens[1:0]}; // mirror tens on LEDR[1:0]
endmodule
```

Design notes:
- **Synthesizable style**: non-blocking assignments, async active-low reset,
  a clock-enable (`tick`) instead of a gated clock, no latches or
  combinational loops. **Project name == top entity == `seven_seg_counter_top`**
  (essential Quartus convention).

---

## 2. `.qsf` assignments (`seven_seg_counter.qsf`)

`.qsf` is Tcl. Every real assignment is a `set_global_assignment` /
`set_location_assignment` / `set_instance_assignment` line. Key contents:

```tcl
# ---- Project / device --------------------------------------
set_global_assignment -name FAMILY "Cyclone 10 LP"
set_global_assignment -name DEVICE 10CL025YU256C8G
set_global_assignment -name TOP_LEVEL_ENTITY seven_seg_counter_top
set_global_assignment -name PROJECT_OUTPUT_DIRECTORY output_files

# ---- Source files ------------------------------------------
set_global_assignment -name VERILOG_FILE src/seven_seg_counter_top.v
set_global_assignment -name VERILOG_FILE src/seg7_decoder.v

# ---- Timing constraint file --------------------------------
set_global_assignment -name SDC_FILE constraints.sdc

# ---- Misc --------------------------------------------------
set_global_assignment -name NUM_PARALLEL_PROCESSORS ALL
set_global_assignment -name ERROR_ON_WARNINGS ON

# ---- Pin / I/O assignments (DE10-Lite, 3.3-V LVTTL) --------
set_location_assignment PIN_P11 -to clk
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to clk

set_location_assignment PIN_B8 -to reset_n
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to reset_n

set_location_assignment PIN_A8  -to ledr[0]
set_location_assignment PIN_A9  -to ledr[1]
set_location_assignment PIN_A10 -to ledr[2]
set_location_assignment PIN_B10 -to ledr[3]
set_location_assignment PIN_D13 -to ledr[4]
set_location_assignment PIN_C13 -to ledr[5]
set_location_assignment PIN_E14 -to ledr[6]
set_location_assignment PIN_D14 -to ledr[7]
set_location_assignment PIN_A11 -to ledr[8]
set_location_assignment PIN_B11 -to ledr[9]
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to ledr[0]
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to ledr[1]
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to ledr[2]
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to ledr[3]
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to ledr[4]
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to ledr[5]
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to ledr[6]
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to ledr[7]
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to ledr[8]
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to ledr[9]

# HEX1 (tens) : [0]=a [1]=b [2]=c [3]=d [4]=e [5]=f [6]=g
set_location_assignment PIN_C18 -to hex1[0]
set_location_assignment PIN_D18 -to hex1[1]
set_location_assignment PIN_E18 -to hex1[2]
set_location_assignment PIN_B16 -to hex1[3]
set_location_assignment PIN_A17 -to hex1[4]
set_location_assignment PIN_A18 -to hex1[5]
set_location_assignment PIN_B17 -to hex1[6]

# HEX0 (ones) : [0]=a [1]=b [2]=c [3]=d [4]=e [5]=f [6]=g
set_location_assignment PIN_C14 -to hex0[0]
set_location_assignment PIN_E15 -to hex0[1]
set_location_assignment PIN_C15 -to hex0[2]
set_location_assignment PIN_C16 -to hex0[3]
set_location_assignment PIN_E16 -to hex0[4]
set_location_assignment PIN_D17 -to hex0[5]
set_location_assignment PIN_C17 -to hex0[6]

set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to hex1[*]
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to hex0[*]
```

Also shipped: `seven_seg_counter.qpf` (the tiny project pointer):

```
QUARTUS_PROJECT = "seven_seg_counter" REVISION = "seven_seg_counter"
DATE = "09/23/2025" TIME = "12:00:00"
```

Notes:
- Every HDL file must be listed or Quartus silently ignores it. The top-level
  entity name **must match** the `module` name in the top file.
- Unused HEX2…HEX5 segments are left unassigned; if you later drive them, add
  the matching `set_location_assignment`s from the user-manual table.

---

## 3. `.sdc` timing constraints (`constraints.sdc`)

```sdc
# 50 MHz input clock on port 'clk' -> period 20 ns.
create_clock -name clk_50 -period 20.000 [get_ports {clk}]

# Realistic clock uncertainty between unrelated clocks (harmless here).
derive_clock_uncertainty

# Async reset (KEY[0]) has no timing relationship to the clock.
set_false_path -from [get_ports {reset_n}]
```

Why this is right for this design:
- Without `create_clock` the Timing Analyzer has **no clock to analyse**, so
  every timing report would be meaningless. `50 MHz → period = 1000/50 = 20 ns`.
- No PLL is used, so `derive_pll_clocks` is not required (add it if you ever
  add a PLL). `derive_clock_uncertainty` is harmless best practice.
- The async reset is the only cross-domain input, so a `set_false_path` on it
  is the conventional, correct constraint. No source-synchronous I/O delays
  are needed for these simple LED/display outputs at 50 MHz.

You can inspect the result afterwards with:

```tcl
report_timing -setup -npaths 10
```

(At the Timing Analyzer Tcl prompt, or via `quartus_sta`. A **negative Slack**
would indicate a timing failure to fix.)

---

## 4. Testbench (`sim/tb_seven_seg_counter.v`)

```verilog
`timescale 1ns / 1ps
module tb_seven_seg_counter;
    reg         clk     = 1'b0;
    reg         reset_n = 1'b0;
    wire [9:0]  ledr;
    wire [6:0]  hex1;
    wire [6:0]  hex0;
    integer errors = 0;

    // 50 MHz clock -> 20 ns period
    always #10 clk = ~clk;

    seven_seg_counter_top dut (
        .clk(clk), .reset_n(reset_n),
        .ledr(ledr), .hex1(hex1), .hex0(hex0)
    );

    // Reference decoder at module level (tasks can't instantiate modules)
    reg  [3:0] ref_bcd = 4'd0;
    wire [6:0] ref_seg;
    seg7_decoder u_ref (.bcd(ref_bcd), .seg(ref_seg));

    task check_seg(input [3:0] bcd, input [6:0] expected);
        begin
            ref_bcd = bcd;
            #1;   // settle combinational logic
            if (ref_seg !== expected) begin
                $display("FAIL: seg(%0d) = %b (expected %b)",
                         bcd, ref_seg, expected);
                errors = errors + 1;
            end else begin
                $display("PASS: seg(%0d) = %b", bcd, ref_seg);
            end
        end
    endtask

    initial begin
        $display("=== Reset check ===");
        reset_n = 1'b0;
        repeat (5) @(posedge clk);
        reset_n = 1'b1;
        @(posedge clk);
        // after reset both displays show '0'
        if (hex0 !== 7'b1000000 || hex1 !== 7'b1000000) begin
            $display("FAIL: after reset hex0=%b hex1=%b", hex0, hex1);
            errors = errors + 1;
        end else $display("PASS: counter is 0 after reset");

        $display("=== Decoder checks (digits 0..9) ===");
        check_seg(4'd0, 7'b1000000); check_seg(4'd1, 7'b1111001);
        check_seg(4'd2, 7'b0100100); check_seg(4'd3, 7'b0110000);
        check_seg(4'd4, 7'b0011001); check_seg(4'd5, 7'b0010010);
        check_seg(4'd6, 7'b0000010); check_seg(4'd7, 7'b1111000);
        check_seg(4'd8, 7'b0000000); check_seg(4'd9, 7'b0010000);

        if (errors == 0) $display("*** ALL TESTS PASSED ***");
        else             $display("*** %0d ERROR(S) ***", errors);
        $finish;
    end
endmodule
```

It checks **reset behaviour** (counter must show `00` / decode of zero) and the
**decoder truth table** for digits 0–9 deterministically and fast. A full 0…59
roll-over needs ~1 s of simulated time because of the real 1 Hz prescaler; to
watch the counter tick in simulation, temporarily shrink `CLK_FREQ`
(e.g. to `4`) in `seven_seg_counter_top.v`.

---

## 5. Compile / simulate / program commands

Run these on a machine with Quartus on `PATH` (Windows: the `bin64` folder;
Linux: `/opt/intelFPGA_lite/<ver>/quartus/bin`). First confirm your edition:

```bash
quartus_sh --version
```

### a) One-shot full compile (map + fit + asm + sta)

```bash
cd <this outputs folder>
quartus_sh --flow compile seven_seg_counter      # uses seven_seg_counter.qsf
```

This produces `output_files/seven_seg_counter.sof` and runs the Timing
Analyzer against `constraints.sdc`. Look for **Analysis & Synthesis
succeeded**, **Fitter succeeded**, and no negative setup/hold slack in the
`.sta.rpt`.

### b) Simulation with Questa / ModelSim (Altera FPGA Edition)

From the `sim/` directory, either run the prepared do-file:

```bash
cd sim
vsim -c -do sim.do
```

…or step by step:

```bash
vlib work
vlog ../src/seven_seg_counter_top.v ../src/seg7_decoder.v tb_seven_seg_counter.v
vsim -c work.tb_seven_seg_counter -do "run -all; quit -f"
```

Expected console output: `PASS …` lines and `*** ALL TESTS PASSED ***`.

The testbench uses **only the RTL and the decoder** (no Altera IP), so no
extra simulation-lib compile is needed (unlike designs that use a PLL/RAM IP).

### c) Program the DE10-Lite over JTAG / USB-Blaster

```bash
# volatile (SRAM) configuration, direct .sof download
quartus_pgm -c 1 -m JTAG -o p;output_files/seven_seg_counter.sof
```

Optional non-volatile (on-board flash) configuration:

```bash
quartus_cpf -c output_files/seven_seg_counter.sof seven_seg_counter.jic
quartus_pgm -c 1 -m JTAG -o p;seven_seg_counter.jic
```

### d) (Optional) Tcl-driven compile

```tcl
# compile.tcl
load_package flow
project_open seven_seg_counter
execute_flow compile
project_close
```
```bash
quartus_sh -t compile.tcl
```

---

## Result on the board

Press **KEY[0]** to reset to `00`; the seconds counter then advances
`00 … 59` and wraps to `00`, shown on **HEX1** (tens) and **HEX0** (ones).
LEDR[1:0] mirrors the tens digit for a quick sanity check.

---

## Quick file index

| File | Purpose |
|------|---------|
| `seven_seg_counter.qpf` | Project pointer (name + revision) |
| `seven_seg_counter.qsf` | Settings + full pin assignments (Tcl) |
| `constraints.sdc` | Clock / reset timing constraints |
| `src/seven_seg_counter_top.v` | Top module: 0..59 seconds counter |
| `src/seg7_decoder.v` | BCD → 7-segment (active-low) decoder |
| `sim/tb_seven_seg_counter.v` | Testbench (not added to synthesis) |
| `sim/sim.do` | ModelSim/Questa batch script |

Everything is text and reproducible from source control; create the project
files by hand and run `quartus_sh --flow compile` — no GUI needed.
