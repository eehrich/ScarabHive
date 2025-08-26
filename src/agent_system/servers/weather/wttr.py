from __future__ import annotations

from typing import Any
from urllib.parse import quote


async def fetch_wttr(location: str, days: int, units: str, ssl_verify: bool) -> dict[str, Any]:
    """Fetch weather from wttr.in - extracted from WeatherServer._fetch_wttr."""
    try:
        import httpx
    except ImportError:
        raise RuntimeError("httpx package required for weather server")

    # wttr.in only provides 3 days maximum
    days = min(days, 3)

    unit_param = "M" if units == "metric" else "u" if units == "imperial" else "M"
    url = f"https://wttr.in/{quote(location)}?format=j1&{unit_param}"

    async with httpx.AsyncClient(verify=ssl_verify, timeout=30.0) as client:
        response = await client.get(url)
        response.raise_for_status()
        data = response.json()

        current = data.get("current_condition", [{}])[0]
        weather_data = data.get("weather", [])[:days]

        result = {
            "location": location,
            "source": "wttr.in",
            "units": "metric" if unit_param == "M" else "imperial",
            "current": {
                "temperature": current.get("temp_C" if unit_param == "M" else "temp_F"),
                "feels_like": current.get("FeelsLikeC" if unit_param == "M" else "FeelsLikeF"),
                "humidity": current.get("humidity"),
                "wind_speed": current.get("windspeedKmph" if unit_param == "M" else "windspeedMiles"),
                "wind_direction": current.get("winddir16Point"),
                "pressure": current.get("pressure"),
                "visibility": current.get("visibility"),
                "weather_desc": current.get("weatherDesc", [{}])[0].get("value"),
                "observation_time": current.get("observation_time"),
            },
            "forecast": [],
        }

        for day_data in weather_data:
            day_info = {
                "date": day_data.get("date"),
                "max_temp": day_data.get("maxtempC" if unit_param == "M" else "maxtempF"),
                "min_temp": day_data.get("mintempC" if unit_param == "M" else "mintempF"),
                "avg_temp": day_data.get("avgtempC" if unit_param == "M" else "avgtempF"),
                "wind_speed": day_data.get("maxwindspeedKmph" if unit_param == "M" else "maxwindspeedMiles"),
                "humidity": day_data.get("avghumidity"),
                "hours": [],
            }

            for hour_data in day_data.get("hourly", []):
                hour_info = {
                    "time": hour_data.get("time"),
                    "temperature": hour_data.get("tempC" if unit_param == "M" else "tempF"),
                    "feels_like": hour_data.get("FeelsLikeC" if unit_param == "M" else "FeelsLikeF"),
                    "wind_speed": hour_data.get("windspeedKmph" if unit_param == "M" else "windspeedMiles"),
                    "humidity": hour_data.get("humidity"),
                    "pressure": hour_data.get("pressure"),
                    "weather_desc": hour_data.get("weatherDesc", [{}])[0].get("value"),
                    "chance_of_rain": hour_data.get("chanceofrain"),
                }
                day_info["hours"].append(hour_info)

            result["forecast"].append(day_info)

        return result
