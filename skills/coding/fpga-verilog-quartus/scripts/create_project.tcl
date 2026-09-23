# create_project.tcl
# Create a new Quartus Prime project headlessly, from the command line.
# Usage:
#   quartus_sh -t create_project.tcl -- <proj> <family> <device> <topentity> <files...>
#
# Examples:
#   quartus_sh -t create_project.tcl myproj
#   quartus_sh -t create_project.tcl counter "Cyclone 10 LP" 10CL006YU256C8G top src/top.v src/decode.v
#
# If only the project name is given, edit the defaults below first and it
# will create a minimal runnable project in the current directory.

package require cmdline

set def_family  "Cyclone V"
set def_device  "5CEBA4F23C7"
set def_top     "top"
set def_sdc     "constraints.sdc"

# ---- parse args ----
if {[llength $argv] >= 1} {
    set proj   [lindex $argv 0]
} else {
    set proj   "myproj"
}
if {[llength $argv] >= 2} { set family  [lindex $argv 1] } else { set family $def_family }
if {[llength $argv] >= 3} { set device  [lindex $argv 2] } else { set device $def_device }
if {[llength $argv] >= 4} { set top     [lindex $argv 3] } else { set top $def_top }
set sources [lrange $argv 4 end]

load_package project

# new project: proj.qpf + proj.qsf (one revision, same name as project)
project_new $proj -overwrite -family $family

# NOTE: project_new with -family applies the family; device/top set below.
set_global_assignment -name DEVICE $device
set_global_assignment -name TOP_LEVEL_ENTITY $top
set_global_assignment -name PROJECT_OUTPUT_DIRECTORY output_files
# let Quartus pick the best-fit part automatically if an exact part is not given
if {$device eq "AUTO"} {
    set_global_assignment -name DEVICE AUTO
}

foreach f $sources {
    # classify by extension so VERILOG/SYSTEMVERILOG/VHDL assignments are correct
    set ext [string tolower [file extension $f]]
    switch $ext {
        ".v"   { set_global_assignment -name VERILOG_FILE $f }
        ".sv"  { set_global_assignment -name SYSTEMVERILOG_FILE $f }
        ".vhd" -
        ".vhdl"{ set_global_assignment -name VHDL_FILE $f }
        ".sdc" { set_global_assignment -name SDC_FILE $f }
        default { set_global_assignment -name SOURCE_FILE $f }
    }
}

# If an .sdc was not passed, point at a conventional constraints.sdc
# (create it yourself; see references/timing-sdc.md).
if {![llength [glob -nocomplain *.sdc references/*.sdc 2>/dev/null]]} {
    if {$def_sdc ne "constraints.sdc"} {
        set_global_assignment -name SDC_FILE constraints.sdc
    }
}

project_close

puts "Created project: $proj"
puts "  family : $family   device : $device   top : $top"
puts "  now run: quartus_sh --flow compile $proj"
