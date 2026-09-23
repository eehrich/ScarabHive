# Writing synthesizable Verilog for Quartus

These rules make RTL compile cleanly, map to real hardware, and match what
Quartus expects. Most synthesis/sporadic bugs come from violating them.

## Sequential logic (flip-flops)

- Use `always @(posedge clk)` (add `negedge rst_n` in the sensitivity list for
  async reset). This infers registers.
- **Use non-blocking (`<=`) assignments inside a clocked always block.** This is
  what makes a block infer registers instead of a combinational mess.
- **Use blocking (`=`) assignments inside `always @(*)` combinational blocks.**
- **Never mix blocking and non-blocking for the same signal**, and never assign a
  signal in two different always blocks — that creates multi-driver / race bugs.

```verilog
// Good sequential: clock + async active-low reset
always @(posedge clk or negedge rst_n) begin
  if (!rst_n)
    q <= 1'b0;
  else if (en)
    q <= d;
end
```

## Avoid inferred latches

If a `always @(*)` combinational block does not assign **every** output in
**every** branch, Quartus infers a **latch** (transparent-register). Latches are
usually a bug and cause timing/simulation surprises. Fix by giving every case a
default (`else` / `default:`) and by assigning outputs before the case/if.

```verilog
// BAD: 'y' not assigned in the else -> latch
always @(*) begin
  if (sel) y = a;
end

// GOOD: default assignment before conditional
always @(*) begin
  y = b;              // default
  if (sel) y = a;
end
```

## No combinational loops

Never create a loop where a path feeds back through combinational logic only
(`assign a = b; assign b = a;` or `a` derived straight from itself). Quartus
reports it and timing/simulation break. Introduce a register to break any loop.

## Resets

- Use a **synchronous or asynchronous reset** consistently; a common pattern is
  async assert, sync deassert (`always @(posedge clk or negedge rst_n)`).
- **Do not rely on `initial` blocks to set FF values in hardware** — FPGAs
  initialise registers to a defined power-up value, but `initial` in RTL is only
  honoured in simulation. Use explicit reset logic for deterministic hardware
  behaviour.

## Clocks / CDC

- **Prefer clock enables over gated clocks** (`clk && en` is synthesis-hostile):
  use `if (en)` inside a `posedge clk` block rather than ANDing the clock.
- **Keep to one clock per always block** where possible; mixing clocks is messy.
- **Cross clock domains carefully**: pass control signals through a **2-flop
  synchronizer** (two FFs in series) before use; never sample async data across
  domains without synchronisation (metastability). Data buses crossing domains
  need an async FIFO or a validated handshake (gray-code counters for pointers).

## Widths, arithmetic

- Size everything: use correct widths (`[WIDTH-1:0]`), mind carry/overflow.
- `parameter` / `localparam` for constants; `localparam` when local.
- Loop `for`/`generate` unroll at synthesis time — fine for fixed small counts,
  careful with large loops.
- Modulo by a power of two (`& (n-1)`) is cheap; `%`/`/` by non-powers is
  expensive and may not map well — use `>>>`/shifts for powers of two.

## Reusable / Quartus-friendly constructs

- **RAM / ROM / FIFO**: let Quartus infer memory by writing in the recognised
  template style (write strobe + address + data in a clocked block), or use the
  IP cores. These map to block RAM (`M9K`, `M10K`, MLABs) automatically.
- **DSP**: multiply in straight RTL; Quartus maps to DSP blocks (`*`).

## SystemVerilog vs Verilog

- Files use `.v` (Verilog-2001) by default. `.sv` (SystemVerilog) is supported —
  add via `SYSTEMVERILOG_FILE` in the `.qsf`, not `VERILOG_FILE`.
- Mixing is fine within a project; just list each file under the right
  assignment. Keep incremental SystemVerilog features (logic, interfaces,
  always_ff) consistent with what the edition supports.

## Debugging checklist (when synthesis/behaviour is wrong)

1. Inferred **latches** (check messages/warnings) → add default assignments.
2. **Multi-driver** / assigning a signal in two always blocks.
3. Missing/duplicate `VERILOG_FILE`/`SYSTEMVERILOG_FILE` entries for a source.
4. Top-level entity name mismatch.
5. Forgetting an **SDC clock** so timing reports are empty/useless.
6. Unconstrained/reset-less FFs producing `X` in simulation.
7. Pin/IO mismatch with the physical board (wrong IO standard kills IO).
8. Comb loop / gated clock harming timing.
9. **Duplicate parameter/localparam/state-value declaration** (see below) — Quartus does not always flag it, yet the value is ambiguous.
10. **Module port list written after the closing `);`** — ports added after a port list was already closed produce a wall of Verilog syntax errors (unexpected reserved keyword `input`/`output`). Keep **all** ports inside a single `( ... );` before the body.

### Non-blocking assignment collision in state machines

A common Quartus/microcoded bug: **setting `mem_addr` for a write and then overriding it for a fetch address in the same `always @(posedge clk)` block**.

Since non-blocking assignments (<=) are evaluated from the right-hand side before any assignment takes effect, **the last write to a signal wins** — all other assignments to the same signal are silently lost. This causes:
- Stack writes that write to the wrong address (e.g. JSR low byte pushed to memory-mapped I/O instead of stack)
- Register file corruption

**Fix:** Decouple the operations into separate states. E.g. instead of:
```verilog
ST_JSR_A: begin
    mem_addr <= stack_addr;  // write address — OVERRIDDEN
    mem_wr <= 1;
    mem_dout <= pc[7:0];
    sp <= sp - 1;
    mem_addr <= target;      // fetch address — wins, write is lost!
    pc <= target;
    st <= ST_FETCH;
end
```
Use two states:
```verilog
ST_JSR_A: begin
    mem_addr <= stack_addr; mem_wr <= 1;
    mem_dout <= pc[7:0]; sp <= sp - 1;
    st <= ST_JSR_B;
end
ST_JSR_B: begin
    pc <= target; mem_addr <= target; st <= ST_FETCH;
end
```

This is especially relevant when designing custom CPU cores or complex memory controllers in Quartus.

### Stratix V / FPGA block-RAM: asynchronous reads do NOT map to M20K

On Stratix V, **M20K block RAM has registered (synchronous) read output**. An
asynchronous-read inferral like:
```verilog
reg [7:0] mem[0:32767];
assign data_out = mem[addr];   // async read, single always
```
**cannot map to M20K**. Quartus then expands the whole array into thousands of
flip-flops + ALUTs (e.g. a 32KB PRG ROM became 278k registers / 127k ALUTs in
one design). If your design unexpectedly uses way more logic than expected,
check for async-read arrays — use synchronous reads (registered output, or a
read-after-write RAM primitive) to map to M20K and free up logic/routing fabric.

### One driver per register (multiple constant drivers)

Quartus errors 10028/10029 ("multiple constant drivers") when two `always`
blocks drive the same reg — ModelSim may accept it, Quartus synthesis does not.
Keep a **single driver per register**; merge next-state logic into one
`always @(*)` combinational block.

### `tri` is a reserved net type

Don't use `tri` as an identifier (vlog-13069); it's reserved. Use `tri_v`.

### JSR/JMP fetch-address vs stack-write collision (non-blocking last-wins)

See the dedicated note in this file: when a state machine sets a write address
then overrides it with a fetch address in the same clocked block, the **last
assignment wins** and the write goes to the wrong address. Split into separate
states.

### Duplicate parameter / localparam / state-value declarations

Declaring the **same named constant twice** (e.g. two `localparam ST_JSR_B = 5'd10;` and later
`localparam ST_JSR_B = 5'd28;`, or a parameter re-defined in the same scope) is at best
**ambiguous** and often silently gives you the wrong value — synthesis may pick one occurrence
while simulation picks another, or a machine `case` falls into the wrong state. Quartus does not
always warn. Rule: **define each named constant exactly once** (a single `localparam` block); if
you need to override for a configurable module use a `parameter` that is intended to be
overridden from instantiation, and give the default in only one place. Watch for stale duplicate
lines left behind after editing — they are easy to miss and produce hard-to-debug behaviour like
a subroutine call that returns to the wrong address.

### Malformed module port list (ports after `);`)

A module header must be a single list closed by `);`:
```verilog
module cartridge (         // one opening
    input  wire [15:0] a,
    output wire [7:0]  d
);                         // one closing — every port above it
// body ...
endmodule
```
If an edit puts new `input`/`output` ports **after** the `);` they land in the module body and are
rejected ("unexpected reserved keyword `input`"), often cascading into many seemingly unrelated
syntax errors. Fix: move every port above the single `);`, separating them with commas.

### Unobserved / dangling logic is trimmed away

Synthesis removes logic whose outputs feed nothing that reaches a device pin or is otherwise
observed — e.g. debug registers (`dbg_*`), counters and status signals that nothing downstream
reads. The map report shows this as "Registers Removed During Synthesis (... connected to dangling
logic)". Consequences:
- A "successful" compile can still have **dramatically fewer registers than the RTL suggests**
  (an entire block may be optimised out).
- You **cannot probe those signals in hardware** (SignalTap) after they were trimmed — you cannot
  "un-trim" them.
- If a whole subsystem appears missing in the resource report, check whether its outputs actually
  reach a top-level port / the observed datapath. If you need the logic kept (or debugged), drive
  it to a real output or a known "used up" path rather than leaving it dangling.
