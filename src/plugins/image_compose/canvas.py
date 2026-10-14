"""Canvas layers: a script draws with the browser's canvas 2D API.

Models draw far better with the canvas API they know from the web than with
a layer vocabulary or PIL calls. A headless Chromium (Edge ships with
Windows; Chrome, Chromium or Edge elsewhere) runs the script at the process
boundary and hands the picture back through ``--dump-dom`` as a data URL.

The script is a function body with ``ctx`` (a 2D context of the layer's
size), ``w``, ``h`` and ``img`` (the layer's preloaded images) in scope. It
may ``await``. On top of the browser's API the page defines:

    Math.random            seeded -- the same seed draws the same picture
    noise(x, y, period)    smooth noise 0..1 on a unit grid; with a period
                           it repeats every ``period`` units
    fbm(x, y, octaves, period)   layered noise, same range and rule
    pixels(ctx, (x, y) => [r, g, b, a])   a per-pixel shader, 0..255
    tile(ctx, () => {...}) draws at the nine offsets of a tiling, each with
                           the same random numbers -- what crosses an edge
                           comes back in on the other side

The page has no network, no file access and no way to write a file. Three
walls, each measured 2026-10-14 against a local HTTP server and the disk:

- The page is served from a loopback address of this process, not opened
  as a file. A ``file:`` page may navigate to any ``file:`` URL -- a local
  file, and on Windows ``file://host/share``, which signs in to that host
  over SMB. An ``http:`` page may not.
- Every other address goes to a proxy nobody listens on, loopback
  included. That stops what no policy governs: a navigation, a form, a
  meta refresh. It also keeps Edge from downloading its components into
  the throwaway profile (7 MB after a run of six seconds, 54 MB without).
- The policy header: ``default-src 'none'`` (images only from ``data:``
  and ``blob:``, fonts only from ``data:``) stops a fetch, an image, a
  beacon and a DNS prefetch on its own, and ``sandbox allow-scripts``
  stops a download, which otherwise lands in the user's Downloads folder.

Not covered: WebRTC, which may send UDP past a proxy. In 11 runs without
the proxy one STUN packet reached a local listener, in 11 behind it none
-- too rare to test, so the gap is named (the guide) and not claimed
closed.

The script is inlined in the page, not loaded as a file of its own: an
error in an inline script reaches ``window.onerror`` with its line.
"""
from __future__ import annotations

import base64
import html
import http.server
import io
import json
import os
import re
import logging
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from PIL import Image

logger = logging.getLogger(__name__)

#: A script is a drawing, not a data file; bulk pixels come in as ``images``.
MAX_SCRIPT_CHARS = 200_000
DEFAULT_TIMEOUT = 60.0
#: What the browser may print: the picture as base64 inside the page. 4096 x
#: 4096 random pixels are 90 MB of it (19 s; this process peaks at 300 MB);
#: 8192 x 8192 of them do not come back within 300 s.
MAX_OUTPUT_BYTES = 512 * 1024 * 1024
#: One error as the model reads it; a script can throw a string of any length.
MAX_ERROR_CHARS = 500
#: The page's answer is two keys, a short list of errors and one long string.
#: A script can make it anything: a few MB of "[],[],[]" parse into twenty
#: times their size. The picture, base64, holds one comma.
MAX_ANSWER_COMMAS = 100_000
#: No route out, for the page and for the browser itself: every request goes
#: to a proxy nobody listens on, loopback too -- except the one address that
#: serves the page.
_OFFLINE = ["--proxy-server=http://127.0.0.1:9", "--proxy-bypass-list=<-loopback>;127.0.0.1:{port}"]

_WINDOWS = [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe"]
_MAC_APPS = ["Google Chrome.app/Contents/MacOS/Google Chrome", "Chromium.app/Contents/MacOS/Chromium",
             "Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
_ON_PATH = ("chromium", "chromium-browser", "google-chrome", "microsoft-edge")

BROWSER_MISSING = (
    "no Chromium browser found for canvas layers (Edge, Chrome or Chromium): install one, "
    "or set `browser` in this server's config to its path"
)


class CanvasError(Exception):
    """A canvas layer that cannot be drawn; the message is for the model."""


def find_browser(configured: str | None = None) -> str:
    """The configured browser (a path or a name on PATH), else Edge or Chrome
    where Windows puts them, an app in /Applications or ~/Applications, a
    Chromium on PATH, or the one Playwright downloaded."""
    if configured:
        found = configured if Path(configured).is_file() else shutil.which(configured)
        if not found:
            raise CanvasError(f"browser {configured!r} (server config) is neither a file nor a program on PATH")
        return found
    candidates = list(_WINDOWS)
    homes = [Path("/Applications")]
    try:
        home = Path.home()
    except RuntimeError:  # no HOME, USERPROFILE or passwd entry
        home = None
    if home is not None:
        homes.append(home / "Applications")
    candidates += [str(d / app) for d in homes for app in _MAC_APPS]
    for c in candidates:
        if Path(c).is_file():
            return c
    for name in _ON_PATH:
        found = shutil.which(name)
        if found:
            return found
    if home is not None:
        for root in (Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright", home / ".cache" / "ms-playwright",
                     home / "Library" / "Caches" / "ms-playwright"):
            for exe in ("chrome-win/chrome.exe", "chrome-linux/chrome",
                        "chrome-mac/Chromium.app/Contents/MacOS/Chromium"):
                hits = sorted(root.glob(f"chromium-*/{exe}"))
                if hits:
                    return str(hits[-1])
    raise CanvasError(BROWSER_MISSING)


#: Sent as a header: `sandbox` is not honoured in a <meta>.
_CSP = ("default-src 'none'; script-src 'unsafe-inline'; img-src data: blob:; font-src data:; "
        "style-src 'unsafe-inline'; sandbox allow-scripts")

_PRELUDE = """<script>
const __err = [];
window.onerror = (m, s, l, c, e) => { __err.push({line: l, msg: String(m), stack: e && e.stack || ""}); return true; };
window.addEventListener("unhandledrejection", e => __err.push({msg: String(e.reason && e.reason.message || e.reason), stack: e.reason && e.reason.stack || ""}));
let __draw = null, __s = 0;
const __seed = %(seed)d;
Math.random = function () {
  __s = (__s + 0x6D2B79F5) | 0;
  let t = Math.imul(__s ^ (__s >>> 15), 1 | __s);
  t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
  return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
};
function noise(x, y, px = 0, py = px) {
  const x0 = Math.floor(x), y0 = Math.floor(y), fx = x - x0, fy = y - y0;
  const wrap = (i, p) => p > 0 ? ((i %% p) + p) %% p : i;
  const grad = (ix, iy, dx, dy) => {
    let n = Math.imul(wrap(ix, px), 374761393) ^ Math.imul(wrap(iy, py), 668265263) ^ __seed;
    n = Math.imul(n ^ (n >>> 13), 1274126177);
    const a = ((n ^ (n >>> 16)) >>> 0) / 4294967296 * 6.283185307179586;
    return Math.cos(a) * dx + Math.sin(a) * dy;
  };
  const u = fx * fx * fx * (fx * (fx * 6 - 15) + 10), v = fy * fy * fy * (fy * (fy * 6 - 15) + 10);
  const top = grad(x0, y0, fx, fy) + (grad(x0 + 1, y0, fx - 1, fy) - grad(x0, y0, fx, fy)) * u;
  const bottom = grad(x0, y0 + 1, fx, fy - 1) + (grad(x0 + 1, y0 + 1, fx - 1, fy - 1) - grad(x0, y0 + 1, fx, fy - 1)) * u;
  return Math.min(1, Math.max(0, 0.5 + 0.7071 * (top + (bottom - top) * v)));
}
function fbm(x, y, octaves = 4, px = 0, py = px) {
  let sum = 0, amp = 1, total = 0, f = 1;
  for (let i = 0; i < octaves; i++, amp /= 2, f *= 2) { sum += amp * noise(x * f, y * f, px * f, py * f); total += amp; }
  return sum / total;
}
function pixels(ctx, fn) {
  const cw = ctx.canvas.width, ch = ctx.canvas.height, im = ctx.getImageData(0, 0, cw, ch), d = im.data;
  for (let y = 0, i = 0; y < ch; y++) for (let x = 0; x < cw; x++, i += 4) {
    const p = fn(x, y); d[i] = p[0]; d[i + 1] = p[1]; d[i + 2] = p[2]; d[i + 3] = p.length > 3 ? p[3] : 255;
  }
  ctx.putImageData(im, 0, 0);
}
function tile(ctx, fn) {
  const cw = ctx.canvas.width, ch = ctx.canvas.height, s = __s;
  for (let dy = -1; dy <= 1; dy++) for (let dx = -1; dx <= 1; dx++) {
    __s = s; ctx.save(); ctx.translate(dx * cw, dy * ch); fn(); ctx.restore();
  }
}
</script>
<script>
__draw = async function (ctx, w, h, img) { {
"""

_POST = """
} };
</script>
<script>
window.addEventListener("load", async () => {
  const out = {errors: __err};
  try {
    // A script that does not parse has said so through window.onerror, with its line.
    if (!__draw) throw new Error(__err.length ? "" : "the script did not parse");
    const c = document.createElement("canvas"); c.width = %(w)d; c.height = %(h)d;
    const img = {};
    for (const el of document.querySelectorAll("img[data-name]")) {
      if (!el.naturalWidth) throw new Error(`images.${el.dataset.name}: the browser cannot decode it`);
      img[el.dataset.name] = el;
    }
    await Promise.all(%(fonts)s.map(f => document.fonts.load(`16px "${f}"`)));
    __s = __seed;
    await __draw(c.getContext("2d"), %(w)d, %(h)d, img);
    out.png = c.toDataURL("image/png");
  } catch (e) { if (__draw || !__err.length) __err.push({msg: String(e && e.message || e), stack: e && e.stack || ""}); }
  document.getElementById("out").textContent = JSON.stringify(out);
});
</script>"""


def _page(script: str, size: tuple[int, int], images: dict[str, str], fonts: dict[str, str],
          seed: int) -> tuple[str, int]:
    """The page and the line of it the script's first line is on."""
    # A family name carries no quote and no backslash (compositor), so one
    # spelling is the same string in CSS and in the script.
    faces = "".join(f'@font-face {{ font-family: "{name}"; src: url({uri}); }}' for name, uri in fonts.items())
    imgs = "".join(f'<img data-name="{html.escape(name, quote=True)}" src="{html.escape(uri, quote=True)}">'
                   for name, uri in images.items())
    head = (f'<!doctype html><meta charset="utf-8"><style>{faces}</style><body><pre id="out"></pre>{imgs}\n'
            + _PRELUDE % {"seed": seed})
    # "</script" inside the script would end the element, and after "<!--" a
    # "<script" keeps the element open past its end tag; "<\/script" and
    # "\x3C!--" are the same JavaScript in a string, a template and a regex
    # (not in String.raw). A "<" that is itself escaped loses its backslash.
    body = re.sub(r"</(script)", r"<\\/\1", script, flags=re.I)
    body = re.sub(r"(\\*)<!--", lambda m: "\\" * (len(m.group(1)) // 2 * 2) + r"\x3C!--", body)
    post = _POST % {"w": size[0], "h": size[1], "fonts": json.dumps(list(fonts))}
    return head + body + post, head.count("\n") + 1


def _message(errors: list[dict[str, Any]], first: int, lines: int) -> str:
    """The errors as the model can act on them: the script's own line, found
    in the error or in the first stack frame that lies inside the script."""
    out = []
    for e in errors[:5]:
        if not isinstance(e, dict):     # the script can reach the list and push anything
            out.append(str(e)[:MAX_ERROR_CHARS])
            continue
        line = e.get("line")
        if not (isinstance(line, int) and first <= line < first + lines):
            stack = e.get("stack") if isinstance(e.get("stack"), str) else ""
            line = next((int(m) for m in re.findall(r"\.html:(\d{1,9}):\d+", stack)
                         if first <= int(m) < first + lines), None)
        msg = re.sub(r"^Uncaught ", "", str(e.get("msg") or "error")[:MAX_ERROR_CHARS])
        out.append(f"line {line - first + 1}: {msg}" if line is not None else msg)
    return "; ".join(out)


def render_canvas(script: str, size: tuple[int, int], images: dict[str, str] | None = None,
                  fonts: dict[str, str] | None = None, seed: int = 1, browser: str | None = None,
                  browser_args: list[str] | None = None, timeout: float = DEFAULT_TIMEOUT) -> Image.Image:
    """Run ``script`` on a canvas of ``size`` and return the picture (RGBA).

    ``images`` and ``fonts`` map a name to a ``data:`` URI. Raises CanvasError
    with the script's line for a JavaScript error, and for a script that does
    not finish within ``timeout`` seconds."""
    if not isinstance(script, str) or not script.strip():
        raise CanvasError("'script' is required: JavaScript that draws on `ctx`")
    if len(script) > MAX_SCRIPT_CHARS:
        raise CanvasError(f"script has {len(script)} characters (max {MAX_SCRIPT_CHARS}); "
                          f"pass bulk pixels as `images`, not as literals")
    exe = find_browser(browser)
    page, first = _page(script, size, images or {}, fonts or {}, seed)
    d = Path(tempfile.mkdtemp(prefix="canvas_"))
    httpd = None
    try:
        httpd = _serve(page.encode("utf-8", "replace"))       # a lone surrogate is no UTF-8
        port, url = httpd.server_address[1], httpd.url
        cmd = [exe, *(browser_args or []), "--headless=new", "--disable-gpu", "--no-first-run",
               *(flag.format(port=port) for flag in _OFFLINE), f"--user-data-dir={d / 'profile'}",
               # Without virtual time --dump-dom prints before an awaited step
               # resolves; with it, waiting costs no wall-clock time.
               f"--virtual-time-budget={int(timeout * 1000)}", "--dump-dom", f"http://127.0.0.1:{port}{url}"]
        # RunAsInvoker, as some launchers pass it on: Edge quits at once, silently.
        env = {k: v for k, v in os.environ.items() if k.upper() != "__COMPAT_LAYER"}
        # The page goes to a file, not into this process: a script can print
        # as much as it likes, and the size is known before anything is read.
        with open(d / "dom.html", "wb") as dom:
            try:
                # The timeout kills the browser process; its renderer, busy in
                # the endless script, ends with it (test_canvas_layer: no
                # process of this run is left). Measured with Edge on Windows
                # and with Debian's Chromium as root with --no-sandbox in a
                # container; without an init process there the children stay
                # as zombies, dead but unreaped.
                done = subprocess.run(cmd, stdout=dom, stderr=subprocess.PIPE, timeout=timeout, env=env)
            except subprocess.TimeoutExpired:
                raise CanvasError(f"the script ran longer than {timeout:.0f}s (an endless loop?)") from None
        if (d / "dom.html").stat().st_size > MAX_OUTPUT_BYTES:
            raise CanvasError(f"the page grew beyond {MAX_OUTPUT_BYTES // 2 ** 20} MB; a script draws, it does not print")
        res = _answer(d / "dom.html")
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        _remove(d)
    if res is None:
        tail = done.stderr.decode("utf-8", "replace").strip()[-300:]
        hint = " Chromium as root needs browser_args: [\"--no-sandbox\"]." if "no-sandbox" in tail else ""
        raise CanvasError(f"the browser returned no picture (a promise that never resolves, or the page was "
                          f"left?).{hint} {tail}".strip())
    if not isinstance(res, dict):
        raise CanvasError("the page's answer is unreadable (the script wrote into the page?)")
    if res.get("errors"):
        raise CanvasError(_message(res["errors"] if isinstance(res["errors"], list) else [res["errors"]],
                                   first, script.count("\n") + 1))
    png = _decode(res.pop("png", None))
    if png is None:
        raise CanvasError(f"the browser made no picture of {size[0]}x{size[1]} (a side beyond what a canvas can be?)")
    try:
        img = Image.open(io.BytesIO(png), formats=["PNG"])      # the header; convert() decodes the pixels
        if img.size == tuple(size):
            return img.convert("RGBA")
    except Exception:       # the script can forge the answer; what it forged is no picture
        raise CanvasError("the page's picture is unreadable (the script wrote into the page?)") from None
    raise CanvasError(f"the script resized the canvas to {img.size[0]}x{img.size[1]}; the layer is "
                      f"{size[0]}x{size[1]} -- set `size` on the layer instead")


class _Page(http.server.BaseHTTPRequestHandler):
    """Serves the one page of a run, once, with the policy as a header."""

    def do_GET(self):
        page = None
        if self.path == self.server.url:
            with self.server.lock:
                page, self.server.page = self.server.page, None
        if page is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(page)))
        if _CSP:        # empty where a test takes the wall down
            self.send_header("Content-Security-Policy", _CSP)
        self.end_headers()
        self.wfile.write(page)

    def log_message(self, *args):
        pass


class _Server(http.server.ThreadingHTTPServer):
    page: bytes | None = None
    url = ""

    def handle_error(self, request, client_address):
        # A browser that exits, or is killed, hangs up on its idle connections.
        logger.debug("canvas: a page request ended early", exc_info=True)


def _serve(page: bytes) -> _Server:
    """The run's page on a loopback port of its own. The page holds the
    layer's images and fonts, and any local process can connect: the address
    carries a token, and it answers once. (Where other users can read the
    browser's command line they can read the token, and then race it.)"""
    httpd = _Server(("127.0.0.1", 0), _Page)
    httpd.page, httpd.url, httpd.lock = page, f"/{secrets.token_urlsafe(16)}/page.html", threading.Lock()
    # The default poll of half a second is what every render would wait in shutdown().
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
    return httpd


def _answer(dom: Path) -> Any:
    """What the page wrote into ``#out``: None for nothing, the parsed JSON,
    or "" for what does not parse. Its own function so that the page text,
    several times the picture, is gone before the picture is decoded."""
    raw = dom.read_bytes()
    # Not a regex: a page of "<pre id=out>" without an end is quadratic in one.
    start = raw.find(b'<pre id="out">')
    end = raw.find(b"</pre>", start)
    body = raw[start + 14:end] if 0 <= start < end else b""
    if not body.strip():
        return None
    if body.count(b",") > MAX_ANSWER_COMMAS:
        return ""
    try:
        return json.loads(html.unescape(body.decode("utf-8", "replace")))
    except (ValueError, RecursionError):
        return ""


def _decode(data_url: Any) -> bytes | None:
    """The bytes of a ``data:`` URL the page answered with; None for none."""
    payload = data_url.partition(",")[2] if isinstance(data_url, str) else ""
    if not payload:
        return None
    try:
        return base64.b64decode(payload)
    except ValueError:
        return b""


def _remove(d: Path) -> None:
    """The run's folder. The browser's helper processes let go of the profile
    a moment after it exits; a folder that stays is said, not swallowed."""
    for _ in range(20):
        shutil.rmtree(d, ignore_errors=True)
        if not d.exists():
            return
        time.sleep(0.25)
    logger.warning("canvas: could not remove the run's folder %s", d)
