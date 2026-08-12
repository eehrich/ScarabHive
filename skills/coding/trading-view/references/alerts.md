# Alerts in Pine Script v6

Alerts notify the user when a condition fires, with a custom message. There
are two mechanisms: `alertcondition` (simple, UI-created) and `alert()`
(flexible, message templates, works in strategies).

## Contents
1. `alertcondition` — the simple path
2. `alert()` — the flexible path
3. Message templates
4. Alert frequency & realtime behavior
5. Strategies & alerts
6. Repainting-aware alerts
7. Examples

## 1. `alertcondition` — the simple path

```pine
alertcondition(condition, title, message)
```

Declares a condition the user can create an alert for via the **Alerts**
dialog (it does not fire anything by itself). Example:

```pine
rsi = ta.rsi(close, 14)
alertcondition(ta.crossover(rsi, 70), "RSI overbought",
               "RSI crossed above 70")
```

- The user creates the alert in the UI and picks the condition by title.
- The default message is shown when the condition turns `true`.
- Limited to simple conditions (no custom templating at creation).

## 2. `alert()` — the flexible path

```pine
alert(message, freq)
```

Fires a realtime alert immediately when executed. This is the modern
mechanism — it can run inside `if` blocks and build dynamic messages.

```pine
if ta.crossover(close, ta.sma(close, 20)) and barstate.isconfirmed
    alert("MA20 cross up on " + syminfo.ticker, alert.freq_once_per_bar)
```

- Runs **only on realtime bars** — never on historical data, so there are
  no backtest alerts (use `alertcondition` + strategy fill alerts for that).
- `freq` controls how often it can fire (see §4).
- Build the message with `{}` interpolation or concatenation.

## 3. Message templates

In strategies, use `alert_message` parameters on order functions to build
rich alert text (the `{{...}}` placeholders are filled by TradingView):

```pine
strategy.entry("Long", strategy.long, alert_message =
    "LONG {{ticker}} @ {{close}} (strategy: {{strategy.order.id}})")
```

Useful placeholders:

| Placeholder | Meaning |
|---|---|
| `{{ticker}}` | Symbol (e.g. "NASDAQ:AAPL"). |
| `{{close}}`, `{{open}}`, `{{high}}`, `{{low}}`, `{{volume}}` | Bar values at alert time. |
| `{{interval}}` | Chart timeframe. |
| `{{strategy.order.action}}` | `buy` / `sell` / `long` / `short`. |
| `{{strategy.order.id}}` | Order id. |
| `{{strategy.order.contracts}}` | Order quantity. |
| `{{strategy.order.price}}` | Fill price. |
| `{{strategy.order.comment}}` | Order comment. |

`plot` and `plotshape` also accept `alert_message` — the message fires when
the plot value/condition changes on realtime bars.

## 4. Alert frequency & realtime behavior

| Frequency | Meaning |
|---|---|
| `alert.freq_once_per_bar` | Fires once per realtime bar. |
| `alert.freq_once_per_bar_close` | Fires only when the bar closes. |
| `alert.freq_once_per_bar_close` is also the *default* for strategies. |
| `alert.freq_all` | Fires on every tick the condition is true. |
| `alert.freq_on_close` | Deprecated in v6 — use `alert.freq_once_per_bar_close`. |

- Use `alert.freq_once_per_bar_close` when the message depends on the
  bar's final values (avoids repainting alerts).
- `alert.freq_all` is for tick-level monitoring (volume spikes, price
  levels) — expect a lot of notifications.

## 5. Strategies & alerts

- Strategies can't use `alert()` to fire *historical* alerts, but every
  order fill generates an alert with the `alert_message` you attach to the
  order function.
- By default, strategy alerts fire on bar close (`freq_once_per_bar_close`).
  With `calc_on_every_tick = true`, alerts follow the `freq` you pass.
- Combine an `alertcondition` (visual/historical condition) with
  `strategy.*` `alert_message` (fill notifications) for full coverage.

## 6. Repainting-aware alerts

An alert fires when the condition is `true` — if the condition can flip
during a realtime bar, users get alerts that later look wrong.

- Guard conditions with `barstate.isconfirmed` for confirmed signals:

```pine
if ta.crossover(fastMA, slowMA) and barstate.isconfirmed
    alert("Confirmed cross up", alert.freq_once_per_bar_close)
```

- For HTF signals, require the HTF bar to be closed (see multi-timeframe.md
  §3): `timeframe.change(tfIn) or barstate.islast`.
- Use `alert.freq_once_per_bar_close` so the message uses final bar data.

## 7. Examples

**RSI alertcondition + dynamic alert:**

```pine
//@version=6
indicator("RSI alerts", precision = 2)

lenInput = input.int(14, "Length", minval = 1)
rsi      = ta.rsi(close, lenInput)

// Simple UI-driven alert
alertcondition(ta.crossover(rsi, 70), "RSI > 70", "RSI overbought")
alertcondition(ta.crossunder(rsi, 30), "RSI < 30", "RSI oversold")

// Dynamic, non-repainting alert with a readable message
if ta.crossover(rsi, 70) and barstate.isconfirmed
    alert("{syminfo.ticker} RSI {str.tostring(rsi, '#.#')} crossed above 70",
          alert.freq_once_per_bar_close)
```

**Strategy with fill alerts:**

```pine
strategy.entry("Long", strategy.long, alert_message =
    "{{strategy.order.action}} {{ticker}} qty {{strategy.order.contracts}} @ {{strategy.order.price}}")
strategy.exit("XL", from_entry = "Long", stop = sl, alert_message =
    "EXIT {{strategy.order.action}} {{ticker}} @ {{strategy.order.price}}")
```
