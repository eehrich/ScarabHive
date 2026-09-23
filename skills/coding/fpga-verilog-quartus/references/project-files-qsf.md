# Project files — .qpf, .qsf, .sdc, and assignment syntax

A Quartus project is a small set of **text files** (mostly Tcl). You can create
them by hand — you do not need the GUI.

## The files

| File | Purpose |
|------|---------|
| `.qpf` | Quartus **Project** File — a tiny pointer that names the project and its main `.qsf` revision. |
| `.qsf` | Quartus **Settings** File — ALL assignments & settings, one `set_..._assignment` per line, Tcl-like. |
| `.sdc` | SDC timing constraints (clock definitions, delays, false paths). Referenced from the `.qsf`. |
| `.qws` | Workspace (GUI-only, optional, safe to ignore). |
| `.sof` | SRAM Object File — the programming bitstream for volatile config. |
| `.jic` | JTAG Indirect Configuration file (for flash/EPCS config). |

## .qpf (project pointer)

```
QUARTUS_PROJECT = "myproj" REVISION = "myproj" DATE = "07/08/2025" TIME = "12:00:00"
```

Usually: project name == revision name == top-level entity. One `.qpf` can point
to several `.qsf` revisions (e.g. for different device variants).

## .qsf — the heart of the project

`.qsf` is Tcl. Comments start with `#`. Every real assignment is a
`set_global_assignment`, `set_location_assignment` or `set_instance_assignment`
line. Quartus writes new assignments at the end of the file; order does not
matter (later `source`d files can override).

**The assignments you must have (minimum viable project):**

```
# ---- Project / device --------------------------------------------------
set_global_assignment -name FAMILY "Cyclone V"
set_global_assignment -name DEVICE 5CEBA4F23C7           # or AUTO
set_global_assignment -name TOP_LEVEL_ENTITY top          # must match a module
set_global_assignment -name PROJECT_OUTPUT_DIRECTORY output_files

# ---- Source files (add every synthesizable HDL file) -------------------
set_global_assignment -name VERILOG_FILE src/top.v
set_global_assignment -name VERILOG_FILE src/sub.v
# SystemVerilog files use:
set_global_assignment -name SYSTEMVERILOG_FILE src/foo.sv
# VHDL files use:  set_global_assignment -name VHDL_FILE src/foo.vhd

# ---- Timing constraint file --------------------------------------------
set_global_assignment -name SDC_FILE constraints.sdc

# ---- Pin / IO assignments ----------------------------------------------
set_location_assignment PIN_M9 -to clk
set_location_assignment PIN_N8 -to led[0]
set_instance_assignment -name IO_STANDARD "3.3-V LVTTL" -to led[0]

# ---- Misc useful ones ---------------------------------------------------
set_global_assignment -name NUM_PARALLEL_PROCESSORS 4   # speed up compile
set_global_assignment -name FMAX_REQUIREMENT "50 MHz"   # optional old-style
```

- **`FAMILY`** and **`DEVICE`** must be supported by the edition you use (see
  `quartus-basics.md`). `DEVICE AUTO` lets Quartus pick, but a real board needs
  the exact part (e.g. `5CEBA4F23C7`) so pin locations resolve.
- Every synthesizable file must be listed, or Quartus silently ignores it
  (synthesis may then fail with “can't find module”, or worse, use a stale netlist).
- Pin numbers and I/O standards are board-specific — confirm from the board
  manual, don't invent them.
- The `.qsf` can `source` other `.qsf` files; assignments imported this way
  override ones read before the `source` line.

## .sdc (timing) — see timing-sdc.md for the full reference

The `.qsf` links the `.sdc` via `SDC_FILE`; the `.sdc` itself contains
`create_clock` and related SDC commands. Without a clock, the Timing Analyzer
has nothing to analyse.

## Revisions

Each revision has its own `.qsf` (e.g. `myproj.qsf` is the default revision).
Use revisions to try e.g. different devices or constraint sets without touching
the working copy.

## Hand-created vs wizard

You can create a full project purely as text (`.qpf` + `.qsf` + sources + `.sdc`)
and compile it headless — see `quartus-cli-flow.md`. This is exactly what the
command-line flow expects.

### Version-dependent QSF assignments

Some `set_global_assignment` lines that appear in newer Quartus templates or
reference `.qsf` files (e.g. from a board vendor) are **not valid in older
Quartus versions** (e.g. 17.0, 18.1). Using them produces errors like:

```
Error (12007): Top-level design entity "..." is undefined
Error (12152): Can't elaborate user hierarchy "..."
```

Common assignments that cause trouble on older releases:

- `ERROR_ON_FORCE_TRIS` — newer synthesis option, not recognised by 17.0.
- `ERRORS_DURING_NOISE_ANALYSIS STD` — noise-analysis option, unavailable in
  17.0 Standard.
- `STARTUP_DEVICE POWER_UP` — startup behaviour, not supported in 17.0.

If a compile fails with an error message mentioning one of these, **remove** the
offending line from the `.qsf`. The Quartus error message tells you which line
is rejected.

General rule: when copying a `.qsf` from a newer template, a vendor reference
design, or an auto-generated example, first try compiling. If it fails, check
the error messages for rejected assignments and strip them one by one. Keep a
backup of the original before editing.
