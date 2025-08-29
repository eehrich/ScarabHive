from __future__ import annotations

from typing import Dict, Any
import yaml
from jinja2 import Template
from datetime import datetime, timedelta
import pytz


def get_datetime_context(timezone_str: str = "UTC", location: str = "Unknown") -> Dict[str, Any]:
    """Generate current datetime context for prompt templates."""
    try:
        if timezone_str.upper() == "UTC":
            tz = pytz.UTC
        else:
            tz = pytz.timezone(timezone_str)
        
        now = datetime.now(tz)
        tomorrow = now + timedelta(days=1)
        
        return {
            "current_date": now.strftime("%Y-%m-%d"),
            "current_time": now.strftime("%H:%M:%S"),
            "current_datetime": now.isoformat(),
            "current_timezone": timezone_str,
            "current_location": location,
            "tomorrow_date": tomorrow.strftime("%Y-%m-%d"),
            "current_weekday": now.strftime("%A"),
            "current_month": now.strftime("%B"),
            "current_year": now.year,
            "unix_timestamp": int(now.timestamp())
        }
    except Exception:
        # Fallback to basic info
        now = datetime.now()
        tomorrow = now + timedelta(days=1)
        return {
            "current_date": now.strftime("%Y-%m-%d"),
            "current_time": now.strftime("%H:%M:%S"),
            "current_datetime": now.isoformat(),
            "current_timezone": "Local",
            "current_location": location,
            "tomorrow_date": tomorrow.strftime("%Y-%m-%d"),
            "current_weekday": now.strftime("%A"),
            "current_month": now.strftime("%B"),
            "current_year": now.year,
            "unix_timestamp": int(now.timestamp())
        }


def render_system_prompt(template_path: str, context: Dict[str, Any]) -> str:
    with open(template_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    # Template content under key 'template' to keep YAML extensible
    raw = data.get("template") if isinstance(data, dict) else None
    if not raw:
        # If file is plain text, just use it directly
        f.seek(0)
        raw = f.read()
    tmpl = Template(raw)
    return tmpl.render(**context)


def render_prompts(template_path: str, context: Dict[str, Any], auto_datetime: bool = True, timezone: str = "UTC", location: str = "Unknown") -> Dict[str, str]:
    """Render a YAML template that may contain multiple sections.

    Expected keys:
      - system_prompt: str (jinja2 template)
      - tools_prompt: str (jinja2 template)

    Backward-compat: if only 'template' key exists, map it to system_prompt.
    """
    # Add automatic datetime context if enabled
    if auto_datetime:
        datetime_context = get_datetime_context(timezone, location)
        context = {**context, **datetime_context}
    
    with open(template_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    out: Dict[str, str] = {}
    if isinstance(data, dict):
        sys_raw = data.get("system_prompt") or data.get("template")
        tools_raw = data.get("tools_prompt")
        if sys_raw:
            out["system_prompt"] = Template(sys_raw).render(**context)
        if tools_raw:
            out["tools_prompt"] = Template(tools_raw).render(**context)
    else:
        out["system_prompt"] = str(data)
    return out
