---
name: fpga-verilog-quartus
description: 'Intel FPGA development in Verilog with Quartus Prime: write, structure, constrain, compile, simulate and program FPGA / SoC / CPLD designs. Use whenever the user wants to create, fix, compile, constrain, simulate or program an Intel/Altera FPGA design in Verilog or SystemVerilog — e.g. "write a Verilog module for my Cyclone V", "why won''t it fit / why the timing failure", "add a .sdc constraint", "build a testbench and simulate", "make a .sof / .jic and program the board", "use a PLL or RAM IP core", "set up a Quartus project". Reach for it even when the user never says "Quartus" or "Verilog" explicitly but mentions an Intel/Altera FPGA device (Cyclone, MAX 10, Agilex, Arria, Stratix), a dev board (DE10-Lite, DE0, Terasic, Arrow), pin assignments, or an .qsf/.sdc/.sof file. Covers Quartus Prime Lite/Standard/Pro, the command-line and Tcl flows, project files, SDC timing constraints, ModelSim/Questa simulation, IP/Platform Designer, synthesizable Verilog, and FPGA OpenCL (AOCL)/OpenVINO acceleration.'
metadata:
  version: 1.0.0
  tags: fpga, verilog, systemverilog, quartus-prime, intel, altera, cyclone, agilex, max10, rtl, hdl, timing, sdc, synthesis, opencl, openvino, aocl, hls
---

# Intel FPGA / Quartus Prime Development (Verilog)

Build, constrain, compile, simulate and program Intel/Altera FPGAs written in
Verilog or SystemVerilog using Intel Quartus Prime.

Quartus differs from other FPGA tools (Vivado, Synplify) in its project-file
format, its command-line/Tcl tool names, and some synthesis defaults. The
purpose of this skill is to record the Quartus-specific facts that are easy to
get wrong from memory, so you do not have to re-derive them. The references
under `references/` hold the details; load the relevant one when the task needs
that depth.

## Trigger flow — decide first, then load

1. **Which Quartus edition?** This decides which devices and toolchain apply.
   - **Lite** (free): MAX 10, Cyclone 10 LP, Cyclone IV/V, MAX II/V.
   - **Standard** (paid, mid-range): Arria 10/V, Cyclone IV/V/10 LP, Stratix V, MAX.
   - **Pro** (paid, high-end): Agilex 3/5/7/9, Stratix 10, Arria 10, Cyclone 10 GX.
   See `references/quartus-basics.md`.
2. **Which part of the task?**
   - New/changed RTL: `references/verilog-for-synthesis.md`.
   - Project / .qsf / .qpf / pin assignments: `references/project-files-qsf.md`.
   - Build/compile/program from the command line: `references/quartus-cli-flow.md`.
   - Timing constraints / timing-closure: `references/timing-sdc.md`.
   - Simulation / testbench: `references/simulation.md`.
   - IP (PLL, RAM, Platform Designer / Qsys): `references/quartus-basics.md` (IP section).
   - OpenCL / AOCL / OpenVINO (HLS-style acceleration, older Quartus & boards): `references/opencl-openvino.md`.
   - Automate the build/program with bundled scripts (create_project.tcl, compile.tcl, Makefile, program.tcl): `scripts/`.

## The golden rules that prevent 80% of the pain

- **Ask, don't guess the board.** An FPGA design is useless without the pin map,
  clock frequency and I/O standard of the actual board. If the user hasn't told
  you the device, board, top-level entity and clock, ask — or state your
  assumptions at the top of the answer (common defaults: Cyclone V, DE10-Lite,
  50 MHz, 3.3-V LVTTL).
- **Project name == top-level entity** is the Quartus convention and avoids
  confusion. The top module's name and the `TOP_LEVEL_ENTITY` must match a module
  in the project or synthesis fails.
- **Never edit generated files.** IP cores, Platform Designer/Qsys generated
  HDL, and the seed `.tcl` get regenerated; your edits are lost. Edit the source
  modules and constraint files only.
- **Write synthesizable Verilog.** Follow the style rules in
  `references/verilog-for-synthesis.md` (blocking vs non-blocking, no inferred
  latches, no combinational loops, proper resets, clock-enables instead of gated
  clocks, 2-FF synchronizers across clock domains).
- **`$readmemh`/`$readmemb` is simulation-only.** Do not rely on it for
  hardware ROM content — Quartus synthesis errors out if the referenced hex file
  does not exist. Use `.mif` files, IP generators, or `case` statements for
  synthesizable ROMs. See `references/simulation.md`.
- **Constrain timing before analysing timing.** Any timing report without a
  clock constraint is meaningless. Always create clocks on your input clock pins
  (and PLL outputs) in the `.sdc`.

## Minimal working project shape

```
myproj/
  myproj.qpf          # project pointer (references a .qsf)
  myproj.qsf          # all settings/assignments (Tcl syntax)
  constraints.sdc     # timing constraints (create_clock etc.)
  src/
    top.v             # top-level Verilog module (matches TOP_LEVEL_ENTITY)
    sub.v
  sim/
    tb_top.v          # testbench (not added to synthesis)
```

See `references/project-files-qsf.md` for the exact file contents and assignment
lines, and `references/quartus-cli-flow.md` to compile it headless.

## Report structure (what you deliver)

For a build / fix / port request, ALWAYS report the concrete Quartus artefacts:
1. The **Verilog/SystemVerilog source** (all modules, top-level first).
2. The **.qsf assignments** needed (family, device, top entity, file list, pins,
   I/O standards, SDC file, output dir).
3. The **.sdc timing constraints**.
4. A **testbench** plus the exact simulation commands (when simulation is in scope).
5. Compile / program commands (when the user is at a machine with Quartus).

If you cannot run Quartus in this environment, still deliver 1–4 and clearly
say the compile/program step must be run on the user's Quartus installation.

## Automation scripts (bundled in scripts/)

These let you build, compile and program headlessly so projects are reproducible
from a terminal (and from CI). Load the script before telling the user to run it
so commands match. All paths below assume Quartus binaries are on PATH.

| Script | What it does | Run it with |
|--------|--------------|-------------|
| `scripts/create_project.tcl` | Create a new .qpf/.qsf headlessly (family, device, top entity, sources, SDC). | `quartus_sh -t create_project.tcl <proj> [family] [device] [top] [files...]` |
| `scripts/compile.tcl` | Full flow (synth→fit→asm→STA) + writes a textual timing report and exit-code signalling timing violation. | `quartus_sh -t compile.tcl <proj>` |
| `scripts/Makefile` | `make compile` / `make program` / `make jic` / `make clean` wrapper. Drop into the project root, set `PROJECT`. | `make compile` |
| `scripts/program.tcl` | Program the `.sof` via JTAG (wraps `quartus_pgm`). | `quartus_sh -t program.tcl [cable]` |

Notes for choosing:
- Quick one-off compile: prefer `quartus_sh --flow compile <proj>` (no script needed).
- Reproducible/CI or when you want an exit code + timing report: `scripts/compile.tcl`.
- New project from scratch: `scripts/create_project.tcl` beats hand-editing .qsf.
- The Makefile wires compile+program+jic+clean together for day-to-day use.

These scripts assume a conventional layout: `output_files/` for results, project
root holds the `.qpf`/`.qsf`. Adjust `QUARTUS_ROOTDIR` / `PROJECT` in the Makefile
for your install and project name.

## When you cannot be sure

Quartus versions and device support change with releases. If you are asked for a
family, setting, or tool name you cannot verify confidently, note the version
you are assuming and tell the user to confirm with their installed release
(`quartus_sh --version`). Do not invent QSF assignment names.
