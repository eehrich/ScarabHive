//=============================================================================
// seg7_decoder.v
//------------------------------------------------------------------------------
// Combinational decoder from a 4-bit BCD digit to a 7-segment pattern.
//
// The DE10-Lite 7-segment displays are COMMON-ANODE: a segment lights up when
// its cathode is pulled LOW.  Output bit mapping used here:
//     seg[6:0] = {g, f, e, d, c, b, a}   (seg[6] = 'g', seg[0] = 'a')
// with an ACTIVE-LOW pattern, i.e. a '0' means "segment ON".
//=============================================================================
module seg7_decoder (
    input  wire [3:0] bcd,     // BCD digit 0..9
    output reg  [6:0] seg      // {g,f,e,d,c,b,a}, active LOW
);

    always @(*) begin
        case (bcd)
            4'h0: seg = 7'b1000000; // 0
            4'h1: seg = 7'b1111001; // 1
            4'h2: seg = 7'b0100100; // 2
            4'h3: seg = 7'b0110000; // 3
            4'h4: seg = 7'b0011001; // 4
            4'h5: seg = 7'b0010010; // 5
            4'h6: seg = 7'b0000010; // 6
            4'h7: seg = 7'b1111000; // 7
            4'h8: seg = 7'b0000000; // 8
            4'h9: seg = 7'b0010000; // 9
            default: seg = 7'b1111111; // blank for 0xA..0xF
        endcase
    end

endmodule
