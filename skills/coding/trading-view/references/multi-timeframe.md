# Multi-Timeframe & Other Symbols in Pine Script v6

Pull in data from higher/lower timeframes or other symbols with
`request.*`. This is where **repainting** and **lookahead** bugs hide —
read §3 and §4 before writing any HTF code.

## Contents
1. `request.security()` — the basics
2. Other request functions
3. Repainting & `barstate.isconfirmed`
4. Lookahead (`barmerge.lookahead_on/off`)
5. Timeframe helpers
6. Common HTF patterns
7. Limitations

## 1. `request.security()` — the basics

```pine
request.security(symbol, timeframe, expression,
                 gaps, lookahead, ignore_invalid_symbol,
                 currency, calc_bars_count)
```

Fetch an expression evaluated on another symbol/timeframe, then merge it
onto the chart's bars:

```pine
// Daily close on any chart timeframe
dailyClose = request.security(syminfo.tickerid, "D", close)

// HTF RSI as a filter
htfRsi = request.security(syminfo.tickerid, "240", ta.rsi(close, 14))
```

Rules:
- The `expression` must be simple/const-friendly — no `var`, no `for`,
  no `plot` calls. Use `ta.*` and math only. (For complex logic, write a
  library function with `export` and call it from the request.)
- Symbol defaults to `syminfo.tickerid` (current chart symbol).
- Timeframe accepts a string ("D", "W", "240", "30") or `timeframe.period`.
- Results are **merged onto the chart timeframe**: the value of the last
  *closed* HTF bar repeats until the next HTF bar closes.

## 2. Other request functions

| Function | Purpose |
|---|---|
| `request.security(symbol, tf, expr, ...)` | Single value/tuple from another symbol+timeframe. |
| `request.security(symbol, tf, [expr1, expr2])` | Array request — multiple expressions in one call (returns array). |
| `request.securities(symbols, tf, expr)` (v6) | Request the same expression for an array of symbols — returns an array of results. |
| `request.symbol(symbol, tf)` (v6) | Full OHLCV context for another symbol (bar object access via `syminfo`-like fields). |
| `request.secure(symbol, tf, expr)` | `security` variant that guarantees no repainting for realtime bars (slower; checks bar close). |
| `request.currency_rate(...)` | FX conversion rate for currency adjustment. |

Tuple example — get several HTF values in one call:

```pine
[htfHigh, htfLow] = request.security(syminfo.tickerid, "D", [high, low])
```

## 3. Repainting & `barstate.isconfirmed`

The core problem: on a **realtime** chart bar, an HTF value from a *still
forming* HTF bar is provisional — it changes as that HTF bar evolves. A
signal built on it therefore "repaints" after the fact.

Rules to avoid it:
1. `request.security` returns the value of the **last closed** HTF bar in
   most cases, but `request.secure`/realtime paths can change during the
   current HTF bar.
2. For signals, only trust HTF data when the *HTF* bar is closed. The
   standard guard is `barstate.isconfirmed` **on the HTF timeframe**, which
   translates to: on the chart, when the chart bar is the *last bar of the
   HTF period*, i.e.:

```pine
htfIsClosed = timeframe.change(tfIn) or barstate.islast
```

3. Simpler and very common: build the signal on **confirmed chart data**
   only (`barstate.isconfirmed`), accepting one bar of delay on the HTF
   transition bar.
4. Use `request.secure(...)` when you want the security call itself to
   never repaint — it's the documented "no repaint" option, at the cost of
   speed.

## 4. Lookahead (`barmerge.lookahead_on/off`)

- `barmerge.lookahead_off` (default): the HTF value is assigned to the
  chart bar that *starts at or after* the HTF close. No future info.
- `barmerge.lookahead_on`: the HTF value is available on the chart bar
  **that closes at the same time** as the HTF bar — i.e. you see today's
  daily close on today's bar. **This leaks future information into the
  backtest** and produces unrealistic results. Never use it for strategies
  or signals that drive decisions; it is occasionally useful for
  *display-only* overlays (e.g. daily levels shown on intraday charts)
  where you consciously accept the bias.

```pine
// Display-only: daily high/low on intraday chart (lookahead accepted)
[dH, dL] = request.security(syminfo.tickerid, "D", [high, low],
                            lookahead = barmerge.lookahead_on)
```

If you don't understand lookahead, leave it at the default `off`.

## 5. Timeframe helpers

```pine
timeframe.period            // current chart TF as string ("D", "240", ...)
timeframe.multiplier        // numeric part (240 → 240, D → 1)
timeframe.isintraday        // true for intraday TFs
timeframe.in_seconds(tf)    // seconds in a TF ("D" → 86400)
timeframe.fromtimeframe(tf) // milliseconds of the TF start
timenow                     // current epoch ms

// True when the chart bar closes an HTF period (see §3)
htfBarClose = timeframe.change(tfIn)
```

`timeframe.change(tf)` is the workhorse: it is `true` on the chart bar
where a new HTF period begins — exactly the bar where a closed HTF value is
fresh.

## 6. Common HTF patterns

**HTF trend filter (only trade in HTF direction):**

```pine
tfIn    = input.timeframe("D", "HTF filter")
htfSma  = request.security(syminfo.tickerid, tfIn, ta.sma(close, 50))
bullish = close > htfSma
```

**HTF session range / pivot levels:**

```pine
// Daily high/low as dynamic SR on an intraday chart
[dH, dL] = request.security(syminfo.tickerid, "D", [high, low])
plot(dH, "Daily High", color = color.red)
plot(dL, "Daily Low",  color = color.green)
```

**Non-repainting signal with confirmed data:**

```pine
// Only act on closed daily bars
htfRsi = request.security(syminfo.tickerid, "D", ta.rsi(close, 14))
sig    = ta.crossover(htfRsi, 50) and barstate.isconfirmed
plotshape(sig, "HTF RSI cross", shape.triangleup, location.belowbar)
```

## 7. Limitations

- **Max 40 `request.*` calls** per script. Batch expressions into array
  requests to stay under the limit.
- Each call adds data to the chart — too many slow scripts down.
- Requested expressions can't use `var`, user-defined *stateful* functions,
  or chart-dependent values like `bar_index` (use `bar_index` on the
  requested symbol sparingly).
- `request.security` needs the requested symbol+TF to be available for the
  whole history — some instruments/timeframes have gaps (see `gaps` param).
- `gaps = barmerge.gaps_on` leaves `na` between HTF bars (no repetition);
  `gaps_off` (default) repeats the last value.
