"""An image goes out under the MIME subtype of its bytes, not under PIL's format name.

A camera JPEG with a second frame (a depth map, a preview) opens as "MPO" in
PIL; ``data:image/mpo`` is refused by the providers, though the bytes are a JPEG.
"""
from __future__ import annotations

from PIL import Image

from agent_system.utils.multimodal_processor import encode_image_to_data_url, validate_image_file


def test_a_multi_picture_jpeg_is_sent_as_a_jpeg(tmp_path):
    path = tmp_path / "camera.jpg"
    first, second = Image.new("RGB", (4, 4), "red"), Image.new("RGB", (4, 4), "blue")
    first.save(path, format="MPO", save_all=True, append_images=[second])
    with Image.open(path) as img:
        assert img.format == "MPO", "the fixture is no MPO: the test proves nothing"

    data_url, meta = encode_image_to_data_url(path)

    assert data_url.startswith("data:image/jpeg;base64,")
    assert meta["format"] == "jpeg"


def test_an_ordinary_png_keeps_its_format(tmp_path):
    path = tmp_path / "plain.png"
    Image.new("RGB", (2, 3)).save(path)
    assert validate_image_file(path) == ("png", (2, 3))
