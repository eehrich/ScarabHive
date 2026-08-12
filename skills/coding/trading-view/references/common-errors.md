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

Quick migration path: bump `//@version=6`, then fix the strict-bool and
int-division errors the compiler flags. Test the result against the v5
output on the same chart before trusting it.

## 7. Delivery checklist

Before handing code to the user, verify:

- [ ] `//@version=6` and exactly one `indicator()`/`strategy()` declaration.
- [ ] All user-tunable params are `input.*` (with `minval` where sensible).
- [ ] Every calculated series the user cares about is plotted or shown.
- [ ] No repainting: realtime signals guarded by `barstate.isconfirmed`.
- [ ] No lookahead: HTF requests use `lookahead_off` unless display-only.
- [ ] Division guarded (`na` checks, `minval` on lengths).
- [ ] Strategy: commissions/slippage set; entries guarded by position size;
  `process_orders_on_close` considered.
- [ ] Alerts use `alert.freq_once_per_bar_close` where data-finality matters.
- [ ] No debug plots/labels left over; comments explain the *why*.
- [ ] Code is complete and copy-paste ready (no placeholders).
