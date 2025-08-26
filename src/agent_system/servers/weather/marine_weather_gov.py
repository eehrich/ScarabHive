from __future__ import annotations

from typing import Any
import datetime


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
