# Quartus Prime — editions, versions, devices, IP

## Editions (which toolchain / devices)

Quartus Prime ships in three editions. Choosing the wrong one is the most common
project-setup mistake, because `family`/`device` must be supported by the edition.

| Edition | License | Typical devices |
|---------|---------|-----------------|
| **Lite** | Free | Cyclone IV, Cyclone V, Cyclone 10 LP, MAX 10, MAX II/V |
| **Standard** | Paid | Arria 10, Arria V, Cyclone IV/V/10 LP, Stratix V, MAX |
| **Pro** | Paid (free for some families) | Agilex 3/5/7/9, Stratix 10, Arria 10, Cyclone 10 GX |

- **Lite** is the right default for hobby/education/low-cost boards
  (DE10-Lite = Cyclone 10 LP or MAX 10; DE0/DE1-SoC = Cyclone IV/V; most Terasic
  entry boards = Cyclone). It is a reduced Standard edition: no partial
  reconfiguration, no design partitioning, no register retiming, no transceiver
  link analysis.
- **Standard** covers mid-range and legacy high-performance parts.
- **Pro** is required for the newest families (Agilex 3/5/7/9, Stratix 10) and
  uses the newer Pro compiler. Agilex 5 support began in **Pro 24.1**.

## Versions

- Editions are versioned per year, e.g. **Lite 23.1**, **Pro 24.3**, **25.x**.
  Newer editions keep dropping support for old devices (e.g. no Cyclone II in
  recent releases) and old editions cannot target new devices.
- Latest and per-device support move independently; always confirm with the
  user's installed release (`quartus_sh --version`) when it matters.

## The compile engine terminology

- **Analysis & Synthesis** = `quartus_map` (parses HDL, infers logic/RAM/DSP,
  maps to device resources).
- **Fitter (Place & Route)** = `quartus_fit` (places logic elements/ALMs and routes).
- **Assembler** = `quartus_asm` (generates the `.sof` programming bitstream).
- **Timing Analyzer** = `quartus_sta` (SDC-based static timing analysis).
- **Programmer** = `quartus_pgm` (writes `.sof`/`.jic` to the device).
- **Programming file converter** = `quartus_cpf` (converts `.sof` to `.jic`, `.hex`, etc.).
- **PowerPlay power analyzer** = `quartus_pow`; **SignalTap** = `quartus_stp`.

## IP cores & Platform Designer (Qsys)

- Add IP (PLL, memory, FIFO, transceivers, NIOS) via the **IP Catalog** in the
  GUI, or via Platform Designer for a full Qsys system.
- Generated IP emits a `.qip` (a file list) plus Verilog `.v`/`.vhd` wrappers
  and a `.bsf`/`.sv` for integration. Reference the `.qip` in the .qsf so all
  generated files are compiled.
- **Do not hand-edit generated IP files** — regenerate instead.
- Common generated cores for beginners: `altera_pll` / `altpll` (PLL), and Intel
  memory cores (RAM/FIFO).
- **Evaluation mode**: Quartus marks unevaluated IP (compiles, but functional
  simulation/limit). For real use, compile the IP with a proper license —
  Assignments → Settings → Compilation Process → disable “Intel FPGA IP
  Evaluation Mode”.

## Device/board shorthand

- Pin assignments, IO standards and clock frequencies come from the board, not
  the tool. Always map to the specific board's handbook.
- `PIN_xx` names come from the device package; combine with `set_location_assignment`.
- I/O standard examples seen on boards: `3.3-V LVTTL`, `2.5-V`, `1.8-V`,
  `LVCMOS33`. Wrong I/O standard = damaged pins or non-functional IO.

## Sources of truth

- Intel/Altera official docs portal: `docs.altera.com` (Quartus Prime Pro /
  Standard / Lite user guides by version).
- `altera.com/products/development-tools/quartus` — edition/device comparison.

### Stratix V / DE5-Net (Terasic)

**Device:** `5SGXEA7N2F45C2` (Stratix V GX, 1932-pin FBGA, speed grade 2).
**Family:** `"Stratix V"` (not `"Stratix V GX"`; use the bare family name).
**Recommended .qsf filter assignments:**
```
set_global_assignment -name DEVICE_FILTER_PACKAGE FBGA
set_global_assignment -name DEVICE_FILTER_PIN_COUNT 1932
```
These filter the device list so that only compatible parts appear (prevents pin-assignment errors).

**Clock:** The DE5-Net has a 50 MHz on-board oscillator. For initial bringup this is usable directly without a PLL:
```
create_clock -name clk50 -period 20.000 [get_ports {clk}]
```
The pin assignment for the primary clock can be found in the DE5-Net schematic (`SystemCD/Schematic/`).

**No direct video/audio:** Unlike most dev boards, the DE5-Net has NO HDMI/VGA/DVI output and NO audio codec. All user-facing media must go out over **PCIe Gen2 x8** (Stratix V Hard IP) to a host PC, which handles display and audio via a Linux driver. See `opencl-openvino.md` for PCIe DMA concepts and the DE5-Net SystemCD `Demonstrations/PCIe_Fundamental/` reference design for a working PCIe endpoint.

### ModelSim-ASE on Windows (Quartus 17.0)

The ModelSim-ASE (Altera Starter Edition) binaries live in:
```
C:\intelFPGA.0\modelsim_ase\win32aloem```
Note `win32aloem` — NOT `win32` (which does not exist for ASE). Add this to PATH or use absolute paths:
```bash
export PATH="/c/intelFPGA/17.0/modelsim_ase/win32aloem:$PATH"
vlog file.v
vsim -c work.tb_top -do "run -all; quit -f"
```
