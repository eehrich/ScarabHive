"""The fonts that ship with the plugin: every built-in name is a file here, with its licence."""
from __future__ import annotations

import re

import pytest
from PIL import ImageFont

from plugins.image_compose import compositor
from plugins.image_compose.compositor import BUNDLED_FONTS, BUNDLED_FONTS_DIR


@pytest.mark.parametrize("name", sorted(BUNDLED_FONTS))
def test_a_built_in_name_is_drawn_with_the_bundled_file(tmp_path, name):
    warnings: list[str] = []
    font = compositor._load_font(name, 40, tmp_path, {}, warnings)
    assert warnings == [], warnings
    assert font.path == str(BUNDLED_FONTS_DIR / BUNDLED_FONTS[name])


def test_every_bundled_font_has_its_licence_next_to_it():
    licences = {p.name.split("-")[0] for p in (BUNDLED_FONTS_DIR / "licenses").glob("*.txt")}
    files = sorted(p.name for p in BUNDLED_FONTS_DIR.glob("*.ttf"))
    assert sorted(BUNDLED_FONTS.values()) == files, "a built-in name without its file, or a file without a name"
    assert [f for f in files if re.split(r"[-_]", f)[0] not in licences] == []


def test_a_configured_alias_of_the_same_name_wins(tmp_path):
    own = tmp_path / "own.ttf"
    own.write_bytes((BUNDLED_FONTS_DIR / BUNDLED_FONTS["pixel"]).read_bytes())
    font = compositor._load_font("sans", 40, tmp_path, {"sans": "own.ttf"}, [])
    assert font.path == str(own)


def test_only_a_plain_file_name_reaches_the_fonts_folder():
    assert compositor._bundled_font("Anton-Regular.ttf") == BUNDLED_FONTS_DIR / "Anton-Regular.ttf"
    assert compositor._bundled_font("../compositor.py") is None
    assert compositor._bundled_font("licenses/Anton-OFL.txt") is None
    assert isinstance(ImageFont.truetype(str(compositor._bundled_font("blackletter")), 10), ImageFont.FreeTypeFont)
