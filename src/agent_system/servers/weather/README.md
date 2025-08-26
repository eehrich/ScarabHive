Weather MCP Server helpers

This module contains the extracted source-specific helper functions used by the `WeatherServer`:

- `fetch_wttr` - fetches and parses wttr.in JSON
- `fetch_weather_gov` - fetches weather data via weather.gov (US)
- `fetch_marine_weather_gov` - fetches marine and atmospheric data (NOAA)
- `fetch_met_no` - fetches met.no (Norwegian Meteorological Institute) data

Notes:
- These helpers use `httpx` and expect to be called from async code.
- They raise `RuntimeError` if `httpx` is not installed.
- Tests mock `httpx.AsyncClient` to avoid network calls.
