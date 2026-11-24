"""Core image generation seam: config validation, profile resolution, capabilities and saving."""
from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image
from pydantic import ValidationError

from agent_system.config.models import (
    AgentSystemConfig,
    ImageModelConfig,
    ImageProfile,
    LLMSystemConfig,
    NetworkConfig,
)
from agent_system.llm.image import (
    GeneratedImage,
    ImageClient,
    ImageInput,
    create_image_from_profile,
    detect_image_mime,
)


def _img_bytes(fmt: str = "PNG", size=(4, 4), mode="RGBA") -> bytes:
    buf = io.BytesIO()
    if fmt == "JPEG" and mode not in ("RGB", "L"):
        mode = "RGB"
    color: object = 1 if mode == "1" else ((255, 0, 0, 128) if mode == "RGBA" else (255, 0, 0))
    Image.new(mode, size, color).save(buf, format=fmt)
    return buf.getvalue()


def test_config_rejects_unknown_image_provider():
    with pytest.raises(ValueError, match="provides_image"):
        LLMSystemConfig(
            image_models={"bad": ImageModelConfig(provider="no_such_image_provider", model="m")},
        )


def test_config_rejects_dangling_image_profile():
    with pytest.raises(ValueError, match="image_profiles"):
        LLMSystemConfig(
            image_profiles={"orphan": ImageProfile(model_ref="missing")},
        )


def test_config_forbids_unknown_keys_blank_model_and_nonpositive_timeout():
    with pytest.raises(ValidationError):
        ImageModelConfig(provider="gemini_image", model="m", bogus_key=1)
    with pytest.raises(ValidationError):
        ImageProfile(model_ref="x", bogus_key=1)
    with pytest.raises(ValidationError):
        ImageModelConfig(provider="gemini_image", model="   ")
    with pytest.raises(ValidationError):
        ImageModelConfig(provider="gemini_image", model="m", request_timeout=0)


def test_shipped_config_merges_gemini_and_openrouter_image_profiles():
    from agent_system.config.settings import load_settings

    llm = load_settings().llm_system
    assert "nano-banana" in llm.image_profiles
    assert "or-nano-banana" in llm.image_profiles
    assert llm.image_models[llm.image_profiles["nano-banana"].model_ref].provider == "gemini_image"
    assert llm.image_models[llm.image_profiles["or-nano-banana"].model_ref].provider == "openrouter_image"


def test_create_image_from_profile_resolves_and_forwards_ssl_verify():
    cfg = AgentSystemConfig(
        network=NetworkConfig(ssl_verify=False),
        llm_system=LLMSystemConfig(
            image_models={"m": ImageModelConfig(provider="gemini_image", model="gemini-nano-banana-2.1")},
            image_profiles={"p": ImageProfile(model_ref="m")},
        ),
    )
    fake_client = MagicMock(spec=ImageClient)
    with patch("agent_system.llm.registry.build_image_client", return_value=fake_client) as mock_build:
        out = create_image_from_profile(cfg, "p")
    assert out is fake_client
    mock_build.assert_called_once()
    args, kwargs = mock_build.call_args
    assert args[0].model == "gemini-nano-banana-2.1"
    assert kwargs["ssl_verify"] is False


def test_create_image_from_profile_errors_on_unknown_profile():
    cfg = AgentSystemConfig(llm_system=LLMSystemConfig())
    with pytest.raises(ValueError, match="not found"):
        create_image_from_profile(cfg, "nope")


@pytest.mark.parametrize("fmt, expected", [
    ("PNG", "image/png"),
    ("JPEG", "image/jpeg"),
    ("WEBP", "image/webp"),
    ("GIF", "image/gif"),
])
def test_detect_image_mime_all_supported_containers(fmt, expected):
    raw = _img_bytes(fmt=fmt, mode="RGB")
    assert detect_image_mime(raw) == expected
    assert ImageInput(data=raw).resolved_mime() == expected


def test_image_input_rejects_mismatched_unknown_and_empty():
    png = _img_bytes()
    with pytest.raises(ValueError, match="claims 'image/jpeg'"):
        ImageInput(data=png, mime_type="image/jpeg", label="x.jpg").resolved_mime()
    with pytest.raises(ValueError, match="Unrecognised image container"):
        detect_image_mime(b"<svg></svg>")
    with pytest.raises(ValueError, match="empty"):
        ImageInput(data=b"", label="empty.png").resolved_mime()


def test_image_client_check_request_enforces_capabilities_and_types():
    class StrictClient(ImageClient):
        supports_reference_images = False
        supports_aspect_ratio = False
        supports_image_size = False
        supports_seed = False
        supports_n = False

    client = StrictClient()
    with pytest.raises(ValueError, match="non-empty"):
        client.check_request("   ")
    with pytest.raises(ValueError, match="reference_images"):
        client.check_request("cat", reference_images=[ImageInput(data=_img_bytes())])
    with pytest.raises(ValueError, match="aspect_ratio"):
        client.check_request("cat", aspect_ratio="16:9")
    with pytest.raises(ValueError, match="image_size"):
        client.check_request("cat", image_size="2K")
    with pytest.raises(ValueError, match="seed"):
        client.check_request("cat", seed=42)
    with pytest.raises(ValueError, match="seed must be an integer"):
        client.check_request("cat", seed=True)
    with pytest.raises(ValueError, match="n must be a positive integer"):
        client.check_request("cat", n=True)
    with pytest.raises(ValueError, match="n=2"):
        client.check_request("cat", n=2)

    class TwoRefClient(ImageClient):
        supports_reference_images = True
        max_reference_images = 1

    with pytest.raises(ValueError, match="at most 1"):
        TwoRefClient().check_request("cat", reference_images=[
            ImageInput(data=_img_bytes()), ImageInput(data=_img_bytes()),
        ])


def test_generated_image_save_converts_non_rgb_modes_to_jpeg_and_respects_flag(tmp_path):
    for mode in ("RGBA", "1"):
        png = _img_bytes(size=(8, 6), mode=mode)
        img = GeneratedImage(data=png, mime_type="image/png", width=8, height=6)
        target, mime, nbytes = img.save(tmp_path / f"out_{mode}.jpg")
        assert target.is_file() and mime == "image/jpeg" and nbytes > 0
        assert detect_image_mime(target.read_bytes()) == "image/jpeg"

    raw_png = _img_bytes(size=(8, 6), mode="RGB")
    img = GeneratedImage(data=raw_png, mime_type="image/png", width=8, height=6)
    target, mime, _ = img.save(tmp_path / "raw.jpg", convert_to_extension=False)
    assert mime == "image/png" and detect_image_mime(target.read_bytes()) == "image/png"
