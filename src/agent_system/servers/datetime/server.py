from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any
import pytz
import calendar

from ...mcp.base import MCPServer


class DateTimeServer(MCPServer):
    """DateTime MCP Server that provides comprehensive date and time information.
    
    Supports:
    - Current date and time in various formats and timezones
    - Date calculations (add/subtract days, weeks, months, years)
    - Timezone conversions
    - Calendar information (weekday, month names, etc.)
    - Date formatting and parsing
    - Unix timestamp conversions
    - Business day calculations
    """
    
    def __init__(self, name: str = "datetime", config: dict[str, Any] = None, ssl_verify: bool = True):
        super().__init__(name, config, ssl_verify)

    async def call(self, tool: str, params: dict[str, Any]) -> Any:
        """Execute datetime operations."""
        
        if tool == "current":
            return await self._get_current_datetime(params)
        elif tool == "format":
            return await self._format_datetime(params)
        elif tool == "parse":
            return await self._parse_datetime(params)
        elif tool == "add":
            return await self._add_time(params)
        elif tool == "subtract":
            return await self._subtract_time(params)
        elif tool == "convert_timezone":
            return await self._convert_timezone(params)
        elif tool == "timestamp":
            return await self._unix_timestamp(params)
        elif tool == "calendar_info":
            return await self._calendar_info(params)
        elif tool == "business_days":
            return await self._business_days(params)
        else:
            raise ValueError(f"Unknown tool: {tool}")

    async def _get_current_datetime(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get current date and time information."""
        timezone_str = params.get("timezone", "UTC")
        format_str = params.get("format", "iso")
        
        try:
            if timezone_str.upper() == "UTC":
                tz = timezone.utc
            elif timezone_str.upper() == "LOCAL":
                tz = None  # Local timezone
            else:
                tz = pytz.timezone(timezone_str)
            
            if tz:
                now = datetime.now(tz)
            else:
                now = datetime.now()
            
            result = {
                "timestamp": now.isoformat(),
                "timezone": str(now.tzinfo) if now.tzinfo else "Local",
                "year": now.year,
                "month": now.month,
                "day": now.day,
                "hour": now.hour,
                "minute": now.minute,
                "second": now.second,
                "weekday": now.weekday(),  # 0=Monday, 6=Sunday
                "weekday_name": now.strftime("%A"),
                "month_name": now.strftime("%B"),
                "unix_timestamp": int(now.timestamp())
            }
            
            # Add formatted versions
            if format_str == "iso":
                result["formatted"] = now.isoformat()
            elif format_str == "human":
                result["formatted"] = now.strftime("%A, %B %d, %Y at %I:%M:%S %p")
            elif format_str == "date_only":
                result["formatted"] = now.strftime("%Y-%m-%d")
            elif format_str == "time_only":
                result["formatted"] = now.strftime("%H:%M:%S")
            elif format_str == "custom":
                custom_format = params.get("custom_format", "%Y-%m-%d %H:%M:%S")
                result["formatted"] = now.strftime(custom_format)
            else:
                result["formatted"] = now.strftime(format_str)
            
            return result
            
        except Exception as e:
            return {"error": str(e), "timezone_requested": timezone_str}

    async def _format_datetime(self, params: dict[str, Any]) -> dict[str, Any]:
        """Format a given datetime string."""
        datetime_str = params.get("datetime", "")
        format_str = params.get("format", "%Y-%m-%d %H:%M:%S")
        input_format = params.get("input_format", "auto")
        
        if not datetime_str:
            return {"error": "Missing required parameter: datetime"}
        
        try:
            # Parse input datetime
            if input_format == "auto":
                # Try common formats
                formats = [
                    "%Y-%m-%dT%H:%M:%S",
                    "%Y-%m-%dT%H:%M:%S.%f",
                    "%Y-%m-%d %H:%M:%S",
                    "%Y-%m-%d",
                    "%d.%m.%Y",
                    "%d/%m/%Y",
                    "%m/%d/%Y"
                ]
                dt = None
                for fmt in formats:
                    try:
                        dt = datetime.strptime(datetime_str, fmt)
                        break
                    except ValueError:
                        continue
                if dt is None:
                    raise ValueError(f"Could not parse datetime: {datetime_str}")
            else:
                dt = datetime.strptime(datetime_str, input_format)
            
            return {
                "original": datetime_str,
                "formatted": dt.strftime(format_str),
                "parsed_datetime": dt.isoformat(),
                "year": dt.year,
                "month": dt.month,
                "day": dt.day,
                "hour": dt.hour,
                "minute": dt.minute,
                "second": dt.second,
                "weekday": dt.weekday(),
                "weekday_name": dt.strftime("%A"),
                "month_name": dt.strftime("%B")
            }
            
        except Exception as e:
            return {"error": str(e), "input": datetime_str}

    async def _parse_datetime(self, params: dict[str, Any]) -> dict[str, Any]:
        """Parse datetime from string with detailed information."""
        datetime_str = params.get("datetime", "")
        
        if not datetime_str:
            return {"error": "Missing required parameter: datetime"}
        
        try:
            # Try parsing with various formats
            formats = [
                "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%dT%H:%M:%S.%f",
                "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d",
                "%d.%m.%Y",
                "%d.%m.%Y %H:%M",
                "%d/%m/%Y",
                "%m/%d/%Y",
                "%B %d, %Y",
                "%d %B %Y",
                "%A, %B %d, %Y"
            ]
            
            dt = None
            used_format = None
            for fmt in formats:
                try:
                    dt = datetime.strptime(datetime_str, fmt)
                    used_format = fmt
                    break
                except ValueError:
                    continue
            
            if dt is None:
                return {"error": f"Could not parse datetime: {datetime_str}"}
            
            return {
                "original": datetime_str,
                "parsed_format": used_format,
                "iso_format": dt.isoformat(),
                "unix_timestamp": int(dt.timestamp()),
                "components": {
                    "year": dt.year,
                    "month": dt.month,
                    "day": dt.day,
                    "hour": dt.hour,
                    "minute": dt.minute,
                    "second": dt.second,
                    "weekday": dt.weekday(),
                    "weekday_name": dt.strftime("%A"),
                    "month_name": dt.strftime("%B"),
                    "quarter": (dt.month - 1) // 3 + 1,
                    "day_of_year": dt.timetuple().tm_yday,
                    "week_of_year": dt.isocalendar()[1]
                }
            }
            
        except Exception as e:
            return {"error": str(e), "input": datetime_str}

    async def _add_time(self, params: dict[str, Any]) -> dict[str, Any]:
        """Add time to a datetime."""
        base_datetime = params.get("datetime", "")
        years = params.get("years", 0)
        months = params.get("months", 0)
        weeks = params.get("weeks", 0)
        days = params.get("days", 0)
        hours = params.get("hours", 0)
        minutes = params.get("minutes", 0)
        seconds = params.get("seconds", 0)
        
        try:
            if not base_datetime:
                dt = datetime.now()
            else:
                dt = datetime.fromisoformat(base_datetime.replace('Z', '+00:00'))
            
            # Add simple time deltas
            delta = timedelta(
                weeks=weeks,
                days=days,
                hours=hours,
                minutes=minutes,
                seconds=seconds
            )
            
            result_dt = dt + delta
            
            # Handle years and months (more complex)
            if years or months:
                total_months = result_dt.month + months + (years * 12)
                result_year = result_dt.year + (total_months - 1) // 12
                result_month = ((total_months - 1) % 12) + 1
                
                # Handle day overflow (e.g., Feb 31 -> Feb 28)
                max_day = calendar.monthrange(result_year, result_month)[1]
                result_day = min(result_dt.day, max_day)
                
                result_dt = result_dt.replace(
                    year=result_year,
                    month=result_month,
                    day=result_day
                )
            
            return {
                "original": base_datetime or "current time",
                "result": result_dt.isoformat(),
                "added": {
                    "years": years,
                    "months": months,
                    "weeks": weeks,
                    "days": days,
                    "hours": hours,
                    "minutes": minutes,
                    "seconds": seconds
                },
                "human_readable": result_dt.strftime("%A, %B %d, %Y at %I:%M:%S %p")
            }
            
        except Exception as e:
            return {"error": str(e), "input": base_datetime}

    async def _subtract_time(self, params: dict[str, Any]) -> dict[str, Any]:
        """Subtract time from a datetime."""
        # Convert all positive values to negative and use add_time
        new_params = params.copy()
        for key in ["years", "months", "weeks", "days", "hours", "minutes", "seconds"]:
            if key in new_params:
                new_params[key] = -new_params[key]
        
        result = await self._add_time(new_params)
        if "added" in result:
            result["subtracted"] = {k: -v for k, v in result["added"].items()}
            del result["added"]
        
        return result

    async def _convert_timezone(self, params: dict[str, Any]) -> dict[str, Any]:
        """Convert datetime between timezones."""
        datetime_str = params.get("datetime", "")
        from_tz = params.get("from_timezone", "UTC")
        to_tz = params.get("to_timezone", "UTC")
        
        try:
            if not datetime_str:
                dt = datetime.now(timezone.utc)
            else:
                dt = datetime.fromisoformat(datetime_str.replace('Z', '+00:00'))
            
            # Ensure datetime is timezone-aware
            if dt.tzinfo is None:
                if from_tz.upper() == "UTC":
                    dt = dt.replace(tzinfo=timezone.utc)
                elif from_tz.upper() == "LOCAL":
                    dt = dt.replace(tzinfo=datetime.now().astimezone().tzinfo)
                else:
                    source_tz = pytz.timezone(from_tz)
                    dt = source_tz.localize(dt)
            
            # Convert to target timezone
            if to_tz.upper() == "UTC":
                target_tz = timezone.utc
            elif to_tz.upper() == "LOCAL":
                target_tz = datetime.now().astimezone().tzinfo
            else:
                target_tz = pytz.timezone(to_tz)
            
            converted_dt = dt.astimezone(target_tz)
            
            return {
                "original": datetime_str or "current time",
                "from_timezone": from_tz,
                "to_timezone": to_tz,
                "original_time": dt.isoformat(),
                "converted_time": converted_dt.isoformat(),
                "time_difference_hours": (converted_dt.utcoffset().total_seconds() - dt.utcoffset().total_seconds()) / 3600,
                "human_readable": converted_dt.strftime("%A, %B %d, %Y at %I:%M:%S %p %Z")
            }
            
        except Exception as e:
            return {"error": str(e), "input": datetime_str, "from_tz": from_tz, "to_tz": to_tz}

    async def _unix_timestamp(self, params: dict[str, Any]) -> dict[str, Any]:
        """Convert between datetime and Unix timestamp."""
        datetime_str = params.get("datetime", "")
        timestamp = params.get("timestamp", None)
        
        try:
            if timestamp is not None:
                # Convert from Unix timestamp to datetime
                dt = datetime.fromtimestamp(float(timestamp), tz=timezone.utc)
                return {
                    "timestamp": timestamp,
                    "datetime": dt.isoformat(),
                    "human_readable": dt.strftime("%A, %B %d, %Y at %I:%M:%S %p UTC"),
                    "components": {
                        "year": dt.year,
                        "month": dt.month,
                        "day": dt.day,
                        "hour": dt.hour,
                        "minute": dt.minute,
                        "second": dt.second,
                        "weekday": dt.weekday(),
                        "weekday_name": dt.strftime("%A"),
                        "month_name": dt.strftime("%B")
                    }
                }
            else:
                # Convert from datetime to Unix timestamp
                if not datetime_str:
                    dt = datetime.now(timezone.utc)
                else:
                    dt = datetime.fromisoformat(datetime_str.replace('Z', '+00:00'))
                
                return {
                    "datetime": datetime_str or "current time",
                    "timestamp": int(dt.timestamp()),
                    "timestamp_milliseconds": int(dt.timestamp() * 1000),
                    "iso_format": dt.isoformat()
                }
                
        except Exception as e:
            return {"error": str(e), "input": {"datetime": datetime_str, "timestamp": timestamp}}

    async def _calendar_info(self, params: dict[str, Any]) -> dict[str, Any]:
        """Get calendar information for a date."""
        datetime_str = params.get("datetime", "")
        
        try:
            if not datetime_str:
                dt = datetime.now()
            else:
                dt = datetime.fromisoformat(datetime_str.replace('Z', '+00:00'))
            
            year, week, weekday = dt.isocalendar()
            
            return {
                "date": dt.strftime("%Y-%m-%d"),
                "year": dt.year,
                "month": dt.month,
                "month_name": dt.strftime("%B"),
                "month_abbr": dt.strftime("%b"),
                "day": dt.day,
                "weekday": dt.weekday(),  # 0=Monday
                "weekday_name": dt.strftime("%A"),
                "weekday_abbr": dt.strftime("%a"),
                "day_of_year": dt.timetuple().tm_yday,
                "week_of_year": week,
                "quarter": (dt.month - 1) // 3 + 1,
                "is_weekend": dt.weekday() >= 5,
                "is_leap_year": calendar.isleap(dt.year),
                "days_in_month": calendar.monthrange(dt.year, dt.month)[1],
                "first_weekday_of_month": calendar.monthrange(dt.year, dt.month)[0],
                "calendar_month": calendar.month(dt.year, dt.month)
            }
            
        except Exception as e:
            return {"error": str(e), "input": datetime_str}

    async def _business_days(self, params: dict[str, Any]) -> dict[str, Any]:
        """Calculate business days between dates or add business days."""
        start_date = params.get("start_date", "")
        end_date = params.get("end_date", "")
        add_days = params.get("add_business_days", None)
        
        try:
            if not start_date:
                start_dt = datetime.now().date()
            else:
                start_dt = datetime.fromisoformat(start_date.replace('Z', '+00:00')).date()
            
            if add_days is not None:
                # Add business days to start_date
                current_date = start_dt
                days_added = 0
                days_to_add = int(add_days)
                
                while days_added < abs(days_to_add):
                    if days_to_add > 0:
                        current_date += timedelta(days=1)
                    else:
                        current_date -= timedelta(days=1)
                    
                    # Skip weekends (Saturday=5, Sunday=6)
                    if current_date.weekday() < 5:
                        days_added += 1
                
                return {
                    "start_date": start_date or "current date",
                    "business_days_added": add_days,
                    "result_date": current_date.isoformat(),
                    "weekday": current_date.weekday(),
                    "weekday_name": current_date.strftime("%A"),
                    "is_business_day": current_date.weekday() < 5
                }
            
            elif end_date:
                # Calculate business days between dates
                end_dt = datetime.fromisoformat(end_date.replace('Z', '+00:00')).date()
                
                if start_dt > end_dt:
                    start_dt, end_dt = end_dt, start_dt
                    swapped = True
                else:
                    swapped = False
                
                business_days = 0
                current_date = start_dt
                
                while current_date < end_dt:
                    if current_date.weekday() < 5:  # Monday=0, Friday=4
                        business_days += 1
                    current_date += timedelta(days=1)
                
                return {
                    "start_date": start_date,
                    "end_date": end_date,
                    "business_days_between": business_days,
                    "total_days": (end_dt - start_dt).days,
                    "dates_swapped": swapped
                }
            
            else:
                return {"error": "Either end_date or add_business_days parameter required"}
                
        except Exception as e:
            return {"error": str(e), "input": {"start_date": start_date, "end_date": end_date, "add_days": add_days}}

    def get_schema(self) -> dict[str, Any]:
        """Return the OpenAI function schema for datetime operations."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": "Get current date/time, format dates, perform date calculations, timezone conversions, and calendar operations. Provides comprehensive datetime functionality.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string", 
                            "enum": ["current", "format", "parse", "add", "subtract", "convert_timezone", "timestamp", "calendar_info", "business_days"],
                            "description": "DateTime operation: 'current' (get current date/time), 'format' (format datetime), 'parse' (parse datetime string), 'add'/'subtract' (date arithmetic), 'convert_timezone' (timezone conversion), 'timestamp' (Unix timestamp conversion), 'calendar_info' (calendar details), 'business_days' (business day calculations)"
                        },
                        "timezone": {
                            "type": "string",
                            "description": "Timezone (e.g., 'UTC', 'Europe/Berlin', 'America/New_York', 'LOCAL'). Default: UTC"
                        },
                        "format": {
                            "type": "string",
                            "description": "Output format: 'iso', 'human', 'date_only', 'time_only', 'custom', or strftime format string"
                        },
                        "datetime": {
                            "type": "string",
                            "description": "Input datetime string in various formats (ISO, human readable, etc.)"
                        },
                        "custom_format": {
                            "type": "string",
                            "description": "Custom strftime format string when format='custom'"
                        },
                        "input_format": {
                            "type": "string",
                            "description": "Expected input format for parsing ('auto' for automatic detection)"
                        },
                        "years": {"type": "integer", "description": "Years to add/subtract"},
                        "months": {"type": "integer", "description": "Months to add/subtract"},
                        "weeks": {"type": "integer", "description": "Weeks to add/subtract"},
                        "days": {"type": "integer", "description": "Days to add/subtract"},
                        "hours": {"type": "integer", "description": "Hours to add/subtract"},
                        "minutes": {"type": "integer", "description": "Minutes to add/subtract"},
                        "seconds": {"type": "integer", "description": "Seconds to add/subtract"},
                        "from_timezone": {
                            "type": "string",
                            "description": "Source timezone for conversion"
                        },
                        "to_timezone": {
                            "type": "string",
                            "description": "Target timezone for conversion"
                        },
                        "timestamp": {
                            "type": "number",
                            "description": "Unix timestamp to convert to datetime"
                        },
                        "start_date": {
                            "type": "string",
                            "description": "Start date for business day calculations"
                        },
                        "end_date": {
                            "type": "string",
                            "description": "End date for business day calculations"
                        },
                        "add_business_days": {
                            "type": "integer",
                            "description": "Number of business days to add to start_date"
                        }
                    },
                    "required": [],
                    "additionalProperties": True,
                },
            },
        }

    def get_default_action(self) -> str:
        """Return the default action for datetime operations."""
        return "current"
