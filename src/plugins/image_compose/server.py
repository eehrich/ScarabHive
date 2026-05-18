"""image_compose MCP server — thin wrapper around compositor.compose()."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict

from agent_system.mcp.schema_based import SchemaBasedMCPServer

from .compositor import CompositionError, compose

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, MCPConfig

logger = logging.getLogger(__name__)

_MIME_BY_FORMAT = {
    "png": "image/png",
    "webp": "image/webp",
    "jpeg": "image/jpeg",
}


class ImageComposeServer(SchemaBasedMCPServer):
    """Renders layered images from a JSON spec."""

    def __init__(self, name: str, system_config: "AgentSystemConfig",
                 mcp_config: "MCPConfig") -> None:
        super().__init__(name, system_config, mcp_config)

        project_root = Path.cwd()
        fonts_dir_cfg = getattr(mcp_config, "fonts_dir", "data/fonts") or "data/fonts"
        output_root_cfg = getattr(mcp_config, "output_root", ".") or "."

        self.project_root = project_root
        self.fonts_dir = (project_root / fonts_dir_cfg).resolve() \
            if not Path(fonts_dir_cfg).is_absolute() else Path(fonts_dir_cfg)
        self.output_root = (project_root / output_root_cfg).resolve() \
            if not Path(output_root_cfg).is_absolute() else Path(output_root_cfg)

        aliases_cfg = getattr(mcp_config, "font_aliases", {}) or {}
        self.font_aliases: dict[str, str] = dict(aliases_cfg) if isinstance(aliases_cfg, dict) else {}

        self.fonts_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "ImageComposeServer initialized — fonts_dir=%s, output_root=%s, aliases=%d",
            self.fonts_dir, self.output_root, len(self.font_aliases),
        )

    async def render(self, params: Dict[str, Any]) -> Dict[str, Any]:
        status = params.get("_status")
        spec = params.get("spec")
        output_path = params.get("output_path")
        include_content = params.get("include_content", True)

        if not isinstance(spec, dict):
            return _error("spec is required and must be an object", "ValidationError")
        if not output_path or not isinstance(output_path, str):
            return _error("output_path is required (string)", "ValidationError")

        try:
            out_full = Path(output_path)
            if not out_full.is_absolute():
                out_full = (self.output_root / out_full).resolve()
            out_full.parent.mkdir(parents=True, exist_ok=True)

            n_layers = len(spec.get("layers") or [])
            if status:
                await status.progress(f"Rendering {n_layers} layer(s) → {out_full.name}")

            meta = await asyncio.to_thread(
                compose, spec, out_full, self.fonts_dir, self.font_aliases, self.project_root,
            )

            warnings_list = meta.get("warnings", []) or []
            # If anything is off-canvas or otherwise dubious, surface it via
            # status="warning" so the calling agent can't ignore the warnings
            # field. The file is still written either way.
            result_status = "warning" if warnings_list else "success"

            result: Dict[str, Any] = {
                "status": result_status,
                "output_path": str(out_full),
                "size": meta["size"],
                "format": meta["format"],
                "bytes": meta["bytes"],
                "layers_rendered": meta["layers_rendered"],
                "warnings": warnings_list,
            }
            if warnings_list:
                result["action_required"] = (
                    "Re-compose: address each item in `warnings` (e.g. reduce "
                    "layer size, change position/anchor, shorten title) and "
                    "call image_compose_render again. Don't accept the cover "
                    "as-is when warnings are present."
                )

            if include_content:
                mime = _MIME_BY_FORMAT.get(meta["format"], "application/octet-stream")
                result["_multimodal_content"] = [{
                    "type": "image",
                    "path": str(out_full),
                    "mime_type": mime,
                    "description": (
                        f"Composed image ({meta['size'][0]}x{meta['size'][1]}, "
                        f"{meta['layers_rendered']} layers)"
                    ),
                }]

            if status:
                end_msg = (
                    f"Rendered {out_full.name}: "
                    f"{meta['size'][0]}x{meta['size'][1]} ({meta['bytes']} bytes)"
                )
                if warnings_list:
                    end_msg += f" — {len(warnings_list)} warning(s)"
                await status.end(
                    end_msg,
                    meta={
                        "output_path": str(out_full),
                        "bytes": meta["bytes"],
                        "warnings": len(warnings_list),
                    },
                )
            return result

        except CompositionError as e:
            msg = str(e)
            if status:
                await status.error(f"Composition failed: {msg}")
            return _error(msg, "CompositionError")
        except Exception as e:
            logger.error("image_compose.render failed", exc_info=True)
            if status:
                await status.error(f"Unexpected error: {e}")
            return _error(str(e), type(e).__name__)


def _error(msg: str, kind: str) -> Dict[str, Any]:
    return {"status": "error", "error": msg, "error_type": kind}


PLUGIN_FACTORY = ImageComposeServer
