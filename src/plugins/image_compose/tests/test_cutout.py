"""`cutout` and `alpha_threshold` on image layers.

rembg sits behind `compositor._remove_background`; these tests replace that
seam with a fake that hands back a vertical alpha gradient, so no model is
ever loaded here. What rembg actually produces was measured live (see the
comment above the seam in compositor.py).
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from plugins.image_compose import compositor
from plugins.image_compose.compositor import CompositionError, compose


def _source(tmp_path, size=(8, 8)):
    p = tmp_path / "src.png"
    Image.new("RGB", size, (200, 30, 30)).save(p)
    return str(p)


def _fake_remove(calls: list):
    """Alpha 0 at the top row rising to 255 at the bottom, whatever the input."""
    def fake(img, model):
        calls.append((img.size, model))
        out = img.convert("RGBA")
        out.putalpha(Image.linear_gradient("L").resize(img.size))
        return out
    return fake


def _alpha(path):
    return np.asarray(Image.open(path).convert("RGBA"))[:, :, 3]


def _spec(src, **layer):
    return {"size": [8, 8], "background": "transparent",
            "layers": [{"type": "image", "src": src, "position": [0, 0], **layer}]}


def test_cutout_true_runs_the_default_model_at_source_size_and_keeps_soft_alpha(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(compositor, "_remove_background", _fake_remove(calls))
    out = tmp_path / "out.png"
    compose(_spec(_source(tmp_path, (16, 16)), cutout=True, size=[8, 8]), out, tmp_path, {}, tmp_path)
    assert calls == [((16, 16), compositor.CUTOUT_DEFAULT_MODEL)]  # before the fit to 8x8
    a = _alpha(out)
    assert a[0].max() < 40 and a[-1].min() > 200
    assert len(np.unique(a)) > 2  # the gradient survived: soft alpha is kept


def test_a_string_cutout_names_the_model(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(compositor, "_remove_background", _fake_remove(calls))
    compose(_spec(_source(tmp_path), cutout="u2net"), tmp_path / "out.png", tmp_path, {}, tmp_path)
    assert calls[0][1] == "u2net"


def test_alpha_threshold_leaves_only_opaque_or_clear_pixels_after_the_fit(tmp_path, monkeypatch):
    monkeypatch.setattr(compositor, "_remove_background", _fake_remove([]))
    out = tmp_path / "out.png"
    # 20x20 source fitted to 8x8: a threshold applied before the resample
    # would come out soft again — the resize must happen first.
    compose(_spec(_source(tmp_path, (20, 20)), cutout=True, alpha_threshold=128, size=[8, 8]),
            out, tmp_path, {}, tmp_path)
    a = _alpha(out)
    assert set(np.unique(a).tolist()) == {0, 255}
    assert a[0].max() == 0 and a[-1].min() == 255


def test_without_cutout_the_seam_is_never_touched(tmp_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(compositor, "_remove_background", _fake_remove(calls))
    out = tmp_path / "out.png"
    compose(_spec(_source(tmp_path)), out, tmp_path, {}, tmp_path)
    assert calls == []
    assert _alpha(out).min() == 255


@pytest.mark.parametrize("bad", [-1, 256, "hard", 0, False, True])
def test_an_unusable_alpha_threshold_is_refused(tmp_path, monkeypatch, bad):
    """0 and booleans included: int(False) == 0 would make the whole layer
    opaque and silently undo the cutout the threshold is meant to sharpen."""
    monkeypatch.setattr(compositor, "_remove_background", _fake_remove([]))
    with pytest.raises(CompositionError, match="alpha_threshold"):
        compose(_spec(_source(tmp_path), cutout=True, alpha_threshold=bad),
                tmp_path / "out.png", tmp_path, {}, tmp_path)


def test_a_pixel_exactly_at_the_threshold_stays(tmp_path, monkeypatch):
    """`>= t`, not `> t` — the boundary pixel is kept, not dropped."""
    p = tmp_path / "src.png"
    Image.new("RGBA", (8, 8), (200, 30, 30, 128)).save(p)  # fills the canvas
    out = tmp_path / "out.png"
    compose(_spec(str(p), alpha_threshold=128), out, tmp_path, {}, tmp_path)
    assert _alpha(out).min() == 255


def test_a_stringified_boolean_is_a_flag_not_a_model_name(tmp_path, monkeypatch):
    """An LLM writes "true"/"false" as readily as true/false. A bare string
    is a model name here, so without normalisation "true" would be looked up
    as a model and "false" would switch the cutout ON."""
    calls: list = []
    monkeypatch.setattr(compositor, "_remove_background", _fake_remove(calls))
    compose(_spec(_source(tmp_path), cutout="true"), tmp_path / "on.png", tmp_path, {}, tmp_path)
    assert calls == [((8, 8), compositor.CUTOUT_DEFAULT_MODEL)]
    compose(_spec(_source(tmp_path), cutout="false"), tmp_path / "off.png", tmp_path, {}, tmp_path)
    assert len(calls) == 1
    assert _alpha(tmp_path / "off.png").min() == 255


def test_a_stringified_boolean_trim_does_not_crop(tmp_path):
    src = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    src.paste((200, 30, 30, 255), (5, 5, 7, 7))
    p = tmp_path / "src.png"
    src.save(p)
    out = tmp_path / "out.png"
    compose(_spec(str(p), trim="false", size=[8, 8], fit="contain"), out, tmp_path, {}, tmp_path)
    assert _alpha(out).min() == 0  # untrimmed: the empty margin is still there


def test_a_contain_fit_keeps_the_cutouts_soft_alpha_and_its_colour(tmp_path):
    """Pasting an RGBA image through itself premultiplies it: alpha 200 came
    out 157 and white came out grey — the dark rim `cutout` is blamed for.
    Both skills prescribe `contain` for cutout sprites."""
    p = tmp_path / "src.png"
    Image.new("RGBA", (10, 20), (255, 255, 255, 200)).save(p)
    out = tmp_path / "out.png"
    compose(_spec(str(p), size=[10, 10], fit="contain"), out, tmp_path, {}, tmp_path)
    assert Image.open(out).convert("RGBA").getpixel((5, 5)) == (255, 255, 255, 200)


def test_the_named_models_still_exist_in_the_installed_rembg(tmp_path):
    """The one thing a dependency bump breaks silently: every test above
    replaces the rembg seam, so a renamed model would ship green. No model
    is downloaded here — the session classes are declared, not loaded."""
    sessions = pytest.importorskip("rembg.sessions")
    names = {c.name() for c in sessions.sessions_class}
    assert compositor.CUTOUT_DEFAULT_MODEL in names
    schema = (Path(__file__).parents[1] / "schema.yaml").read_text(encoding="utf-8")
    for advertised in ("u2net", "isnet-anime", "birefnet-general"):
        if advertised in schema:
            assert advertised in names, f"schema.yaml offers {advertised}, rembg has no such model"


def test_trim_crops_to_the_visible_box_before_the_fit(tmp_path):
    src = Image.new("RGBA", (8, 8), (0, 0, 0, 0))
    src.paste((200, 30, 30, 255), (5, 5, 7, 7))  # a 2x2 object bottom-right
    p = tmp_path / "src.png"
    src.save(p)
    out = tmp_path / "out.png"
    compose(_spec(str(p), trim=True, size=[8, 8], fit="contain"), out, tmp_path, {}, tmp_path)
    a = _alpha(out)
    assert a.min() == 255  # the 2x2 object was scaled up to fill the whole canvas


def test_trim_on_an_empty_image_is_an_error(tmp_path):
    p = tmp_path / "src.png"
    Image.new("RGBA", (8, 8), (0, 0, 0, 0)).save(p)
    with pytest.raises(CompositionError, match="trim"):
        compose(_spec(str(p), trim=True), tmp_path / "out.png", tmp_path, {}, tmp_path)


def test_missing_rembg_is_a_named_error_not_a_silent_opaque_layer(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "rembg", None)  # makes `from rembg import ...` raise ImportError
    monkeypatch.setattr(compositor, "_cutout_sessions", {})
    with pytest.raises(CompositionError, match="rembg"):
        compose(_spec(_source(tmp_path), cutout=True), tmp_path / "out.png", tmp_path, {}, tmp_path)
