from __future__ import annotations

from typing import Any, TYPE_CHECKING

from agent_system.mcp.schema_based import SchemaBasedMCPServer
from . import sources

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig


class WeatherServer(SchemaBasedMCPServer):
    """Weather plugin using modern MCPServer pattern."""
    
    def __init__(self, name: str, system_config: AgentSystemConfig, mcp_config: MCPConfig) -> None:
        """
        Modern constructor signature.
        
        Args:
            name: Plugin instance name
            system_config: System-wide configuration
            mcp_config: Plugin-specific configuration
        """
        super().__init__(name, system_config, mcp_config)
        
        # Extract SSL verification setting from system config if available
        self.ssl_verify = getattr(system_config, 'ssl_verify', True)
    
    async def get_weather(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        Get weather conditions and forecasts for any location.
        
        Tool method - automatically called by generic dispatcher.
        Method name matches tool name in schema.yaml.
        """
        status = params.get("_status")
        cancellation_token = params.get("_cancellation_token")

        location = params.get("location", "")
        if not location:
            error_msg = "Missing required parameter: location"
            await status.error(error_msg)
            return {"status": "error", "error": error_msg}

        # Check for cancellation before weather fetch
        if cancellation_token and cancellation_token.is_cancelled:
            return {"status": "error", "error": "Weather request cancelled by user", "cancelled": True}

        # Publish status for operation progress
        await status.progress(f"Fetching weather for {location}")

        source = params.get("source", "met.no").lower()
        days = min(int(params.get("days", 3)), 7)
        units = params.get("units", "metric").lower()
        include_marine = params.get("include_marine", False)
        summary_format = params.get("summary_format", "detailed").lower()

        if days > 3 and source == "wttr.in":
            source = "met.no"
        elif include_marine and source not in ["marine.weather.gov"]:
            source = "marine.weather.gov"

        try:
            if source == "wttr.in":
                result = await sources.fetch_wttr(location, days, units, self.ssl_verify)
            elif source == "weather.gov":
                result = await sources.fetch_weather_gov(location, days, units, self.ssl_verify)
            elif source == "met.no":
                result = await sources.fetch_met_no(location, days, units, self.ssl_verify)
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
            self.logger.error(error_msg, exc_info=True)
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
                    wind_unit = "km/h" if units == "metric" else "mph"
                    current_line += f", wind {wind} {wind_unit}"
                if weather_desc:
                    current_line += f" - {weather_desc}"
                lines.append(current_line)
        
        # Forecast summary
        if forecast and format_type != "hourly":
            lines.append("\nForecast:")
            for day in forecast[:3]:  # Limit to 3 days for brevity
                date = day.get("date", "")
                max_temp = day.get("max_temp")
                min_temp = day.get("min_temp")
                
                # Check for precipitation in hourly data
                hourly = day.get("hourly", [])
                precipitation = False
                max_precip = 0.0
                for hour in hourly:
                    precip_val = hour.get("precipitation", 0)
                    if precip_val and precip_val > 0:
                        precipitation = True
                        max_precip = max(max_precip, precip_val)
                
                if max_temp is not None and min_temp is not None:
                    day_line = f"  {date}: {min_temp}{temp_unit} to {max_temp}{temp_unit}"
                    if precipitation:
                        day_line += f", rain expected (up to {max_precip}mm)"
                    lines.append(day_line)
        
        return "\n".join(lines)
