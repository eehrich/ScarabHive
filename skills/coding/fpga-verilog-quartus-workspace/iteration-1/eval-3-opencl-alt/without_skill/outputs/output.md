# OpenCL auf DE1-SoC (Cyclone V) mit Quartus Prime 17.1

## 1. Welche Versionen du brauchst (aoc/aocl)

Die Intel **FPGA SDK for OpenCL** (früher *Altera SDK for OpenCL*, kurz **AOCL/ACL**) ist **version-locked an deine Quartus-Version gekoppelt**: Quartus und SDK müssen exakt dieselbe Versionsnummer haben. Ein neueres SDK/BSP läuft nicht mit einem älteren Quartus und umgekehrt.

| Quartus Prime | FPGA SDK for OpenCL | Bemerkung |
|---|---|---|
| **Quartus Prime 17.1** (Lite/Standard/Pro) | **SDK 17.1** | SDK-Lizenz ab 17.1 **entfernt** – SDK ist frei / wird mit Quartus gebündelt |
| Quartus Prime 17.0 | SDK 17.0 | |
| Quartus 16.0/16.1 | SDK 16.0/16.1 | DE10-Standard-Anleitungen nutzen 16.1 |
| Quartus 18.0/18.1 | SDK 18.0/18.1 | Alternativ für DE-Serie gut dokumentiert |

Für dein Ziel **Quartus 17.1 ⇒ FPGA SDK for OpenCL 17.1**.

Die zwei Kommandozeilen-Werkzeuge im SDK:

- **`aoc`** (AOC / *Altera/Intel Offline Compiler*): kompiliert ein OpenCL-Kernel (`.cl`) in einen FPGA-Bitstream **`.aocx`**. Dauert Minuten bis Stunden (Hardware-Synthese).
- **`aocl`** (Runtime-/Utility-Tool): `version`, `install`, `uninstall`, `program`, `flash`, `diagnose`, `list-devices`, `list-packages`.

**Wichtiger Lizenz-Hinweis:** Vor 17.1 brauchte das OpenCL-SDK eine **eigene Lizenz** (`LM_LICENSE_FILE`). **Ab 17.1 ist die SDK-Lizenz entfallen** – du brauchst also nur die normale Quartus-17.1-Lizenz, keine zusätzliche OpenCL-Lizenz.

## 2. Welches BSP (Board Support Package)

Das **BSP ist NICHT Teil des SDK** – es wird pro Board separat von Terasic/Intel geladen. Für den DE1-SoC (Cyclone V SoC, ARM-„HPS"-Host) ist das BSP unbedingt nötig, sonst kennt `aoc` das Board nicht.

- **DE1-SoC OpenCL BSP** von der Terasic-Website herunterladen (unter DE1-SoC → Resources → „BSP for Intel FPGA SDK OpenCL"). Terasic liefert DE1-SoC-BSPs u. a. für SDK 14.0 und 16.0; der neuere Stand (v05, De1-SoC_OpenCL) ist für 18.1. For dein 17.1-Setup nimm das zum **SDK 17.1 passende** BSP – achte auf die Versionsangabe auf der Download-Seite.
- Inhalt entpacken nach: `<quartusroot>/hld/board/<vendor>/<board>/`  (z. B. `.../17.1/hld/board/terasic/de1soc/`)
- **`AOCL_BOARD_PACKAGE_ROOT`** auf genau dieses Verzeichnis setzen – es muss eine Datei **`board_env.xml`** enthalten (das ist der Marker, dass das BSP korrekt installiert ist).
- Verifizieren: `aoc -list-boards` (bzw. `aoc --list-boards`; in neueren SDKs `aoc -list-board-packages`) – der DE1-SoC-Eintrag (Board-Name `de1soc`, ältere BSPs `de1soc_sharedonly`) muss gelistet sein.

> Hinweis zu Cyclone-V/SoC-BSPs: Neuere SDK-Versionen lassen ältere Bausteine fallen. Cyclone-V-SoC-Karten haben nur bis zu einer bestimmten SDK-Version ein offizielles BSP. Für ein robustes, dokumentiertes 17.1-Setup ist es oft am einfachsten, beim **gleichen Quartus/SDK/BSP-Dreiklang** zu bleiben und NICHT aufs neueste Release zu gehen.

## 3. Kernel-Compile – der Ablauf

Das Ein-Zeilen-Modell: **`aoc` macht aus deinem `.cl`-Kernel einen `.aocx`-Bitstream; das Host-Programm (a) programmiert diesen Bitstream aufs FPGA und (b) startet die Kernel darüber per OpenCL-API.**

### 3.1 Umgebung setzen (einmal pro Shell)
```bash
# Beispiel für Quartus 17.1 – Pfade an deine Installation anpassen
export QUARTUS_ROOTDIR=/opt/intelFPGA/17.1/quartus
export ALTERAOCLSDKROOT=/opt/intelFPGA/17.1/hld          # SDK-Wurzel
export AOCL_BOARD_PACKAGE_ROOT=$ALTERAOCLSDKROOT/board/terasic/de1soc
export PATH=$PATH:$QUARTUS_ROOTDIR/bin:$ALTERAOCLSDKROOT/bin:$ALTERAOCLSDKROOT/linux64/bin
export LD_LIBRARY_PATH=$ALTERAOCLSDKROOT/linux64/lib
export QUARTUS_64BIT=1
# Kein LM_LICENSE_FILE für das OpenCL-SDK ab 17.1 nötig (nur Quartus-Lizenz)
```
Oder bequemer: `source <quartusroot>/hld/init_opencl.sh` (setzt alles einmalig für die Shell).

### 3.2 Kernel kompilieren (Offline-Compile)
```bash
aoc -board=de1soc mein_kernel.cl -o bin/mein_kernel.aocx
```
- Der `-board=...`-Name muss dem in `aoc -list-boards` entsprechen.
- **Kernel-Compile ist langsam** (minuten- bis stundenlang, es ist echte Hardware-Synthese): erst im Simulator/Emulator arbeiten, nur für den Endlauf voll kompilieren.
  - Simulation: `aoc -march=simulator mein_kernel.cl` (danach mit `CL_CONTEXT_MPSIM_DEVICE_INTELFPGA=1` laufen lassen)
  - Emulation (schnell, funktional): `aoc -march=emulator -legacy-emulator mein_kernel.cl` (ältere SDKs) bzw. `aoc -march=emulator` (neuere)
  - Schnell iterieren: `-incremental` / `-fast-compile`; `-rtl` gibt das erzeugte RTL aus (zur Kontrolle, was der Kernel wird).
- Ergebnis: eine `.aocx`-Datei, die aufs FPGA programmiert wird.

### 3.3 Host-Programm kompilieren (ARM / SoC)
Da der Host auf dem **ARM** des Cyclone-V-SoC läuft, muss das C/C++-Host-Programm **für ARM** gebaut werden:
- Native Kompilierung direkt auf dem Board (Linux), oder
- **Cross-Compile** mit dem **Intel SoC EDS / DS-5** (arm-linux-gnueabihf), libs aus `hld/host/arm32/lib`, verlinke `-lalteracl`.

### 3.4 Auf dem Board laufen lassen
```bash
# auf dem DE1-SoC unter Linux
source /home/root/OpenCL/init_opencl.sh           # Treiber/Umgebung laden
chmod +x mein_host
aocl program /dev/acl0 mein_kernel.aocx           # FPGA mit dem Bitstream programmieren
./mein_host                                       # Host-Programm startet die Kernel
```
Verifizieren: `aocl version` bzw. `aocl diagnose` / `aocl list-devices` zeigen, ob Treiber und Gerät da sind.

## 4. Kurz-Zusammenfassung
1. **Quartus 17.1 + FPGA SDK for OpenCL 17.1** (Version exakt gleich; SDK-Lizenz ab 17.1 frei).
2. **DE1-SoC OpenCL BSP** separat laden → nach `<quartusroot>/hld/board/...` entpacken, `AOCL_BOARD_PACKAGE_ROOT` setzen (enthält `board_env.xml`), `aoc -list-boards` prüfen.
3. Kernel: `aoc -board=de1soc kernel.cl -o kernel.aocx` (langsam – erst simulieren/emulieren).
4. Host für ARM bauen (SoC EDS/DS-5 oder nativ auf dem Board).
5. Auf dem Board: `aocl program /dev/acl0 kernel.aocx` und Host ausführen.

## Quellen
- Skill-Referenz `fpga-verilog-quartus/references/opencl-openvino.md` (Version-Pairing-Tabelle, aoc/aocl, BSP, Kernel-Flow, Env-Variablen).
- Intel FPGA University Program – „Using Intel FPGA SDK for OpenCL on DE-Series Boards" (18.1): BSP-Installation nach `<quartusroot>/hld/board/`, `aoc -list-boards`, Kernel-Compile mit `-board=...`, Host auf ARM nativ via `make`, `aocl program /dev/acl0` – https://ftp.intel.com/Public/Pub/fpgaup/pub/Teaching_Materials/current/Tutorials/OpenCL_On_DE_Series_Boards.pdf – bestätigt: „As of version 17.1, you no longer require a license to use the Intel FPGA SDK for OpenCL."
- Terasic DE1-SoC CD-ROM-Listing: offizielle DE1-SoC OpenCL BSPs (14.0, 16.0; neuere v05 für 18.1) – http://www.terasic.com/downloads/cd-rom/de1-soc
- DE1-SoC OpenCL BSP-Listing & ED1-SoC-OpenCL-Doku (Board-Name `de1soc_sharedonly`, `aoc --list-boards`) – https://people.ece.cornell.edu/land/courses/ece5760/DE1_SOC/DE1SOC_OpenCL_v02.pdf ; neuere Version: https://download.terasic.com/downloads/cd-rom/de1-soc/linux_BSP/OPENCL18.1/DE1-SoC_OpenCL_v05.pdf
- Community-Erfahrung 17.1 auf DE1-SoC: https://github.com/thinkoco/c5soc_opencl/issues/19 (Bestätigung „Quartus Prime Lite 17.1 + Intel FPGA SDK for OpenCL 17.1 + SoC EDS", Host-Libs aus `hld/board/terasic/de1soc/arm32/lib`).
