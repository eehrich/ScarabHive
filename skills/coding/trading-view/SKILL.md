---
name: trading-view
description: 'TradingView Pine Script (v6) development: build, debug, convert and improve indicators and strategies. Use whenever the user wants to create a TradingView indicator or strategy, write or fix Pine Script code, turn a trading idea into a chart script, port an indicator from another platform (TradingView-style), convert Pine v5 code to v6, or add inputs/plots/alerts to an existing TradingView script — even if they never say "Pine Script" or "TradingView" explicitly (e.g. "build me an RSI oscillator", "I need a backtestable MA-cross strategy"). Also reach for it when the user asks about TradingView indicator logic, repainting, multi-timeframe requests, or strategy backtesting results.'
metadata:
  version: 1.0.0
  tags: tradingview, pine-script, indicators, strategies, trading, ta
---

# TradingView Pine Script Development

Build production-quality TradingView indicators and strategies in Pine Script v6.

## Ask first, then write

TradingView code is only as good as the spec. If the request does not fully
specify what the script should do, **ask** — or state your assumptions
explicitly at the top of the answer:

1. **Script type** — `indicator()` (display/analysis) or `strategy()` (backtest/orders)?
2. **Timeframe & symbol scope** — chart timeframe only, or multi-timeframe via `request.security()`?
3. **Inputs** — which parameters should the user be able to tune in Settings/Inputs?
4. **Visuals** — overlay on price or separate pane? Which plots, colors, labels, tables?
5. **Alerts** — `alertcondition`, `alert()`, or none?

Default when nothing is said: **indicator(), chart timeframe, inputs for the
main parameters, overlay where natural, no alerts** — and say so.

## Core rules

- **Pine Script v6** is the default (`//@version=6`). Convert old v5 code (see
  `references/common-errors.md`).
- **Never repaint.** On realtime (unclosed) bars, only emit signals on
  confirmed data — use `barstate.isconfirmed` or calculate on `close` — so
  the indicator does not change after the fact. Repainting is the #1
  complaint about TradingView scripts.
- **No lookahead bias.** Never pull future data into a decision. When
  requesting other timeframes, understand `barmerge.lookahead_on` before using
  it (default is `barmerge.lookahead_off`).
- **Make parameters inputs.** Anything the user might reasonably want to tune
  belongs in the Settings/Inputs tab via `input.*`.
- **Plot, don't just compute.** Every computed series the user cares about
  should be visible: `plot`, `plotshape`, `fill`, `bgcolor`, `barcolor`,
  `table`, or `label`.
- **One complete script per file.** Copy-paste ready, no placeholders, no
  pseudo-code, no `...`. The user pastes it into the Pine Editor and it
  compiles.

## Workflow

1. **Clarify the request** (see "Ask first"). If the user gave you a strategy
   idea, translate it into concrete rules: entries, exits, filters, position
   sizing, stop/take-profit.
2. **Pick the right reference doc** (below) and follow it.
3. **Write the complete script.** Comment the non-obvious parts (the *why*,
   not the *what*). Structure: declaration → inputs → calculations → visuals →
   alerts.
4. **Self-review** against the Core rules and the checklist in
   `references/common-errors.md`. Watch for repainting, division by zero,
   unused variables, missing `na` handling, and invalid plot arguments.
5. **Deliver** the code in a block the user can paste straight into the Pine
   Editor, plus a short "How to use" note (add to chart, what the inputs do,
   anything they must configure).

## Reference docs

Read the one(s) you need — don't read them all:

- `references/language-basics.md` — Pine Script v6 syntax, execution model, types, variables (`var`/`varip`), control flow, functions, namespaces.
- `references/indicators.md` — building indicators: declaration, inputs, `ta.*` built-ins, plotting, overlay vs. pane, complete examples.
- `references/strategies.md` — building strategies: orders, pyramiding, position sizing, backtesting realism, exits.
- `references/inputs-settings.md` — all `input.*` functions, groups, sessions, symbol/timeframe inputs.
- `references/plots-and-visuals.md` — `plot*`, `fill`, `bgcolor`, `barcolor`, colors, labels, tables, line/box/polyline drawings.
- `references/multi-timeframe.md` — `request.security()` and friends, repainting, lookahead, HTF patterns.
- `references/alerts.md` — `alertcondition`, `alert()`, message templates, alert frequencies.
- `references/common-errors.md` — frequent compiler errors, debugging techniques, v5→v6 migration.
- `references/cheatsheet.md` — quick lookup table of the most used functions.
