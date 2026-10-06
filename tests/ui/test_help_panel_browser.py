"""The Help panel -- the kit's <pk-guide> -- in a real browser: buttons, Retrace, Browse, search, links, images.

The page is the real panel template, the real kit element and the real help
routes; the guides are fixtures -- a small manual in place of the shipped one
and one plugin with a guide and a picture -- so the checks do not follow the
manual's text. help_embed_probe.html is a plugin panel with a viewer inside.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from agent_system.ui import help as help_module
from agent_system.ui.resources import STATIC_DIR
from agent_system.ui.routes import router
from tests.ui.browser import find_browser, run_app_test_page
from tests.ui.test_help import PNG
from tests.ui.test_help_index import use_fake_model

BROWSER = find_browser()
PAGE_TIMEOUT = 120
pytestmark = [pytest.mark.skipif(BROWSER is None, reason="no Chromium-based browser installed"),
              pytest.mark.timeout(PAGE_TIMEOUT + 60)]

UI_TESTS = Path(__file__).resolve().parent

MANUAL = """@database "Probe manual"
@smartwrap
@node main "Probe manual"
Welcome to the probe.

@{" Second " link second} @{" Plugin " link probe/main 60} @{" Dead " link nowhere} @{" Web " link https://example.org/}
@endnode
@node second "Second page"
This is the second page.
@{code}
  code line one
  code line two
@{body}
@endnode
@node help "Using this help"
Help text.
@endnode
@node md "Blocks page"
@{h1}Heading
@{bullet}item one
@{code yaml}
key: 1
@{body}
@{code yaml}
next: @{" second " link second}
@{body}
@{table}
A | B
1 | 2
@{body}
@{" To second " link second} and @{" the design " link docs/design.md}
@endnode
@node big "Big code"
@{code json}
""" + '{"key": 12345, "value": "abcdefghij"},\n' * 1500 + """@{body}
@{code json}
""" + '{"key": 12345, "value": "abcdefghij"},\n' * 1500 + """@{body}
@{code yaml}
small: 1
@{body}
@endnode
"""
DESIGN = "# Design\n\nThe design file.\n\n```Python\nprint(1)\n```\n"  # a README's fence may be capitalised

#: Long enough to scroll, and wide: the manual's plugin button and "back" jump to source line 60.
WIDE = " " + "w" * 300
PLUGIN = (f'@node main "Probe plugin"\nPlugin line one.{WIDE}\n@{{image pic.png "Probe picture"}} '
          '@{" notes " link docs/notes.md}\n') + "".join(
    f"Filler line {n}.\n" for n in range(4, 60)) + "Line sixty.\n" + "".join(
    f"More filler {n}.\n" for n in range(61, 120)) + f'@endnode\n@node extra "Extra"\nExtra page. @{{" back " link main 60}}{WIDE}\n' + "".join(
    f"Extra filler {n}.\n" for n in range(100)) + "@endnode\n"
NOTES = "# Notes\n\nThe plugin's notes.\n"


def refuse_prism_once(app: FastAPI) -> None:
    """The first load of the syntax colours fails, as on a restart in between: the viewer must ask again."""
    refused: list[bool] = []

    @app.get("/static/vendor/prism/prism.js")
    def prism_after_one_refusal():
        if not refused:
            refused.append(True)
            return Response(status_code=503)
        return FileResponse(STATIC_DIR / "vendor" / "prism" / "prism.js", media_type="text/javascript")


def stub_app(plugin_dirs: Path) -> FastAPI:
    app = FastAPI()
    app.state.config = SimpleNamespace(plugins=SimpleNamespace(plugin_dirs=[str(plugin_dirs)]))
    refuse_prism_once(app)
    app.include_router(router)
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.mount("/tests/ui", StaticFiles(directory=UI_TESTS), name="ui-tests")
    return app


@pytest.fixture(scope="module")
def results(tmp_path_factory):
    base = tmp_path_factory.mktemp("help")
    manual = base / "guides"
    manual.mkdir()
    (manual / "scarabhive.guide").write_text(MANUAL, encoding="utf-8")
    (manual / "docs").mkdir()
    (manual / "docs" / "design.md").write_text(DESIGN, encoding="utf-8")
    plugin = base / "plugins" / "probe"
    plugin.mkdir(parents=True)
    (plugin / "plugin.toml").write_text('[plugin]\nname = "probe"\ndescription = "Probe"\n', encoding="utf-8")
    (plugin / "probe.guide").write_text(PLUGIN, encoding="utf-8")
    (plugin / "pic.png").write_bytes(PNG)
    (plugin / "docs").mkdir()
    (plugin / "docs" / "notes.md").write_text(NOTES, encoding="utf-8")
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(help_module, "GUIDES_DIR", manual)
        use_fake_model(patch, base / "search_index.json")  # the ranking by shared words, the index kept here
        return run_app_test_page(BROWSER, stub_app(base / "plugins"), "tests/ui/help_panel_tests.html",
                                 timeout=PAGE_TIMEOUT)


EXPECTED = [
    'the manual opens on its main node with Retrace and Browse back off',
    'a button opens its node and Retrace comes back',
    'an overtaken Retrace keeps its station',
    'Browse forward walks the file and code keeps its lines',
    'a link into a plugin guide opens it with its picture',
    'a jump to a line scrolls only the viewer, a new page starts at its top',
    'a dead link is crossed out and named below the page',
    'a web link opens in a new tab and nothing else',
    'a search lists its hits, a hit opens, Retrace returns to the hits',
    'a search naming a node shows it exactly above the ranking',
    'a node that does not exist says so and offers the manual',
    'a search as the first page still offers the manual',
    'a failed load of the colours is tried again on the next page',
    'the extended format is drawn; its buttons open a node and a Markdown file',
    'code is coloured up to a budget per page, a coloured block is no Tab stop',
    'an embedded viewer leaves the page alone and follows its attributes',
    'an embedded viewer without a height scrolls its panel, never the page around it',
]


@pytest.mark.parametrize("name", EXPECTED)
def test_help_panel(results, name):
    assert results.get(name) == "ok", results
