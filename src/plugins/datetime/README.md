# Date and Time

A clock and a calendar for agents: the current time in any timezone, date arithmetic, timezone conversion, Unix
timestamps, weekdays, calendar facts, business days and days until a date. The time reaches the model as a tool
answer when it asks, never frozen into the prompt, so the prompt cache stays intact. No state, no panel.

- **Tool** `datetime_operations` -- one tool, the `operation` parameter picks `current`, `format`, `parse`,
  `add_time`, `subtract_time`, `convert_timezone`, `to_timestamp`, `calendar_info`, `business_days`, `day_of_week`
  or `days_until`.

Enable it in `config/plugins.yaml` (`datetime: {type: datetime, enabled: true}`) and allow `+datetime/*` in an
agent's tool list.

The full manual -- every operation, parameter and answer field, how dates are read, what `timezone` does per
operation, the error texts and the known gaps -- is the plugin's guide, `datetime.guide`, in the Help panel.
