# Pine Script v6 — Quick Reference

Fast lookup of the most-used functions and constants. Details and examples
live in the other reference docs.

## Declaration

```pine
//@version=6
indicator("Title", shorttitle = "T", overlay = false, precision = 2)
strategy("Title", overlay = true, initial_capital = 10000,
         default_qty_type = strategy.percent_of_equity, default_qty_value = 10,
         commission_type = strategy.commission.percent, commission_value = 0.05,
         slippage = 1, process_orders_on_close = true)
```

## Inputs

| Call | Type |
|---|---|
| `input(defval, "Title")` | generic (infers type) |
| `input.int(14, "Len", minval = 1, step = 1)` | int |
| `input.float(2.0, "Mult", minval = 0.1, step = 0.1)` | float |
| `input.bool(true, "Show")` | bool |
| `input.string("EMA", "Type", options = ["SMA", "EMA"])` | string choice |
| `input.color(color.blue, "Color")` | color |
| `input.source(close, "Source")` | series source |
| `input.session("0930-1600", "Session")` | session |
| `input.symbol("BINANCE:BTCUSDT", "Symbol")` | symbol |
| `input.timeframe("D", "HTF")` | timeframe string |

Common params: `group`, `tooltip`, `minval`, `maxval`, `step`, `options`,
`inline`, `display`.

## Technical analysis (`ta.*`)

```pine
ta.sma(src, len)  ta.ema(src, len)  ta.wma(src, len)  ta.rma(src, len)
ta.vwap(src)      ta.alma(src, len, offset, sigma)   ta.hma(src, len)
ta.linreg(src, len, offset)         ta.supertrend(factor, atrLen)
ta.rsi(src, len)                    ta.macd(src, f, s, sig)  // [macd, signal, hist]
ta.stoch(h, l, c, k, d)             ta.stochrsi(src, len, k, d)
ta.cci(src, len)   ta.cmo(src, len) ta.wpr(len)
ta.bollinger(src, numDev, len)      // [basis, upper, lower]
ta.atr(len)        ta.tr(handleGaps) ta.true_range(h, l, c)
ta.deviation(src, len)  ta.stdev(src, len)  ta.variance(src, len)
ta.crossover(a, b)  ta.crossunder(a, b)  ta.cross(a, b)
ta.change(x)  ta.cum(x)  ta.rising(x, len)  ta.falling(x, len)
ta.highest(src, len)  ta.highestbars(src, len)
ta.lowest(src, len)   ta.lowestbars(src, len)
ta.pivothigh(src, lb, rb)  ta.pivotlow(src, lb, rb)
ta.correlation(a, b, len)  ta.median(src, len)
ta.accdist  ta.obv  ta.vwma  ta.dmi(diLen, adxSm)  ta.psar(start, inc, max)
```

## Requests (multi-timeframe / other symbols)

```pine
request.security(symbol, tf, expr, gaps, lookahead, ...)
request.security(syminfo.tickerid, "D", close)
request.security(syminfo.tickerid, "D", [high, low])      // tuple
request.security(syminfo.tickerid, "D", [high, low, close]) // array request
request.securities(symbolsArray, tf, expr)   // v6: many symbols
request.symbol(symbol, tf)                   // v6: full OHLCV context
request.secure(symbol, tf, expr)             // no-repaint variant
```

## Strategy orders

```pine
strategy.entry("Long", strategy.long, qty, limit, stop)
strategy.exit("XL", from_entry = "Long", stop = sl, limit = tp,
              trail_price = p, trail_offset = o)
strategy.close("Long")  strategy.close_all()
strategy.order(id, direction, qty)  strategy.cancel(id)  strategy.cancel_all()
strategy.risk.max_position_size(qty)  strategy.risk.allow_entry_in(dir)
strategy.position_size  strategy.position_avg_price  strategy.openprofit
strategy.equity  strategy.netprofit  strategy.wintrades  strategy.losstrades
```

## Plots & visuals

```pine
plot(series, "Title", color, linewidth, style, histbase, trackprice)
plotshape(cond, "Title", shape.triangleup, location.belowbar, color, size)
plotchar(cond, "Title", "▲", location.belowbar)
plotarrow(up ? 1 : down ? -1 : 0, "Arrows", colorup, colordown)
plotcandle(o, h, l, c)  plotbar(o, h, l, c)
p1 = plot(a); p2 = plot(b); fill(p1, p2, color = color.new(color.blue, 85))
hline(0, "Zero", color = color.gray, linestyle = hline.style_dotted)
bgcolor(cond ? color.new(color.green, 90) : na)
barcolor(cond ? color.orange : na)
label.new(bar_index, high, "Text", style = label.style_label_down)
table.new(position.top_right, 2, 4)  table.cell(t, col, row, "Text")
line.new(x1, y1, x2, y2, color, width, extend = extend.right)
box.new(left, top, right, bottom, bgcolor, border_color)
polyline.new(points, color)
```

Plot styles: `plot.style_line`, `style_linebr`, `style_circles`,
`style_cross`, `style_area`, `style_columns`, `style_histogram`,
`style_stepline`. Shape styles: `shape.triangleup/down`, `shape.xcross`,
`shape.circle`, `shape.square`, `shape.diamond`, `shape.labelup/down`,
`shape.flag`, `shape.arrowup/down`. Colors: `color.new(c, transp)`,
`color.rgb(r, g, b, transp)`, `color.from_gradient(...)`, `color.t(c, t)`.

## Bar states & time

```pine
barstate.isconfirmed  barstate.isnew  barstate.islast  barstate.isfirst
barstate.isrealtime   bar_index   time   timenow
timeframe.period  // v6: multiplier always included - "1D"/"1W"/"1M"/"240" (not "D"/"W"/"M")
timeframe.multiplier  timeframe.isintraday  timeframe.isdaily  timeframe.isweekly
timeframe.in_seconds(tf)  timeframe.change(tf)  timeframe.fromtimeframe(tf)
year() month() dayofmonth() hour() minute() second()
```

## Alerts

```pine
alertcondition(cond, "Title", "Message")
alert("Message", alert.freq_once_per_bar_close)
// strategy fill alerts:
strategy.entry("Long", strategy.long, alert_message = "LONG {{ticker}} @ {{close}}")
```

Frequencies: `alert.freq_once_per_bar`, `alert.freq_once_per_bar_close`,
`alert.freq_all`. Message placeholders: `{{ticker}}`, `{{close}}`,
`{{interval}}`, `{{strategy.order.action}}`, `{{strategy.order.id}}`,
`{{strategy.order.contracts}}`, `{{strategy.order.price}}`,
`{{strategy.order.comment}}`.

## Useful built-in variables & helpers

```pine
open high low close volume hl2 hlc3 ohlc4 vwap
syminfo.tickerid syminfo.ticker syminfo.currency syminfo.mintick
math.abs math.max math.min math.sqrt math.log math.sum math.round
str.tostring(x, "#.##")  str.format("{}", x)  str.contains  str.substring
nz(x, fallback)  na(x)  runtime.error("msg")
int(x)  // float->int cast - for line.new x-coords and int-division results
array.new_float()  array.push  array.get  array.size  array.sort
var  varip  const  input  simple
```

## Pre-flight checklist (tl;dr)

1. `//@version=6` + one declaration.
2. Everything tunable is an `input.*`.
3. Signals on confirmed data only — no repaint, no lookahead.
4. Strategies: commissions + slippage + `process_orders_on_close`.
5. Plot everything the user cares about; no debug leftovers.
6. Complete, copy-paste-ready code with *why* comments.
