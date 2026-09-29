# OpenCL / OpenVINO on Intel/Altera FPGAs (incl. older tools & boards)

This covers high-level FPGA programming: **Intel FPGA SDK for OpenCL** (formerly
**Altera SDK for OpenCL**, still referred to as AOCL / ACL) and the **OpenVINO**
Inference-Engine FPGA plugin. Emphasis on older tool versions (Quartus 17.x and
earlier) and older boards, because the toolchain pairing there is non-obvious.

Contrast with the rest of this skill: the other references are about RTL
Verilog with Quartus. This one is about *software-driven, HLS-style*
acceleration where you write OpenCL C kernels instead of RTL.

## Terminology (the confusing part)

| Term | Means |
|------|-------|
| **OpenCL** | A C-based host+device programming standard. Kernels (`.cl`) run on the FPGA as "devices"; the host (C/C++ on CPU or SoC ARM) manages them via the OpenCL API. |
| **AOCL / ACL** | Colloquial names for the Intel/Altera *FPGA SDK for OpenCL*. |
| **FPGA SDK for OpenCL** | The Intel toolchain: Offline Compiler + runtime. |
| `aoc` | The **Offline Compiler**: compiles a `.cl` kernel into an FPGA bitstream `.aocx`. Takes minutes to hours. |
| `aocl` | The **runtime/utility** tool: `version`, `install`, `uninstall`, `program`, `flash`, `diagnose`, `list-devices`, `list-packages`. |
| `.cl` | OpenCL kernel source (the "device code"). |
| `.aocx` | Compiled kernel binary (bitstream + metadata) programmed to the FPGA. |
| `.aoco` | Intermediate object from incremental compilation. |
| **BSP** | **Board Support Package**: board-specific files (pinouts, PCIe/SoC plumbing, drivers) the SDK needs to target a specific board. |
| **OpenVINO** | Intel's inference toolkit; its Inference Engine has an **FPGA plugin** that loads a prebuilt "DLA" bitstream and runs CNNs on the FPGA. |

The one-line mental model: **`aoc` turns your `.cl` kernel into a `.aocx`
bitstream; the host program (a) programs that bitstream onto the FPGA and
(b) dispatches kernel work to it through OpenCL.**

## The two-tool model

1. **Offline compile (aoc)** — turn kernel source into hardware:
   ```
   aoc -board=<board> mykernel.cl -o mykernel.aocx
   ```
   - `aoc -list-board-packages` (older: `aoc --list-boards`) lists installed BSPs.
   - Simulation: `aoc -march=simulator mykernel.cl` (run via `CL_CONTEXT_MPSIM_DEVICE_INTELFPGA=1`).
   - Emulation (fast functional, no hardware): `aoc -march=emulator` (newer) or `-march=emulator -legacy-emulator` (older).
   - Iterate quickly with `-incremental` / `-fast-compile`; `-rtl` dumps intermediate RTL (useful to inspect what a kernel becomes).
   - Kernel compile is **slow** (minutes to hours) — optimise the kernel logic first and only do full hardware compiles when needed.

2. **Runtime (aocl + host program)** — load and run:
   ```
   aocl install <bsp>                    # install kernel driver (once)
   aocl program <board> mykernel.aocx     # program FPGA with the bitstream
   aocl diagnose / aocl list-devices
   ```
   The host program (C/C++) uses normal OpenCL calls: get platform/device,
   create context+program, `clCreateProgramWithBinary` with the `.aocx`,
   create kernel, set args, `clEnqueueNDRangeKernel`.

## Version pairing — critical for older setups

The SDK is **version-locked to the Quartus release**; a newer kernel/BSP will not
work with an older Quartus/OpenCL SDK, and vice-versa. Install both with the
SAME version number.

| Quartus (Prime/II) | FPGA SDK for OpenCL | Notes |
|--------------------|---------------------|-------|
| Quartus II 13.x / 14.0 | Altera SDK 13.x/14.0 | DE1-SoC guides use 14.0; standalone SDK, board folder copied into `<alt>/14.0/hld/board`. |
| Quartus 15.1 | SDK 15.1 | Cyclone V SoC, Arria 10 early, Stratix V. |
| Quartus 16.0 / 16.1 | SDK 16.0/16.1 | DE10-Standard guide uses 16.1. |
| Quartus Prime 17.0 | SDK 17.0 | |
| Quartus Prime 17.1 Lite/Standard/Pro | SDK 17.1 | **SDK license removed in 17.1** — became free / bundled with Quartus. |
| Quartus Prime 18.0 / 18.1 | SDK 18.0/18.1 | |
| Quartus Prime 19.1 Standard | SDK 19.1 | **Last version for Standard edition.** |
| Quartus Prime Pro 19.1+ | SDK Pro 19.1+ | Newer devices move to Pro only. |
| Quartus Pro 20+ / oneAPI | oneAPI DPC++ (SYCL) | OpenCL SDK phased out; **Intel oneAPI DPC++ with FPGA support** is the successor. Older boards had no newer support. |

Key older-version rules:
- Before 17.1 the FPGA SDK for OpenCL required its **own separate license**
  (`LM_LICENSE_FILE`), in addition to the Quartus license.
- The **BSP is not part of the SDK** (older ones were copied in; newer ones are
  downloaded separately per board from Intel/board-vendor pages).
- Newer SDK versions **drop support for older devices** — e.g. Cyclone V / older
  Stratix boards only have BSPs up to a certain SDK version. Match the SDK version
  to your board's available BSP, not the newest release.

## Environment variables (typed by hand in old setups)

Taken from real DE1-SoC / DE10-Standard guides:

```
export QUARTUS_ROOTDIR=/root/intelFPGA/16.1/quartus
export ALTERAOCLSDKROOT=/root/intelFPGA/16.1/hld          # SDK root
export PATH=$PATH:$QUARTUS_ROOTDIR/bin:$ALTERAOCLSDKROOT/bin:$ALTERAOCLSDKROOT/linux64/bin
export LD_LIBRARY_PATH=$ALTERAOCLSDKROOT/linux64/lib
export AOCL_BOARD_PACKAGE_ROOT=$ALTERAOCLSDKROOT/board/<vendor>/<board>
export QUARTUS_64BIT=1
export LM_LICENSE_FILE=1800@<server>                      # older versions only
```

Runtime behaviour toggles:
- `CL_CONTEXT_COMPILER_MODE_INTELFPGA=1` -> program/compile the bitstream at
  runtime; `=3` -> **use a pre-programmed bitstream, do NOT reprogram** (helps
  startup, common with OpenVINO DLA).
- `CL_CONTEXT_MPSIM_DEVICE_INTELFPGA=1` -> expose only the simulator device.
- `CL_CONTEXT_EMULATOR_DEVICE_INTELFPGA=<n>` -> number of emulated devices.

## Board Support Packages & older boards

Every physical board needs its own BSP. Common Altera/Intel boards:

| Board | Device | Note |
|-------|--------|------|
| DE1-SoC / DE10-Standard / DE10-Nano | Cyclone V SoC | ARM HPS host; cross-compile host for ARM (SoC EDS / DS-5). BSP e.g. 18.1. |
| DE10-Lite | Cyclone 10 LP / MAX 10 | MAX 10 is not an OpenCL target; little/no OpenCL support. |
| DE5-Net / DE5a-Net | Stratix V / Arria 10 | PCIe cards; Stratix OpenCL BSPs. |
| Intel Arria 10 GX Dev Kit / BittWare 520N | Arria 10 GX | reference platforms; `aocl program ac10` naming. |
| DE4-530 | Stratix IV | older kit. |

- Install: run `aocl install <path_to_bsp>` once; the driver auto-loads on reboot.
- The Stratix 10 reference-platform porting guide shows the canonical modern BSP
  layout (`hardware/...`, `scripts/...`, one BSP per revision).

## OpenCL features worth knowing on Intel FPGAs

- **Channels** (Intel extension): kernel-to-kernel or kernel-to-IO high-bandwidth
  FIFO pipes; a key reason to use FPGA OpenCL over generic OpenCL.
- Standard `__global`/`__local` buffers, plus `#pragma unroll`, loop II
  (initiation interval) hints, and `__attribute__((num_compute_units(N)))`.
- Include the SDK's OpenCL headers; if other OpenCL stacks (GPU) coexist, link
  the host to the Khronos ICD loader so platforms can be enumerated.

## OpenVINO + FPGA

OpenVINO's Inference Engine has an **FPGA plugin** (older releases
`computer_vision_sdk_fpga_*`, e.g. `2018.3.343`,
`l_openvino_toolkit_fpga_p_2018.1.267`). It runs CNNs on an FPGA using a fixed
**Deep Learning Accelerator (DLA)** bitstream:

Workflow:
1. Install OpenVINO **with FPGA support** + the **FPGA RTE (OpenCL runtime)** and
   the matching board BSP; set `AOCL_BOARD_PACKAGE_ROOT` correctly.
2. Program the FPGA with a pre-built DLA `.aocx` (per model architecture):
   ```
   aocl program ac10 $DIR/a10_dcp_bitstreams/2-0-1_RC_FP11_SqueezeNet.aocx
   ```
3. Register the FPGA OpenCL ICD (Linux: link `Altera.icd` / alteracl into
   `/etc/OpenCL/vendors`) so the Inference Engine finds the FPGA platform.
4. Run inference through the plugin:
   ```
   classification_sample -m squeezenet1.1.xml -i image.png -d HETERO:FPGA,CPU
   ```
   `HETERO:FPGA,CPU` splits layers between FPGA and CPU (often faster than FPGA
   alone for small graphs, because FPGA setup overhead is large).
5. Set `CL_CONTEXT_COMPILER_MODE_INTELFPGA=3` to avoid re-programming each run.

Gotchas:
- The **FPGA plugin only supports a fixed set of prebuilt DLA bitstreams**, not
  arbitrary kernels — the bitstream must match the model architecture.
- Needs the correct `LD_LIBRARY_PATH`: Inference-Engine lib dir plus the SDK's
  `hld/host/linux64/lib`.
- Newer OpenVINO dropped the legacy FPGA plugin in favour of the **FPGA AI Suite**
  (Altera) and oneAPI SYCL tooling. To keep using an old Arria 10 / Stratix V
  card today, freeze on the matching old OpenVINO + FPGA RTE version.

## Older-tool gotchas summary

- Match Quartus <-> OpenCL SDK <-> BSP versions exactly; don't mix.
- Older SDKs (pre-17.1) need their own license (`LM_LICENSE_FILE`).
- BSP is per-board and not included in new SDKs — download it, never assume.
- `aoc` offline compiles are slow; use simulation/emulation for iteration.
- Verify install with `aocl version` and `aoc -list-boards` (vs
  `-list-board-packages` in newer SDKs).
- Cross-compile the host for ARM SoC boards (Cyclone V SoC) with SoC EDS / DS-5.
- Use the ICD loader + correct `.icd` registration when multiple OpenCL
  implementations (FPGA + GPU) coexist.

## Sources of truth

- "Intel FPGA SDK for OpenCL Programming Guide" (Pro/Standard, by version).
- Terasic per-board OpenCL guides (DE1-SoC, DE10-Standard, ...) — the most
  concrete install recipes for old kits.
- OpenVINO "FPGA Plugin" / heterogeneous-plugin docs (per release; the classic
  `2018R1` guide covers Arria 10).
