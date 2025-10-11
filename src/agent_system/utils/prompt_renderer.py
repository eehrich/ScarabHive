from __future__ import annotations

from typing import Dict, Any
import logging
import yaml
from jinja2 import Template
from datetime import datetime, timedelta
import pytz


logger = logging.getLogger(__name__)


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

    This function supports flexible multi-section prompts. All top-level
    string keys in the YAML file are treated as sections and rendered separately.
    
    Common sections (from system_prompt.yaml):
      - system_prompt: Main agent identity and behavior
      - tools_prompt: Tool usage instructions
      - general_instructions_prompt: Formatting and context guidelines
    
    Any additional sections defined in agent-specific templates will also be included.
    
    Returns:
        Dict mapping section_name -> rendered_content (after Jinja2 template rendering)
    """
    # Add automatic datetime context if enabled
    if auto_datetime:
        datetime_context = get_datetime_context(timezone, location)
        context = {**context, **datetime_context}
    
    with open(template_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    
    out: Dict[str, str] = {}
    
    if isinstance(data, dict):
        # Render all string-valued top-level keys as sections
        for section_name, section_content in data.items():
            # Skip non-string values and internal keys (starting with _)
            if not isinstance(section_content, str):
                continue
            if section_name.startswith('_'):
                continue
            
            # Render the section with Jinja2
            try:
                rendered = Template(section_content).render(**context)
                out[section_name] = rendered
            except Exception as e:
                logger.warning(f"Failed to render section '{section_name}' in template {template_path}: {e}")
                out[section_name] = section_content  # Fallback to unrendered content
    else:
        # Non-dict YAML: treat entire content as system_prompt
        logger.warning(f"Template {template_path} is not a dict, treating as single system_prompt section")
        out["system_prompt"] = str(data)
    
    return out
