from __future__ import annotations

from typing import Any


async def fetch_met_no(location: str, days: int, units: str, ssl_verify: bool) -> dict[str, Any]:
    """Fetch weather from met.no - extracted from WeatherServer._fetch_met_no."""
    try:
        import httpx
    except ImportError:
        raise RuntimeError("httpx package required for weather server")

    geocode_url = "https://nominatim.openstreetmap.org/search"
    geocode_params = {"q": location, "format": "json", "limit": 1}

    headers = {"User-Agent": "AgentSystem-Weather/1.0 (github.com/agent-system)"}

    async with httpx.AsyncClient(verify=ssl_verify, timeout=30.0, headers=headers) as client:
        geocode_response = await client.get(geocode_url, params=geocode_params)
        geocode_response.raise_for_status()
        geocode_data = geocode_response.json()

        if not geocode_data:
            raise ValueError(f"Could not find coordinates for location: {location}")

        lat = float(geocode_data[0]["lat"])
        lon = float(geocode_data[0]["lon"])

        weather_url = f"https://api.met.no/weatherapi/locationforecast/2.0/compact"
        weather_params = {"lat": lat, "lon": lon}

        weather_response = await client.get(weather_url, params=weather_params)
        weather_response.raise_for_status()
        weather_data = weather_response.json()

        timeseries = weather_data["properties"]["timeseries"]

        result = {
            "location": location,
            "source": "met.no",
            "units": "metric",
            "coordinates": {"lat": lat, "lon": lon},
            "current": None,
            "forecast": [],
        }

        if timeseries:
            current_data = timeseries[0]
            instant_data = current_data["data"]["instant"]["details"]
            result["current"] = {
                "time": current_data["time"],
                "temperature": instant_data.get("air_temperature"),
                "humidity": instant_data.get("relative_humidity"),
                "pressure": instant_data.get("air_pressure_at_sea_level"),
                "wind_speed": instant_data.get("wind_speed"),
                "wind_direction": instant_data.get("wind_from_direction"),
            }

        daily_forecasts: dict[str, Any] = {}
        for entry in timeseries[: days * 8]:
            date = entry["time"][:10]
            data = entry["data"]

            if date not in daily_forecasts:
                daily_forecasts[date] = {"date": date, "temperatures": [], "entries": []}

            instant = data["instant"]["details"]
            entry_data = {
                "time": entry["time"],
                "temperature": instant.get("air_temperature"),
                "humidity": instant.get("relative_humidity"),
                "pressure": instant.get("air_pressure_at_sea_level"),
                "wind_speed": instant.get("wind_speed"),
                "wind_direction": instant.get("wind_from_direction"),
            }

            next_6_hours = data.get("next_6_hours")
            if next_6_hours:
                entry_data["precipitation"] = next_6_hours["details"].get("precipitation_amount")
                entry_data["weather_symbol"] = next_6_hours["summary"].get("symbol_code")

            daily_forecasts[date]["entries"].append(entry_data)
            if instant.get("air_temperature") is not None:
                daily_forecasts[date]["temperatures"].append(instant["air_temperature"])

        for date, day_data in daily_forecasts.items():
            temps = day_data["temperatures"]
            if temps:
                forecast_day = {
                    "date": date,
                    "max_temp": max(temps),
                    "min_temp": min(temps),
                    "avg_temp": sum(temps) / len(temps),
                    "hourly": day_data["entries"],
                }
                result["forecast"].append(forecast_day)

        return result
