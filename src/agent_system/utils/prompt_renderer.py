from __future__ import annotations

from typing import Dict, Any, Tuple
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


def render_prompts(template_path: str, context: Dict[str, Any]) -> Dict[str, str]:
    """Render a YAML template that may contain multiple sections.

    Expected keys:
      - system_prompt: str (jinja2 template)
      - tools_prompt: str (jinja2 template)

    Backward-compat: if only 'template' key exists, map it to system_prompt.
    """
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
