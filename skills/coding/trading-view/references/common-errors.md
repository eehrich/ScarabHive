# Common Errors, Debugging & v5→v6 Migration

The difference between a script that works and one that frustrates is often
a single subtle Pine behavior. This doc collects the frequent pitfalls, the
debugging toolkit, and what changed between v5 and v6.

## Contents
1. Frequent compiler errors
2. Repainting — sources & fixes
3. Lookahead bias
4. Performance & limit errors
5. Debugging toolkit
6. v5 → v6 migration notes
7. Delivery checklist

## 1. Frequent compiler errors

| Error | Cause / fix |
|---|---|
| `Undeclared identifier` | Variable used before declaration, misspelled, or out of scope (declared inside an `if`). |
| `Cannot use mutable variable in security` | `var`/`varip` or reassigned variables inside `request.security`. Use only const/simple/`ta.*` expressions. |
| `Mismatched input` | Missing indentation, stray quotes, unbalanced brackets. Pine is indentation-sensitive — use 4 spaces. |
| `line too long` | Break long lines; Pine code lines have a hard length limit. |
| `The function ... does not have a `na`-safe version` | The expression can return `na` where a function needs a number. Guard with `na(x) ? default : x` or `nz(x, 0)`. |
| `Cannot call ... with arguments` | Wrong arity/types. Check the signature in the cheatsheet. |
| `The expression cannot be used as a series` | A `series` value where `simple`/`const` is required (e.g. some `input` or `request` contexts). |
| `Compilation of %d loop(s) aborted` | Loop too expensive — reduce iterations or restructure. |
| `Too many bars referenced` | `x[n]` with large `n`. Set `max_bars_back` on the declaration or the specific call. |
| `Arrays of type array are not supported.(CE10022)` | Pine has **no nested arrays** — `array<array<float>>` fails to compile. Use one flat array (ring buffer) or `matrix`. Pattern in §1b. |
| `shorttitle is too long (11 characters). 10 characters or less.(SHORT_TITLE_TOO_LONG)` | The `shorttitle` argument is capped at **10 characters** (`title` has no such limit). Shorten it, e.g. `shorttitle="VolaProfil"`. |
| `No value assigned to the "start_column" / "start_row" parameter in table.clear()(CE10165)` | `table.clear(id, start_column, start_row, end_column, end_row)` — the first two params are **mandatory**. For a 1×1 table use `table.clear(tbl, 0, 0)`. |
| `Cannot call 'time'/'request.security' with argument 'timeframe' ... 'series string' is expected 'simple string'` | TF/symbol arguments must be a **simple string** (known at first execution). Build a dynamic TF with a nested ternary of const strings, *not* with `if/else :=` (reassignment makes it a `series string`). |
| `Cannot call 'line.new' with argument 'x2' ... 'series float' is expected 'series int'` | `line.new`/`line.set_x1/x2` x-coordinates are **bar indices (int)**, y-coordinates are prices (float). Cast float offsets: `int(math.round(...))`. |
| `Ternary operations cannot return tuples.(CE10163)` | `cond ? [a, b] : [c, d]` is illegal — a ternary **cannot return a tuple**. Use an `if/else` whose branches return the tuple. Classic v4->v6 trap when replacing the removed `iff()` function. |

### 1b. Pattern: per-slot statistics without nested arrays (CE10022)

Need one history per *time-of-day slot* (e.g. the average volatility at the
same intraday time over the last N days)? Pine forbids `array<array<float>>`
(CE10022). Use one **flat array as a ring buffer** plus a per-slot counter:

```pine
int windowSize = lookback             // last N completed days
int allocSlots = barsPerDay           // e.g. 86400 / timeframe.in_seconds()
var array<float> buf = array.new<float>(allocSlots * windowSize, 0.0)
var array<int>   cnt = array.new<int>(allocSlots, 0)

int slotIdx = int((hour * 60 + minute) / minutesPerBar)   // time-of-day slot

float avg = na
int c      = cnt.get(slotIdx)
int stored = math.min(c, windowSize)
if stored > 0
    float sum = 0.0
    for i = 0 to windowSize - 1
        sum += buf.get(slotIdx * windowSize + i)
    avg := sum / stored               // average of completed days only

// CRITICAL: write history once per bar, on the close (see §1d) —
// on a realtime bar Pine runs the script on every tick.
if barstate.isconfirmed
    int pos = slotIdx * windowSize + (c % windowSize)     // ring position
    buf.set(pos, value)
    cnt.set(slotIdx, c + 1)
```

The ring position overwrites the oldest value once the window is full — no
`array.shift()` needed; cost is O(windowSize) per bar for the sum. Guard with
`timeframe.isminutes` first so `allocSlots` stays sane (1m chart = 1440
slots; a seconds chart would blow up the buffer). **Always gate the ring
write with `barstate.isconfirmed`** — see §1d for why.

**Alternative:** `matrix<float>` is a true 2-D store but has no per-row
rolling window — the flat ring buffer above is usually simpler.

### 1c. Logic lesson: which side does the factor multiply?

A user-tunable factor in a threshold comparison has two possible meanings,
and only one behaves intuitively:

```pine
cur * factor > avg    // factor 2 → MORE signals (looser) — usually NOT intended
cur > avg * factor    // factor 2 → FEWER signals (stricter) — intuitive
```

Rule of thumb: for a "X times above the baseline" signal, multiply the
**baseline**, not the current value, so a higher factor makes the condition
stricter (less red / fewer alerts). State the direction explicitly in the
input tooltip ("höher = strenger") and sanity-check both extremes
(factor 0.5 vs 2.0) before shipping.

### 1d. Realtime ticks hammer `var` accumulators (the "always red at 15:30" bug)

On the *realtime* (still-forming) bar, Pine executes the script body on
**every tick** — not once per bar. Any `var` mutation in the main body
(`cnt += 1`, ring-buffer writes, running sums) therefore runs many times on
that one bar. Symptom seen in the wild: a time-of-day volatility ring got
flooded with the ticks of a single 15:30 bar, so the "average of the last N
days" collapsed to ~today's ticks and the threshold was exceeded almost
every tick → "15:30 is always red".

Rules:
- **Write history / increment counters only on `barstate.isconfirmed`** — one
  guaranteed execution per bar (historical bars count as confirmed, so this
  still writes exactly once per historical bar).
- Read-only calculations (plotting, comparisons, reading the ring) may run
  on every tick.
- The same guard applies to *any* `var` accumulator that must count **bars**,
  not **ticks**.

## 2. Repainting — sources & fixes

**What it is:** the indicator shows a signal on a bar, then the signal
disappears or moves to another bar when that bar closes — because it was
computed on data that was still forming.

**Common sources:**
1. Signals computed on the *realtime* bar without waiting for its close.
2. HTF values from a still-forming HTF bar (see multi-timeframe.md §3).
3. Strategies with `calc_on_every_tick = true` filling orders intrabar.
4. Lookahead in `request.security` (see §3 of this doc).
5. `plotshape`/`alert` conditions that use `high`/`low`/`close` of the
   current realtime bar (they change as the bar forms).

**Fixes:**
- Only emit signals on `barstate.isconfirmed`.
- Or calculate signals on the *previous* closed bar: `signal = cond[1]`.
- Use `request.secure(...)` for HTF data.
- For strategies prefer `process_orders_on_close = true` (default-ish) and
  no `calc_on_every_tick`.
- Test for repainting: compare the indicator's last bar with the same
  indicator on a *closed* chart (e.g. the daily close vs. a finished day).

## 3. Lookahead bias

Using a bar's own `close` to decide something *on the same bar* is fine
(the bar is complete in the backtest). Using **future** data is not:

- `request.security(..., lookahead = barmerge.lookahead_on)` makes today's
  HTF close available on today's bar — future info.
- `x[negative]` looks forward — only allowed on specific functions
  (`ta.pivothigh`, `ta.pivotlow`, `ta.valuewhen`, ...) and is *not* a
  general pattern. Never hand-roll `close[0]`-style forward references for
  signal decisions.
- Repeating HTF values with `gaps_off` do not leak future data, but
  re-evaluating a *still open* HTF bar does.

Rule: if the number could not have been known at that bar's close, it must
not drive a signal in the backtest.

## 4. Performance & limit errors

| Limit | Practical guidance |
|---|---|
| 40 `request.*` calls per script | Batch with array requests. |
| ~5000 `strategy.*` signals / 9000 trades (v5); **no 9000-trade limit in v6** | Keep loop counts sane. |
| Loop budget ~5000 ms / ~10M operations per run | Avoid per-bar heavy loops. |
| Object limits (labels ~50 by default, lines, boxes) | Delete old objects; raise via declaration params only if needed. |
| `max_bars_back` (default 5000) | Set explicitly when using deep history. |

Performance tips:
- Prefer built-in `ta.*` over manual loops.
- Guard heavy work with `barstate.isconfirmed` (skip realtime ticks).
- Limit `request.*` calls and use the tightest timeframe.
- Avoid plotting hundreds of series; fewer plots, faster render.

## 5. Debugging toolkit

- **Plot it.** `plot(expression, "debug")` — the fastest way to see a value.
  Use `plot(cond ? 1 : 0)` to visualize booleans. Remove before shipping.
- **Status line / Data Window**: hover a bar to inspect plotted values.
- **Labels as print()**: `label.new(bar_index, high, str.tostring(x))` for
  point-in-time values.
- **`var` counters**: count how often a branch executes:
  `var int cnt = 0` ... `cnt += 1` and print on the last bar.
- **`runtime.error(msg)`** (v6): raise a runtime error with a custom message
  — useful for invalid input combos.
- **`str.tostring` formats**: `"#.##"`, `"#.#####"`, `"0.000"` for readable
  debug output.
- **Compare against known-good**: for a hand-rolled indicator, plot the
  built-in equivalent (`ta.rsi` vs. your RSI) to verify the math.

## 6. v5 → v6 migration notes

Highlights of what changed when migrating from v5:

| Area | v5 | v6 |
|---|---|---|
| Version line | `//@version=5` | `//@version=6` |
| Integer division | `5 / 2` → `2` (int) | `5 / 2` → `2.5` (float) |
| Booleans | implicit `na` falsiness in places | strict `bool`; check `na()` explicitly |
| Negative indexing | not allowed on arrays | allowed (`arr[-1]` = last) |
| Trade limit | 9000-trade cap | no 9000-trade cap |
| `request.*` | mostly static | **dynamic** requests allowed (v6) |
| `alert.freq_on_close` | used | deprecated → `alert.freq_once_per_bar_close` |
| `color.new(c, t)` | `transp` arg | same (second arg transparency) |
| `%` operator | remainder | still remainder |
| `matrix`/`map` | limited | richer, with methods |
| Footprint/range charts | no | **supported** (v6) |
| `switch` statement | no | yes (v6) |
| Enums | no | yes (v6) |
| `timeframe.period` | `"D"`, `"W"`, `"M"` (no multiplier) | **always includes the multiplier**: `"1D"`, `"1W"`, `"1M"` — `== "D"` never matches |
| `bool` `na` state | bools can be `na` | **no `na` bools** — a comparison involving `na` evaluates to `false`; `[]` history of a bool on early bars = `false` |
| `for` loop bounds | evaluated once before the loop | **re-evaluated before every iteration** — mutating the bound in the body can loop forever; snapshot it first |
| `[]` on literals | allowed | **compile error** — history-referencing needs a variable |
| `and` / `or` | strict evaluation | **lazy** — a call in the right operand may be skipped when the result is already known |

> **Silent v6 breakers** (compile fine, logic quietly changes):
> - `timeframe.period` never equals `"D"` / `"W"` / `"M"` anymore — `==` comparisons silently fall
>   through to the default branch. Use `timeframe.isdaily` / `isweekly` / `ismonthly`, or compare
>   against `"1D"` / `"1W"` / `"1M"`.
> - Anything that would have been bool-`na` (comparisons with `na`, early-bar bool history) now
>   evaluates to `false` — conditions/alerts just stay off, no crash.

Quick migration path: bump `//@version=6`, then fix the strict-bool and
int-division errors the compiler flags. Test the result against the v5
output on the same chart before trusting it.

## 7. Delivery checklist

Before handing code to the user, verify:

- [ ] `//@version=6` and exactly one `indicator()`/`strategy()` declaration.
- [ ] All user-tunable params are `input.*` (with `minval` where sensible).
- [ ] Every calculated series the user cares about is plotted or shown.
- [ ] No repainting: realtime signals guarded by `barstate.isconfirmed`.
- [ ] `var` accumulators (counters, ring buffers, running sums) write only on `barstate.isconfirmed` — realtime ticks would otherwise corrupt per-bar history (§1d).
- [ ] No lookahead: HTF requests use `lookahead_off` unless display-only.
- [ ] Division guarded (`na` checks, `minval` on lengths).
- [ ] Strategy: commissions/slippage set; entries guarded by position size;
  `process_orders_on_close` considered.
- [ ] Alerts use `alert.freq_once_per_bar_close` where data-finality matters.
- [ ] No debug plots/labels left over; comments explain the *why*.
- [ ] If offline checking is available: `scripts/validate_pine.sh <file> --no-hints --no-information` reports 0 errors.
- [ ] Code is complete and copy-paste ready (no placeholders).
