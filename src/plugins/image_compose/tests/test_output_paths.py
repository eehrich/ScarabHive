"""Where a write path lands.

One rule: relative to the project root, or absolute -- the same one the read
side uses and the one the schema states. There used to be a second, an
``output_root`` the server prepended, and the two disagreed whenever it was
not the project root. Measured in a real session (2026-09-04): a sub-agent
passed the project-relative path it had been briefed with, the root was
prepended, and the PNG landed at ``<root>/data/workspace/images/x.png``.
Inside the sandbox, so nothing refused it; the agent then looked for its
asset where it had meant to put it, and reported the doubled path upward.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from plugins.image_compose.server import PLUGIN_FACTORY

SPEC = {"size": [8, 8], "background": "#ffffff",
        "layers": [{"type": "rect", "rect": [0, 0, 4, 4], "fill": "#ff0000"}]}
WORKSPACE = "data/workspace/images"


@pytest.fixture
def server(tmp_path):
    """The production geometry of the image agent's instance: a sandbox
    directory inside the project root.

    ``project_root`` is assigned after construction because ``__init__``
    reads ``Path.cwd()``; everything else is passed absolute, so nothing in
    the result depends on the real working directory.
    """
    out = tmp_path / WORKSPACE
    out.mkdir(parents=True)
    config = ToolServerConfig(type="image_compose", enabled=True,
                       fonts_dir=str(tmp_path / "fonts"),
                       output_directories=[str(out)])
    srv = PLUGIN_FACTORY(name="images", system_config=AgentSystemConfig(), server_config=config)
    srv.project_root = tmp_path
    return srv


@pytest.fixture
def workspace(server) -> Path:
    return server.project_root / WORKSPACE


async def render(server, **params):
    return await server.render({"spec": SPEC, "include_content": False, "layers_dir": "", **params})


async def test_the_briefed_project_path_is_where_the_file_lands(server, workspace):
    result = await render(server, output_path=f"{WORKSPACE}/sky.png")
    assert result["status"] == "success", result
    assert Path(result["output_path"]) == workspace / "sky.png"
    assert (workspace / "sky.png").is_file()
    assert not (workspace / "data").exists()


async def test_a_subdirectory_is_created_and_written(server, workspace):
    result = await render(server, output_path=f"{WORKSPACE}/tiles/sky.png")
    assert Path(result["output_path"]) == workspace / "tiles" / "sky.png"
    assert (workspace / "tiles" / "sky.png").is_file()


async def test_the_layer_directory_and_the_spec_follow_the_same_rule(server, workspace):
    result = await render(server, output_path=f"{WORKSPACE}/sky.png",
                          layers_dir=f"{WORKSPACE}/sky_layers",
                          spec_path=f"{WORKSPACE}/sky.json")
    assert result["status"] == "success", result
    assert (workspace / "sky_layers").is_dir()
    assert (workspace / "sky.json").is_file()
    assert not (workspace / "data").exists()


async def test_the_auto_derived_layer_directory_lands_next_to_the_composite(server, workspace):
    """The third write, and the one nobody passes: it must follow the
    composite, not a base directory of its own."""
    result = await render(server, output_path=f"{WORKSPACE}/sky.png", layers_dir=None)
    assert result["status"] == "success", result
    assert (workspace / "sky_layers").is_dir()
    assert [Path(p).parent for p in result["layer_files"]] == [workspace / "sky_layers"]


async def test_a_bare_name_is_refused_by_the_sandbox_not_silently_relocated(server):
    """It resolves to the project root, which is outside the sandbox. An
    error sends the agent back with a fixable message; a silent relocation
    is what cost a session."""
    result = await render(server, output_path="sky.png")
    assert result["status"] == "error" and "output_path" in result["error"]
    assert not (server.project_root / "sky.png").exists()


async def test_a_path_into_another_agents_tree_is_refused(server):
    result = await render(server, output_path="data/writer/covers/sneak.png")
    assert result["status"] == "error" and "output_path" in result["error"]
    assert not (server.project_root / "data" / "writer").exists()


async def test_layers_dir_may_not_be_the_directory_the_composite_goes_to(server, workspace):
    """compose() clears layer_*.png there before writing; pointed at the
    delivery directory it deletes delivered assets and still says success."""
    delivered = workspace / "layer_grass.png"
    Image.new("RGB", (4, 4), (0, 200, 0)).save(delivered)
    result = await render(server, output_path=f"{WORKSPACE}/sky.png", layers_dir=WORKSPACE)
    assert result["status"] == "error" and "layers_dir" in result["error"]
    assert delivered.is_file()


async def test_an_absolute_path_inside_the_sandbox_is_taken_as_given(server, workspace):
    inside = workspace / "abs.png"
    result = await render(server, output_path=str(inside))
    assert Path(result["output_path"]) == inside
    assert inside.is_file()


async def test_an_unconfined_instance_still_writes_where_it_is_told(tmp_path):
    """The writer's cover pipeline: no sandbox, project-relative paths --
    exactly what it passed before, landing exactly where it did."""
    config = ToolServerConfig(type="image_compose", enabled=True, fonts_dir=str(tmp_path / "fonts"))
    srv = PLUGIN_FACTORY(name="images", system_config=AgentSystemConfig(), server_config=config)
    srv.project_root = tmp_path
    result = await render(srv, output_path="data/writer/covers/book.png")
    assert result["status"] == "success", result
    assert Path(result["output_path"]) == tmp_path / "data" / "writer" / "covers" / "book.png"
    assert (tmp_path / "data" / "writer" / "covers" / "book.png").is_file()
