"""The help library: every AmigaGuide database the Help panel can show, and its routes.

Where the guides come from, all read with the same parser (``amigaguide``):

* the manual, ``agent_system/ui/guides/*.guide`` -- the id is the file name
  without ``.guide``; ``scarabhive`` is the one the panel opens with,
* a plugin's own guide, ``<plugin folder>/<folder name>.guide`` -- the id is the
  folder name, which is also the plugin type (``sub_agent_manager/main``),
* a plugin without a guide but with a ``README.md``: that README, rendered as Markdown,
* ``plugins``: generated, one line per plugin in ``plugins.plugin_dirs``, linked
  to whichever of the two it has.

Files are read again when they change on disk, so the author of a guide sees an
edit with the next click and nobody restarts anything.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from agent_system.plugins.plugin_manifest import load_plugin_metadata

from .amigaguide import Guide, Node, decode, escape, file_node, inside, layout, parse, plain_text, stamp

logger = logging.getLogger(__name__)

GUIDES_DIR = Path(__file__).parent / "guides"
MANUAL = "scarabhive"
PLUGIN_INDEX = "plugins"
#: Where the Help button leads when a guide names no help node of its own.
VIEWER_HELP = (MANUAL, "help")
SEARCH_LIMIT = 50

#: What a guide may show with @{image}, and how it is served.
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif",
               ".webp": "image/webp", ".svg": "image/svg+xml"}
#: An image opened on its own (an SVG can carry script) runs nothing and loads nothing.
IMAGE_HEADERS = {"Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox"}

_cache: dict[tuple[Path, str], tuple[list[tuple[int, int] | None], Guide]] = {}


def _cached(path: Path, guide_id: str, build: Callable[[str], str], folder: Path | None = None,
            also: tuple[Path, ...] = ()) -> Guide:
    """The guide built from ``path``; parsed again when it, a file in ``also`` or a file it @embeds changed."""
    hit = _cache.get((path, guide_id))
    if hit is not None and hit[0] == [stamp(one) for one in (path, *also, *hit[1].embeds)]:
        return hit[1]
    stamps = [stamp(one) for one in (path, *also)]  # before reading: a write in between shows next time
    guide = parse(build(decode(path.read_bytes())), guide_id, folder)
    for warning in guide.warnings:
        logger.warning("%s: %s", path, warning)
    _cache[(path, guide_id)] = ([*stamps, *guide.embeds.values()], guide)
    return guide


def guide_id(reference: str) -> str:
    """The id a link names: ``HELP:x.guide``, ``../x.guide`` and ``x`` are all ``x``."""
    name = re.split(r"[/\\:]", reference)[-1]
    return (name[:-6] if name.lower().endswith(".guide") else name).lower()


@dataclass
class PluginDoc:
    id: str
    folder: Path
    description: str
    guide: Path | None
    readme: Path | None


def plugin_docs(plugin_dirs: Iterable[str | Path]) -> Iterator[PluginDoc]:
    """Every plugin (a folder with a plugin.toml) and the documentation it ships; the first of a name counts."""
    seen: set[str] = set()
    for root in map(Path, plugin_dirs):
        if not root.is_dir():
            continue
        for folder in sorted(root.iterdir()):
            metadata = load_plugin_metadata(folder) if folder.is_dir() else {}
            if not metadata or folder.name in seen:
                continue
            seen.add(folder.name)
            guide, readme = folder / f"{folder.name}.guide", folder / "README.md"
            yield PluginDoc(folder.name, folder, " ".join(str(metadata.get("description") or "").split()),
                            guide if guide.is_file() else None, readme if readme.is_file() else None)


def _readme_guide(plugin: PluginDoc) -> Callable[[str], str]:
    def build(text: str) -> str:
        return "\n".join([
            f'@database "{plugin.id}"',
            "@wordwrap",
            f'@node main "{plugin.id}"',
            f"@toc {PLUGIN_INDEX}/main",
            f"@{{b}}{escape(plugin.id)}@{{ub}} -- {escape(plugin.description)}",
            "@{fg shadow}No guide yet; this is the plugin's README.md.@{fg text}",
            "",
            "@embed README.md",  # rendered as Markdown, its links to docs/*.md open in the viewer
            "@endnode",
        ])
    return build


def _plugin_index(plugins: list[PluginDoc]) -> Guide:
    # a table keeps a long description in its cell instead of wrapping under the next name
    lines = ['@database "Plugins"', "@smartwrap", '@node main "Plugins"', f"@toc {MANUAL}/main",
             "Every plugin in @{tt}plugins.plugin_dirs@{utt}. Its name opens the plugin's guide, or its README "
             "where it has no guide yet.", "", "@{table}", "Plugin | Docs | What it does"]
    for plugin in sorted(plugins, key=lambda one: one.id):
        name = f'@{{" {plugin.id} " link "{plugin.id}/main"}}' if plugin.guide or plugin.readme else escape(plugin.id)
        kind = "guide" if plugin.guide else "README" if plugin.readme else ""
        description = escape(plugin.description).replace("|", "\\|")
        lines.append(f"{name} | {kind} | {description}")
    lines += ["@{body}", "@endnode"]
    return parse("\n".join(lines), PLUGIN_INDEX)


class Library:
    """The guides of this installation, for one request."""

    def __init__(self, plugin_dirs: Iterable[str | Path]):
        self.guides: dict[str, Guide] = {}
        for path in sorted(GUIDES_DIR.glob("*.guide")):
            self._load(guide_id(path.name), path, lambda text: text, path.parent)
        plugins = list(plugin_docs(plugin_dirs))
        for plugin in plugins:
            key = guide_id(plugin.id)  # looked up in lower case, so kept in lower case: MyPlugin/main works
            if key in self.guides or key == PLUGIN_INDEX:
                logger.warning("Help: plugin %s shares its name with a manual guide; its docs are left out", plugin.id)
            elif plugin.guide:
                self._load(key, plugin.guide, lambda text: text, plugin.folder)
            elif plugin.readme:
                # the page's head line comes from plugin.toml: a new description is a new page
                self._load(key, plugin.readme, _readme_guide(plugin), plugin.folder, also=(plugin.folder / "plugin.toml",))
        self.guides[PLUGIN_INDEX] = _plugin_index(plugins)

    def _load(self, key: str, path: Path, build: Callable[[str], str], folder: Path,
              also: tuple[Path, ...] = ()) -> None:
        try:
            self.guides[key] = _cached(path, key, build, folder, also)
        except OSError as error:  # an editor's save in between, a lock: this guide is missing, not every one
            logger.warning("Help: cannot read %s: %s", path, error)

    def resolve(self, current: str, target: str) -> tuple[str, str] | None:
        """A link target (``node`` or ``file.guide/node``) seen from guide ``current``."""
        where, _, node = target.rpartition("/")
        guide = self.guides.get(guide_id(where) if where else current)
        if guide is None or node.lower() not in guide.nodes:
            return None
        return guide.id, node.lower()

    def image_file(self, guide_name: str, path: str) -> Path | None:
        """An image at the top of a guide's folder or under its docs/, or None -- as for documentation pages,
        not whatever else lies in a plugin folder."""
        guide = self.guides.get(guide_id(guide_name))
        folder = guide.folder if guide is not None else None
        file = inside(folder, path)
        if folder is None or file is None or file.suffix.lower() not in IMAGE_TYPES:
            return None
        parts = file.relative_to(folder.resolve()).parts
        return file if len(parts) == 1 or parts[0].lower() == "docs" else None

    def _image_url(self, guide: Guide, path: str) -> str | None:
        if self.image_file(guide.id, path) is None:
            return None
        return f"/api/help/asset?{urlencode({'guide': guide.id, 'path': path})}"

    def _find(self, guide_name: str, node_name: str, file: str | None) -> tuple[Guide, Node]:
        guide = self.guides.get(guide_id(guide_name))
        if guide is None:
            raise KeyError(f"No guide {guide_name!r}")
        node = guide.nodes.get(node_name.lower()) if file is None else file_node(guide, file)
        if node is None:
            raise KeyError(f"No node {node_name!r} in {guide.id}" if file is None
                           else f"No documentation file {file!r} that guide {guide.id} links to")
        return guide, node

    def _target(self, guide: Guide, name: str | None) -> dict[str, str] | None:
        found = self.resolve(guide.id, name) if name else None
        return {"guide": found[0], "node": found[1]} if found else None

    def page(self, guide_name: str, node_name: str = "main", file: str | None = None) -> dict[str, Any]:
        """One node laid out, with where each of the viewer's buttons leads from it.

        ``file``: instead of a node, a documentation file next to the guide (what a link in a
        README opens) -- a page of its own, outside the guide's reading order.
        """
        guide, node = self._find(guide_name, node_name, file)
        order = [key for key, one in guide.nodes.items() if not one.generated]
        key = node.name.lower()
        at = order.index(key) if key in order else None
        neighbour = {"prev": order[at - 1] if at else None,
                     "next": order[at + 1] if at is not None and at + 1 < len(order) else None}
        contents = self._target(guide, node.toc or "main") or self._target(guide, order[0] if order else None)
        laid_out = layout(node, guide, lambda target: self.resolve(guide.id, target),
                          lambda path: self._image_url(guide, path))
        return {
            "guide": guide.id,
            "node": None if file is not None else key,
            "file": file,
            "title": node.title,
            "database": guide.title,
            "author": guide.author,
            "version": guide.version,
            "nav": {
                "contents": contents,
                "index": self._target(guide, node.index or guide.index),
                "help": self._target(guide, node.help or guide.help)
                        or {"guide": VIEWER_HELP[0], "node": VIEWER_HELP[1]},
                "prev": self._target(guide, node.prev or neighbour["prev"]),
                "next": self._target(guide, node.next or neighbour["next"]),
            },
            **laid_out,
        }

    def search(self, query: str) -> list[dict[str, str]]:
        """Nodes holding every word of ``query``; a title hit first."""
        terms = query.lower().split()
        if not terms:
            return []
        hits: list[tuple[int, dict[str, str]]] = []
        for guide in self.guides.values():
            for key, node in guide.nodes.items():
                if node.generated:
                    continue
                if node.search_text is None:  # rendering Markdown for every search costs 0.7 s over all READMEs
                    node.search_text = plain_text(layout(node, guide, lambda _target: None))
                text = node.search_text
                haystack = f"{node.title}\n{text}".lower()
                if not all(term in haystack for term in terms):
                    continue
                in_title = all(term in node.title.lower() for term in terms)
                hits.append((0 if in_title else 1, {
                    "guide": guide.id, "node": key, "title": node.title,
                    "database": guide.title, "snippet": _snippet(text, terms[0]),
                }))
        hits.sort(key=lambda hit: hit[0])  # stable: the manual before the plugins, file order within
        return [hit for _, hit in hits]


def _snippet(text: str, term: str, width: int = 140) -> str:
    flat = " ".join(text.split())
    at = flat.lower().find(term)
    start = max(0, at - width // 3) if at >= 0 else 0
    return ("…" if start else "") + flat[start:start + width] + ("…" if start + width < len(flat) else "")


router = APIRouter(tags=["help"])


def _library(request: Request) -> Library:
    plugins = request.app.state.config.plugins  # None in a configuration without plugins
    return Library(plugins.plugin_dirs or [] if plugins is not None else [])


# Plain def: reading and parsing guides is file work, FastAPI runs it off the event loop.
@router.get("/api/help/node")
def help_node(request: Request, guide: str = MANUAL, node: str = "main", file: str | None = None) -> dict[str, Any]:
    """One node of one guide -- or, with ``file``, a documentation file next to it -- laid out for the viewer."""
    try:
        return _library(request).page(guide, node, file)
    except KeyError as error:
        raise HTTPException(status_code=404, detail=error.args[0]) from None


@router.get("/api/help/asset")
def help_asset(request: Request, guide: str, path: str) -> FileResponse:
    """An image a guide shows with @{image}: only from the guide's own folder, only an image type."""
    file = _library(request).image_file(guide, path)
    if file is None:
        raise HTTPException(status_code=404, detail=f"No image {path!r} next to guide {guide!r}")
    return FileResponse(file, media_type=IMAGE_TYPES[file.suffix.lower()], headers=IMAGE_HEADERS)


@router.get("/api/help/search")
def help_search(request: Request, q: str = "") -> dict[str, Any]:
    """Nodes in every guide that contain all words of ``q``."""
    hits = _library(request).search(q)
    return {"query": q, "hits": hits[:SEARCH_LIMIT], "total": len(hits)}
