"""Auto-upload for LoadImage parameters (structural fix for "Invalid image file").

Agents referenced freshly generated outputs by file name, but LoadImage only
reads ComfyUI's INPUT folder — and a manual upload_image lands on the primary,
while execute picks the least-loaded server. _op_execute now resolves local
images itself (allowlist-safe) and uploads them to the exec server;
queue validation errors reach the agent with node_errors + actionable hint
instead of as a bare error message.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_system_config() -> MagicMock:
    return MagicMock()


def _make_server(mock_system_config, tmp_path, *, workflows=None):
    from plugins.comfyui.server import ComfyUIServer
    cfg = MagicMock()
    cfg.host = "127.0.0.1"
    cfg.port = 8188
    cfg.timeout_seconds = 30
    cfg.output_dir = str(tmp_path / "outputs")
    cfg.workflow_files_dir = str(tmp_path / "workflows")
    cfg.workflows = workflows or []
    cfg.upload_source_dirs = [str(tmp_path / "outputs")]
    cfg.upload_image_extensions = [".png", ".jpg"]
    return ComfyUIServer("comfyui", mock_system_config, cfg)


CAPTION_WF = [{
    "id": "caption", "name": "Caption", "workflow_file": "caption.json",
    "parameters": [{"name": "image", "type": "string", "required": True,
                    "node_id": "1", "field": "inputs.image"}],
}]


def _write_caption_workflow(tmp_path: Path) -> None:
    wf_dir = tmp_path / "workflows"
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "caption.json").write_text(json.dumps(
        {"1": {"inputs": {"image": "input.png"}, "class_type": "LoadImage"}}))


class TestResolveLocalImageSource:
    def test_direct_path(self, mock_system_config, tmp_path: Path):
        server = _make_server(mock_system_config, tmp_path)
        f = tmp_path / "outputs" / "gen_00001_.png"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"png")
        assert server._resolve_local_image_source(str(f)) == f.resolve()

    def test_bare_filename_newest_wins(self, mock_system_config, tmp_path: Path):
        server = _make_server(mock_system_config, tmp_path)
        old = tmp_path / "outputs" / "a" / "cover_00001_.png"
        new = tmp_path / "outputs" / "b" / "cover_00001_.png"
        for f in (old, new):
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_bytes(b"png")
        os.utime(old, (1000, 1000))
        os.utime(new, (2000, 2000))
        assert server._resolve_local_image_source("cover_00001_.png") == new.resolve()

    def test_rejects_outside_allowlist_and_bad_ext(self, mock_system_config, tmp_path: Path):
        server = _make_server(mock_system_config, tmp_path)
        outside = tmp_path / "elsewhere" / "x.png"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_bytes(b"png")
        assert server._resolve_local_image_source(str(outside)) is None
        bad = tmp_path / "outputs" / "x.db"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"db")
        assert server._resolve_local_image_source(str(bad)) is None

    def test_missing_path_with_separator_not_hunted(self, mock_system_config, tmp_path: Path):
        """A non-existent PATH (with a separator) must not be redirected to
        another file by a basename search."""
        server = _make_server(mock_system_config, tmp_path)
        f = tmp_path / "outputs" / "real.png"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"png")
        assert server._resolve_local_image_source("wrong/dir/real.png") is None


class TestExecuteAutoUpload:
    @pytest.mark.asyncio
    async def test_auto_uploads_loadimage_param(self, mock_system_config, tmp_path: Path):
        """E2E through _op_execute: a local output file name is uploaded to
        the exec server and the upload name is injected."""
        server = _make_server(mock_system_config, tmp_path, workflows=CAPTION_WF)
        _write_caption_workflow(tmp_path)
        local = tmp_path / "outputs" / "cover932_v1_sdxl_refined_00051_.png"
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(b"png")

        exec_client = MagicMock()
        exec_client.base_url = "http://x:1"
        exec_client.upload_image = AsyncMock(
            return_value={"name": "cover932_v1_sdxl_refined_00051_.png"})
        captured = {}

        async def fake_queue(wf_json):
            captured["wf"] = wf_json
            return {"status": "queued", "prompt_id": "p1"}
        exec_client.queue_prompt = fake_queue
        server._pick_client = AsyncMock(return_value=exec_client)

        result = await server._op_execute(
            {"workflow_id": "caption",
             "parameters": json.dumps({"image": "cover932_v1_sdxl_refined_00051_.png"})},
            status=None)
        assert result.get("status") == "queued"
        exec_client.upload_image.assert_awaited_once()
        assert captured["wf"]["1"]["inputs"]["image"] == "cover932_v1_sdxl_refined_00051_.png"

    @pytest.mark.asyncio
    async def test_leaves_unknown_filename_untouched(self, mock_system_config, tmp_path: Path):
        """A name without a local counterpart (already in the ComfyUI input)
        is injected unchanged, no upload attempt."""
        server = _make_server(mock_system_config, tmp_path, workflows=CAPTION_WF)
        _write_caption_workflow(tmp_path)

        exec_client = MagicMock()
        exec_client.base_url = "http://x:1"
        exec_client.upload_image = AsyncMock()
        exec_client.queue_prompt = AsyncMock(
            return_value={"status": "queued", "prompt_id": "p1"})
        server._pick_client = AsyncMock(return_value=exec_client)

        result = await server._op_execute(
            {"workflow_id": "caption",
             "parameters": json.dumps({"image": "already_uploaded.png"})},
            status=None)
        assert result.get("status") == "queued"
        exec_client.upload_image.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_queue_failure_surfaces_node_errors_and_hint(self, mock_system_config, tmp_path: Path):
        """Validation error: node_errors + actionable hint reach the agent
        (before, it saw only 'prompt_outputs_failed_validation' and looped
        blindly)."""
        server = _make_server(mock_system_config, tmp_path, workflows=CAPTION_WF)
        _write_caption_workflow(tmp_path)

        exec_client = MagicMock()
        exec_client.base_url = "http://x:1"
        exec_client.upload_image = AsyncMock()
        exec_client.queue_prompt = AsyncMock(return_value={
            "status": "error",
            "error": {"type": "prompt_outputs_failed_validation",
                      "message": "Prompt outputs failed validation"},
            "node_errors": {"1": {"errors": [{
                "message": "Custom validation failed for node",
                "details": "image - Invalid image file: nope.png"}]}},
        })
        server._pick_client = AsyncMock(return_value=exec_client)

        result = await server._op_execute(
            {"workflow_id": "caption",
             "parameters": json.dumps({"image": "nope.png"})},
            status=None)
        assert "error" in result
        assert "node_errors" in result
        assert "hint" in result and "uploaded automatically" in result["hint"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
