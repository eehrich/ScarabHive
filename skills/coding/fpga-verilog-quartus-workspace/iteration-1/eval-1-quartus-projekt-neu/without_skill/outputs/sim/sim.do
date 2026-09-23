# sim.do - ModelSim / Questa (Altera FPGA Edition) simulation script
vlib work
vlog ../src/seven_seg_counter_top.v ../src/seg7_decoder.v tb_seven_seg_counter.v
vsim -c work.tb_seven_seg_counter
run -all
quit
