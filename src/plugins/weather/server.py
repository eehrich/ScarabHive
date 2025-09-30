from __future__ import annotations

from typing import Any, TYPE_CHECKING

from agent_system.mcp.schema_based import SchemaBasedMCPServer
from . import sources

if TYPE_CHECKING:
    from agent_system.config.models import AgentConfig


class WeatherServer(SchemaBasedMCPServer):
    def __init__(self, name: str, config: AgentConfig, registry=None) -> None:
        # Ignore registry parameter - this is not an Agent plugin
        super().__init__(name, config)
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        status = params.get("_status")

        if tool != "get_weather":
            error_msg = f"Unknown tool: {tool}. Only 'get_weather' supported."
            await status.error(error_msg)
            return {"status": "error", "error": error_msg}

        location = params.get("location", "")
        if not location:
            error_msg = "Missing required parameter: location"
            await status.error(error_msg)
            return {"status": "error", "error": error_msg}

        # Check for cancellation before weather fetch
        cancellation_token = params.get("_cancellation_token")
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
                await status.error(f"Unsupported weather source: {source}")
                return {"status": "error", "error": f"Unsupported weather source: {source}"}

            if "error" not in result:
                result["status"] = "success"
                await status.end(f"Successfully fetched weather for {location}")
            else:
                result["status"] = "error"
                await status.error(f"Error fetching weather: {result.get('error')}")

            return result

        except Exception as e:
            await status.error(f"Exception during weather fetch: {str(e)}")
            return {
                "status": "error",
                "error": str(e),
                "location": location,
                "source": source,
                "message": f"Failed to fetch weather data from {source}",
            }

    def get_default_action(self) -> str:
        return "get_weather"
