from __future__ import annotations

from typing import Any
from pathlib import Path
import json

from jinja2 import Environment, FileSystemLoader, select_autoescape  # type: ignore

import yaml
import logging

logger = logging.getLogger(__name__)


def load_schema_from_dir(
    dir_path: str | Path,
    template_vars: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Load and return schema dict from schema.yaml in dir_path.

    If Jinja2 is available, render the template before parsing YAML.
    `template_vars` is passed to Jinja2 rendering (useful for plugin-provided values,
    e.g. {'name': 'web_scraper'}). Returns None if no schema.yaml is found.
    """
    p = Path(dir_path)
    schema_file = p / "schema.yaml"
    if not schema_file.exists():
        return None

    text = schema_file.read_text(encoding="utf-8")
    # Render using Jinja2 (Jinja2 is required by this project)
    env = Environment(loader=FileSystemLoader(str(p)), autoescape=select_autoescape())
    # render using the filename as template name so includes work
    template = env.get_template("schema.yaml")
    # Render with provided template vars
    text = template.render(**(template_vars or {}))
    logger.debug("Rendered schema for %s with template_vars=%s", schema_file, template_vars)

    try:
        data = yaml.safe_load(text)
        if isinstance(data, dict):
            return data
    except Exception:
        # try JSON as last resort
        try:
            return json.loads(text)
        except Exception:
            return None

    return None
