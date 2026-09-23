# Simulation with ModelSim / Questa (Altera edition)

Quartus ships **Questa – Altera FPGA Edition** (previously ModelSim-Altera) for
RTL simulation. You simulate a **testbench** against your RTL, or (for
post-synthesis timing sim) against the generated netlist.

## The testbench

A testbench is a normal Verilog module with **no ports** (`module tb_top;`). It
instantiates the DUT, drives stimulus (via `initial` blocks / `#` delays /
`forever`), and usually has `$display`/`$stop`/`$finish`. Testbenches are **not
synthesizable** and must **not** be added to the `.qsf` synthesis file list — keep
them in a `sim/` directory.

## RTL (functional) simulation flow

1. Compile the source + testbench with `vlog`:
   ```
   vlog src/top.v src/sub.v sim/tb_top.v
   ```
   (This builds the library `work`.)
2. Elaborate and run with `vsim`:
   ```
   vsim work.tb_top -do "run -all; quit"
   ```
   `-do "run -all"` runs until `$finish`. Add `-voptargs=+acc` if you need
   internal visibility.

Or drive it all from a do-file `sim.do`:
```tcl
vlib work
vlog src/top.v src/sub.v sim/tb_top.v
vsim -c work.tb_top
run -all
quit
```
Then: `vsim -c -do sim.do` (or run `vlog`/`vsim` at the prompt).

## Gate-level (post-synthesis timing) simulation

1. Compile the design in Quartus (Analysis & Synthesis + Fitter).
2. Generate the simulation netlist via EDA Netlist Writer settings (produces
   `.vo` + `.sdo` delay files).
3. Compile the `.vo` (netlist) and `.sdo` (SDF delay) plus the Altera
   simulation libraries, then run `vsim -sdftyp /tb_top/dut=design.vo.sdo`.
   Functional-vs-timing simulation mode is selected in Quartus (Processing /
   EDA Netlist Writer settings).

## Tips

- **Reset the design** in the testbench (drive `reset_n` low for a few cycles
  then high) or you will see `X`/undefined behaviour from uninitialised FFs.
- Use `$display`/`$monitor` to observe signals; use `$finish` to end.
- For long runs use `run -all` and query with `-do "run -all; quit -f"`.
- Quartus can also launch the simulation from the GUI: Assignments → Settings →
  EDA Tool Settings → Simulation → select “Questa Intel FPGA”.

## IP simulation libraries

When your design uses Quartus IP (PLL, RAM), the simulation must compile the
Altera simulation models. Reference the IP's `.qip`; the EDA netlist writer /
simulation setup pulls in the needed libraries (`altera_mf`, `altera_lnsim`).
Documentation: Intel/Altera “Questa Intel FPGA Edition” docs under
`docs.altera.com`.

### ModelSim-ASE (Quartus 17.0, Windows) gotchas

- Binaries are in `modelsim_ase/win32aloem/` (vlog.exe, vsim.exe) — not `win64`
  in this install. Set that path explicitly in scripts.
- A `.do` file should not invoke `vsim -do` recursively. Put `run -all; quit -f`
  **inside** the do file and call `vsim -c -do run.do`. Nesting `-do` fails.
- `/dev/null` redirection fails inside the ModelSim Tcl shell ("couldn't write
  file /dev/null") — avoid it in `.do` scripts on Windows.

### `$readmemh` / `$readmemb` — simulation-only, not for synthesis

`$readmemh("file.hex", mem)` loads a hex file into a memory array at the start of
simulation. **Quartus synthesis** treats the referenced file as a required Verilog
Design File and **errors out** (Error 10054: "can't open Verilog Design File") if
the file does not exist at compile time — even if you only intended it for
simulation. To prevent this:

- **Guard** with `// synthesis translate_off` / `// synthesis translate_on`:
  ```verilog
  // synthesis translate_off
  initial $readmemh("rom.hex", mem);
  // synthesis translate_on
  ```
  This makes synthesis skip the construct entirely.
- Or **use a separate simulation-only file** (not in the `.qsf`) and keep the
  `$readmemh` in the testbench, not in the synthesizable module.
- For synthesizable ROMs, initialize with a **`.mif` file** via Quartus IP or a
  `case` statement; do not rely on `$readmemh` to produce hardware content.

### Testbench read-model must match DUT read protocol

A CPU core that uses a **synchronous (registered) read protocol** ("present
`mem_addr`, sample `mem_din` on the next clock edge") needs a memory model that
supplies data **before** that clock edge. A testbench using a combinational
`assign mem_din = mem[addr]` works for this (data is available immediately).
Conversely, a CPU with an **asynchronous (combinational) read** expects data
within the same clock cycle, which also works with a combinational model.

The mismatch that causes false failures is when the testbench uses a
**registered/clocked memory read** while the DUT expects data on the same cycle.
If the testbench was written for a different CPU core, verify its memory-read
timing model matches the DUT's protocol before debugging the CPU.

In general: the testbench memory model should be as simple as possible
(combinational read, clocked write) unless the DUT explicitly requires a
pipelined read (e.g. cache-like behaviour).
