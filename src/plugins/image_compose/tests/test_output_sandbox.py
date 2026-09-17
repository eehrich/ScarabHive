"""The optional write sandbox of image_compose.

Three things get written by one render call -- the composite, a directory of
per-layer PNGs, and optionally the spec -- and every one of them takes a
path the model chose. With ``output_directories`` set, none of the three
may resolve outside; with it empty (the default), nothing changes for the
writer's cover pipeline, which writes into data/writer/covers.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from plugins.image_compose.server import PLUGIN_FACTORY

SPEC = {"size": [8, 8], "background": "#ffffff",
        "layers": [{"type": "rect", "rect": [0, 0, 4, 4], "fill": "#ff0000"}]}


def make_server(tmp_path: Path, confined: bool):
    """Write paths are project-relative, so the test's project root is
    tmp_path and the sandbox a directory inside it."""
    allowed = tmp_path / "allowed"
    allowed.mkdir(exist_ok=True)
    kwargs = {"output_directories": [str(allowed)]} if confined else {}
    config = ToolServerConfig(type="image_compose", enabled=True,
                       fonts_dir=str(tmp_path / "fonts"), **kwargs)
    srv = PLUGIN_FACTORY(name="images", system_config=AgentSystemConfig(), server_config=config)
    srv.project_root = tmp_path
    return srv, allowed


async def render(server, **params):
    return await server.render({"spec": SPEC, "include_content": False, "layers_dir": "", **params})


async def test_a_composite_inside_the_sandbox_is_written(tmp_path):
    server, allowed = make_server(tmp_path, confined=True)
    result = await render(server, output_path="allowed/sub/ok.png")
    assert result["status"] == "success", result
    assert Path(result["output_path"]) == allowed / "sub" / "ok.png"
    assert (allowed / "sub" / "ok.png").is_file()


@pytest.mark.parametrize("escape", ["../escape.png", "../../escape.png", "sub/../../escape.png"])
async def test_a_composite_outside_the_sandbox_is_refused_before_anything_is_created(tmp_path, escape):
    server, allowed = make_server(tmp_path, confined=True)
    result = await render(server, output_path=escape)
    assert result["status"] == "error" and "outside the allowed output directories" in result["error"]
    assert not (tmp_path / "escape.png").exists()
    assert not list(tmp_path.glob("escape*")), "no file, no layer directory"


async def test_an_absolute_path_elsewhere_is_refused_too(tmp_path):
    server, _ = make_server(tmp_path, confined=True)
    target = tmp_path / "elsewhere" / "x.png"
    result = await render(server, output_path=str(target))
    assert result["status"] == "error" and not target.exists()


async def test_the_layer_directory_and_the_spec_are_confined_as_well(tmp_path):
    """The composite alone being inside is not enough: the other two writes
    take their own paths."""
    server, allowed = make_server(tmp_path, confined=True)
    result = await render(server, output_path="allowed/ok.png", layers_dir="../layers_out")
    assert result["status"] == "error" and "layers_dir" in result["error"]
    assert not (tmp_path / "layers_out").exists()
    result = await render(server, output_path="allowed/ok.png", spec_path="../spec.json")
    assert result["status"] == "error" and "spec_path" in result["error"]
    assert not (tmp_path / "spec.json").exists()
    # Refused BEFORE the render: a composite left behind under an error
    # result is the silent half of the failure.
    assert not (allowed / "ok.png").exists()
    assert not (allowed / "ok_layers").exists()


async def test_without_the_setting_writes_go_wherever_they_are_told(tmp_path):
    """Counter-check: the default is unrestricted, or the writer's covers
    would start failing the day this shipped."""
    server, allowed = make_server(tmp_path, confined=False)
    assert server.output_directories == []
    result = await render(server, output_path="elsewhere/free.png")
    assert result["status"] == "success" and (tmp_path / "elsewhere" / "free.png").is_file()
