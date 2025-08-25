from __future__ import annotations

from typing import Dict, Any
import yaml
from jinja2 import Template


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
