from __future__ import annotations

from typing import Any

from agent_system.mcp.base import MCPServer
from .sources import fetch_wttr, fetch_weather_gov, fetch_marine_weather_gov, fetch_met_no


class WeatherServer(MCPServer):
    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        supported_actions = ["forecast", "search", "query", "get", "check", "lookup"]
        if tool not in supported_actions:
            return {"status": "error", "error": f"Unknown tool: {tool}. Supported tools: {', '.join(supported_actions)}"}

        location = params.get("location", "")
        if not location:
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
                result = await fetch_wttr(location, days, units, self.ssl_verify)
            elif source == "weather.gov":
                result = await fetch_weather_gov(location, days, units, self.ssl_verify)
            elif source == "met.no":
                result = await fetch_met_no(location, days, units, self.ssl_verify)
            elif source == "marine.weather.gov":
                result = await fetch_marine_weather_gov(location, days, units, self.ssl_verify, include_marine)
            else:
                return {"status": "error", "error": f"Unsupported weather source: {source}"}

            if "error" not in result:
                result["status"] = "success"
            else:
                result["status"] = "error"

            return result

        except Exception as e:
            return {
                "status": "error",
                "error": str(e),
                "location": location,
                "source": source,
                "message": f"Failed to fetch weather data from {source}",
            }

    def get_schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "Get weather forecast and current conditions for any location worldwide.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": ["forecast", "search", "query", "get", "check", "lookup"]},
                        "location": {"type": "string"},
                        "source": {"type": "string", "enum": ["wttr.in", "weather.gov", "met.no", "marine.weather.gov"], "default": "met.no"},
                        "days": {"type": "integer", "minimum": 1, "maximum": 7, "default": 3},
                        "units": {"type": "string", "enum": ["metric", "imperial"], "default": "metric"},
                        "include_marine": {"type": "boolean", "default": False},
                        "summary_format": {"type": "string", "enum": ["detailed", "daily_summary", "hourly"], "default": "detailed"},
                        "include_radiation": {"type": "boolean", "default": False},
                    },
                    "required": ["location"],
                    "additionalProperties": True,
                },
            },
        }

    def get_default_action(self) -> str:
        return "forecast"
