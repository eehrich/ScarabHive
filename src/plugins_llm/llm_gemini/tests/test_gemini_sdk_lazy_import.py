"""Constructing a GeminiSDKClient must not import google.genai; the first
call must. And a missing package must still fail at construction, where the
agent turns it into a visible startup warning.

Run in a subprocess so the module state of the test process (which may have
imported the SDK for other tests) cannot fake the result either way.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]


def _run(code: str) -> str:
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        cwd=str(REPO), capture_output=True, text=True, timeout=120,
        env={**__import__("os").environ, "PYTHONPATH": str(REPO / "src")},
    )
    assert result.returncode == 0, result.stderr
    return result.stdout


def test_sdk_import_is_deferred_to_the_first_call():
    out = _run("""
        import sys
        from agent_system.llm import registry
        registry._scan_manifests()
        registry._load_plugin(registry._provider_dirs["gemini_sdk"])
        from plugins_llm.llm_gemini.gemini_sdk_client import GeminiSDKClient
        client = GeminiSDKClient(model="gemini-x", api_key="k")
        print("after_ctor", "google.genai" in sys.modules)
        client._client  # first use builds the SDK client
        print("after_use", "google.genai" in sys.modules, client._sdk_client is not None)
    """)
    assert "after_ctor False" in out, out
    assert "after_use True True" in out, out


def test_missing_sdk_fails_at_construction():
    out = _run("""
        import importlib.util
        from agent_system.llm import registry
        registry._scan_manifests()
        registry._load_plugin(registry._provider_dirs["gemini_sdk"])
        from plugins_llm.llm_gemini import gemini_sdk_client as m
        m.importlib.util.find_spec = lambda name: None if name == "google.genai" else importlib.util.find_spec(name)
        try:
            m.GeminiSDKClient(model="gemini-x", api_key="k")
        except ImportError as e:
            print("import_error", "google-genai" in str(e))
        else:
            print("no_error")
    """)
    assert "import_error True" in out, out
