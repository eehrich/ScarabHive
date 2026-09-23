# program.tcl
# Program an FPGA via quartus_sh, with a device-cable sanity check.
# Usage:
#   quartus_sh -t program.tcl [cable index]
# Example:
#   quartus_sh -t program.tcl 1
#
# Programs the .sof (volatile) from output_files/ for the project inferred
# from the .qpf in the current directory. For flash (.jic) see the Makefile.
#
# NOTE: quartus_pgm is usually the right tool for this; this script wraps it
# via Tcl for consistency with the rest of the headless flow.

if {[llength $argv] >= 1} { set cable [lindex $argv 0] } else { set cable 1 }

set qpf [lindex [glob -nocomplain *.qpf] 0]
if {$qpf eq ""} {
    puts "ERROR: no .qpf in current directory."
    exit 1
}
set proj [file rootname [file tail $qpf]]
set sof  "output_files/${proj}.sof"

if {![file exists $sof]} {
    puts "ERROR: $sof not found. Run 'make compile' first."
    exit 1
}

puts "Programming $sof via JTAG cable $cable ..."
# Run quartus_pgm as a subprocess (Tcl exec) to keep it simple and observable.
set cmd "quartus_pgm -c $cable -m JTAG -o p;${sof}"
puts "  \$ $cmd"
if {[catch {exec {*}[split $cmd " "]} res]} {
    puts "ERROR programming: $res"
    exit 1
}
puts "Programmed OK."
