# SDC timing constraints for Quartus

Static timing analysis is only meaningful with constraints. The Timing Analyzer
in Quartus uses standard **SDC** syntax (`.sdc` file, referenced via the `.qsf`
`SDC_FILE` assignment).

## The minimum: clock on the input pin

```sdc
# 50 MHz input clock on port 'clk'
create_clock -name clk50 -period 20.000 [get_ports {clk}]
```

Always name the clock and give a period in **ns**. `period = 1000 / freq_MHz`
(50 MHz → 20 ns).

## Typical complete start-of-file set

```sdc
create_clock -name clk50 -period 20.000 [get_ports {clk}]

# If you use a PLL, let Quartus derive its output clocks:
derive_pll_clocks
derive_clock_uncertainty

# Async clock domains that must not be timed together:
set_clock_groups -asynchronous -group {clk50} -group {clk2}
```

- `derive_pll_clocks` creates clocks on PLL outputs so they get inserted into
  timing automatically.
- `derive_clock_uncertainty` adds realistic clock uncertainty between unrelated
  clocks (needed for reliable closure).

## I/O constraints

```sdc
# Input data arrives 2 ns after the clock edge
set_input_delay -clock clk50 -max 2.0 [get_ports {data_in}]
set_input_delay -clock clk50 -min 0.5 [get_ports {data_in}]

# Output data is captured 3 ns before the next edge at the receiver
set_output_delay -clock clk50 -max 3.0 [get_ports {data_out}]
set_output_delay -clock clk50 -min 1.0 [get_ports {data_out}]
```

For source-synchronous interfaces (DDR, SDR SDRAM) use `-clock` on the related
strobe/clock output and add `-add_delay`.

## False paths & multi-cycle (the two that fix most failures)

```sdc
# Asynchronous resets, no timing relationship:
set_false_path -from [get_ports {reset_n}]

# Cross clock-domain handshake signals (once synchronized):
set_false_path -from [get_clocks clk50] -to [get_clocks clk2]

# Path allowed 2 clock cycles (slack-relaxing, e.g. slow counters feeding logic):
set_multicycle_path 2 -setup -from [get_pins {cnt_reg*}] -to [get_pins {out_reg*}]
```

## Getting the path / slack

At the Tcl level (or the Timing Analyzer GUI):

```tcl
report_timing -setup -npaths 10
```

Look at the reported `Slack`: a negative setup/hold slack means a timing failure
you must fix (reduce logic depth on the path, add pipeline stages, or fix clock
groups).

## Common timing-closure fixes

- **Reduce logic depth on the critical path**: insert pipeline registers (in
  RTL), split combinational functions across more FF stages.
- **Use clock enables instead of gated clocks** (gated clocks hurt timing and
  add skew).
- **Fix multi-clock confusion** with correct `set_clock_groups` / false paths.
- **Pure combinational loop**: breaks synthesis/timing; remove it in RTL.
- **Metastability on CDC**: synchronize with a 2 (or 3) FF chain and put a
  `set_false_path` between the domains — never run data straight across an async
  clock domain.
