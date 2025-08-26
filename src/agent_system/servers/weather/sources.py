from __future__ import annotations

from typing import Any
import json
import datetime
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


async def fetch_marine_weather_gov(location: str, days: int, units: str, ssl_verify: bool, include_marine: bool = True) -> dict[str, Any]:
    """Fetch marine weather from NOAA - extracted from WeatherServer._fetch_marine_weather_gov."""
    try:
        import httpx
    except ImportError:
        raise RuntimeError("httpx package required for weather server")

    geocode_url = "https://nominatim.openstreetmap.org/search"
    geocode_params = {"q": location, "format": "json", "limit": 1}

    async with httpx.AsyncClient(verify=ssl_verify, timeout=30.0) as client:
        geocode_response = await client.get(geocode_url, params=geocode_params)
        geocode_response.raise_for_status()
        geocode_data = geocode_response.json()

        if not geocode_data:
            raise ValueError(f"Could not find coordinates for location: {location}")

        lat = float(geocode_data[0]["lat"])
        lon = float(geocode_data[0]["lon"])

        result: dict[str, Any] = {
            "location": location,
            "source": "marine.weather.gov",
            "units": units,
            "coordinates": {"lat": lat, "lon": lon},
            "current": {},
            "forecast": [],
            "marine_data": {
                "sea_surface_temperatures": [],
                "wave_heights": [],
                "wind_waves": [],
            }
            if include_marine
            else None,
        }

        try:
            weather_url = f"https://api.weather.gov/points/{lat},{lon}"
            weather_response = await client.get(weather_url)
            if weather_response.status_code == 200:
                weather_data = weather_response.json()
                forecast_url = weather_data["properties"]["forecast"]
                forecast_response = await client.get(forecast_url)
                if forecast_response.status_code == 200:
                    forecast_data = forecast_response.json()
                    periods = forecast_data["properties"]["periods"][: days * 2]

                    for i in range(0, len(periods), 2):
                        day_period = periods[i] if i < len(periods) else None
                        if day_period:
                            day_info = {
                                "date": day_period["startTime"][:10],
                                "max_temp": day_period["temperature"],
                                "min_temp": periods[i + 1]["temperature"] if i + 1 < len(periods) else day_period["temperature"],
                                "avg_temp": (
                                    day_period["temperature"]
                                    + (periods[i + 1]["temperature"] if i + 1 < len(periods) else day_period["temperature"])
                                )
                                / 2,
                                "wind_speed": day_period["windSpeed"],
                                "wind_direction": day_period["windDirection"],
                                "weather_desc": day_period["shortForecast"],
                                "detailed_forecast": day_period["detailedForecast"],
                            }
                            result["forecast"].append(day_info)
        except Exception as e:
            result["atmospheric_data_error"] = f"NOAA weather API failed: {str(e)}"

        if include_marine:
            try:
                # Simplified, estimated SST generation for demonstration
                current_date = datetime.date.today()
                for day in range(days):
                    forecast_date = current_date + datetime.timedelta(days=day)
                    seasonal_adjustment = 2 * (datetime.datetime.now().month - 6) / 6
                    latitude_adjustment = (90 - abs(lat)) / 3
                    estimated_sst = 15 + latitude_adjustment + seasonal_adjustment

                    result["marine_data"]["sea_surface_temperatures"].append(
                        {
                            "date": forecast_date.isoformat(),
                            "temperature": round(estimated_sst, 1),
                            "units": "Celsius",
                            "source": "estimated",
                            "note": "Estimated SST - upgrade to real OISST/Copernicus API for production",
                        }
                    )

                    estimated_wave_height = max(0.5, min(4.0, abs(lat) / 20 + 0.5))
                    result["marine_data"]["wave_heights"].append(
                        {
                            "date": forecast_date.isoformat(),
                            "significant_wave_height": round(estimated_wave_height, 1),
                            "units": "meters",
                            "source": "estimated",
                        }
                    )
            except Exception as e:
                result["marine_data"]["error"] = f"Marine data fetch failed: {str(e)}"

        return result


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
