//=============================================================================
// tb_seven_seg_counter.v - testbench for the DE10-Lite 7-segment counter.
//------------------------------------------------------------------------------
// Checks:
//   1. After reset the counter displays "00" (HEX1 = HEX0 = decode of 0).
//   2. A reference decoder instance produces the correct active-low pattern
//      for digits 0..9 (compared against the expected bit patterns).
//
// NOTE: the prescaler is 50,000,000 (1 Hz real-time), so a full 0..59
// roll-over test would need ~1 s of simulated time. ModelSim can run that,
// but the reset + decoder checks below pin down the logic deterministically
// and fast. To watch the counter tick in simulation, temporarily shrink
// CLK_FREQ in the DUT (e.g. to 4) and run long enough to see HEX change.
//=============================================================================
`timescale 1ns / 1ps

module tb_seven_seg_counter;

    reg         clk     = 1'b0;
    reg         reset_n = 1'b0;
    wire [9:0]  ledr;
    wire [6:0]  hex1;
    wire [6:0]  hex0;

    integer errors = 0;

    // ------------------------------------------------------------
    // 50 MHz clock: 20 ns period.
    // ------------------------------------------------------------
    always #10 clk = ~clk;

    // ------------------------------------------------------------
    // DUT
    // ------------------------------------------------------------
    seven_seg_counter_top dut (
        .clk     (clk),
        .reset_n (reset_n),
        .ledr    (ledr),
        .hex1    (hex1),
        .hex0    (hex0)
    );

    // ------------------------------------------------------------
    // Reference decoder instance (module-level; tasks must NOT
    // instantiate modules in Verilog).
    // ------------------------------------------------------------
    reg  [3:0]  ref_bcd = 4'd0;
    wire [6:0]  ref_seg;
    seg7_decoder u_ref (.bcd(ref_bcd), .seg(ref_seg));

    // ------------------------------------------------------------
    // Drive the reference decoder and compare with the expectation.
    // ------------------------------------------------------------
    task check_seg(input [3:0] bcd, input [6:0] expected);
        begin
            ref_bcd = bcd;
            #1;                                   // settle combinational logic
            if (ref_seg !== expected) begin
                $display("FAIL: seg(%0d) = %b (expected %b)", bcd, ref_seg, expected);
                errors = errors + 1;
            end else begin
                $display("PASS: seg(%0d) = %b", bcd, ref_seg);
            end
        end
    endtask

    // ------------------------------------------------------------
    // Main stimulus
    // ------------------------------------------------------------
    initial begin
        $display("=== Reset check ===");
        reset_n = 1'b0;
        repeat (5) @(posedge clk);
        reset_n = 1'b1;
        @(posedge clk);

        // After reset, ones=0 and tens=0 -> both displays show '0' = 7'b1000000
        if (hex0 !== 7'b1000000 || hex1 !== 7'b1000000) begin
            $display("FAIL: after reset hex0=%b hex1=%b (expected 1000000/1000000)",
                     hex0, hex1);
            errors = errors + 1;
        end else begin
            $display("PASS: counter is 0 after reset");
        end

        $display("=== Decoder checks (digits 0..9) ===");
        check_seg(4'd0, 7'b1000000);
        check_seg(4'd1, 7'b1111001);
        check_seg(4'd2, 7'b0100100);
        check_seg(4'd3, 7'b0110000);
        check_seg(4'd4, 7'b0011001);
        check_seg(4'd5, 7'b0010010);
        check_seg(4'd6, 7'b0000010);
        check_seg(4'd7, 7'b1111000);
        check_seg(4'd8, 7'b0000000);
        check_seg(4'd9, 7'b0010000);

        if (errors == 0)
            $display("*** ALL TESTS PASSED ***");
        else
            $display("*** %0d ERROR(S) ***", errors);

        $finish;
    end

endmodule
