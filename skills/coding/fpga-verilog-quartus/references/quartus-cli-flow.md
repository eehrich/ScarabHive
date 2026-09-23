# Quartus Prime command-line & Tcl flows

Everything the GUI can do has a command-line equivalent. This is how you compile
and program without opening the GUI, and how the tool is scripted.

## Binary / executable map

| Executable | Stage |
|------------|-------|
| `quartus_sh` | Quartus Shell: runs Tcl scripts, `--flow`, `--tcl_eval`. The entry point for scripting. |
| `quartus_map` | Analysis & Synthesis (HDL to logic/netlist) |
| `quartus_fit` | Fitter (place & route) |
| `quartus_asm` | Assembler (produces `.sof`) |
| `quartus_sta` | Timing Analyzer (SDC analysis) |
| `quartus_pgm` | Programmer (writes device) |
| `quartus_cpf` | Programming-file converter (`.sof` to `.jic` etc.) |
| `quartus_pow` | Power analyzer |
| `quartus_drc` | Design Rule Checker |

On Windows these live under the Quartus `bin64` directory; on Linux under
`/opt/intelFPGA_pro/<ver>/quartus/bin`. Add it to PATH or invoke by full path.

## One-shot full compile

```
quartus_sh --flow compile myproj          # default revision (myproj.qsf)
quartus_sh --flow compile myproj -c rev1  # specific revision (rev1.qsf)
```

`--flow compile` runs map → fit → asm (→ sta/drc/eda depending on settings). It
is the recommended way to compile rather than invoking each executable and
risking mismatched options.

## Running each stage individually (only when you need control)

```
quartus_map --read_settings_files=on myproj -c myproj
quartus_fit myproj -c myproj
quartus_asm myproj -c myproj
quartus_sta myproj -c myproj              # timing analysis
```

If you run these individually you are responsible for keeping settings/STEP
consistent; prefer `--flow compile`.

## Tcl scripting

`quartus_sh` runs a full Tcl interpreter with Quartus Tcl packages:

```
# compile via Tcl
quartus_sh -t compile.tcl
```

`compile.tcl`:
```tcl
load_package flow
project_open myproj
execute_flow compile
project_close
```

Other useful packages: `project` (assignments: `set_global_assignment`),
`device`, `timing` (`report_timing`, `create_clock` at the Tcl level),
`fit`, `report`.

### Creating/editing assignments from Tcl

```tcl
load_package project
project_open myproj
set_global_assignment -name VERILOG_FILE src/top.v
set_location_assignment PIN_M9 -to clk
project_close
```

## Timing analysis from the command line

```
quartus_sta -t sta.tcl myproj
```

or load the timing package in Tcl:

```tcl
load_package timing
project_open myproj -current_revision
create_clock -period 20.0 [get_ports clk]
report_timing -setup -to [get_registers] -npaths 10
```

## Programming the device

After a successful `--flow compile`, program the `.sof`:

```
quartus_pgm -c 1 -m JTAG -o p;myproj.sof
```

- `-c 1` = cable/index; `-m JTAG` = mode; `-o p;<file>` = program operation.
- For non-volatile (flash) config, first convert with `quartus_cpf` to a `.jic`,
  then program the `.jic`.

## Converting to .jic (flash configuration)

```
quartus_cpf -c myproj.sof myproj.jic
```

(Exact conversion options vary by device/config-scheme; consult the Programming
File Converter help for a specific device.)

## Environment checks

- `quartus_sh --version` → prints installed version (confirm edition/version).
- If a binary is not found, it is not on PATH — locate it in the Quartus `bin64`
  or `bin` folder.

## Makefile tip

Keep `.qpf`/`.qsf`/sources/`.sdc` as text, and drive `quartus_sh --flow compile`
from a Makefile; the project is fully reproducible from source control.

### Headless flow ordering & common gotchas (Quartus 17.0)

- **Run the full flow, not just map+fit separately.** The correct sequence is
  `quartus_sh --flow compile <proj>` which does map → **Partition Merge** →
  fit → asm → sta. Running `quartus_fit` right after `quartus_map` fails with
  error 11761 ("Run Partition Merge (quartus_cdb --merge) before running Fitter")
  — because synthesis output is held in partitions until merged.
- `--logfile` is **not** a standalone `quartus_map`/`quartus_fit` option (error
  23024). Reports are written automatically under `output_files/`.
- Keep the core design **PLL/IP-free** so it compiles headless without a license
  and without IP generation; add PCIe Hard IP / PLL only via Platform Designer
  for the full board build.
