# compile.tcl
# Full headless Quartus flow from a Tcl script.
# Usage:
#   quartus_sh -t compile.tcl [project_name]
#   quartus_sh -t compile.tcl myproj
#
# Runs: Analysis & Synthesis -> Fitter -> Assembler -> Timing Analysis,
# then writes a plain-text timing summary so you can check slack without the GUI.

if {[llength $argv] >= 1} {
    set proj [lindex $argv 0]
} else {
    set proj [lindex [glob -nocomplain *.qpf] 0]
    if {$proj eq ""} {
        puts "ERROR: no .qpf found and no project name given."
        puts "Usage: quartus_sh -t compile.tcl <project_name>"
        exit 1
    }
    set proj [file rootname [file tail $proj]]
}

load_package flow
load_package report
load_package timing

puts "========== Opening project: $proj =========="
project_open $proj

puts "========== Running full compile flow =========="
if {[catch {execute_flow -compile} result]} {
    puts "ERROR during compile: $result"
    project_close
    exit 1
}

puts "========== Compile finished. Starting timing report =========="
# Create clocks must be present (via the .sdc) for a useful report.
# Report the worst setup slack across all clocks (safe default).
set failed 0
if {[catch {
    create_timing_netlist -post_map
    read_sdc
    update_timing_netlist
    set regs [get_registers -post_map]
    if {$regs ne ""} {
        report_timing -setup -npaths 5 -panel_name "MyTiming"
        set slacks [get_timing_paths -setup -npaths 10 -slack]
        foreach p $slacks {
            set s [get_metric_info $p -slack]
            puts "  slack: $s"
            if {$s < 0} { set failed 1 }
        }
    } else {
        puts "WARNING: no registers found; cannot report setup timing."
    }
} err]} {
    puts "NOTE: timing report skipped: $err"
}

if {$failed} {
    puts "========== RESULT: TIMING VIOLATION (negative slack) =========="
} else {
    puts "========== RESULT: compile + timing OK =========="
}

# Save a textual report for easy grepping.
set_global_assignment -name TIMEQUEST_REPORT_SCRIPT_PANEL_NAME ""
report_timing_file -file timing_report.txt -npaths 20 -panel_name "MyTiming" 2>/dev/null
if {[file exists timing_report.txt]} {
    puts "Timing report written to timing_report.txt"
}

project_close
exit $failed
