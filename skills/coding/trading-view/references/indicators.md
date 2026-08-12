# Building Indicators in Pine Script v6

Indicators analyze price/volume and display the result — they place **no
orders**. Use `strategy()` instead when the user wants backtesting.

## Contents
1. The `indicator()` declaration
2. Skeleton of a complete indicator
3. Inputs (quick overview)
4. `ta.*` built-in reference by category
5. Overlay vs. separate pane
6. Complete worked examples
7. Performance notes

## 1. The `indicator()` declaration

```pine
indicator(title, shorttitle, overlay, precision, format, timeframe,
          timeframe_gaps, max_bars_back, max_lines_count, ...)
```

Most-used parameters:

| Param | Meaning |
|---|---|
| `title` | Full name shown in the indicator list. |
| `shorttitle` | Short name on the chart. |
| `overlay` | `true` = draw on the price chart; `false` (default) = separate pane below. |
| `precision` | Decimals shown (e.g. `precision = 2`). |
| `format` | `format.price`, `format.percent`, `format.volume`. |
| `timeframe` | Default timeframe for `request.*` calls (e.g. `"D"`), used with `timeframe_gaps`. |

Overlay vs. pane is a **design decision**, not a technical one — see §5.

## 2. Skeleton of a complete indicator

Every indicator follows the same shape:

```pine
//@version=6
indicator("RSI with bands", shorttitle = "RSI+", overlay = false, precision = 2)

// --- Inputs (see inputs-settings.md) ---
rsiLengthInput = input.int(14, "RSI length", minval = 1)
srcInput       = input.source(close, "Source")
obLevelInput   = input.int(70, "Overbought", minval = 1)
osLevelInput   = input.int(30, "Oversold", minval = 1)

// --- Calculations ---
rsiValue = ta.rsi(srcInput, rsiLengthInput)

// --- Visuals (see plots-and-visuals.md) ---
hline(obLevelInput, "Overbought", color = color.new(color.red, 0))
hline(osLevelInput, "Oversold",  color = color.new(color.green, 0))
plot(rsiValue, "RSI", color = rsiValue > obLevelInput ? color.red : rsiValue < osLevelInput ? color.green : color.blue)
```

Note the order: declaration → inputs → calculations → visuals. Keep it.

## 3. Inputs (quick overview)

Full details in `inputs-settings.md`. The pattern:

```pine
fastInput = input.int(12, "Fast length")
maType    = input.string("EMA", "MA type", options = ["SMA", "EMA", "WMA"])
showPlot  = input.bool(true, "Show plot")
```

Everything a user might tune becomes an input — never hardcode.

## 4. `ta.*` built-in reference by category

**Moving averages / trend**

| Function | Returns |
|---|---|
| `ta.sma(src, len)` | Simple moving average |
| `ta.ema(src, len)` | Exponential moving average |
| `ta.wma(src, len)` | Weighted moving average |
| `ta.rma(src, len)` | Wilder's smoothing (used by RSI/ATR) |
| `ta.vwap(src)` | Volume-weighted average |
| `ta.alma(src, len, offset, sigma)` | Arnaud Legoux MA |
| `ta.hma(src, len)` | Hull MA |
| `ta.linreg(src, len, offset)` | Linear regression |
| `ta.supertrend(factor, atrLen)` | Supertrend (returns 2-tuple) |
| `ta.roc(src, len)`, `ta.mom(src, len)` | Rate of change / momentum |

**Oscillators**

| Function | Returns |
|---|---|
| `ta.rsi(src, len)` | RSI |
| `ta.macd(src, fast, slow, sig)` | 3-tuple `[macd, signal, hist]` |
| `ta.stoch(srcH, srcL, srcC, k, d)` | 2-tuple `[k, d]` |
| `ta.stochrsi(src, len, k, d)` | Stochastic RSI |
| `ta.cci(src, len)` | Commodity Channel Index |
| `ta.cmo(src, len)` | Chande Momentum Oscillator |
| `ta.wpr(len)` | Williams %R |
| `ta.bollinger(series, numDev, len)` | 3-tuple `[basis, upper, lower]` |
| `ta.atr(len)` | Average True Range |
| `ta.tr(true)` | True Range (with `false` = w/o gaps) |
| `ta.true_range(high, low, close)` | True Range |
| `ta.deviation(src, len)`, `ta.stdev(src, len)` | Deviation / std dev |
| `ta.variance(src, len)` | Variance |

**Crossovers & signals**

| Function | Meaning |
|---|---|
| `ta.crossover(a, b)` | `a` crosses **above** `b` |
| `ta.crossunder(a, b)` | `a` crosses **below** `b` |
| `ta.cross(a, b)` | `a` crosses `b` (either direction) |
| `ta.change(x)` | `x - x[1]` |
| `ta.cum(x)` | Running total |
| `ta.highest(src, len)`, `ta.highestbars(src, len)` | Highest value / bars since |
| `ta.lowest(src, len)`, `ta.lowestbars(src, len)` | Lowest value / bars since |
| `ta.pivothigh(src, lb, rb)` / `ta.pivotlow(...)` | Pivot detection (returns `na` unless pivot) |
| `ta.correlation(a, b, len)` | Pearson correlation |
| `ta.median(src, len)`, `ta.percentile_linear_interpolation(...)` | Statistics |
| `ta.accdist`, `ta.obv`, `ta.vwma` | Volume-based |

**Directional / other**

| Function | Meaning |
|---|---|
| `ta.dmi(diLen, adxSmoothing)` | 4-tuple `[diPlus, diMinus, adx, adxr]` |
| `ta.psar(start, inc, max)` | Parabolic SAR |
| `ta.cci` | (already listed) |
| `ta.rising(x, len)` / `ta.falling(x, len)` | Value rising/falling over `len` bars |

Pattern notes:
- `ta.crossover`/`ta.crossunder` return `true` **only on the bar** where the
  cross happens — ideal for `plotshape` and `alertcondition`.
- Many oscillators return tuples — destructure them:
  `[macdLine, signalLine, histLine] = ta.macd(close, 12, 26, 9)`.
- `ta.rsi` and friends are internally smoothed — results only become valid
  after warm-up bars; guard with `bar_index > len` or let the plot show `na`.

## 5. Overlay vs. separate pane

- **Overlay (`overlay = true`)**: price-derived values — MAs, bands
  (Bollinger), Supertrend, VWAP, pivots, support/resistance. Because they
  live on the price axis, users can trade off them directly.
- **Separate pane (`overlay = false`, default)**: oscillators and indicators
  on a different scale — RSI, MACD, Stoch, volume-derived, ATR. Their
  absolute values are not price-comparable.

When in doubt: oscillators go in a pane, MAs/bands on price. If a script
computes both, you can only pick one per script — put the primary on price
and mention the tradeoff.

## 6. Complete worked examples

**MACD (classic, with histogram)**

```pine
//@version=6
indicator("MACD", shorttitle = "MACD", precision = 4)

fastInput   = input.int(12, "Fast length", minval = 1)
slowInput   = input.int(26, "Slow length", minval = 1)
signalInput = input.int(9,  "Signal length", minval = 1)

[macdLine, signalLine, histLine] = ta.macd(close, fastInput, slowInput, signalInput)

plot(macdLine,   "MACD",   color = color.blue)
plot(signalLine, "Signal", color = color.orange)
plot(histLine,   "Histogram", style = plot.style_histogram,
     color = histLine >= 0 ? color.teal : color.maroon)
hline(0, "Zero", color = color.gray, linestyle = hline.style_dotted)
```

**Bollinger Bands with price crossing the upper band**

```pine
//@version=6
indicator("Bollinger Bands", shorttitle = "BB", overlay = true, precision = 2)

lengthInput  = input.int(20, "Length", minval = 2)
multInput    = input.float(2.0, "StdDev mult", minval = 0.5, step = 0.1)

[basis, upper, lower] = ta.bollinger(close, multInput, lengthInput)

p_upper = plot(upper, "Upper", color = color.new(color.red, 0))
p_lower = plot(lower, "Lower", color = color.new(color.green, 0))
p_basis = plot(basis, "Basis", color = color.blue, linewidth = 2)
fill(p_upper, p_lower, color = color.new(color.blue, 92))

// Signal: close crosses above the upper band
longCond  = ta.crossover(close, upper)
shortCond = ta.crossunder(close, lower)
plotshape(longCond,  "Breakout up",   shape.triangleup,   location.abovebar, color.green, size = size.small)
plotshape(shortCond, "Breakout down", shape.triangledown, location.belowbar, color.red,   size = size.small)
```

## 7. Performance notes

- Prefer built-in `ta.*` functions over hand-rolled loops — they are
  compiled and fast.
- `for` loops run every bar: keep iteration counts small, or guard with
  `barstate.isconfirmed`/`var` so you don't recompute on every realtime tick.
- Every `request.security()` costs resources (see multi-timeframe.md).
- `max_bars_back` on the declaration avoids "too many bars referenced" errors
  when you use `x[n]` with large `n` — or pass `lookahead`/`max_bars_back`
  to the specific call.
- Plot fewer series than you compute; hidden debug plots waste chart space.
- Test output: read `references/common-errors.md` for the debugging toolkit.
