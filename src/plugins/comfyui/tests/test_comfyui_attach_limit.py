"""An output over the attachment limit is not reported as in front of the model."""
from pathlib import Path

from agent_system.utils import multimodal_tool_content as mtc


def test_attach_problem_names_size_and_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(mtc, "DEFAULT_MAX_AUDIO_SIZE_MB", 0.001)
    big = tmp_path / "long.wav"
    big.write_bytes(b"x" * 4096)
    small = tmp_path / "short.wav"
    small.write_bytes(b"x" * 10)
    assert "over the" in mtc.attach_problem(big, "audio")
    assert mtc.attach_problem(small, "audio") is None
    assert "cannot be read" in mtc.attach_problem(Path(tmp_path / "missing.png"), "image")


def test_a_too_large_load_is_named_and_not_attached(tmp_path, monkeypatch):
    from plugins.comfyui import server as module

    monkeypatch.setattr(module, "attach_problem", lambda path, kind: "too big" if "big" in str(path) else None)
    big, ok = tmp_path / "big.png", tmp_path / "ok.png"
    big.write_bytes(b"x")
    ok.write_bytes(b"x")
    server = object.__new__(module.ComfyUIServer)  # _add_loaded reads no instance state
    loaded, multimodal = [], []
    for path in (big, ok):
        server._add_loaded(path, "image", "Loaded image", loaded, multimodal)
    assert [m["path"] for m in multimodal] == [str(ok)]
    assert loaded[0]["not_attached"] == "too big"
