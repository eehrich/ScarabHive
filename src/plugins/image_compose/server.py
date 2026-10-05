"""image_compose tool server — thin wrapper around compositor.compose()."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict

from agent_system.paths import data_path, resolve_data_path
from agent_system.tools.schema_based import SchemaBasedToolServer

from .compositor import CompositionError, analyze_image, check_local, compose, find_text_region

if TYPE_CHECKING:
    from agent_system.config import AgentSystemConfig, ToolServerConfig

logger = logging.getLogger(__name__)

_MIME_BY_FORMAT = {
    "png": "image/png",
    "webp": "image/webp",
    "jpeg": "image/jpeg",
}
MAX_WARNINGS = 20


class ImageComposeServer(SchemaBasedToolServer):
    """Renders layered images from a JSON spec."""

    def __init__(self, name: str, system_config: "AgentSystemConfig",
                 server_config: "ToolServerConfig") -> None:
        super().__init__(name, system_config, server_config)

        project_root = Path.cwd()
        fonts_dir_cfg = getattr(server_config, "fonts_dir", None) or data_path("fonts")

        self.project_root = project_root
        self.fonts_dir = (project_root / fonts_dir_cfg).resolve() \
            if not Path(fonts_dir_cfg).is_absolute() else Path(fonts_dir_cfg)

        aliases_cfg = getattr(server_config, "font_aliases", {}) or {}
        self.font_aliases: dict[str, str] = dict(aliases_cfg) if isinstance(aliases_cfg, dict) else {}

        # Optional write sandbox. Empty (the default) keeps the historical
        # behaviour -- the composite, its layer directory and the spec land
        # wherever output_path says, which the writer's cover pipeline relies
        # on. A non-empty list confines all three to those directories, so an
        # instance handed to a sub-agent cannot write into another agent's
        # tree on a model-chosen path.
        dirs_cfg = getattr(server_config, "output_directories", None) or []
        self.output_directories: list[Path] = [
            (project_root / d).resolve() if not Path(d).is_absolute() else Path(d).resolve()
            for d in dirs_cfg
        ]

        # Inter-layer overlap check (text/svg pairs only). Defaults match the
        # cover_artist prompt's HARTE REGEL #7. Plugin users with different
        # composition policies can disable or retune via plugin config.
        overlap_enabled = getattr(server_config, "overlap_check_enabled", True)
        self.overlap_check_enabled: bool = (
            bool(overlap_enabled) if overlap_enabled is not None else True
        )
        overlap_gap = getattr(server_config, "overlap_min_gap_px", 30)
        try:
            self.overlap_min_gap_px: int = max(0, int(overlap_gap))
        except (TypeError, ValueError):
            self.overlap_min_gap_px = 30

        self.fonts_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "ImageComposeServer initialized — fonts_dir=%s, write_sandbox=%s, aliases=%d, "
            "overlap_check=%s (min_gap=%dpx)",
            self.fonts_dir, self.output_directories or "unrestricted", len(self.font_aliases),
            self.overlap_check_enabled, self.overlap_min_gap_px,
        )

    async def render(self, params: Dict[str, Any]) -> Dict[str, Any]:
        status = params.get("_status")
        spec = params.get("spec")
        output_path = params.get("output_path")
        include_content = params.get("include_content", True)
        layers_dir_param = params.get("layers_dir")
        spec_path = params.get("spec_path")

        # `spec` accepts either a dict (internal/test callers) or a JSON string
        # (LLM callers). The schema declares an object; the string form stays
        # accepted because Gemini's constrained decoder collapsed on freeform
        # objects and models fell back to JSON-as-string (see
        # MALFORMED_FUNCTION_CALL incidents 2026-05-26).
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
            out_full = self._confine(self._resolve_out(output_path, "output_path"), "output_path")
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
                layers_dir = self._resolve_out(layers_dir_param, "layers_dir")
                # compose() clears layer_*.png from this directory before
                # writing. Pointed at the directory the composite goes to,
                # that deletes delivered assets whose name starts with
                # "layer_" -- and the reply would still say success.
                if layers_dir == out_full.parent:
                    raise CompositionError(
                        f"layers_dir {layers_dir} is the directory the composite is written "
                        f"to; the per-layer export needs one of its own (omit layers_dir for "
                        f"{out_full.stem}_layers, or pass \"\" to skip it)")
            else:
                # Omitted → auto-derive next to the composite
                layers_dir = out_full.parent / f"{out_full.stem}_layers"
            if layers_dir is not None:
                layers_dir = self._confine(layers_dir, "layers_dir")

            # Resolved and confined HERE, before compose() writes anything:
            # a refused spec_path after the render would leave the composite
            # and its layer directory on disk under an error result.
            spec_full: Path | None = None
            if spec_path and isinstance(spec_path, str):
                spec_full = self._confine(self._resolve_out(spec_path, "spec_path"), "spec_path")

            n_layers = len(spec.get("layers") or [])
            if status:
                await status.progress(f"Rendering {n_layers} layer(s) → {out_full.name}")

            meta = await asyncio.to_thread(
                compose, spec, out_full, self.fonts_dir, self.font_aliases,
                self.project_root, layers_dir,
                self.overlap_check_enabled, self.overlap_min_gap_px,
            )

            warnings_list = meta.get("warnings", []) or []
            n_warnings = len(warnings_list)
            # Overlap warnings grow with the square of the text/svg layers:
            # forty stacked labels would put 780 of them into the context.
            if len(warnings_list) > MAX_WARNINGS:
                warnings_list = warnings_list[:MAX_WARNINGS] + [
                    f"... and {len(warnings_list) - MAX_WARNINGS} more warnings"]
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

            # Optional: persist the (validated, in-memory) spec as JSON next
            # to the composite. We write the dict ourselves with
            # ensure_ascii=False so non-ASCII text (German umlauts etc.) is
            # stored as UTF-8 chars instead of \u escapes. This sidesteps the
            # double-encoding mojibake we saw when LLMs serialised specs as
            # JSON strings themselves and passed them through file_ops.
            if spec_full is not None:
                spec_full.parent.mkdir(parents=True, exist_ok=True)
                spec_full.write_text(
                    json.dumps(spec, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                result["spec_path"] = str(spec_full)
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
                    end_msg += f" — {n_warnings} warning(s)"
                await status.end(
                    end_msg,
                    meta={
                        "output_path": str(out_full),
                        "bytes": meta["bytes"],
                        "warnings": n_warnings,
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

    def _resolve_out(self, value: str, what: str) -> Path:
        """A write path: relative to the project root, or absolute.

        The same rule the read side has always used (``analyze``,
        ``find_region``, and an image layer's ``src``), and the one the
        schema states. There used to be a second one -- an ``output_root``
        the server prepended to relative paths -- and the two disagreed
        whenever it was not the project root: an agent that passed the
        project-relative path it had been briefed with got the root
        prepended, so the file landed at
        ``<root>/data/workspace/images/x.png``. That is inside the write
        sandbox, so nothing refused it, and the caller then looked for its
        asset where it had meant to put it (measured 2026-09-04; the
        sub-agent reported the doubled path upward). One rule cannot
        disagree with itself, and where a path may go is what
        ``output_directories`` says.

        ``base / absolute`` is that absolute path, so absolutes need no
        branch of their own -- nor does a configured data directory: a
        ``data/...`` path lands there (agent_system/paths.py), absolute.

        A host path is refused on its text first, unless it lies in an
        output directory: ``resolve()`` would already reach the host.
        """
        check_local(value, what, self.output_directories)
        return (self.project_root / resolve_data_path(value)).resolve()

    def _resolve_in(self, value: str) -> Path:
        """An image to read: same rule as ``_resolve_out``, but never a host path."""
        check_local(value, "path")
        full = (self.project_root / resolve_data_path(value)).resolve()
        if not full.exists():
            raise FileNotFoundError(f"image not found: {full}")
        return full

    def _confine(self, path: Path, what: str) -> Path:
        """``path`` if it lies inside one of ``output_directories`` (or the
        sandbox is off); raises CompositionError otherwise, BEFORE anything
        is created on disk."""
        if not self.output_directories:
            return path
        resolved = path.resolve()
        for root in self.output_directories:
            if resolved == root or root in resolved.parents:
                return resolved
        allowed = ", ".join(str(r) for r in self.output_directories)
        raise CompositionError(
            f"{what} {path} lies outside the allowed output directories ({allowed})")

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

        try:
            full = self._resolve_in(path)
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
        # The schema's 1..10 is not enforced by the framework: 0 answered
        # "list index out of range", a negative dropped the last candidates,
        # and a non-number raised out of the handler.
        try:
            max_candidates = max(1, min(10, int(params.get("max_candidates", 3))))
        except (TypeError, ValueError, OverflowError):
            return _error("max_candidates must be an integer 1-10", "ValidationError")

        if not path or not isinstance(path, str):
            return _error("path is required (string)", "ValidationError")
        if (not isinstance(region_size, (list, tuple))
                or len(region_size) != 2
                or not all(isinstance(v, (int, float)) for v in region_size)):
            return _error("region_size must be [w, h] (2 numbers)", "ValidationError")
        if not isinstance(prefer, str):
            return _error("prefer must be a string", "ValidationError")

        try:
            full = self._resolve_in(path)
            size_tuple: tuple[int, int] = (int(region_size[0]), int(region_size[1]))
            if status:
                await status.progress(f"Searching {full.name} for best {size_tuple[0]}x{size_tuple[1]} text region")
            result = await asyncio.to_thread(
                find_text_region, full, size_tuple, prefer, max_candidates,
            )
            payload = {"status": "success", "path": str(full), **result}
            if status:
                best = result["best"]
                r = best["region"]
                await status.end(
                    f"best region x={r[0]},y={r[1]},w={r[2]},h={r[3]} "
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
