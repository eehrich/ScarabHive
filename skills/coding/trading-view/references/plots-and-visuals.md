# Plots & Visuals in Pine Script v6

Everything that makes a script readable: lines, shapes, fills, background
colors, labels, tables and drawings. See `indicators.md` for when to use
which, and `language-basics.md` for the color namespace basics.

## Contents
1. `plot()` — the workhorse
2. `plotshape` / `plotchar` / `plotarrow`
3. `plotcandle` / `plotbar` / `fill`
4. `hline` and horizontal levels
5. `bgcolor` / `barcolor`
6. Colors
7. Labels
8. Tables (dashboards)
9. Drawings: `line`, `box`, `polyline`
10. Design rules of thumb

## 1. `plot()` — the workhorse

```pine
plot(series, title, color, linewidth, style, trackprice, histbase,
     offset, join, editable, display, precision, format)
```

Most-used parameters:

| Param | Meaning |
|---|---|
| `series` | The value to draw (float series). |
| `title` | Name in the legend/Data Window. |
| `color` | Line color — can be a series (changes per bar!). |
| `linewidth` | 1–4 (scale line weight higher for MAs). |
| `style` | `plot.style_line` (default), `style_linebr` (breaks at na), `style_circles`, `style_cross`, `style_area`, `style_columns`, `style_histogram`, `style_stepline`. |
| `trackprice` | `true` → show value on the right price scale. |
| `histbase` | Baseline for histogram/columns (e.g. 0). |
| `offset` | Shift the plot n bars left/right. |
| `join` | Connect across `na` gaps (for `style_linebr`). |

Series color pattern — color each bar by condition:

```pine
plot(close, "Close", color = close >= open ? color.green : color.red)
```

## 2. `plotshape` / `plotchar` / `plotarrow`

Signal markers. All accept a *condition* that is `true` only on signal bars:

```pine
plotshape(series, title, style, location, color, offset, text,
          textcolor, size, editable, show_last, display)

plotshape(longCond, "Buy", shape.triangleup, location.belowbar,
          color.green, size = size.small, text = "BUY")
```

- **Styles**: `shape.triangleup`, `shape.triangledown`, `shape.xcross`,
  `shape.circle`, `shape.square`, `shape.diamond`, `shape.labelup`,
  `shape.labeldown`, `shape.flag`, `shape.arrowup`, `shape.arrowdown`.
- **Locations**: `location.abovebar`, `location.belowbar`, `location.top`,
  `location.bottom`, `location.absolute`.
- `plotchar` draws a single character: `plotchar(cond, "Title", "▲", location.belowbar)`.
- `plotarrow` draws arrows sized by a signed value:

```pine
plotarrow(longCond ? 1 : shortCond ? -1 : 0, "Arrows", colorup = color.green, colordown = color.red)
```

## 3. `plotcandle` / `plotbar` / `fill`

- `plotcandle(open, high, low, close, ...)` — draw candles from series
  (useful for HTF candles on a lower-TF chart).
- `plotbar(open, high, low, close, ...)` — bars instead of candles.
- `fill(plot1, plot2, color, ...)` — fill between two `plot()` objects:

```pine
p1 = plot(ta.sma(close, 20), "MA20")
p2 = plot(ta.sma(close, 50), "MA50")
fill(p1, p2, color = color.new(color.blue, 85))
```

`fill` needs the plot *objects* returned by `plot()`, not the series.

## 4. `hline` and horizontal levels

```pine
hline(price, title, color, linestyle, linewidth, editable, display)
hline(0, "Zero", color = color.gray, linestyle = hline.style_dotted)
```

Styles: `hline.style_solid`, `style_dotted`, `style_dashed`. Only works in a
separate pane or with `plot` on the same scale — `hline` cannot be used
inside `fill`.

## 5. `bgcolor` / `barcolor`

```pine
bgcolor(condition ? color.new(color.green, 90) : na, title = "Background")
barcolor(condition ? color.orange : na, title = "Bar color")
```

- `bgcolor` paints the bar's background region — great for session/day
  shading and regime coloring.
- `barcolor` recolors candles/bar bodies.
- Pass `na` when the effect should be off — no color, no fill.

## 6. Colors

Named colors (`color.red`, `color.blue`, ...) plus:

```pine
color.new(baseColor, transp)     // transparency 0–100 (v6: second arg)
color.rgb(red, green, blue, transp)
color.from_gradient(value, bottomVal, topVal, bottomColor, topColor)
```

- Prefer `color.new(color.blue, 80)` for translucent fills (nice over chart).
- Series colors (per-bar) are the standard way to highlight conditions:
  `color = cond ? color.green : color.red`.
- `color.gradient`/`color.from_gradient` for heat-map-style plots.
- `color.t(base, transparency)` — the v6-friendly way to apply transparency
  to a series color.

## 7. Labels

Text annotations on the chart. Created with `label.new` (many objects) and
managed with `label.set_*` / `label.delete`:

```pine
label.new(bar_index, high, "Text", style = label.style_label_down,
          color = color.blue, textcolor = color.white,
          size = size.normal, xloc = xloc.bar_index, yloc = yloc.price)
```

- Styles: `label.style_label_up/down`, `label.style_label_left/right`,
  `label.style_circle`, `label.style_arrowup/down`, `label.style_upper_left`, ...
- Series text works: `label.new(bar_index, high, "RSI {str.tostring(rsi, '#.#')}")`.
- Label text can be multi-line with `\n`.
- To keep labels from accumulating forever, delete old ones:

```pine
var label lastLabel = na
if signal
    label.delete(lastLabel)                 // remove previous
    lastLabel := label.new(bar_index, high, "Signal")
```

- Default label limit ~50 per script — use `label.delete` in loops or
  keep counts low.

## 8. Tables (dashboards)

`table.new` + `table.cell` build info panels in the corners of the chart:

```pine
var table infoTbl = table.new(position.top_right, 2, 4, border_width = 1)
if barstate.islast
    table.cell(infoTbl, 0, 0, "RSI", text_color = color.white)
    table.cell(infoTbl, 1, 0, str.tostring(rsi, "#.##"), text_color = color.orange)
```

- `table.new(position, columns, rows, bgcolor, frame_color, frame_width, border_width)`.
- Positions: `position.top_left/right`, `position.bottom_left/right`,
  `position.middle_left/right/center`.
- `table.cell(table, col, row, text, text_color, text_size, bgcolor, tooltip)`.
- `table.merge_cells(table, startCol, startRow, endCol, endRow)` merges a
  block (v6) — useful for headers.
- Update only on `barstate.islast` (or `var`) so you don't rebuild it every
  bar/tick — build once, refresh text.

## 9. Drawings: `line`, `box`, `polyline`

For trend lines, zones, and custom shapes (like `label`, these persist until
deleted — manage lifetimes):

```pine
// Trend line through two pivots
line.new(x1, y1, x2, y2, color = color.blue, width = 2,
         style = line.style_solid, extend = extend.right)

// Demand zone
box.new(left, top, right, bottom, bgcolor = color.new(color.green, 85),
        border_color = color.green)

// Polyline (v6): sequence of points
points = array.new<chart.point>()
points.push(chart.point.new(bar_index[5], high[5]))
points.push(chart.point.new(bar_index[2], low[2]))
polyline.new(points, color = color.orange)
```

- `line.style_solid/dotted/dashed`, `extend.none/right/left/both`.
- `line.new`/`line.set_x1/x2` x-coordinates are **bar indices** (`series int`),
  y-coordinates are prices (`series float`). `bar_index + <float>` is a type
  error — cast with `int(math.round(...))`.
- Use `line.delete`, `box.delete`, `polyline.delete` (or `*[1]` history
  management) to avoid exceeding object limits.
- These are great for pivots, SR levels, session ranges, ATR channels.

## 10. Design rules of thumb

- **One clear message per pane.** Don't plot 6 overlapping series with no
  legend — the user can't read it.
- **Color means something.** Green/red for direction, gray for context,
  translucent fills so the price stays visible.
- **Signals > raw math.** `plotshape` markers are easier to read than a line
  you have to compare by eye.
- **Dashboards in corners, not on the chart.** `position.top_right` tables
  for stats; labels for point events.
- **Manage object lifetimes.** Labels/lines/boxes accumulate — delete old
  ones or you hit object limits on long charts.
