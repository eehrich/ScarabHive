# Pine Script v6 — Language Basics

The foundation every TradingView script builds on. Read this first if you are
new to Pine, otherwise skim for reminders.

## Contents
1. Version directive & script types
2. Execution model
3. Built-in variables
4. Types & qualifiers
5. Operators
6. Control flow
7. Functions
8. Namespaces
9. String interpolation
10. Comments

## 1. Version directive & script types

Every script starts with a version annotation, then exactly **one** declaration:

```pine
//@version=6
indicator("My Indicator", overlay = true)   // or: strategy("My Strategy")
```

- `indicator()` — analysis/display scripts (no orders).
- `strategy()` — backtesting/order scripts.
- A file may also contain **library** code: `//@version=6` + `library("name")`,
  plus `export`ed functions. Libraries are imported with `import`.

Never mix `indicator` and `strategy` in one file.

## 2. Execution model

Pine executes your script **once per bar**, from the oldest to the newest
bar in the dataset, in the order the statements appear. On the realtime
(unclosed) bar, the script re-runs on every price tick, so variables that
"remember" state across ticks must be declared `var` (see below).

Key consequences:
- Code placed early in the script can use values computed later only via
  lookahead helpers — which you should avoid (see multi-timeframe.md).
- `close`, `high`, etc. change on every tick of the realtime bar.
- Use `barstate.isconfirmed` to act only when a bar is final (no repaint).

## 3. Built-in variables

| Group | Examples |
|---|---|
| OHLCV | `open`, `high`, `low`, `close`, `volume`, `hl2`, `hlc3`, `ohlc4`, `typical` (alias `hlc3`), `vwap` |
| Bar | `bar_index`, `barstate.isconfirmed`, `barstate.isnew`, `barstate.islast`, `barstate.isfirst`, `barstate.isrealtime` |
| Timeframe | `timeframe.period` (string — **v6 always includes the multiplier**: `"1D"`, `"1W"`, `"1M"`, `"240"`), `timeframe.multiplier`, `timeframe.isintraday`/`isdaily`/`isweekly`/`ismonthly`, `timeframe.in_seconds()` |
| Symbol | `syminfo.tickerid`, `syminfo.ticker`, `syminfo.prefix`, `syminfo.currency`, `syminfo.mintick`, `syminfo.pointvalue` |
| Time | `time`, `timenow`, `timestamp()`, `year()`, `month()`, `dayofmonth()`, `hour()`, `minute()`, `second()` |
| Strategy | `strategy.position_size`, `strategy.position_avg_price`, `strategy.openprofit`, `strategy.equity` |
| Chart | `chart.left_visible_bar_time`, `chart.right_visible_bar_time` |

## 4. Types & qualifiers

Pine's fundamental types: `int`, `float`, `bool`, `color`, `string`, plus the
container/object types `array`, `matrix`, `map`, `line`, `label`, `box`,
`polyline`, `table`, `chart.point`. Enums (e.g. `barmerge.lookahead_on`) are
a distinct type.

The **qualifier** of a variable controls what it can do:

| Qualifier | Meaning |
|---|---|
| (none) | A `series` value — recalculated on every bar. Default for results of calculations. |
| `const` | Known at compile time, e.g. `const int x = 2`. |
| `input` | Set in Settings/Inputs (`input.*` functions). Constant during execution. |
| `simple` | Fixed after the first bar, e.g. from `input()` in some contexts. |
| `var` | Initialized once, then **remembers** its last value across bars. Use for state, accumulators, counters. |
| `varip` | Like `var`, but updates **intrabar** on realtime ticks. For intrabar state (order management, tick counters). |

Examples:

```pine
var float runningHigh = high        // set on first bar, then persists
var int   crossCount  = 0           // counter
if ta.crossover(close, ta.sma(close, 20))
    crossCount += 1
```

`varip` only makes sense in realtime/intrabar contexts (e.g. counting ticks
since last fill). On historical bars it behaves like `var`.

## 5. Operators

```pine
+ - * / %            // arithmetic; note: / ALWAYS returns float in v6
== != < > <= >=      // comparison; strict bool results (no `na == x` shortcuts)
and or not           // logical
? :                  // ternary: condition ? ifTrue : ifFalse
:=                   // assignment (re-assignment) to an existing variable
=                    // declaration (only at first use)
+= -= *= /= %=       // compound assignment
```

Gotchas:
- In v6, `5 / 2 == 2.5` (float), not 2. Integer division returning int was
  a v5 behavior.
- Booleans are strict: you cannot use `na` where a bool is expected. Check
  `na(x)` explicitly, and a condition like `close > open` is fine, but
  `close == na` is a type error.
- `timeframe.period` always includes the multiplier in v6 (`"D"` -> `"1D"`).
  Never compare it to `"D"`/`"W"`/`"M"` — use `timeframe.isdaily`/`isweekly`/
  `ismonthly` or the `"1D"` strings.
- Bools are never `na` in v6: a comparison that involves `na` evaluates to
  `false`, and the `[]` history of a bool on early bars is `false`. Test for
  missing data with `na(x)`, never `x == na`.
- Arrays/matrices/maps support negative indexing in v6 (`arr[-1]` = last
  element).

## 6. Control flow

```pine
// if / else if / else
if condition
    // do this
else if otherCondition
    // do that
else
    // fallback

// for loop (fixed bounds)
for i = 1 to 10
    sum += i

// while loop (v6, dynamic condition)
var int i = 0
while i < 100 and someCondition
    i += 1

// switch (v6) — matches a value
switch timeframe.period
    "D"    => dailyLogic
    "W"    => weeklyLogic
    => fallbackLogic
```

Notes:
- `if` without `else` returns `na` for the unset branch.
- Local scopes (inside `if`/`for`/function bodies) cannot be referenced
  outside. Declare variables with `var`/at top level first, then assign
  inside.
- `for` loops are limited (max 5000 ms / ~10M operations) — heavy loops on
  every bar are a performance problem.
- In v6 a `for` loop re-evaluates its `to_num` bound before **each** iteration.
  If the bound expression mutates state (e.g. `array.pop(arr) - 1`), the loop
  can run away — snapshot the bound into a variable outside the loop first.

## 7. Functions

User-defined functions are declared with the `f_` prefix (reserved namespace):

```pine
f_ema(source, length) =>
    alpha = 2.0 / (length + 1)
    sum  = 0.0
    sum  := na(sum[1]) ? source : alpha * source + (1 - alpha) * sum[1]
```

- The `=>` body returns the **last expression** (or a tuple).
- Functions return tuples like the built-ins: `[a, b] = f_something(x)`.
- Function parameters are local — use `var` only at script level.

## 8. Namespaces

Pine groups built-ins into namespaces — always write the full path:

| Namespace | Used for |
|---|---|
| `ta.*` | Technical analysis: `ta.sma`, `ta.ema`, `ta.rsi`, `ta.macd`, `ta.crossover`, ... (see indicators.md) |
| `math.*` | `math.abs`, `math.max`, `math.min`, `math.sqrt`, `math.log`, `math.sum`, `math.round`, `math.ceil`, `math.floor`, `math.random`, `math.pi` |
| `string.*` | `str.tostring`, `str.format`, `str.contains`, `str.substring`, `str.replace` |
| `array.*` | `array.new_float`, `array.push`, `array.get`, `array.size`, `array.sort`, `array.reverse`, `array.sum`, `array.stdev` |
| `matrix.*` / `map.*` | 2-D matrices / key-value maps (v6 adds map methods like `.put`, `.get`) |
| `request.*` | `request.security`, `request.symbol`, `request.securities`, `request.secure` (see multi-timeframe.md) |
| `strategy.*` | Order management (see strategies.md) |
| `input.*` | Settings inputs (see inputs-settings.md) |
| `plot*`, `fill`, `bgcolor`, `barcolor`, `label.*`, `table.*`, `line.*`, `box.*`, `polyline.*` | Visuals (see plots-and-visuals.md) |
| `alertcondition`, `alert` | Alerts (see alerts.md) |
| `color.*`, `format.*`, `barmerge.*`, `timeframe.*`, `syminfo.*`, `chart.*`, `session.*` | Helpers |

## 9. String interpolation

v6 uses `{}` for interpolation inside double-quoted strings:

```pine
plot(ta.rsi(close, 14), title = "RSI")
label.new(bar_index, high, "RSI = {str.tostring(ta.rsi(close, 14), '#.##')}")
```

Old v5 `+` concatenation still works; `{}` is preferred for readability.

## 10. Comments

```pine
// line comment
/* block
   comment */
```

Comment the *why* of non-obvious logic — the code says what, comments say
why. Keep them short and factual.
