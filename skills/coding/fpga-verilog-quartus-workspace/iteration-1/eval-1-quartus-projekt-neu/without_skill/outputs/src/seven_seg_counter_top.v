//=============================================================================
// seven_seg_counter_top.v
//------------------------------------------------------------------------------
// Top-level design for the DE10-Lite board (Cyclone 10 LP, 10CL025YU256C8G).
//
// A 50 MHz clock drives a 1 Hz tick (prescaler), which increments a 0..59
// "seconds" counter.  The value is shown on two 7-segment displays:
//     HEX1 = tens digit (0..5),  HEX0 = ones digit (0..9)
// The design is reset with the on-board push-button KEY[0] (active LOW,
// asynchronous reset).
//
// Board connections (from the DE10-Lite User Manual, Table 3-2 ... 3-6):
//   clk      -> CLOCK_50  PIN_P11   (50 MHz, 3.3-V LVTTL)
//   reset_n  -> KEY[0]    PIN_B8    (push-button, active low)
//   hex1[6:0]-> HEX1      (common anode, active-low segments)
//   hex0[6:0]-> HEX0
//   ledr[9:0]-> LEDR[9:0]
//=============================================================================
module seven_seg_counter_top (
    input  wire       clk,        // 50 MHz system clock
    input  wire       reset_n,    // active-low asynchronous reset (KEY[0])
    output wire [9:0] ledr,       // 10 on-board LEDs (diagnostic)
    output wire [6:0] hex1,       // tens digit, active-low segments
    output wire [6:0] hex0        // ones digit, active-low segments
);

    // ------------------------------------------------------------------
    // 1 Hz tick generator from the 50 MHz clock (50,000,000 cycles/sec).
    // ------------------------------------------------------------------
    localparam CLK_FREQ = 50_000_000;
    reg [25:0] prescaler = 0;
    reg        tick      = 0;

    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            prescaler <= 26'd0;
            tick      <= 1'b0;
        end
        else begin
            if (prescaler == CLK_FREQ - 1) begin
                prescaler <= 26'd0;
                tick      <= 1'b1;
            end
            else begin
                prescaler <= prescaler + 1'b1;
                tick      <= 1'b0;
            end
        end
    end

    // ------------------------------------------------------------------
    // Seconds counter: ones digit 0..9, tens digit 0..5  (roll-over 59->0)
    // ------------------------------------------------------------------
    reg [3:0] ones = 4'd0;
    reg [3:0] tens = 4'd0;

    always @(posedge clk or negedge reset_n) begin
        if (!reset_n) begin
            ones <= 4'd0;
            tens <= 4'd0;
        end
        else if (tick) begin
            if (ones == 4'd9) begin
                ones <= 4'd0;
                if (tens == 4'd5)
                    tens <= 4'd0;
                else
                    tens <= tens + 1'b1;
            end
            else begin
                ones <= ones + 1'b1;
            end
        end
    end

    // ------------------------------------------------------------------
    // Decode both digits and drive the displays.
    // ------------------------------------------------------------------
    seg7_decoder u_tens (.bcd(tens), .seg(hex1));
    seg7_decoder u_ones (.bcd(ones), .seg(hex0));

    // Visual aid: mirror the tens digit on LEDR[1:0], leave the rest off.
    assign ledr = {8'd0, tens[1:0]};

endmodule
