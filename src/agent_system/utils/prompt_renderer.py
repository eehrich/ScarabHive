from __future__ import annotations

from typing import Dict, Any
import logging
from jinja2 import Template
from datetime import datetime, timedelta
import pytz


logger = logging.getLogger(__name__)


def get_datetime_context(timezone_str: str = "UTC", location: str = "Unknown") -> Dict[str, Any]:
    """Generate current datetime context for prompt templates."""
    try:
        if timezone_str.upper() == "UTC":
            tz: Any = pytz.UTC  # pytz types are complex, use Any
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


def _is_text_template(template_path: str) -> bool:
    """Check if file is a plain text/markdown template."""
    lower_path = template_path.lower()
    return lower_path.endswith(('.md', '.txt', '.markdown'))


def _render_text_template(template_path: str, context: Dict[str, Any]) -> Dict[str, str]:
    """Render a plain text or markdown file as a single system_prompt section.
    
    The entire file content is treated as the system_prompt and rendered
    with Jinja2 template substitution.
    
    Args:
        template_path: Path to the text/markdown file
        context: Template context variables for Jinja2 rendering
        
    Returns:
        Dict with single 'system_prompt' key containing the rendered content
    """
    with open(template_path, "r", encoding="utf-8") as f:
        raw_content = f.read()
    
    try:
        rendered = Template(raw_content).render(**context)
    except Exception as e:
        logger.warning(f"Failed to render text template {template_path}: {e}")
        rendered = raw_content  # Fallback to unrendered content
    
    return {"system_prompt": rendered}


def render_prompts(template_path: str, context: Dict[str, Any], auto_datetime: bool = True, timezone: str = "UTC", location: str = "Unknown") -> Dict[str, str]:
    """Render a markdown/text prompt template into a single ``system_prompt``.

    The entire file is the system prompt, rendered with Jinja2 (e.g.
    ``{{ current_date }}``, ``{{ tools }}``, ``{{ current_step }}``). Templates
    are ``.md`` / ``.txt`` / ``.markdown``.

    The former multi-section YAML format (``system_prompt`` / ``tools_prompt`` /
    ``general_instructions_prompt`` keys) was removed — everything is one
    markdown document now. A ``.yaml``/``.yml`` path raises a clear error with
    migration guidance rather than silently embedding raw YAML.

    Returns:
        Dict with a single ``system_prompt`` key.
    """
    if auto_datetime:
        datetime_context = get_datetime_context(timezone, location)
        context = {**context, **datetime_context}

    if not _is_text_template(template_path):
        raise ValueError(
            f"Prompt template '{template_path}' is not markdown. YAML prompt "
            f"templates are no longer supported — convert it to a single "
            f"markdown (.md) file (Jinja2 variables still work).")

    logger.debug(f"Rendering markdown prompt template: {template_path}")
    return _render_text_template(template_path, context)
