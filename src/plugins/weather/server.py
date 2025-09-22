from __future__ import annotations

from typing import Any
from pathlib import Path

from agent_system.mcp.base import MCPServer
from . import sources


class WeatherServer(MCPServer):
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        status = params.get("_status")

        # Publish status for operation start
        location = params.get("location", "unknown")
        await status.progress(f"Fetching weather for {location}")

        supported_actions = ["forecast", "search", "query", "get", "check", "lookup"]
        if tool not in supported_actions:
            await status.error(f"Unknown tool: {tool}")
            return {"status": "error", "error": f"Unknown tool: {tool}. Supported tools: {', '.join(supported_actions)}"}

        location = params.get("location", "")
        if not location:
            await status.error("Missing location parameter")
            return {"status": "error", "error": "Missing required parameter: location"}

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

    def get_schema(self) -> dict[str, Any]:
        from agent_system.plugins.schema_loader import load_schema_from_dir
        schema = load_schema_from_dir(Path(__file__).parent, template_vars={"name": self.name})
        if not schema:
            raise RuntimeError("Missing required schema.yaml for weather plugin")
        return schema

    def get_default_action(self) -> str:
        return "forecast"
