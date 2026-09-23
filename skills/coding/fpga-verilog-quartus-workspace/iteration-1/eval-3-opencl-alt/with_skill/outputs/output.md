# OpenCL auf dem DE1-SoC (Cyclone V) mit Quartus 17.1

Antwort auf die Frage: **"Welche aoc/aocl-Versionen und BSP brauch ich, und wie läuft der Kernel-Compile?"**

## 1. Welche aoc/aocl-Version (FPGA SDK for OpenCL) du brauchst

Das FPGA SDK for OpenCL (früher *Altera SDK for OpenCL*, kurz **AOCL/ACL**)
ist **versions-gekoppelt an deine Quartus-Version** — die beiden müssen
**exakt dieselbe Versionsnummer** haben. Da du **Quartus Prime 17.1** nutzt,
brauchst du:

| Komponente | Version |
|-----------|---------|
| Quartus Prime | **17.1** (Lite/Standard/Pro) |
| FPGA SDK for OpenCL | **17.1** |
| `aoc` (Offline-Compiler) | 17.1 (aus demselben SDK) |
| `aocl` (Runtime/Util) | 17.1 (aus demselben SDK) |

**Wichtig zur 17.1:** Ab **17.1 wurde die separate OpenCL-SDK-Lizenz
entfernt** — das SDK ist bei 17.1 frei/gratis und direkt im Quartus-Paket
enthalten. Bei älteren Versionen (< 17.1) brauchte man zusätzlich eine eigene
Lizenz (`LM_LICENSE_FILE`). Mit 17.1 entfällt das.

Mische **nie** verschiedene Versionen: Ein 16.1-BSP oder ein 18.1-`aocx`
funktioniert nicht mit einem 17.1-SDK und umgekehrt.

## 2. Welches BSP (Board Support Package) du brauchst

Das BSP ist **pro Board** und **nicht im SDK enthalten** — du musst es separat
von Terasic herunterladen:

- **Board:** DE1-SoC (Cyclone V SoC / HPS)
- **BSP:** „DE1-SoC OpenCL Board Support Package" von der **Terasic-Website**
  (für Quartus/SDK 17.1 passende Version auswählen; Windows: `DE1-SoC_openCL_BSP.zip`,
  Linux: `DE1-SoC_openCL_BSP.bz2` — gleicher Inhalt, nur anderes Archivformat).
- **Installation:** Das BSP (Ordner `board/terasic/de1soc`) nach
  `<install>/17.1/hld/board/` legen und die Umgebungsvariable setzen:

```bash
export ALTERAOCLSDKROOT=<install>/17.1/hld
export AOCL_BOARD_PACKAGE_ROOT=$ALTERAOCLSDKROOT/board/terasic/de1soc
export PATH=$PATH:.../17.1/quartus/bin:.../17.1/hld/bin:.../17.1/hld/linux64/bin
export LD_LIBRARY_PATH=$ALTERAOCLSDKROOT/linux64/lib
```

- **Board-Name prüfen:** Nach der Installation
  `aoc -list-boards` (ältere SDKs) bzw. `aoc -list-board-packages` (17.1)
  aufrufen und prüfen, dass **`de1soc`** (bzw. `de1soc_sharedonly`) gelistet ist.
- Auf dem DE1-SoC läuft der **Host auf dem eingebetteten ARM (HPS)** — der
  Host-Code wird mit **SoC EDS / DS-5 cross-compiliert**.

## 3. So läuft der Kernel-Compile (Workflow)

Der Ablauf hat zwei Teile: **Offline-Compile (aoc)** und **Runtime (aocl + Host-Programm)**.

### a) Offline-Compile: `.cl` → Bitstream `.aocx`

```bash
# 1. Verfügbare BSPs prüfen (Board-Name merken)
aoc -list-boards

# 2. Kernel kompilieren (dauert MINUTEN bis STUNDEN → zuerst simulieren/emulieren!)
aoc -board=de1soc mykernel.cl -o mykernel.aocx
```

**Tipps für den Compile:**
- **Simulation** (schnell, kein Hardware): `aoc -march=simulator mykernel.cl`
  und dann laufen lassen mit `CL_CONTEXT_MPSIM_DEVICE_INTELFPGA=1`.
- **Emulation** (funktional, schnell): neuere SDKs `aoc -march=emulator`,
  ältere `aoc -march=emulator -legacy-emulator`.
- Für schnelle Iterationen: `aoc -incremental` / `-fast-compile`.
- `aoc -rtl` gibt das erzeugte Zwischen-RTL aus (schau nach, was der Kernel wird).
- **Erst den Kernel optimal schreiben, dann den langsamen Hardware-Compile** machen.

### b) Runtime: Bitstream programmieren + Host-Programm starten

```bash
aocl install <path_to_bsp>          # Kernel-Treiber einmalig installieren
aocl program de1soc mykernel.aocx   # FPGA mit dem .aocx programmieren
aocl diagnose / aocl list-devices   # Verbindung prüfen
```

Das **Host-Programm** (C/C++, cross-compiliert für den ARM) lädt den Bitstream
und startet den Kernel über die normale OpenCL-API:
`clGetPlatformIDs` → `clGetDeviceIDs` → `clCreateContext` → `clCreateProgramWithBinary`
(mit dem `.aocx`) → `clCreateKernel` → Args setzen → `clEnqueueNDRangeKernel`.

## 4. Zusammenfassung / Checkliste

1. **Quartus Prime 17.1** installieren (mit Cyclone-V-Geräte-Paket).
2. **FPGA SDK for OpenCL 17.1** — gleiche Version, ab 17.1 lizenzfrei.
3. **DE1-SoC OpenCL-BSP (17.1)** von Terasic herunterladen,
   nach `17.1/hld/board/terasic/de1soc` legen.
4. **`AOCL_BOARD_PACKAGE_ROOT`** setzen.
5. Mit `aoc -list-boards` prüfen, dass das Board da ist.
6. Kernel mit `aoc -board=de1soc kernel.cl -o kernel.aocx` kompilieren
   (vorher emulieren/simulieren!).
7. Host für ARM cross-compilieren (SoC EDS/DS-5), `aocl install` + `aocl program`,
   Host starten.

## Quellen
- Skill `fpga-verilog-quartus` → `references/opencl-openvino.md` (Versionstabelle
  Quartus↔SDK, BSP-Umgebungsvariablen, aoc/aocl-Arbeitsablauf).
- Terasic-Dokumentation „DE1-SoC OpenCL" (BSP-Download, `AOCL_BOARD_PACKAGE_ROOT`).
- Intel „FPGA SDK for OpenCL Programming Guide" (17.1).

> Hinweis: Die genauen Terasic-Download-Links und der exakte Board-Name
> (`de1soc` vs. `de1soc_sharedonly`) können je nach BSP-Version variieren —
> immer mit `aoc -list-boards` auf deiner Installation verifizieren.
