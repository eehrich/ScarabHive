"""Canvas layers: a script draws in a headless Chromium (canvas.py).

Every test here starts a real browser through the real tool call -- the
harness page, the process boundary and the way back through ``--dump-dom``
are what could break. Without a Chromium on the host the tests skip; the
refusals that need none run always.
"""
from __future__ import annotations

import base64
import http.server
import io
import json
import logging
import sys
import threading
import time
import uuid
from pathlib import Path

import psutil
import pytest
from PIL import Image, ImageFont

from agent_system.config.models import AgentSystemConfig, ToolServerConfig
from plugins.image_compose import canvas, compositor
from plugins.image_compose.server import PLUGIN_FACTORY

try:
    BROWSER: str | None = canvas.find_browser()
except canvas.CanvasError:
    BROWSER = None
needs_browser = pytest.mark.skipif(BROWSER is None, reason="no Chromium browser on this host")

RED, GREEN, CLEAR = (255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 0, 0)


def make_server(tmp_path, **config):
    cfg = ToolServerConfig(type="image_compose", enabled=True, fonts_dir=str(tmp_path / "fonts"), **config)
    server = PLUGIN_FACTORY(name="images", system_config=AgentSystemConfig(), server_config=cfg)
    server.project_root = tmp_path
    return server


async def draw(tmp_path, layer, size=(64, 48), server=None, **spec):
    """One render call with a single canvas layer; the result and, on
    success, the picture."""
    server = server or make_server(tmp_path)
    out = tmp_path / "o.png"
    result = await server.call_with_status("images_render", {
        "spec": {"size": list(size), "layers": [{"type": "canvas", **layer}], **spec},
        "output_path": str(out), "layers_dir": "", "include_content": False})
    return result, (Image.open(out).convert("RGBA") if result["status"] != "error" else None)


@needs_browser
async def test_a_canvas_layer_is_what_the_script_drew_at_the_layer_size_and_position(tmp_path):
    result, img = await draw(tmp_path, {
        "size": [20, 10], "position": [30, 5],
        "script": "if (w !== 20 || h !== 10) throw new Error(`size ${w}x${h}`);\n"
                  "ctx.fillStyle = '#f00'; ctx.fillRect(0, 0, 5, h);"})
    assert result["status"] == "success", result
    assert img.size == (64, 48)
    assert img.getpixel((30, 5)) == RED and img.getpixel((34, 14)) == RED
    assert img.getpixel((35, 5)) == CLEAR, "the layer is wider than what was drawn"
    assert img.getpixel((29, 5)) == CLEAR and img.getpixel((30, 15)) == CLEAR, "drawn outside the layer"


@needs_browser
async def test_a_canvas_layer_without_a_size_fills_the_canvas(tmp_path):
    result, img = await draw(tmp_path, {"script": "ctx.fillStyle = '#0f0'; ctx.fillRect(0, 0, w, h);"})
    assert result["status"] == "success", result
    assert img.getpixel((0, 0)) == GREEN and img.getpixel((63, 47)) == GREEN


@needs_browser
@pytest.mark.parametrize("script, line, word", [
    ("ctx.fillRect(0, 0, 1, 1);\n\nnope();", 3, "nope is not defined"),
    ("ctx.fillRect(0, 0, 1, 1);\nctx.fillRect(0, 0, 1, 1;\n", 2, "SyntaxError"),
    ("pixels(ctx, (x, y) => {\n  if (x === 3) undefinedThing();\n  return [0, 0, 0];\n});", 2,
     "undefinedThing is not defined"),
    ("await new Promise(r => setTimeout(r, 5));\nawait Promise.reject(new Error('late'));", 2, "late"),
    ("ctx.fillRect(0, 0, 1, 1);\r\n\r\nnope();", 3, "nope is not defined"),
], ids=["runtime", "syntax", "inside-a-helper", "after-await", "crlf"])
async def test_a_script_error_names_the_line_of_the_script(tmp_path, script, line, word):
    result, _ = await draw(tmp_path, {"script": script})
    assert result["status"] == "error" and result["error_type"] == "CompositionError", result
    assert result["error"].startswith(f"layer 0 (canvas): canvas layer: line {line}: "), result["error"]
    assert word in result["error"], result["error"]
    assert not (tmp_path / "o.png").exists(), "an error result left a file behind"


@needs_browser
async def test_an_endless_script_is_stopped_and_leaves_no_browser_behind(tmp_path, monkeypatch):
    server = make_server(tmp_path, canvas_timeout=4)
    profiles: list[str] = []
    real_popen = canvas.subprocess.Popen

    def recording_popen(cmd, *args, **kwargs):
        # This run's browser and its children all carry its profile folder.
        profiles.extend(a.split("=", 1)[1] for a in cmd if a.startswith("--user-data-dir="))
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(canvas.subprocess, "Popen", recording_popen)
    started = time.monotonic()
    result, _ = await draw(tmp_path, {"script": "for (;;) {}"}, server=server)
    assert result["status"] == "error" and "longer than 4s" in result["error"], result
    assert time.monotonic() - started < 20
    assert len(profiles) == 1, profiles
    left = [p.info for p in psutil.process_iter(["pid", "name", "cmdline"])
            if profiles[0] in " ".join(p.info["cmdline"] or [])]
    assert not left, f"the browser outlived its timeout: {left}"
    assert not Path(profiles[0]).parent.exists(), "the run's folder stayed behind"


@needs_browser
@pytest.mark.parametrize("script", [
    "for (let i = 0; i < 40; i++) { ctx.fillStyle = `rgb(${Math.random() * 255 | 0}, 0, 0)`; "
    "ctx.fillRect(Math.random() * w | 0, Math.random() * h | 0, 4, 4); }",
    "pixels(ctx, (x, y) => { const v = noise(x / 8, y / 8) * 255; return [v, v, v]; });",
], ids=["random", "noise"])
async def test_math_random_and_noise_are_seeded_so_a_seed_is_a_picture(tmp_path, script):
    pictures = []
    for seed in (7, 7, 8):
        result, img = await draw(tmp_path, {"script": script, "seed": seed})
        assert result["status"] == "success", result
        pictures.append(img.tobytes())
    assert pictures[0] == pictures[1], "the same seed drew two pictures"
    assert pictures[0] != pictures[2], "another seed drew the same picture"


@needs_browser
async def test_waiting_costs_no_real_time_and_a_promise_that_never_resolves_is_an_error(tmp_path):
    started = time.monotonic()
    result, img = await draw(tmp_path, {
        "script": "await new Promise(r => setTimeout(r, 30000));\nctx.fillStyle = '#0f0'; ctx.fillRect(0, 0, w, h);"})
    assert result["status"] == "success", result
    assert img.getpixel((5, 5)) == GREEN, "the picture was taken before the script had finished"
    assert time.monotonic() - started < 20, "thirty seconds of setTimeout were waited out"

    result, _ = await draw(tmp_path, {"script": "await new Promise(() => {});"})
    assert result["status"] == "error" and "the browser returned no picture" in result["error"], result


@needs_browser
async def test_noise_with_a_period_repeats_exactly_and_without_one_does_not(tmp_path):
    # 65 pixels over a period of 64: column and row 64 are column and row 0 of the next tile.
    shader = "pixels(ctx, (x, y) => { const v = %s * 255; return [v, v, v]; });"
    seamless = {}
    for name, call in (("noise", "noise(x / 16, y / 16, 4)"), ("fbm", "fbm(x / 16, y / 16, 3, 4)"),
                       ("free", "noise(x / 16, y / 16)")):
        result, img = await draw(tmp_path, {"script": shader % call}, size=(65, 65))
        assert result["status"] == "success", result
        seamless[name] = (
            all(img.getpixel((0, y)) == img.getpixel((64, y)) for y in range(64)),
            all(img.getpixel((x, 0)) == img.getpixel((x, 64)) for x in range(64)))
        values = {img.getpixel((x, y))[0] for x in range(0, 64, 3) for y in range(0, 64, 3)}
        assert len(values) > 20, f"{name} is flat: {sorted(values)}"
    assert seamless == {"noise": (True, True), "fbm": (True, True), "free": (False, False)}


@needs_browser
async def test_tile_brings_back_what_crosses_an_edge_with_the_same_random_numbers(tmp_path):
    # A square that sticks out on the right by half, in a random red.
    shape = "ctx.fillStyle = `rgb(${50 + Math.random() * 200 | 0}, 0, 0)`; ctx.fillRect(w - 8, 10, 16, 16);"
    result, img = await draw(tmp_path, {"script": f"tile(ctx, () => {{ {shape} }});"})
    assert result["status"] == "success", result
    right, left = img.getpixel((60, 12)), img.getpixel((3, 12))
    assert right[3] == 255 and right == left, f"right {right}, wrapped in on the left {left}"
    assert img.getpixel((12, 12)) == CLEAR

    result, img = await draw(tmp_path, {"script": shape})
    assert result["status"] == "success", result
    assert img.getpixel((60, 12))[3] == 255 and img.getpixel((3, 12)) == CLEAR, "the control drew on both sides too"


@needs_browser
async def test_images_arrive_in_the_script_from_a_path_a_data_uri_and_svg_markup(tmp_path):
    Image.new("RGBA", (8, 8), RED).save(tmp_path / "red.png")
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="8" height="8">'
           '<rect width="8" height="8" fill="#00ff00"/></svg>')
    uri = "data:image/png;base64," + base64.b64encode((tmp_path / "red.png").read_bytes()).decode()
    result, img = await draw(tmp_path, {
        "images": {"file": "red.png", "uri": uri, "vector": svg},
        "script": "ctx.drawImage(img.file, 0, 0, 16, 16); ctx.drawImage(img.vector, 16, 0, 16, 16);\n"
                  "ctx.drawImage(img.uri, 32, 0, 16, 16);"})
    assert result["status"] == "success", result
    assert [img.getpixel((x, 8)) for x in (8, 24, 40, 56)] == [RED, GREEN, RED, CLEAR]


@needs_browser
async def test_an_image_the_browser_cannot_decode_is_an_error_not_an_empty_drawing(tmp_path):
    result, _ = await draw(tmp_path, {"images": {"bad": "<svg this is not xml"},
                                      "script": "ctx.drawImage(img.bad, 0, 0);"})
    assert result["status"] == "error", result
    assert "images.bad: the browser cannot decode it" in result["error"], result["error"]


@needs_browser
@pytest.mark.parametrize("script", [
    "const s = '</script><p>x</p></SCRIPT>';\n"
    "const same = s === '<' + '/script><p>x</p><' + '/SCRIPT>';",
    # After "<!--" a "<script" keeps the element open past its end tag: nothing would run.
    # ... and a "<" that is escaped itself, or follows an escaped backslash, stays what it was.
    "const s = '<!--<script>', t = `<!--`, r = /<!--/.test(s) && /\\<!--/.test(s), u = '\\\\<!--';\n"
    "const same = r && t.length === 4 && u.length === 5 && s === '<' + '!--<' + 'script>';",
], ids=["end-tag", "comment-and-start-tag"])
async def test_a_script_may_contain_the_text_that_would_end_or_swallow_its_script_element(tmp_path, script):
    result, img = await draw(tmp_path, {
        "script": script + "\nctx.fillStyle = same ? '#0f0' : '#f00'; ctx.fillRect(0, 0, w, h);"})
    assert result["status"] == "success", result
    assert img.getpixel((5, 5)) == GREEN


@needs_browser
async def test_a_font_of_the_fonts_directory_is_loaded_before_the_script_draws(tmp_path):
    try:
        system_font = ImageFont.truetype("arial.ttf").path
    except OSError:
        try:
            system_font = ImageFont.truetype("DejaVuSans.ttf").path
        except OSError:
            pytest.skip("no TrueType font on this host to copy")
    fonts = tmp_path / "fonts"
    fonts.mkdir()
    for name in ("Brand.ttf", "Gr\u00fc\u00dfe.ttf"):
        (fonts / name).write_bytes(open(system_font, "rb").read())
    server = make_server(tmp_path, font_aliases={"brand": "Brand.ttf", 'bad"name': "Brand.ttf"})
    # document.fonts.check() is true for a family nobody declared; the face's own status is not.
    script = ("if (![...document.fonts].some(f => f.family.replace(/\"/g, '') === '%s' && f.status === 'loaded'))\n"
              "  throw new Error('font not loaded');\n"
              "ctx.font = '20px \"%s\"'; ctx.fillStyle = '#f00'; ctx.fillText('Hg', 4, 30);")
    # The family is the alias, the file's name (of an absolute path too), or
    # a built-in name of the text layer -- `sans` is the bundled Inter.
    for name, fonts_field in (("brand", ["brand"]), ("Brand.ttf", ["Brand.ttf"]),
                              ("Brand.ttf", [str(fonts / "Brand.ttf")]), ("sans", ["sans"]), ("blackletter", ["blackletter"]),
                              ("Gr\u00fc\u00dfe.ttf", ["Gr\u00fc\u00dfe.ttf"])):
        result, img = await draw(tmp_path, {"fonts": fonts_field, "script": script % (name, name)}, server=server)
        assert result["status"] == "success", (fonts_field, result)
        assert img.getbbox() is not None, "no text was drawn"
    result, _ = await draw(tmp_path, {"fonts": ['bad"name'], "script": "ctx.fillRect(0, 0, 1, 1);"}, server=server)
    assert result["status"] == "error" and "may not contain a quote" in result["error"], result
    result, _ = await draw(tmp_path, {"script": script % ("brand", "brand")}, server=server)
    assert result["status"] == "error" and "font not loaded" in result["error"], \
        "the font is there without being asked for -- the check above proves nothing"


@needs_browser
@pytest.mark.parametrize("wall", ["all", "policy-alone", "none"])
async def test_the_page_reaches_neither_the_network_nor_the_disk(tmp_path, monkeypatch, wall):
    # The policy on its own still stops what it governs; with no wall the
    # requests arrive, which is what makes the empty list above mean something.
    if wall != "all":
        monkeypatch.setattr(canvas, "_OFFLINE", [])
    if wall == "none":
        monkeypatch.setattr(canvas, "_CSP", "")
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *args):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    try:
        result, img = await draw(tmp_path, {"script": f"""
new Image().src = "{base}/img";
try {{ await fetch("{base}/fetch", {{mode: "no-cors"}}); }} catch (e) {{}}
try {{ navigator.sendBeacon("{base}/beacon", "x"); }} catch (e) {{}}
let read = "no";
try {{ read = await (await fetch("{secret.as_uri()}")).text(); }} catch (e) {{}}
await new Promise(r => setTimeout(r, 300));
ctx.fillStyle = read === "secret" ? "#f00" : "#0f0"; ctx.fillRect(0, 0, w, h);"""})
        time.sleep(0.3)
    finally:
        httpd.shutdown()
    assert result["status"] == "success", result
    assert sorted(hits) == (["/beacon", "/fetch", "/img"] if wall == "none" else []), f"{wall}: {hits}"
    assert img.getpixel((5, 5)) == GREEN, "the page read a file from disk"


@needs_browser
async def test_a_render_leaves_no_folder_behind(tmp_path, monkeypatch):
    commands: list[list[str]] = []
    real_popen = canvas.subprocess.Popen

    def recording_popen(cmd, *args, **kwargs):
        commands.append(list(cmd))
        return real_popen(cmd, *args, **kwargs)

    monkeypatch.setattr(canvas.subprocess, "Popen", recording_popen)
    result, _ = await draw(tmp_path, {"script": "ctx.fillRect(0, 0, 1, 1);"})
    assert result["status"] == "success", result
    [cmd] = commands
    [profile] = [a.split("=", 1)[1] for a in cmd if a.startswith("--user-data-dir=")]
    assert not Path(profile).parent.exists(), "the run's folder stayed behind"


@needs_browser
@pytest.mark.parametrize("leave", [
    'location.href = "{base}/nav";',
    'const f = document.createElement("form"); f.method = "POST"; f.action = "{base}/form";\n'
    'document.body.appendChild(f); f.submit();',
    'const m = document.createElement("meta"); m.httpEquiv = "refresh"; m.content = "0;url={base}/meta";\n'
    'document.head.appendChild(m);',
], ids=["location", "form", "meta-refresh"])
@pytest.mark.parametrize("walls", [True, False], ids=["walled", "open"])
async def test_a_page_that_tries_to_leave_reaches_nobody(tmp_path, monkeypatch, leave, walls):
    if not walls:       # the control: without proxy and policy each of them arrives
        monkeypatch.setattr(canvas, "_OFFLINE", [])
        monkeypatch.setattr(canvas, "_CSP", "")
    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        do_POST = do_GET

        def log_message(self, *args):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        result, _ = await draw(tmp_path, {
            "script": leave.replace("{base}", base) + "\nawait new Promise(r => setTimeout(r, 2000));"})
        time.sleep(0.5)
    finally:
        httpd.shutdown()
    assert bool(hits) != walls, f"walls {walls}: {hits} ({result.get('status')})"


@needs_browser
async def test_a_page_cannot_navigate_to_a_file(tmp_path):
    # A file: page may: to a local file, and on Windows to file://host/share,
    # which signs in to that host. The page is served over http, so it stays.
    secret = tmp_path / "secret.txt"
    secret.write_text("secret", encoding="utf-8")
    result, img = await draw(tmp_path, {
        "script": f'location.href = "{secret.as_uri()}";\n'
                  "await new Promise(r => setTimeout(r, 2000));\n"
                  "ctx.fillStyle = '#0f0'; ctx.fillRect(0, 0, w, h);"})
    assert result["status"] == "success", f"the page was left for the file: {result}"
    assert img.getpixel((5, 5)) == GREEN


@needs_browser
@pytest.mark.parametrize("how", [
    'const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob(["x"], {{type: "text/html"}}));\n'
    'a.download = "{name}"; document.body.appendChild(a); a.click();',
    'location.href = URL.createObjectURL(new Blob(["x"], {{type: "application/octet-stream"}}));',
    # A page the script made and moved to: it has to inherit the sandbox.
    'location.href = URL.createObjectURL(new Blob([`<a id=a download="{name}" href="data:text/plain,x">.</a>`\n'
    '  + `<script>a.click()<\\/script>`], {{type: "text/html"}}));',
], ids=["link", "navigation", "page-of-its-own"])
async def test_a_page_cannot_save_a_file(tmp_path, how):
    downloads = Path.home() / "Downloads"
    if not downloads.is_dir():
        pytest.skip("no Downloads folder to watch")
    before = {p.name for p in downloads.iterdir()}
    name = f"canvas_test_{uuid.uuid4().hex}.html"
    try:
        # Eight seconds: a download that does start keeps the browser open until the timeout.
        result, _ = await draw(tmp_path, {
            "script": how.format(name=name) + "\nawait new Promise(r => setTimeout(r, 3000));\n"
                      "ctx.fillRect(0, 0, 1, 1);"}, server=make_server(tmp_path, canvas_timeout=8))
        time.sleep(1)
        saved = sorted({p.name for p in downloads.iterdir()} - before)
    finally:
        for p in downloads.iterdir():
            if p.name not in before and (p.name == name or p.stat().st_size == 1):
                p.unlink()
    assert saved == [], f"the page saved {saved} into {downloads}"
    assert result["status"] == ("error" if "<script>" in how else "success"), result


@needs_browser
async def test_a_script_that_resizes_its_canvas_is_refused(tmp_path):
    result, _ = await draw(tmp_path, {"script": "ctx.canvas.width = 128; ctx.fillRect(0, 0, 10, 10);"})
    assert result["status"] == "error", result
    assert "resized the canvas to 128x48; the layer is 64x48" in result["error"], result["error"]


@needs_browser
async def test_a_script_may_declare_the_names_it_was_given(tmp_path):
    # What a model writes by habit; as a parameter clash it was a SyntaxError on line 1.
    result, img = await draw(tmp_path, {
        "script": "const w = ctx.canvas.width, h = ctx.canvas.height;\n"
                  "ctx.fillStyle = '#0f0'; ctx.fillRect(0, 0, w, h);"})
    assert result["status"] == "success", result
    assert img.getpixel((63, 47)) == GREEN


@needs_browser
@pytest.mark.parametrize("script, expected", [
    ("__err.push(42, {msg: 'mine', stack: 7});", "canvas layer: 42; mine"),
    ("document.getElementById('out').remove();", "the browser returned no picture"),
    ("JSON.stringify = () => 'x{';", "the page's answer is unreadable"),
    ("JSON.stringify = () => '{\"errors\": [], \"png\": \"data:image/png;base64,AAAA\"}';",
     "the page's picture is unreadable"),
    ("JSON.stringify = () => '{\"errors\": [], \"png\": \"data:image/png;base64,A\"}';",
     "the page's picture is unreadable"),
    # A real header of the right size over half a picture, and a picture that is no PNG.
    ("const real = ctx.canvas.toDataURL('image/png'), cut = real.slice(0, real.indexOf(',') + 81);\n"
     "JSON.stringify = () => '{\"errors\": [], \"png\": \"' + cut + '\"}';", "the page's picture is unreadable"),
    ("ctx.fillRect(0, 0, w, h); const jpeg = ctx.canvas.toDataURL('image/jpeg');\n"
     "JSON.stringify = () => '{\"errors\": [], \"png\": \"' + jpeg + '\"}';", "the page's picture is unreadable"),
    ("JSON.stringify = () => '[1, 2]';", "the page's answer is unreadable"),
    ("JSON.stringify = () => '['.repeat(5000);", "the page's answer is unreadable"),
    ("const e = new Error('deep'); e.stack = '.html:' + '9'.repeat(5000) + ':1'; throw e;", "canvas layer: deep"),
], ids=["junk-in-the-error-list", "answer-removed", "answer-not-json", "picture-forged", "picture-not-base64",
        "picture-cut-short", "picture-not-png", "answer-forged", "answer-nested", "line-number-forged"])
async def test_a_script_that_meddles_with_the_page_gets_an_error_it_can_read(tmp_path, script, expected):
    result, _ = await draw(tmp_path, {"script": script})
    assert result["status"] == "error" and result["error_type"] == "CompositionError", result
    assert expected in result["error"], result["error"]
    for noise in ("TypeError", "AttributeError", "Traceback", "JSONDecodeError"):
        assert noise not in result["error"], result["error"]


def test_a_page_full_of_answer_tags_without_an_end_is_read_in_no_time(tmp_path):
    # As a regex this was quadratic: 30,000 of them took about a minute, in the server's process.
    dom = tmp_path / "dom.html"
    dom.write_bytes(b"<html><style>" + b'<pre id="out">' * 30_000 + b"</style></html>")
    started = time.monotonic()
    assert canvas._answer(dom) is None
    assert time.monotonic() - started < 5


def test_an_answer_of_many_small_values_is_not_parsed(tmp_path):
    # 3 MB of "[],[],[]" parse into 60 MB; the picture, base64, holds one comma.
    dom = tmp_path / "dom.html"
    dom.write_bytes(b'<pre id="out">[' + b"[]," * (canvas.MAX_ANSWER_COMMAS + 1) + b"[]]</pre>")
    assert canvas._answer(dom) == ""
    dom.write_bytes(b'<pre id="out">{"errors": [], "png": "data:image/png;base64,AAAA"}</pre>')
    assert canvas._answer(dom) == {"errors": [], "png": "data:image/png;base64,AAAA"}


@needs_browser
async def test_a_script_with_a_lone_surrogate_is_drawn_and_leaves_no_folder(tmp_path, monkeypatch):
    made: list[Path] = []
    real_mkdtemp = canvas.tempfile.mkdtemp

    def recording_mkdtemp(*args, **kwargs):
        made.append(Path(real_mkdtemp(*args, **kwargs)))
        return str(made[-1])

    monkeypatch.setattr(canvas.tempfile, "mkdtemp", recording_mkdtemp)
    result, img = await draw(tmp_path, {"script": "const s = '\ud800';\nctx.fillStyle = '#0f0'; ctx.fillRect(0, 0, w, h);"})
    assert result["status"] == "success", result
    assert img.getpixel((5, 5)) == GREEN
    assert len(made) == 1 and not made[0].exists(), "the run's folder stayed behind"


@needs_browser
async def test_the_page_asks_no_name_server(tmp_path, monkeypatch):
    """A DNS lookup leaves the machine even where no connection can. The
    browser's own net log says whether one was asked for; with the walls
    down it is, which is what makes the silence mean something."""
    script = """
for (const rel of ["dns-prefetch", "preconnect", "prefetch"]) {
  const l = document.createElement("link"); l.rel = rel; l.href = "http://HOST/x"; document.head.appendChild(l);
}
const t = performance.now(); while (performance.now() - t < 2000) { await new Promise(r => setTimeout(r, 50)); }
ctx.fillRect(0, 0, 1, 1);"""
    asked = {}
    for walls in (True, False):
        if not walls:
            monkeypatch.setattr(canvas, "_OFFLINE", [])
            monkeypatch.setattr(canvas, "_CSP", "")
        host, log = f"canvas-test-{'walled' if walls else 'open'}.example.com", tmp_path / f"net_{walls}.json"
        result, _ = await draw(tmp_path, {"script": script.replace("HOST", host)},
                               server=make_server(tmp_path, browser_args=[f"--log-net-log={log}"]))
        assert result["status"] == "success", result
        data = json.loads(log.read_text(encoding="utf-8"))
        names = {number: name for name, number in data["constants"]["logEventTypes"].items()}
        asked[walls] = sorted({names[e["type"]] for e in data["events"] if host in json.dumps(e.get("params", {}))
                               and names[e["type"]].startswith(("DNS_", "HOST_RESOLVER"))})
    assert asked[True] == [], f"behind the walls the browser resolved the name: {asked[True]}"
    assert asked[False], "the control resolved nothing either -- the check above proves nothing"


@needs_browser
async def test_an_error_of_any_length_comes_back_short(tmp_path):
    result, _ = await draw(tmp_path, {"script": "__err.push('y'.repeat(1e6));\nthrow new Error('x'.repeat(1e6));"})
    assert result["status"] == "error" and "xxxx" in result["error"] and "yyyy" in result["error"], result
    assert len(result["error"]) < 3 * canvas.MAX_ERROR_CHARS, len(result["error"])


@needs_browser
async def test_a_page_that_prints_more_than_a_picture_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(canvas, "MAX_OUTPUT_BYTES", 2000)
    result, _ = await draw(tmp_path, {"script": "ctx.fillRect(0, 0, w, h);"})
    assert result["status"] == "error" and "the page grew beyond" in result["error"], result


async def test_a_canvas_layer_is_refused_before_a_browser_starts(tmp_path, monkeypatch):
    def no_browser(*a, **kw):
        raise AssertionError("a browser was started")

    monkeypatch.setattr(canvas.subprocess, "Popen", no_browser)
    ok = "ctx.fillRect(0, 0, 1, 1);"
    (tmp_path / "fonts").mkdir()
    (tmp_path / "fonts" / "fake.ttf").write_text("not a font", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("a file that is no font", encoding="utf-8")
    (tmp_path / "notes.svg").write_text("a file that is no drawing", encoding="utf-8")
    huge = io.BytesIO()
    Image.new("1", (9000, 9000)).save(huge, "PNG")      # 81 Mpx in a few kB
    cases = {
        "'script' is required": {},
        "size too large (max 8192 per side): [100, 8193]": {"script": ok, "size": [100, 8193]},
        "font 'fake.ttf' (fake.ttf) is not a font file": {"script": ok, "fonts": ["fake.ttf"]},
        "(notes.txt) is not a font file": {"script": ok, "fonts": [str(tmp_path / "notes.txt")]},
        "font 'notes.txt' is no file": {"script": ok, "fonts": ["notes.txt"]},
        "f.ttf is a network or device path": {"script": ok, "fonts": ["\\\\host\\share\\f.ttf"]},
        "images.a 9000x9000 is too large": {"script": ok, "images": {
            "a": "data:image/png;base64," + base64.b64encode(huge.getvalue()).decode()}},
        "images.a: failed to decode the data URI": {"script": ok, "images": {"a": "data:image/png;base64,AAAA"}},
        "size must be positive": {"script": ok, "size": [0, 10]},
        "characters (max": {"script": "//" + "x" * canvas.MAX_SCRIPT_CHARS},
        "images.a not found": {"script": ok, "images": {"a": "missing.png"}},
        "images.a (notes.svg) is not an SVG file": {"script": ok, "images": {"a": "notes.svg"}},
        "x.png is a network or device path": {"script": ok, "images": {"a": "\\\\host\\share\\x.png"}},
        "images.a is a data URI but not an image": {"script": ok, "images": {"a": "data:text/html,<p>"}},
        "images must be an object": {"script": ok, "images": ["a.png"]},
        "font 'nope' is no file": {"script": ok, "fonts": ["nope"]},
    }
    monkeypatch.chdir(tmp_path)     # notes.txt is right here, and still no font of the fonts folder
    server = make_server(tmp_path)
    for expected, layer in cases.items():
        result, _ = await draw(tmp_path, layer, server=server)
        assert result["status"] == "error" and expected in result["error"], (expected, result)


async def test_a_configured_browser_that_is_missing_is_named_at_start_and_in_the_answer(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="plugins.image_compose.server"):
        server = make_server(tmp_path, browser="no-such-browser-xyz")
    assert any("canvas layers will fail" in r.message and "no-such-browser-xyz" in r.message
               for r in caplog.records), [r.message for r in caplog.records]
    result, _ = await draw(tmp_path, {"script": "ctx.fillRect(0, 0, 1, 1);"}, server=server)
    assert result["status"] == "error" and "no-such-browser-xyz" in result["error"], result
    assert "server config" in result["error"]


def stand_in_browser(tmp_path, body: str) -> dict:
    """Server config for a 'browser' that is this Python running `body`."""
    script = tmp_path / "stand_in_browser.py"
    script.write_text("import json, os, sys\n" + body, encoding="utf-8")
    return {"browser": sys.executable, "browser_args": [str(script)]}


async def test_the_browser_gets_the_configured_arguments_and_no_compat_layer(tmp_path, monkeypatch):
    # __COMPAT_LAYER=RunAsInvoker, as some launchers pass it on, makes Edge quit at once.
    monkeypatch.setenv("__COMPAT_LAYER", "RunAsInvoker")
    server = make_server(tmp_path, **stand_in_browser(tmp_path, """
seen = {"compat": os.environ.get("__COMPAT_LAYER"), "dump": "--dump-dom" in sys.argv}
print('<pre id="out">' + json.dumps({"errors": [{"msg": json.dumps(seen, sort_keys=True)}]}) + "</pre>")
"""))
    result, _ = await draw(tmp_path, {"script": "ctx.fillRect(0, 0, 1, 1);"}, server=server)
    assert result["status"] == "error", result
    assert '{"compat": null, "dump": true}' in result["error"], result["error"]


async def test_the_page_is_served_with_its_policy_and_only_at_its_own_address(tmp_path):
    # The page holds the layer's images and fonts; any local process can connect to the port.
    server = make_server(tmp_path, **stand_in_browser(tmp_path, """
import urllib.error, urllib.request
url = sys.argv[-1]
def get(address):
    try:
        with urllib.request.urlopen(address) as page:
            return [page.headers.get("Content-Security-Policy"), "MARK-7" in page.read().decode()]
    except urllib.error.HTTPError as e:
        return e.code
seen = {"guessed": get(url.rsplit("/", 2)[0] + "/page.html"), "first": get(url), "again": get(url)}
print('<pre id="out">' + json.dumps({"errors": [{"msg": json.dumps(seen, sort_keys=True)}]}) + "</pre>")
"""))
    result, _ = await draw(tmp_path, {"script": "// MARK-7"}, server=server)
    assert result["status"] == "error", result
    assert f'{{"again": 404, "first": ["{canvas._CSP}", true], "guessed": 404}}' in result["error"], result["error"]
    assert "sandbox allow-scripts" in canvas._CSP and "default-src 'none'" in canvas._CSP


@needs_browser
async def test_the_sandboxed_page_still_has_the_drawing_api_the_skill_teaches(tmp_path):
    Image.new("RGBA", (4, 4), RED).save(tmp_path / "red.png")
    result, img = await draw(tmp_path, {"images": {"red": "red.png"}, "script": """
const small = new OffscreenCanvas(8, 8), s = small.getContext("2d");
s.fillStyle = "#0f0"; s.fillRect(0, 0, 8, 8);
ctx.imageSmoothingEnabled = false; ctx.drawImage(small, 0, 0, 16, 16);
ctx.fillStyle = ctx.createPattern(img.red, "repeat"); ctx.fillRect(16, 0, 16, 16);
ctx.filter = "blur(1px)"; ctx.fillStyle = "#00f"; ctx.fill(new Path2D("M 34 2 h 12 v 12 h -12 Z")); ctx.filter = "none";
if (!(ctx.measureText("Hg").width > 0)) throw new Error("no text metrics");
if (ctx.getImageData(40, 8, 1, 1).data[2] < 200) throw new Error("the path was not filled");"""})
    assert result["status"] == "success", result
    assert [img.getpixel((x, 8)) for x in (8, 24)] == [GREEN, RED]
    assert img.getpixel((40, 8))[2] > 200 and img.getpixel((60, 8)) == CLEAR


async def test_a_chromium_that_refuses_to_run_as_root_is_answered_with_the_setting(tmp_path):
    server = make_server(tmp_path, **stand_in_browser(tmp_path, """
sys.stderr.write("Running as root without --no-sandbox is not supported.\\n")
sys.exit(1)
"""))
    result, _ = await draw(tmp_path, {"script": "ctx.fillRect(0, 0, 1, 1);"}, server=server)
    assert result["status"] == "error" and "the browser returned no picture" in result["error"], result
    assert 'needs browser_args: ["--no-sandbox"]' in result["error"], result["error"]


def test_canvas_is_a_layer_type_the_schema_names():
    import yaml
    from pathlib import Path

    schema = (Path(compositor.__file__).parent / "schema.yaml").read_text(encoding="utf-8")
    text = yaml.safe_load(schema.replace("{{ name }}", "x"))["tools"][0]["function"]["parameters"][
        "properties"]["spec"]["description"]
    assert "canvas" in compositor.LAYER_TYPES
    for word in ("canvas", "script", "images", "fonts", "seed", "noise(", "fbm(", "pixels(", "tile("):
        assert word in text, f"the render tool's description does not mention {word}"
