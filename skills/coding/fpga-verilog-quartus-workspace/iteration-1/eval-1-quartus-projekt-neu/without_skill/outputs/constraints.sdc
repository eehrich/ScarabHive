# ============================================================
# constraints.sdc - timing constraints for the DE10-Lite
# 7-segment counter (Cyclone 10 LP, 50 MHz).
# ============================================================

# 50 MHz input clock on port 'clk' -> period 20 ns.
create_clock -name clk_50 -period 20.000 [get_ports {clk}]

# Optionally let the tools add realistic clock uncertainty between
# unrelated clocks (no PLL here, but harmless and good practice).
derive_clock_uncertainty

# The reset (KEY[0]) is asynchronous and has no timing relationship
# to the clock; do not time paths launched/captured by it.
set_false_path -from [get_ports {reset_n}]
