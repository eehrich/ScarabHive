from __future__ import annotations

from typing import Any


async def fetch_weather_gov(location: str, days: int, units: str, ssl_verify: bool) -> dict[str, Any]:
    """Fetch weather from weather.gov - extracted from WeatherServer._fetch_weather_gov."""
    try:
        import httpx
    except ImportError:
        raise RuntimeError("httpx package required for weather server")

    geocode_url = f"https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
    geocode_params = {"address": location, "benchmark": "2020", "format": "json"}

    async with httpx.AsyncClient(verify=ssl_verify, timeout=30.0) as client:
        geocode_response = await client.get(geocode_url, params=geocode_params)
        geocode_response.raise_for_status()
        geocode_data = geocode_response.json()

        matches = geocode_data.get("result", {}).get("addressMatches", [])
        if not matches:
            raise ValueError(f"Could not find coordinates for US location: {location}")

        coordinates = matches[0]["coordinates"]
        lat, lon = coordinates["y"], coordinates["x"]

        weather_url = f"https://api.weather.gov/points/{lat},{lon}"
        weather_response = await client.get(weather_url)
        weather_response.raise_for_status()
        weather_data = weather_response.json()

        forecast_url = weather_data["properties"]["forecast"]
        forecast_response = await client.get(forecast_url)
        forecast_response.raise_for_status()
        forecast_data = forecast_response.json()

        periods = forecast_data["properties"]["periods"][: days * 2]

        result = {
            "location": location,
            "source": "weather.gov",
            "units": "imperial",
            "coordinates": {"lat": lat, "lon": lon},
            "forecast": [],
        }

        day_periods = []
        for i in range(0, len(periods), 2):
            day_period = periods[i] if i < len(periods) else None
            night_period = periods[i + 1] if i + 1 < len(periods) else None

            if day_period:
                day_info = {
                    "date": day_period["startTime"][:10],
                    "day": {
                        "temperature": day_period["temperature"],
                        "temperature_unit": day_period["temperatureUnit"],
                        "wind_speed": day_period["windSpeed"],
                        "wind_direction": day_period["windDirection"],
                        "weather_desc": day_period["shortForecast"],
                        "detailed_forecast": day_period["detailedForecast"],
                    },
                }

                if night_period:
                    day_info["night"] = {
                        "temperature": night_period["temperature"],
                        "temperature_unit": night_period["temperatureUnit"],
                        "wind_speed": night_period["windSpeed"],
                        "wind_direction": night_period["windDirection"],
                        "weather_desc": night_period["shortForecast"],
                        "detailed_forecast": night_period["detailedForecast"],
                    }

                day_periods.append(day_info)

        result["forecast"] = day_periods
        return result
