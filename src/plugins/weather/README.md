# Weather

Current weather and a forecast of up to seven days for a place, from free public services: met.no and wttr.in
worldwide, the US National Weather Service for places in the US. No key, no account. The answer starts with a
short text summary; the full data follows.

- **Tool** `weather_forecast` -- one tool with `location`, `source`, `days`, `units`, `include_marine` and
  `summary_format`. `include_marine` adds estimated (not measured) sea data.
- No hooks, no panel.

Enable it in `config/plugins.yaml` (`weather: {type: weather, enabled: true}`) and allow `+weather/*` in an agent's
tool list.

The full manual -- every parameter and answer field, what each source accepts and returns, the errors and the
size of the answers -- is the plugin's guide, `weather.guide`, in the Help panel.
