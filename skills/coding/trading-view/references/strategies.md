# Building Strategies in Pine Script v6

Strategies run **backtests** and place **simulated orders**. Use
`strategy()` when the user wants to test a trading idea on historical data
(and optionally alert on fills). Pure analysis → `indicator()`.

## Contents
1. The `strategy()` declaration
2. Order functions
3. Exits & position management
4. Position sizing & capital
5. Backtesting realism
6. Realtime behavior & repainting
7. Complete worked example
8. Performance report — what the numbers mean

## 1. The `strategy()` declaration

```pine
strategy(title, shorttitle, overlay, initial_capital, currency,
         default_qty_type, default_qty_value, commission_type,
         commission_value, slippage, pyramiding, calc_on_every_tick,
         calc_on_order_fills, process_orders_on_close, ...)
```

Most-used parameters:

| Param | Default | Meaning |
|---|---|---|
| `overlay` | `true` | Strategies typically draw on price. |
| `initial_capital` | 100000 | Starting capital for the backtest. |
| `currency` | `currency.NONE` | e.g. `currency.USD`. |
| `default_qty_type` | `strategy.fixed` | `strategy.fixed`, `strategy.cash`, `strategy.percent_of_equity`. |
| `default_qty_value` | 1 | Quantity when using `strategy.fixed`. |
| `commission_type` | `strategy.commission.cash_per_order` | Also `commission.percent`. |
| `commission_value` | 0 | e.g. 0.05 for 0.05% per side. |
| `slippage` | 0 | Ticks added to market fills. |
| `pyramiding` | 0 | Max number of concurrent entries in one direction (0 = no pyramiding). |
| `calc_on_every_tick` | `false` | Evaluate on every realtime tick instead of bar close. |
| `calc_on_order_fills` | `false` | Recalculate after an intrabar fill (uses `varip`). |
| `process_orders_on_close` | `false` | Process order signals on bar close (avoids intrabar repainting). |
| `max_bars_back` | ... | History depth for calculations. |

Keep the declaration readable — one line with only the params you actually
use.

## 2. Order functions

| Function | Purpose |
|---|---|
| `strategy.entry(id, direction, qty, limit, stop, oco, comment, alert_message)` | Open or add to a position. `direction = strategy.long` / `strategy.short`. |
| `strategy.exit(id, from_entry, qty, limit, stop, trail_price, trail_offset, oco, comment, alert_message)` | Exit with stop/limit/trailing — the **only** way to place stops/TPS. |
| `strategy.close(id, qty, comment, alert_message)` | Close a specific entry. |
| `strategy.close_all(comment, alert_message)` | Close everything. |
| `strategy.order(id, direction, qty, ...)` | Raw order (reverse/flip allowed). |
| `strategy.cancel(id)` / `strategy.cancel_all()` | Cancel pending orders. |
| `strategy.risk.max_position_size(qty)` (v6) | Risk-management helpers. |
| `strategy.risk.allow_entry_in(direction)` | Allow entries only in one direction. |

Key points:
- `strategy.entry` does **not** guarantee execution — it places a market
  order (or a limit/stop if you pass those).
- An `id` identifies an entry for later management. `strategy.exit` with
  `from_entry` ties an exit to a specific entry.
- Calling `strategy.entry` again with the same `id` **replaces** the order,
  it doesn't add a second one.
- For "exit the position when X": check the condition and call
  `strategy.close_all()` (or `strategy.exit` with `stop`/`limit`).

## 3. Exits & position management

**Stop + take-profit via `strategy.exit`** (recommended — fills at the stop/TP
price, not intrabar):

```pine
strategy.exit("XL", from_entry = "Long", stop = longSL, limit = longTP)
```

- `stop` — protective stop (below entry for longs). Market-on-touch.
- `limit` — take-profit (above entry for longs). Limit order.
- `trail_price`/`trail_offset` — trailing stop: stop is initially
  `trail_price`, then trails the price by `trail_offset`.
- `oco` — cancel the sibling order when one fills (OCO group).
- `qty` — partial exit: exit only part of the position.

**Timed / rule-based exits** — just call `strategy.close_all()` when the exit
condition is true (e.g. a cross, session end, N bars held).

Position state is available via built-ins:
`strategy.position_size` (positive long / negative short), 
`strategy.position_avg_price`, `strategy.openprofit`, `strategy.equity`,
`strategy.netprofit`, `strategy.wintrades`, `strategy.losstrades`.

Guard every entry/exit with a position check:

```pine
notInPosition = strategy.position_size == 0
if longCond and notInPosition
    strategy.entry("Long", strategy.long)
```

## 4. Position sizing & capital

- `default_qty_type = strategy.percent_of_equity` with
  `default_qty_value = 10` → each entry risks/uses 10% of current equity.
- `strategy.cash` → fixed cash amount per order.
- For risk-based sizing (e.g. risk 1% of equity per trade), compute qty
  from `strategy.equity` and the stop distance:

```pine
riskEquity = strategy.equity * 0.01
stopDist   = close - longSL                 // price distance to stop
qty        = riskEquity / stopDist
```

Then pass `qty = qty` to `strategy.entry`/`strategy.exit`.

## 5. Backtesting realism

Defaults are **optimistic**. For believable results:

1. **Commissions** — set `commission_type = strategy.commission.percent`,
   `commission_value = 0.05` (or cash-per-order for futures).
2. **Slippage** — `slippage = 1` (one tick) is a reasonable minimum for
   market orders.
3. **`process_orders_on_close = true`** — signals computed from the bar's
   close are filled at the *next* bar's open. Without it, a close-based
   signal is filled intrabar at a price you couldn't actually have gotten.
4. Beware **repainting entries**: if the entry condition uses the realtime
   bar before it closes, live results diverge from the backtest (see §6).
5. **Survivorship / universe effects** are on the user's data, not the
   strategy — mention them only if relevant.

## 6. Realtime behavior & repainting

- Strategies by default execute **once per bar close** in realtime.
- `calc_on_every_tick = true` evaluates on every tick; combined with
  `process_orders_on_close = false` this can fill orders *before* the bar
  closes — a classic source of repainting.
- If you want live behavior to match the backtest: leave the defaults
  (close-only) or use `calc_on_every_tick = true` +
  `process_orders_on_close = true`.
- `alert()` calls in strategies fire with `alert.freq_once_per_bar_close`
  unless `calc_on_every_tick = true` (see alerts.md).

## 7. Complete worked example

**MA cross with ATR-based stop and take-profit**

```pine
//@version=6
strategy("MA Cross + ATR exit", overlay = true, initial_capital = 10000,
         default_qty_type = strategy.percent_of_equity, default_qty_value = 100,
         commission_type = strategy.commission.percent, commission_value = 0.05,
         slippage = 1, process_orders_on_close = true)

fastInput = input.int(20, "Fast MA", minval = 1)
slowInput = input.int(50, "Slow MA", minval = 1)
atrLen    = input.int(14, "ATR length", minval = 1)
atrMult   = input.float(2.0, "ATR mult", minval = 0.1, step = 0.1)

fastMA = ta.sma(close, fastInput)
slowMA = ta.sma(close, slowInput)
atr    = ta.atr(atrLen)

plot(fastMA, "Fast MA", color = color.blue)
plot(slowMA, "Slow MA", color = color.orange)

longCond  = ta.crossover(fastMA, slowMA)
closeCond = ta.crossunder(fastMA, slowMA)

if longCond and strategy.position_size == 0
    strategy.entry("Long", strategy.long)

// Protective stop and take-profit, both anchored to the current bar
stopLevel = strategy.position_avg_price - atrMult * atr
tpLevel   = strategy.position_avg_price + atrMult * atr
if strategy.position_size > 0
    strategy.exit("XL", from_entry = "Long", stop = stopLevel, limit = tpLevel)

if closeCond and strategy.position_size > 0
    strategy.close_all(comment = "MA cross down")
```

Notes on this pattern:
- Entries only when flat → no accidental averaging.
- The exit is re-anchored every bar via `strategy.exit` (same id replaces
  the old order) — this implements a *trailing* ATR stop.
- `process_orders_on_close = true` keeps the backtest honest.

## 8. Performance report — what the numbers mean

factor (gross profit / gross
loss), max drawdown (peak-to-trough equity drop), win rate, average trade,
Sharpe/Sortino, exposure, and the **equity curve** (chart). Point out the
ones the user asked about; if they didn't ask, keep it short.

Warnings to surface automatically:
- **High max drawdown** relative to net profit → strategy fragility.
- **Profit factor < 1** → losing strategy, don't sugarcoat it.
- **Very few trades** (< ~30) → results are statistically weak.
- **Perfect win rate with huge returns** → almost certainly repainting or
  lookahead; check with §5/§6 and common-errors.md.
- Results differ wildly between symbol/timeframe → likely overfitting; say so.
