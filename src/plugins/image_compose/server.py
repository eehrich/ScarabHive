"""image_compose MCP server — thin wrapper around compositor.compose()."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict

from agent_system.mcp.schema_based import SchemaBasedMCPServer

from .compositor import CompositionError, analyze_image, compose, find_text_region

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

        # Inter-layer overlap check (text/svg pairs only). Defaults match the
        # cover_artist prompt's HARTE REGEL #7. Plugin users with different
        # composition policies can disable or retune via plugin config.
        overlap_enabled = getattr(mcp_config, "overlap_check_enabled", True)
        self.overlap_check_enabled: bool = (
            bool(overlap_enabled) if overlap_enabled is not None else True
        )
        overlap_gap = getattr(mcp_config, "overlap_min_gap_px", 30)
        try:
            self.overlap_min_gap_px: int = max(0, int(overlap_gap))
        except (TypeError, ValueError):
            self.overlap_min_gap_px = 30

        self.fonts_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "ImageComposeServer initialized — fonts_dir=%s, output_root=%s, aliases=%d, "
            "overlap_check=%s (min_gap=%dpx)",
            self.fonts_dir, self.output_root, len(self.font_aliases),
            self.overlap_check_enabled, self.overlap_min_gap_px,
        )

    async def render(self, params: Dict[str, Any]) -> Dict[str, Any]:
        status = params.get("_status")
        spec = params.get("spec")
        output_path = params.get("output_path")
        include_content = params.get("include_content", True)
        layers_dir_param = params.get("layers_dir")

        # `spec` accepts either a dict (internal/test callers) or a JSON string
        # (LLM callers). String is the schema-declared form because Gemini's
        # constrained decoder collapses on freeform objects with
        # `additionalProperties: true` — emitting JSON-as-string sidesteps that
        # entirely (see MALFORMED_FUNCTION_CALL incidents 2026-05-26).
        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except json.JSONDecodeError as e:
                return _error(f"spec is not valid JSON: {e}", "ValidationError")
        if not isinstance(spec, dict):
            return _error("spec is required and must be a JSON object or JSON string", "ValidationError")
        if not output_path or not isinstance(output_path, str):
            return _error("output_path is required (string)", "ValidationError")

        try:
            out_full = Path(output_path)
            if not out_full.is_absolute():
                out_full = (self.output_root / out_full).resolve()
            out_full.parent.mkdir(parents=True, exist_ok=True)

            # Per-layer PNG export:
            #   layers_dir given (non-empty string)  → use it
            #   layers_dir explicitly False / ""     → opt out
            #   layers_dir omitted (None / missing)  → auto-derive
            #       <output-parent>/<output-stem>_layers/
            # Auto-default chosen because cover_artist callers were forgetting
            # the param and silently losing the per-layer export.
            layers_dir: Path | None
            if layers_dir_param is False or layers_dir_param == "":
                layers_dir = None  # explicit opt-out
            elif layers_dir_param:
                layers_dir = Path(layers_dir_param)
                if not layers_dir.is_absolute():
                    layers_dir = (self.output_root / layers_dir).resolve()
            else:
                # Omitted → auto-derive next to the composite
                layers_dir = out_full.parent / f"{out_full.stem}_layers"

            n_layers = len(spec.get("layers") or [])
            if status:
                await status.progress(f"Rendering {n_layers} layer(s) → {out_full.name}")

            meta = await asyncio.to_thread(
                compose, spec, out_full, self.fonts_dir, self.font_aliases,
                self.project_root, layers_dir,
                self.overlap_check_enabled, self.overlap_min_gap_px,
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
            if meta.get("layer_files"):
                result["layer_files"] = meta["layer_files"]
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

    async def analyze(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Measure brightness/colour/edges of an image (optionally a region).

        Lets agents pick contrasting text colours from real pixel data instead
        of "looking at the background" with a VLM.
        """
        status = params.get("_status")
        path = params.get("path")
        region = params.get("region")

        if not path or not isinstance(path, str):
            return _error("path is required (string)", "ValidationError")
        if region is not None and (
            not isinstance(region, (list, tuple)) or len(region) != 4
            or not all(isinstance(v, (int, float)) for v in region)
        ):
            return _error("region must be [x, y, w, h] (4 numbers)", "ValidationError")

        full = Path(path)
        if not full.is_absolute():
            full = (self.project_root / full).resolve()
        if not full.exists():
            return _error(f"image not found: {full}", "FileNotFoundError")

        try:
            region_tuple: tuple[int, int, int, int] | None = (
                (int(region[0]), int(region[1]), int(region[2]), int(region[3]))
                if region else None
            )
            if status:
                await status.progress(f"Analysing {full.name}")
            result = await asyncio.to_thread(analyze_image, full, region_tuple)
            payload = {"status": "success", "path": str(full), **result}
            if status:
                await status.end(
                    f"brightness={result['brightness']:.2f} "
                    f"std={result['brightness_std']:.2f} "
                    f"→ {result['recommendation']}",
                    meta={
                        "brightness": result["brightness"],
                        "recommendation": result["recommendation"],
                    },
                )
            return payload
        except Exception as e:
            logger.error("image_compose.analyze failed", exc_info=True)
            if status:
                await status.error(f"Analyse failed: {e}")
            return _error(str(e), type(e).__name__)

    async def find_region(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Find the most homogeneous rectangle of a target size in an image.

        Use BEFORE picking a text-layer position to let the agent place text
        on the calmest area available, instead of guessing.
        """
        status = params.get("_status")
        path = params.get("path")
        region_size = params.get("region_size")
        prefer = params.get("prefer", "any")
        max_candidates = int(params.get("max_candidates", 3))

        if not path or not isinstance(path, str):
            return _error("path is required (string)", "ValidationError")
        if (not isinstance(region_size, (list, tuple))
                or len(region_size) != 2
                or not all(isinstance(v, (int, float)) for v in region_size)):
            return _error("region_size must be [w, h] (2 numbers)", "ValidationError")
        if not isinstance(prefer, str):
            return _error("prefer must be a string", "ValidationError")

        full = Path(path)
        if not full.is_absolute():
            full = (self.project_root / full).resolve()
        if not full.exists():
            return _error(f"image not found: {full}", "FileNotFoundError")

        try:
            size_tuple: tuple[int, int] = (int(region_size[0]), int(region_size[1]))
            if status:
                await status.progress(f"Searching {full.name} for best {size_tuple[0]}x{size_tuple[1]} text region")
            result = await asyncio.to_thread(
                find_text_region, full, size_tuple, prefer, max_candidates,
            )
            payload = {"status": "success", "path": str(full), **result}
            if status:
                best = result["best"]
                await status.end(
                    f"best region [{best['region'][0]},{best['region'][1]}] "
                    f"homogeneity={best['homogeneity']:.2f} "
                    f"→ {best['recommendation']}",
                    meta={"best_region": best["region"], "homogeneity": best["homogeneity"]},
                )
            return payload
        except CompositionError as e:
            if status:
                await status.error(f"Find-region failed: {e}")
            return _error(str(e), "CompositionError")
        except Exception as e:
            logger.error("image_compose.find_region failed", exc_info=True)
            if status:
                await status.error(f"Find-region failed: {e}")
            return _error(str(e), type(e).__name__)


def _error(msg: str, kind: str) -> Dict[str, Any]:
    return {"status": "error", "error": msg, "error_type": kind}


PLUGIN_FACTORY = ImageComposeServer
