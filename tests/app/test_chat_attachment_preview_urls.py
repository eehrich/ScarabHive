"""The object URL behind an attachment preview must outlive the image load.

`URL.createObjectURL(file)` was revoked in the image's own `onload` handler.
The picture stays visible — it is decoded by then — but `img.src` becomes a
dead reference, and the click handler opened exactly that: a blob: address
the browser no longer resolves.

There is no JS runtime in this repo, so this reads the source. It is a weak
test, but it goes red when the revoke moves back next to the creation.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

CHAT_JS = Path(__file__).parents[2] / "static" / "js" / "chat_module.js"


@pytest.fixture(scope="module")
def source() -> str:
    return CHAT_JS.read_text(encoding="utf-8")


def test_no_preview_url_is_revoked_on_load(source: str):
    offenders = re.findall(r"on(?:load|Load)\s*=\s*[^\n]*revokeObjectURL", source)
    assert not offenders, (
        f"a preview URL is revoked as soon as the image loads: {offenders} — "
        f"the click target dies with it")


def test_every_created_object_url_is_tracked(source: str):
    """An untracked URL is one nobody can release later — it lives until the
    tab closes, holding the whole file in memory."""
    created = source.count("URL.createObjectURL(")
    tracked = source.count("previewObjectUrls.add(")
    assert created == tracked, (
        f"{created} object URL(s) created, {tracked} tracked — the difference "
        f"leaks for the lifetime of the tab")
    assert created >= 2, "the previews stopped creating object URLs at all"


def test_no_click_handler_navigates_to_a_raw_attachment_address(source: str):
    """The restored history hands out data: URLs, which Chrome refuses as a
    top level navigation — the same dead link, a different cause. Both paths
    go through the opener that makes a fresh blob first."""
    raw = re.findall(r"onclick\s*=\s*\(\)\s*=>\s*\{?\s*window\.open\(", source)
    assert not raw, (
        f"{len(raw)} click handler(s) open the attachment address directly — "
        f"a data: URL is refused and a stale blob: URL resolves to nothing")
    renderer = source[source.index("function renderAttachments("):source.index("function addUser(")]
    assert "img.onclick = () => openAttachmentInNewTab(url)" in renderer, \
        "the attachment renderer's images no longer open through the opener"
    # One renderer, two callers: the message just sent and the restored history.
    assert len(re.findall(r"(?<!function )renderAttachments\(msgDiv, \{", source)) == 2, \
        "the live preview and the restored history must share the renderer"


def test_wiping_the_chat_releases_them(source: str):
    """The rows are gone, so the URLs behind them are unreachable — and the
    only place the history is wiped is the session load."""
    wipe = source.index("chatEl.innerHTML = ''")
    window = source[wipe:wipe + 200]
    assert "releasePreviewObjectUrls()" in window, (
        "the chat is cleared without releasing the preview object URLs — "
        "they leak for the lifetime of the tab")
