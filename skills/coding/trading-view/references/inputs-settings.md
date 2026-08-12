# Inputs & Settings in Pine Script v6

Inputs turn hardcoded values into user-tunable settings in the
"Settings/Inputs" tab. Every parameter a user might reasonably adjust should
be an input.

## Contents
1. Input functions
2. Common parameters
3. Groups & tooltips
4. Special input types (source, session, symbol, timeframe)
5. Inputs in expressions
6. Good practices

## 1. Input functions

| Function | Returns | Typical use |
|---|---|---|
| `input(defval, title, ...)` | as `defval` | Generic; infers type from `defval`. |
| `input.int(defval, title, ...)` | `input int` | Lengths, levels, counts. |
| `input.float(defval, title, ...)` | `input float` | Multipliers, percentages. |
| `input.bool(defval, title, ...)` | `input bool` | Toggles. |
| `input.string(defval, title, options, ...)` | `input string` | Choice lists / text. |
| `input.color(defval, title, ...)` | `input color` | Color picker. |
| `input.source(defval, title, ...)` | `input series` | Pick a source (close/high/... or another plot). |
| `input.session(defval, title, ...)` | `input session` | Session (e.g. "0930-1600"). |
| `input.symbol(defval, title, ...)` | `input string` | Symbol selector, often for `request.security`. |
| `input.timeframe(defval, title, ...)` | `input string` | Timeframe selector for HTF requests. |

Examples:

```pine
lenInput    = input.int(14, "Length", minval = 1)
multInput   = input.float(2.0, "Multiplier", minval = 0.1, step = 0.1)
showPlotIn  = input.bool(true, "Show plot")
maTypeIn    = input.string("EMA", "MA type", options = ["SMA", "EMA", "WMA"])
colIn       = input.color(color.blue, "Line color")
srcIn       = input.source(close, "Source")
tfIn        = input.timeframe("D", "HTF")
symIn       = input.symbol("BINANCE:BTCUSDT", "Symbol")
sessIn      = input.session("0930-1600", "Session")
```

## 2. Common parameters

| Param | Meaning |
|---|---|
| `defval` | Default value shown in Settings. |
| `title` | Label in the Settings dialog. |
| `options` | Restrict to a list (e.g. `options = [3, 5, 10, 20]`). |
| `minval` / `maxval` | Allowed range (for numeric inputs). |
| `step` | Increment for the up/down arrows (floats). |
| `tooltip` | Hover help text. |
| `group` | Group title for organizing the Inputs tab. |
| `inline` | Same inline group name → controls on one row. |
| `confirm` | `true` → require user confirmation when changing in Settings. |
| `display` | `display.none` hides the input (v6). |

## 3. Groups & tooltips

Group inputs so the Settings tab stays navigable:

```pine
grpMain = "Main"
grpVis  = "Visuals"

lenInput = input.int(14, "Length", group = grpMain, tooltip = "RSI period")
upCol    = input.color(color.green, "Bull color", group = grpVis)
```

Use `tooltip` for anything non-obvious — users appreciate it, and it costs
nothing.

## 4. Special input types

**`input.source`** — lets the user pick which series is analyzed, including
other scripts' plots:

```pine
srcIn = input.source(close, "Source")
rsi   = ta.rsi(srcIn, lenInput)
```

**`input.timeframe`** — a timeframe string; combine with `request.security`:

```pine
tfIn  = input.timeframe("D", "Higher timeframe")
htfCl = request.security(syminfo.tickerid, tfIn, close)
```

**`input.session`** — a trading session; combine with `time()` / `ta.change`:

```pine
sessIn   = input.session("0930-1600", "Trading session")
inSess   = not na(time(timeframe.period, sessIn))
```

**`input.symbol`** — pick another symbol (e.g. BTCUSDT on an ETHUSDT chart):

```pine
symIn    = input.symbol("BINANCE:BTCUSDT", "Compare symbol")
btcClose = request.security(symIn, timeframe.period, close)
```

## 5. Inputs in expressions

Inputs are compile-time constants per run — you can use them anywhere a
`simple` value is accepted, including inside `request.security` expressions
and plot parameters.

```pine
showRSI = input.bool(true, "Show RSI")
plot(showRSI ? rsi : na, "RSI")
```

## 6. Good practices

- Defaults should match the *canonical* values of the concept (RSI 14,
  MACD 12/26/9, Bollinger 20/2) unless the user asked otherwise.
- Add `minval` to lengths to avoid division-by-zero-style errors.
- `options` beats free text when a closed set makes sense.
- Don't over-input: only expose what the user can meaningfully change.
- v6 note: `input()` (generic) works, but explicit `input.int`/`input.float`
  produce clearer Settings dialogs.
