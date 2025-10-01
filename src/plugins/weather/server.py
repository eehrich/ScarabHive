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
