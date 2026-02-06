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
    try:
        env = Environment(loader=FileSystemLoader(str(p)), autoescape=select_autoescape())
        # render using the filename as template name so includes work
        template = env.get_template("schema.yaml")
        # Render with provided template vars
        text = template.render(**(template_vars or {}))
        logger.debug("Rendered schema for %s with template_vars=%s", schema_file, template_vars)
    except Exception as e:
        logger.error(f"Jinja2 template rendering failed for {schema_file}: {e}")
        raise RuntimeError(f"Failed to render schema template {schema_file}: {e}") from e

    try:
        data = yaml.safe_load(text)
        if isinstance(data, dict):
            return data
        else:
            logger.error(f"Schema file {schema_file} did not parse to a dictionary, got {type(data)}")
            return None
    except yaml.YAMLError as e:
        logger.error(f"YAML syntax error in {schema_file}: {e}")
        raise RuntimeError(f"Invalid YAML syntax in {schema_file}: {e}") from e
    except Exception as e:
        # try JSON as last resort
        logger.debug(f"YAML parsing failed for {schema_file}, trying JSON: {e}")
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                logger.warning(f"Schema {schema_file} parsed as JSON instead of YAML")
                return data
            else:
                logger.error(f"Schema {schema_file} JSON parse result is not a dictionary")
                return None
        except Exception as json_err:
            logger.error(f"Failed to parse {schema_file} as YAML or JSON: YAML error: {e}, JSON error: {json_err}")
            raise RuntimeError(f"Failed to parse schema {schema_file}: {e}") from e

    return None
