# Makefile for Quartus Prime headless builds.
# Targets:
#   make compile           -> quartus_sh --flow compile
#   make program           -> program the .sof via JTAG
#   make jic               -> convert .sof -> .jic for flash
#   make clean             -> delete build artefacts
#   make distclean         -> clean + delete .qsf/.qpf (start fresh)
#
# Usage:
#   1. Place this in your project root (next to .qpf/.qsf)
#   2. Edit PROJECT to match your .qpf name
#   3. make compile
#
# Requires Quartus binaries on PATH (or set QUARTUS_ROOTDIR).

QUARTUS_ROOTDIR ?= /opt/intelFPGA_pro/24.3/quartus
PROJECT        ?= myproj
SOF            ?= output_files/$(PROJECT).sof
JIC            ?= output_files/$(PROJECT).jic
QSH             = $(QUARTUS_ROOTDIR)/bin/quartus_sh
PGM             = $(QUARTUS_ROOTDIR)/bin/quartus_pgm
CPF             = $(QUARTUS_ROOTDIR)/bin/quartus_cpf

.PHONY: compile program jic clean distclean

compile:
	$(QSH) --flow compile $(PROJECT)

program: $(SOF)
	$(PGM) -c 1 -m JTAG -o "p;$(SOF)"

jic: $(SOF)
	$(CPF) -c $(SOF) $(JIC)

clean:
	rm -rf output_files db incremental_db *.rpt *.pin *.smsg *.summary
	rm -f timing_report.txt

distclean: clean
	rm -f $(PROJECT).qpf $(PROJECT).qsf $(PROJECT).sld $(PROJECT).done
