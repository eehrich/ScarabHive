from __future__ import annotations

import logging
from typing import Any, TYPE_CHECKING

from agent_system.tools.schema_based import SchemaBasedToolServer
from . import sources

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)


class WeatherServer(SchemaBasedToolServer):
    """Weather plugin using modern ToolServer pattern."""
    
    def __init__(self, name: str, system_config: AgentSystemConfig, server_config: ToolServerConfig) -> None:
        """
        Modern constructor signature.
        
        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            server_config: Plugin-specific configuration
        """
        super().__init__(name, system_config, server_config)
        
        # Extract SSL verification setting from system config if available
        self.ssl_verify = getattr(system_config, 'ssl_verify', True)
    
    async def forecast(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Get current weather for a location.
        
        Tool method - automatically called by generic dispatcher.
        Tool name: {{ name }}_forecast → Method: forecast (after stripping {{ name }}_ prefix)
        """
        status = params.get("_status")
        cancellation_token = params.get("_cancellation_token")

        location = params.get("location", "")
        if not location:
            error_msg = "Missing required parameter: location"
            await status.error(error_msg)
            return {"status": "error", "error": error_msg}

        raw_days = params.get("days")
        try:
            days = max(1, min(int(3 if raw_days is None else raw_days), 7))
        except (TypeError, ValueError, OverflowError):
            error_msg = f"days must be a whole number from 1 to 7, got {raw_days!r}"
            await status.error(error_msg)
            return {"status": "error", "error": error_msg}

        # Check for cancellation before weather fetch
        if cancellation_token and cancellation_token.is_cancelled:
            return {"status": "error", "error": "Weather request cancelled by user", "cancelled": True}

        # Publish status for operation progress
        await status.progress(f"Fetching weather for {location}")

        source = str(params.get("source") or "met.no").lower()
        units = str(params.get("units") or "metric").lower()
        # Only a real true: the string "false" is truthy.
        include_marine = params.get("include_marine") is True
        summary_format = str(params.get("summary_format") or "daily").lower()
        hourly = summary_format == "hourly"

        if days > 3 and source == "wttr.in":
            source = "met.no"
        # Not elif: include_marine must win over the wttr.in fallback above.
        if include_marine and source not in ["marine.weather.gov"]:
            source = "marine.weather.gov"

        try:
            if source == "wttr.in":
                result = await sources.fetch_wttr(location, days, units, self.ssl_verify, hourly)
            elif source == "weather.gov":
                result = await sources.fetch_weather_gov(location, days, units, self.ssl_verify)
            elif source == "met.no":
                result = await sources.fetch_met_no(location, days, units, self.ssl_verify, hourly)
            elif source == "marine.weather.gov":
                result = await sources.fetch_marine_weather_gov(location, days, units, self.ssl_verify, include_marine)
            else:
                error_msg = f"Unsupported weather source: {source}"
                await status.error(error_msg)
                return {"status": "error", "error": error_msg}

            if "error" not in result:
                result["status"] = "success"
                
                # Add human-readable summary for agent consumption
                # IMPORTANT: Place summary at the TOP of response for LLM visibility
                summary_text = self._create_summary(result, summary_format, units)
                
                # Restructure response to prioritize summary
                result = {
                    "summary": summary_text,
                    "status": "success",
                    **result  # Merge remaining fields (location, coordinates, current, forecast, etc.)
                }
                
                await status.end(f"Successfully fetched weather for {location}")
            else:
                result["status"] = "error"
                await status.error(f"Error fetching weather: {result.get('error')}")

            return result

        except Exception as e:
            error_msg = f"Exception during weather fetch: {str(e)}"
            logger.error(error_msg, exc_info=True)
            await status.error(error_msg)
            return {
                "status": "error",
                "error": str(e),
                "location": location,
                "source": source,
                "message": f"Failed to fetch weather data from {source}",
            }
    
    def _create_summary(self, result: dict[str, Any], format_type: str, units: str) -> str:
        """Create human-readable weather summary for agent consumption."""
        location = result.get("location", "Unknown location")
        current = result.get("current", {})
        forecast = result.get("forecast", [])
        # Label with the units the source answered in, not the requested ones:
        # only wttr.in honours the request.
        units = result.get("units", units)

        # Temperature unit
        temp_unit = "°C" if units == "metric" else "°F"
        
        # Build summary
        lines = [f"Weather for {location}:"]
        
        # Current conditions
        if current:
            temp = current.get("temperature")
            humidity = current.get("humidity")
            wind = current.get("wind_speed")
            weather_desc = current.get("weather_desc", "")
            
            if temp is not None:
                current_line = f"Currently: {temp}{temp_unit}"
                if humidity:
                    current_line += f", humidity {humidity}%"
                if wind:
                    if result.get("source") == "met.no":
                        wind_unit = "m/s"
                    else:
                        wind_unit = "km/h" if units == "metric" else "mph"
                    current_line += f", wind {wind} {wind_unit}"
                if weather_desc:
                    current_line += f" - {weather_desc}"
                lines.append(current_line)
        
        # Forecast summary
        if forecast:
            lines.append("\nForecast:")
            for day in forecast[:3]:  # Limit to 3 days for brevity
                date = day.get("date", "")
                max_temp = day.get("max_temp")
                min_temp = day.get("min_temp")
                if max_temp is None and min_temp is None:
                    # weather.gov: a day and/or a night period instead of min/max
                    high = (day.get("day") or {}).get("temperature")
                    low = (day.get("night") or {}).get("temperature")
                    max_temp = high if high is not None else low
                    min_temp = low if low is not None else high
                
                precipitation = day.get("precipitation")

                if max_temp is not None and min_temp is not None:
                    if max_temp == min_temp:
                        day_line = f"  {date}: {max_temp}{temp_unit}"
                    else:
                        day_line = f"  {date}: {min_temp}{temp_unit} to {max_temp}{temp_unit}"
                    if precipitation:
                        day_line += f", rain expected ({precipitation} mm)"
                    lines.append(day_line)
        
        return "\n".join(lines)
