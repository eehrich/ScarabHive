from __future__ import annotations

from typing import Any
import re
import json
from urllib.parse import quote

from ...mcp.base import MCPServer


class WeatherServer(MCPServer):
    """Weather MCP Server that fetches weather data from multiple free sources.
    
    Supports various weather data sources without requiring API tokens:
    - wttr.in: Command-line weather service with JSON API
    - openweathermap.org: Free tier (requires registration but no paid API key)
    - weatherapi.com: Free tier
    - weather.gov: US National Weather Service (US locations only)
    - met.no: Norwegian Meteorological Institute (global, free API)
    """

    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        if tool != "forecast":
            raise ValueError(f"Unknown tool: {tool}")

        location = params.get("location", "")
        if not location:
            raise ValueError("Missing required parameter: location")
        
        source = params.get("source", "wttr.in").lower()
        days = min(int(params.get("days", 3)), 7)  # Limit to 7 days max
        units = params.get("units", "metric").lower()  # metric, imperial
        
        try:
            if source == "wttr.in":
                return await self._fetch_wttr(location, days, units)
            elif source == "weather.gov":
                return await self._fetch_weather_gov(location, days, units)
            elif source == "met.no":
                return await self._fetch_met_no(location, days, units)
            else:
                raise ValueError(f"Unsupported weather source: {source}")
        except Exception as e:
            return {
                "error": str(e),
                "location": location,
                "source": source,
                "message": f"Failed to fetch weather data from {source}"
            }

    async def _fetch_wttr(self, location: str, days: int, units: str) -> dict[str, Any]:
        """Fetch weather from wttr.in - excellent free service with JSON API."""
        try:
            import httpx
        except ImportError:
            raise RuntimeError("httpx package required for weather server")

        # wttr.in format: ?format=j1 for JSON, ?M for metric, ?u for imperial
        unit_param = "M" if units == "metric" else "u" if units == "imperial" else "M"
        url = f"https://wttr.in/{quote(location)}?format=j1&{unit_param}"
        
        async with httpx.AsyncClient(verify=self.ssl_verify, timeout=30.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            data = response.json()
            
            # Parse wttr.in response
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
                    "observation_time": current.get("observation_time")
                },
                "forecast": []
            }
            
            for day_data in weather_data:
                day_info = {
                    "date": day_data.get("date"),
                    "max_temp": day_data.get("maxtempC" if unit_param == "M" else "maxtempF"),
                    "min_temp": day_data.get("mintempC" if unit_param == "M" else "mintempF"),
                    "avg_temp": day_data.get("avgtempC" if unit_param == "M" else "avgtempF"),
                    "wind_speed": day_data.get("maxwindspeedKmph" if unit_param == "M" else "maxwindspeedMiles"),
                    "humidity": day_data.get("avghumidity"),
                    "hours": []
                }
                
                # Add hourly data (every 3 hours)
                for hour_data in day_data.get("hourly", []):
                    hour_info = {
                        "time": hour_data.get("time"),
                        "temperature": hour_data.get("tempC" if unit_param == "M" else "tempF"),
                        "feels_like": hour_data.get("FeelsLikeC" if unit_param == "M" else "FeelsLikeF"),
                        "wind_speed": hour_data.get("windspeedKmph" if unit_param == "M" else "windspeedMiles"),
                        "humidity": hour_data.get("humidity"),
                        "pressure": hour_data.get("pressure"),
                        "weather_desc": hour_data.get("weatherDesc", [{}])[0].get("value"),
                        "chance_of_rain": hour_data.get("chanceofrain")
                    }
                    day_info["hours"].append(hour_info)
                
                result["forecast"].append(day_info)
            
            return result

    async def _fetch_weather_gov(self, location: str, days: int, units: str) -> dict[str, Any]:
        """Fetch weather from weather.gov (US National Weather Service) - US locations only."""
        try:
            import httpx
        except ImportError:
            raise RuntimeError("httpx package required for weather server")

        # First, try to geocode the location to get coordinates
        # For US locations, we can use weather.gov's geocoding
        geocode_url = f"https://geocoding.geo.census.gov/geocoder/locations/onelineaddress"
        geocode_params = {
            "address": location,
            "benchmark": "2020",
            "format": "json"
        }
        
        async with httpx.AsyncClient(verify=self.ssl_verify, timeout=30.0) as client:
            # Get coordinates
            geocode_response = await client.get(geocode_url, params=geocode_params)
            geocode_response.raise_for_status()
            geocode_data = geocode_response.json()
            
            matches = geocode_data.get("result", {}).get("addressMatches", [])
            if not matches:
                raise ValueError(f"Could not find coordinates for US location: {location}")
            
            coordinates = matches[0]["coordinates"]
            lat, lon = coordinates["y"], coordinates["x"]
            
            # Get weather data from weather.gov
            weather_url = f"https://api.weather.gov/points/{lat},{lon}"
            weather_response = await client.get(weather_url)
            weather_response.raise_for_status()
            weather_data = weather_response.json()
            
            # Get forecast URL
            forecast_url = weather_data["properties"]["forecast"]
            forecast_response = await client.get(forecast_url)
            forecast_response.raise_for_status()
            forecast_data = forecast_response.json()
            
            periods = forecast_data["properties"]["periods"][:days * 2]  # Day and night periods
            
            result = {
                "location": location,
                "source": "weather.gov",
                "units": "imperial",  # weather.gov uses imperial
                "coordinates": {"lat": lat, "lon": lon},
                "forecast": []
            }
            
            # Group periods by day
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
                            "detailed_forecast": day_period["detailedForecast"]
                        }
                    }
                    
                    if night_period:
                        day_info["night"] = {
                            "temperature": night_period["temperature"],
                            "temperature_unit": night_period["temperatureUnit"],
                            "wind_speed": night_period["windSpeed"],
                            "wind_direction": night_period["windDirection"],
                            "weather_desc": night_period["shortForecast"],
                            "detailed_forecast": night_period["detailedForecast"]
                        }
                    
                    day_periods.append(day_info)
            
            result["forecast"] = day_periods
            return result

    async def _fetch_met_no(self, location: str, days: int, units: str) -> dict[str, Any]:
        """Fetch weather from met.no (Norwegian Meteorological Institute) - global coverage, free API."""
        try:
            import httpx
        except ImportError:
            raise RuntimeError("httpx package required for weather server")

        # For met.no, we need coordinates. Try to use a simple geocoding service
        # OpenStreetMap Nominatim is free and reliable
        geocode_url = "https://nominatim.openstreetmap.org/search"
        geocode_params = {
            "q": location,
            "format": "json",
            "limit": 1
        }
        
        headers = {
            "User-Agent": "AgentSystem-Weather/1.0 (github.com/agent-system)"  # Required by Nominatim
        }
        
        async with httpx.AsyncClient(verify=self.ssl_verify, timeout=30.0, headers=headers) as client:
            # Get coordinates
            geocode_response = await client.get(geocode_url, params=geocode_params)
            geocode_response.raise_for_status()
            geocode_data = geocode_response.json()
            
            if not geocode_data:
                raise ValueError(f"Could not find coordinates for location: {location}")
            
            lat = float(geocode_data[0]["lat"])
            lon = float(geocode_data[0]["lon"])
            
            # Get weather data from met.no
            weather_url = f"https://api.met.no/weatherapi/locationforecast/2.0/compact"
            weather_params = {"lat": lat, "lon": lon}
            
            weather_response = await client.get(weather_url, params=weather_params)
            weather_response.raise_for_status()
            weather_data = weather_response.json()
            
            timeseries = weather_data["properties"]["timeseries"]
            
            result = {
                "location": location,
                "source": "met.no",
                "units": "metric",  # met.no uses metric
                "coordinates": {"lat": lat, "lon": lon},
                "current": None,
                "forecast": []
            }
            
            # Current weather (first entry)
            if timeseries:
                current_data = timeseries[0]
                instant_data = current_data["data"]["instant"]["details"]
                result["current"] = {
                    "time": current_data["time"],
                    "temperature": instant_data.get("air_temperature"),
                    "humidity": instant_data.get("relative_humidity"),
                    "pressure": instant_data.get("air_pressure_at_sea_level"),
                    "wind_speed": instant_data.get("wind_speed"),
                    "wind_direction": instant_data.get("wind_from_direction")
                }
            
            # Forecast by day
            daily_forecasts = {}
            for entry in timeseries[:days * 8]:  # Roughly 8 entries per day (3-hour intervals)
                date = entry["time"][:10]
                data = entry["data"]
                
                if date not in daily_forecasts:
                    daily_forecasts[date] = {
                        "date": date,
                        "temperatures": [],
                        "entries": []
                    }
                
                instant = data["instant"]["details"]
                entry_data = {
                    "time": entry["time"],
                    "temperature": instant.get("air_temperature"),
                    "humidity": instant.get("relative_humidity"),
                    "pressure": instant.get("air_pressure_at_sea_level"),
                    "wind_speed": instant.get("wind_speed"),
                    "wind_direction": instant.get("wind_from_direction")
                }
                
                # Add precipitation and weather symbol if available
                next_6_hours = data.get("next_6_hours")
                if next_6_hours:
                    entry_data["precipitation"] = next_6_hours["details"].get("precipitation_amount")
                    entry_data["weather_symbol"] = next_6_hours["summary"].get("symbol_code")
                
                daily_forecasts[date]["entries"].append(entry_data)
                if instant.get("air_temperature") is not None:
                    daily_forecasts[date]["temperatures"].append(instant["air_temperature"])
            
            # Convert to final format
            for date, day_data in daily_forecasts.items():
                temps = day_data["temperatures"]
                if temps:
                    forecast_day = {
                        "date": date,
                        "max_temp": max(temps),
                        "min_temp": min(temps),
                        "avg_temp": sum(temps) / len(temps),
                        "hourly": day_data["entries"]
                    }
                    result["forecast"].append(forecast_day)
            
            return result

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI function schema for weather forecast."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "Get weather forecast and current conditions for any location worldwide. Supports multiple free weather data sources without requiring API tokens.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["forecast"], "description": "Use 'forecast' to get weather data"},
                        "location": {
                            "type": "string", 
                            "description": "Location name (city, address, coordinates). Examples: 'Berlin, Germany', 'New York, NY', 'Tokyo, Japan'"
                        },
                        "source": {
                            "type": "string", 
                            "enum": ["wttr.in", "weather.gov", "met.no"],
                            "default": "wttr.in",
                            "description": "Weather data source: 'wttr.in' (global, best), 'weather.gov' (US only), 'met.no' (global, detailed)"
                        },
                        "days": {
                            "type": "integer", 
                            "minimum": 1, 
                            "maximum": 7, 
                            "default": 3,
                            "description": "Number of forecast days (1-7)"
                        },
                        "units": {
                            "type": "string",
                            "enum": ["metric", "imperial"],
                            "default": "metric", 
                            "description": "Temperature units: 'metric' (Celsius) or 'imperial' (Fahrenheit)"
                        }
                    },
                    "required": ["location"],
                    "additionalProperties": True,
                },
            },
        }

    def get_default_action(self) -> str:
        """Return the default action for weather forecast."""
        return "forecast"
