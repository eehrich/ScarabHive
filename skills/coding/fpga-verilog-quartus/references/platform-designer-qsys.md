# Platform Designer / Qsys headless (no GUI)

Quartus "Platform Designer" (formerly Qsys) builds systems of IP and
interconnect as a `.qsys` XML file, then *generates* HDL. Many IPs (PCIe Hard
IP, PLL, on-chip RAM) must live inside a Qsys system.

Hard-won, verified-on-Quartus-17.0/Stratix-V facts for attaching your own
Verilog module as an Avalon-MM slave to a PCIe Hard IP **entirely from a
terminal** - no GUI, no IP Parameter Editor.

## The two headless tools (do not confuse them)

- `qsys-script`   = the scriptable editor. Loads/saves the `.qsys` and lets you
                    add/connect components. It does NOT generate HDL.
- `qsys-generate` = generates the HDL. Run it AFTER `qsys-script` saved.
- (`qsys-edit` is the GUI; its `--script` switch does not exist in 16.1/17.x.)

`qsys-script` exposes the system-editing Tcl API only after
`package require qsys`:

```tcl
package require qsys
# registers: load_system, save_system, add_instance, add_connection,
#   set_connection_parameter_value, lock/unlock_avalon_base_address,
#   validate_system, get_instance_interfaces, get_instances, ...
# (there is NO `generate` and NO `add_component` here)
```

Scope queries on a named instance use `get_instance_interfaces <inst>` and
`get_instance_interface_ports <inst> <ifc>` (plain `get_interfaces` returns the
top-level system interfaces, not yours).

Let the tool find your custom component: pass a comma-separated `--search-path`
AND call `reload_ip_catalog` before `add_instance`:

```sh
qsys-script --system-file=top.qsys --search-path="my_ipdir,$" --script=edit.tcl
# inside edit.tcl:
#   package require qsys
#   load_system top.qsys
#   reload_ip_catalog
#   add_instance nes_top nes_top     # name + component type from _hw.tcl
```

## Write a `_hw.tcl` component for your own module

A Qsys component definition (`*_hw.tcl`) wraps your RTL with its interfaces.
Keep it minimal; the interface-property whitelist is strict in 16.1:

```tcl
package require -exact qsys 16.1
set_module_property NAME nes_top
set_module_property VERSION 1.0
set_module_property INTERNAL false
set_module_property GROUP "Custom/Board"
add_fileset quartus_synth QUARTUS_SYNTH "" ""
set_fileset_property quartus_synth TOP_LEVEL nes_top
foreach f {nes_top.v sub.v} { add_fileset_file $f VERILOG PATH ./$f }

add_interface clk clock end
add_interface_port clk clk clk input 1
add_interface reset_n reset end
set_interface_property reset_n associatedClock clk
add_interface_port reset_n reset_n reset input 1
add_interface s1 avalon slave end
set_interface_property s1 associatedClock clk
set_interface_property s1 associatedReset reset_n
set_interface_property s1 addressUnits SYMBOLS      # BYTES addressing
set_interface_property s1 maximumPendingReadTransactions 1
add_interface_port s1 addr address input 21         # see "address window"
add_interface_port s1 write write input 1
add_interface_port s1 writedata writedata input 32
add_interface_port s1 read read input 1
add_interface_port s1 readdata readdata output 32
add_interface_port s1 waitrequest waitrequest output 1
add_interface_port s1 readdatavalid readdatavalid output 1
```

**Gotchas that silently break a `_hw.tcl`** (they abort the file, leaving the
interface empty):
- `set_interface_property s1 dataBitsPerSymbol 8` -> "No parameter
  dataBitsPerSymbol" -> s1 empty ("Interface has no signals"). Omit it.
- Conduit port direction is `output`, NOT `export` (on a leaf component
  `export` aborts with "8 not allowed for Direction").
- Do not set reset `associatedDirectReset` / `associatedClockSink`.
- If interfaces go missing after `add_instance`, read the `_hw.tcl` output
  (a property error there has already killed the file).

## Attach your slave to a PCIe Hard-IP bar (DE5 / Stratix V)

The Stratix-V PCIe Hard IP `altera_pcie_256_hip_avmm` exposes
`<hip>.Rxm_BAR4`
as the "application" Avalon-MM master that reflects PCIe BAR4 to your Avalon
slaves. Map your logic onto it:

```tcl
# inside qsys-script script:
load_system top.qsys
add_instance nes_top nes_top          # + reload_ip_catalog first
add_connection clk_0.clk nes_top.clk          # 50 MHz: no PLL needed
add_connection clk_0.clk_reset nes_top.reset_n
add_connection pcie_256_dma.Rxm_BAR4 nes_top.s1
set_connection_parameter_value pcie_256_dma.Rxm_BAR4/nes_top.s1 baseAddress 0x0000
save_system
```

Address bases are named <master>/<slave> with a slash. Give your slave the LOW
window so it sees the host's absolute offsets; push any on-chip RAM up out of
it.

Then generate (search path is required so it can resolve your `_hw.tcl`):

```sh
qsys-generate --synthesis=VERILOG --search-path="my_ipdir,$" top.qsys
```

Add the generated QIP to the Quartus project:
`set_global_assignment -name QIP_FILE top/synthesis/top.qip`

Board top: instantiate the generated `top` module, wire the HIP's real pins
(`refclk_clk`, `pcie_rstn_pin_perst`), tie the PIPE debug interfaces off
(`hip_ctrl_test_in`, `hip_ctrl_simu_mode_pipe`, `hip_pipe_sim_pipe_pclk_in` =
0), then a normal `quartus_sh --flow compile`.

## Address-window rule that trips everyone

A 32-bit `address` port spans the whole 4 GB and overlaps every sibling slave
("...0x0..0xffffffff overlaps..."). Narrow `address` to the highest decoded bit
(NES regions reach 0x15xxxx, so bits [19:0] -> width 21), give it base 0x0000,
and place other RAM above that window.

## One more PCIe-IP fact

The hard-IP owns the high-speed transceivers on dedicated die pins. Only
`refclk_clk`, `pin_perst` and the optional PIPE/serial debug/test buses come out
to the fabric; those debug buses (the many `hip_*` / `hip_pipe_*` / `hip_serial_*`
ports) are tied off or ignored when you use the Avalon reading/writing path.
