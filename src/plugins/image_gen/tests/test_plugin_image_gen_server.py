"""Unit tests for the image_gen tool server plugin."""
from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image

from agent_system.config.models import (
    AgentSystemConfig,
    ImageModelConfig,
    ImageProfile,
    LLMSystemConfig,
    ToolServerConfig,
)
from agent_system.llm.image import GeneratedImage, ImageResult
from plugins.image_gen.server import DEFAULT_MAX_REFERENCE_MB, ImageGenServer


def _png_bytes(size=(16, 12), mode="RGBA") -> bytes:
    buf = io.BytesIO()
    color = (0, 128, 255, 255) if mode == "RGBA" else (0, 128, 255)
    Image.new(mode, size, color).save(buf, format="PNG")
    return buf.getvalue()


def _make_server(tmp_path: Path, *, use_nested_config: bool = False, **extra) -> ImageGenServer:
    out_dir = tmp_path / "out"
    out_dir.mkdir(exist_ok=True)
    sys_cfg = AgentSystemConfig(
        llm_system=LLMSystemConfig(
            image_models={
                "m1": ImageModelConfig(provider="gemini_image", model="gemini-nano-banana-2.1"),
                "m2": ImageModelConfig(provider="openrouter_image", model="google/gemini-nano-banana-2.1"),
            },
            image_profiles={
                "nano-banana": ImageProfile(model_ref="m1", description="Direct Gemini"),
                "or-nano-banana": ImageProfile(model_ref="m2", description="OpenRouter"),
            },
        )
    )
    opts = {"output_directories": [str(out_dir)], "input_directories": [str(out_dir)], **extra}
    if use_nested_config:
        srv_cfg = ToolServerConfig(type="image_gen", enabled=True, config=opts)
    else:
        srv_cfg = ToolServerConfig(type="image_gen", enabled=True, **opts)
    return ImageGenServer("image_gen", sys_cfg, srv_cfg)


def test_schema_reflects_allowed_profiles_default_and_config_edge_cases(tmp_path):
    no_default = _make_server(tmp_path, profiles=["or-nano-banana"])
    tools = {t["function"]["name"]: t["function"] for t in no_default.get_tools()}
    assert set(tools) == {"image_gen_generate", "image_gen_list_profiles"}
    gen = tools["image_gen_generate"]["parameters"]
    assert gen["properties"]["profile"]["enum"] == ["or-nano-banana"]
    assert "profile" in gen["required"]

    with_default = _make_server(
        tmp_path, use_nested_config=True, profiles=["or-nano-banana"], default_profile="or-nano-banana",
    )
    gen_d = {t["function"]["name"]: t["function"] for t in with_default.get_tools()}["image_gen_generate"]["parameters"]
    assert "profile" not in gen_d["required"]

    # Blank entries in an explicit allowlist must fail closed, not open all profiles.
    blank_allowlist = _make_server(tmp_path, profiles=["   "])
    assert blank_allowlist._visible_profiles() == {}

    # Non-finite max_reference_size_mb falls back to default cleanly.
    inf_limit = _make_server(tmp_path, max_reference_size_mb="inf")
    assert inf_limit.max_reference_size_mb == DEFAULT_MAX_REFERENCE_MB


async def test_list_profiles_returns_only_allowed_profiles(tmp_path):
    server = _make_server(tmp_path, profiles=["or-nano-banana"], default_profile="or-nano-banana")
    res = await server.call_with_status("image_gen_list_profiles", {})
    assert res["status"] == "success"
    assert res["default_profile"] == "or-nano-banana"
    assert [p["profile"] for p in res["profiles"]] == ["or-nano-banana"]
    assert res["profiles"][0]["provider"] == "openrouter_image"


async def test_generate_writes_file_attaches_preview_and_passes_refs(tmp_path):
    server = _make_server(tmp_path, default_profile="or-nano-banana")
    ref_file = tmp_path / "out" / "sketch.png"
    ref_file.write_bytes(_png_bytes((8, 8)))
    generated_png = _png_bytes((32, 24), mode="RGBA")

    fake_client = MagicMock()
    fake_client.supports_reference_images = True
    fake_client.max_reference_images = 14
    fake_client.generate = AsyncMock(
        return_value=ImageResult(
            images=[GeneratedImage(data=generated_png, mime_type="image/png", width=32, height=24)],
            model="google/gemini-nano-banana-2.1",
            text="Done",
        )
    )

    out_jpg = tmp_path / "out" / "hero.jpg"
    with patch("plugins.image_gen.server.create_image_from_profile", return_value=fake_client):
        res = await server.call_with_status(
            "image_gen_generate",
            {
                "prompt": "Make it shiny",
                "output_path": str(out_jpg),
                "reference_images": [str(ref_file)],
                "aspect_ratio": "16:9",
                "image_size": "2K",
            },
        )

    assert res["status"] == "success", res
    assert out_jpg.is_file()
    assert res["mime_type"] == "image/jpeg"
    assert res["profile"] == "or-nano-banana"
    assert res["text"] == "Done"
    assert res["_multimodal_content"][0]["path"] == str(out_jpg.resolve())
    assert res["_multimodal_content"][0]["mime_type"] == "image/jpeg"
    call_kwargs = fake_client.generate.call_args.kwargs
    assert len(call_kwargs["reference_images"]) == 1
    assert call_kwargs["reference_images"][0].label == "sketch.png"


async def test_generate_attach_preview_false_and_oversized_preview(tmp_path):
    server = _make_server(tmp_path, default_profile="or-nano-banana")
    fake_client = MagicMock()
    fake_client.generate = AsyncMock(
        return_value=ImageResult(
            images=[GeneratedImage(data=_png_bytes(), mime_type="image/png", width=16, height=12)],
            model="google/gemini-nano-banana-2.1",
        )
    )
    out_png = tmp_path / "out" / "plain.png"
    with patch("plugins.image_gen.server.create_image_from_profile", return_value=fake_client):
        res = await server.call_with_status(
            "image_gen_generate",
            {"prompt": "p", "output_path": str(out_png), "attach_preview": False},
        )
    assert res["status"] == "success" and "_multimodal_content" not in res and "not_attached" not in res

    with patch("plugins.image_gen.server.create_image_from_profile", return_value=fake_client), \
            patch("plugins.image_gen.server.attach_problem", return_value="plain.png: over 20 MB"):
        res2 = await server.call_with_status(
            "image_gen_generate",
            {"prompt": "p", "output_path": str(out_png), "attach_preview": True},
        )
    assert res2["status"] == "success" and "_multimodal_content" not in res2
    assert res2["not_attached"] == "plain.png: over 20 MB"


async def test_generate_refuses_bad_paths_dirs_profiles_and_refs_before_billing(tmp_path):
    server = _make_server(
        tmp_path, profiles=["or-nano-banana"], default_profile="or-nano-banana",
        max_reference_size_mb=0.0001,  # ~104 bytes
    )
    factory_mock = MagicMock()
    with patch("plugins.image_gen.server.create_image_from_profile", factory_mock):
        outside_out = await server.call_with_status(
            "image_gen_generate",
            {"prompt": "x", "output_path": str(tmp_path / "escape.png")},
        )
        assert outside_out["status"] == "error" and "outside" in outside_out["error"].lower()

        dir_out = await server.call_with_status(
            "image_gen_generate",
            {"prompt": "x", "output_path": str(tmp_path / "out")},
        )
        assert dir_out["status"] == "error" and "directory" in dir_out["error"].lower()

        wrong_profile = await server.call_with_status(
            "image_gen_generate",
            {"prompt": "x", "output_path": str(tmp_path / "out" / "ok.png"), "profile": "nano-banana"},
        )
        assert wrong_profile["status"] == "error" and "not allowed" in wrong_profile["error"]
    factory_mock.assert_not_called()

    # Reference checks happen after client init (for capability limits) and before client.generate().
    fake_client = MagicMock()
    fake_client.supports_reference_images = True
    fake_client.max_reference_images = 2
    fake_client.generate = AsyncMock()

    secret_ref = tmp_path / "other.png"
    secret_ref.write_bytes(_png_bytes())
    big_ref = tmp_path / "out" / "big.png"
    big_ref.write_bytes(_png_bytes((32, 32)))
    bad_ref = tmp_path / "out" / "fake.png"
    bad_ref.write_bytes(b"not an image")

    with patch("plugins.image_gen.server.create_image_from_profile", return_value=fake_client):
        outside_in = await server.call_with_status(
            "image_gen_generate",
            {"prompt": "x", "output_path": str(tmp_path / "out" / "ok.png"), "reference_images": [str(secret_ref)]},
        )
        assert outside_in["status"] == "error" and "outside" in outside_in["error"].lower()

        missing_in = await server.call_with_status(
            "image_gen_generate",
            {"prompt": "x", "output_path": str(tmp_path / "out" / "ok.png"),
             "reference_images": [str(tmp_path / "out" / "missing.png")]},
        )
        assert missing_in["status"] == "error" and "not found" in missing_in["error"]

        oversized_in = await server.call_with_status(
            "image_gen_generate",
            {"prompt": "x", "output_path": str(tmp_path / "out" / "ok.png"), "reference_images": [str(big_ref)]},
        )
        assert oversized_in["status"] == "error" and "limit" in oversized_in["error"]

        invalid_in = await server.call_with_status(
            "image_gen_generate",
            {"prompt": "x", "output_path": str(tmp_path / "out" / "ok.png"), "reference_images": [str(bad_ref)]},
        )
        assert invalid_in["status"] == "error" and "invalid reference image" in invalid_in["error"]

        too_many = await server.call_with_status(
            "image_gen_generate",
            {"prompt": "x", "output_path": str(tmp_path / "out" / "ok.png"),
             "reference_images": [str(bad_ref), str(bad_ref), str(bad_ref)]},
        )
        assert too_many["status"] == "error" and "at most 2" in too_many["error"]

    fake_client.generate.assert_not_called()


async def test_empty_output_directories_fails_closed(tmp_path):
    server = _make_server(tmp_path, output_directories=[], default_profile="or-nano-banana")
    res = await server.call_with_status(
        "image_gen_generate",
        {"prompt": "x", "output_path": str(tmp_path / "out" / "a.png")},
    )
    assert res["status"] == "error" and "outside" in res["error"].lower()
