"""A stand-in for the Godot binary, driven by the plugin tests.

It answers the handful of invocations the plugin makes, the way Godot 4.7.2
was measured to answer them -- banner on stdout, error BLOCKS on stderr,
exit 0 for a runtime push_error and exit 1 for a parse error -- and records
every argv line into ``$GODOT_STUB_LOG`` so a test can assert what was sent.

Behaviour is keyed on markers in the files it is pointed at:
  BROKEN      in a .gd -> parse error for that file, line 3
  PUSH_ERROR  in a .gd -> runtime error block from _ready, line 4
  NOINST      in a .gd -> the check walker reports it FAILED, no stderr block
  PRINTERR    in main.gd -> an unprefixed stderr line, exit 0
  CRASH       in main.gd -> a crash handler dump (no ERROR: prefix), exit 139
  hang.tscn   as scene -> never quits (for the timeout path); its pid in stub.pid
  spawn.tscn  as scene -> starts a child holding the pipes (pid in child.pid), then hangs
  orphan.tscn as scene -> prints, leaves an orphan holding the pipes (orphan.pid), hangs;
              orphan_exit.tscn the same, but exits 0 instead of hanging
  $GODOT_STUB_HANG_VERSION -> --version writes its pid there and never answers
  HANG_IMPORT / IMPORT_FAIL marker files in the project -> --import hangs / exits 2
  PORT_TAKEN  marker file -> the bundled addon cannot bind its port (editor open)
  ADDON_ERROR marker file -> a different addon error, which IS a project error
  preset "Partial" -> writes the file, then exits 1; "Slow" -> writes, then hangs
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

BANNER = "Godot Engine v4.7.2.stub - https://godotengine.org"


def log(argv: list[str]) -> None:
    path = os.environ.get("GODOT_STUB_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(argv) + "\n")


def parse_error(path: str) -> None:
    sys.stderr.write(
        'SCRIPT ERROR: Parse Error: Expected expression for variable initial value after "=".\n'
        f"   at: GDScript::reload ({path}:3)\n"
        f'ERROR: Failed to load script "{path}" with error "Parse error".\n'
        "   at: load (modules/gdscript/gdscript_resource_format.cpp:46)\n")


def runtime_error(script: str) -> None:
    sys.stderr.write(
        "ERROR: runtime probe error\n"
        "   at: push_error (core/variant/variant_utility.cpp:1023)\n"
        "   GDScript backtrace (most recent call first):\n"
        f"       [0] _ready ({script}:4)\n")


def flag(argv: list[str], name: str) -> str | None:
    if name in argv:
        i = argv.index(name)
        return argv[i + 1] if i + 1 < len(argv) else ""
    return None


def res_to_path(project: Path, res: str) -> Path:
    return project / res[len("res://"):] if res.startswith("res://") else Path(res)


def addon_port_taken() -> None:
    """What the bundled addon prints when a running editor holds the port.
    Every EDITOR-mode run loads it: --import and --export-*."""
    sys.stderr.write(
        "ERROR: [godot-mcp] Failed to start server on 127.0.0.1:6550: Already in use\n"
        "   at: push_error (core/variant/variant_utility.cpp:1023)\n")


def main(argv: list[str]) -> int:
    log(argv)
    print(BANNER)
    print()
    if "--version" in argv:
        hang = os.environ.get("GODOT_STUB_HANG_VERSION")
        if hang:  # a pid file to write, then no answer
            Path(hang).write_text(str(os.getpid()), encoding="utf-8")
            time.sleep(30)
        print("4.7.2.stable.stub")
        return 0

    project = Path(flag(argv, "--path") or ".")
    script = flag(argv, "-s")

    if "--check-only" in argv and script:
        text = res_to_path(project, script).read_text(encoding="utf-8")
        if "BROKEN" in text:
            parse_error(script)
            return 1
        return 0

    if script and script.endswith("check_scripts.gd"):
        failed = 0
        found = [p for p in project.rglob("*.gd")
                 if "addons" not in p.parts and ".godot" not in p.parts]
        for p in found:
            text = p.read_text(encoding="utf-8")
            res = "res://" + p.relative_to(project).as_posix()
            if "BROKEN" in text:
                parse_error(res)
                print(f"FAILED {res}")
                failed += 1
            elif "NOINST" in text:
                # can_instantiate() false without any stderr block.
                print(f"FAILED {res}")
                failed += 1
        print(f"CHECKED {len(found)} FAILED {failed}")
        return 1 if failed else 0

    if script:  # an agent script
        text = Path(script).read_text(encoding="utf-8")
        if "BROKEN" in text:
            parse_error(script.replace("\\", "/"))
            return 1
        for m in re.finditer(r"print\('([^']*)'\)", text):
            print(m.group(1))
        if "PUSH_ERROR" in text:
            runtime_error(script.replace("\\", "/"))
        if "LEAK" in text:
            sys.stderr.write("ERROR: 17 resources still in use at exit (run with --verbose for details).\n"
                             "   at: clear (core/io/resource.cpp:822)\n")
        return 3 if "quit(3)" in text else 0

    if "--import" in argv:
        print("[   0% ] \x1b[90m\x1b[1mfirst_scan_filesystem\x1b[22m | Started\x1b[39m\x1b[0m")
        if (project / "PORT_TAKEN").exists():
            addon_port_taken()
        if (project / "ADDON_ERROR").exists():
            sys.stderr.write(
                "ERROR: [godot-mcp] command_router.gd: failed to register commands\n"
                "   at: push_error (core/variant/variant_utility.cpp:1023)\n")
        if (project / "IMPORT_FAIL").exists():
            return 2
        cfg = project / "project.godot"
        text = cfg.read_text(encoding="utf-8")
        addon_ok = (project / "addons" / "godot_mcp" / "plugin.cfg").is_file()
        enabled = "res://addons/godot_mcp/plugin.cfg" in text
        # What the real addon does on its first editor load: register the
        # autoload. Only if it is installed AND enabled -- that is the order
        # setup has to get right.
        if addon_ok and enabled and "MCPGameBridge=" not in text:
            text = text.rstrip("\n") + (
                '\n\n[autoload]\n\nMCPGameBridge='
                '"res://addons/godot_mcp/game_bridge/mcp_game_bridge.gd"\n')
            cfg.write_text(text, encoding="utf-8")
        main_scene = re.search(r'run/main_scene="([^"]+)"', text)
        if main_scene and not res_to_path(project, main_scene.group(1)).is_file():
            sys.stderr.write(f"ERROR: Cannot open file '{main_scene.group(1)}'.\n"
                             "   at: load (scene/resources/resource_format_text.cpp:1442)\n")
        # The real order on a big project: the addon registers its autoload
        # early (plugin load), then the asset import grinds on. A hang AFTER
        # the write is what a timeout looks like; before it, the missing
        # autoload would mask the timeout entirely.
        if (project / "HANG_IMPORT").exists():
            time.sleep(30)
        return 0

    for exp in ("--export-release", "--export-debug"):
        if exp in argv:
            i = argv.index(exp)
            preset, out = argv[i + 1], argv[i + 2]
            if (project / "PORT_TAKEN").exists():
                addon_port_taken()
            print("[  16% ] \x1b[90mfirst_scan_filesystem | Lese Dateistruktur\x1b[0m")
            if preset == "Nope":
                sys.stderr.write("ERROR: This project doesn't have an `export_presets.cfg` file at its root.\n"
                                 "   at: _fs_changed (editor/editor_node.cpp:1417)\n")
                return 1
            Path(out).write_bytes(b"PCK" * 700)
            if preset == "Partial":
                return 1  # the file exists and is garbage
            if preset == "Slow":
                time.sleep(30)
            return 0

    # A plain run of the project or a scene.
    scene = next((a for a in argv if a.endswith(".tscn")), None)
    if scene and scene.endswith("spawn.tscn"):
        # What the console wrapper does: a child that inherits the pipes.
        import subprocess
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(20)"])
        (project / "child.pid").write_text(str(child.pid), encoding="utf-8")
    if scene and scene.endswith(("orphan.tscn", "orphan_exit.tscn")):
        # A grandchild whose parent is already gone: out of reach of the tree
        # kill, and it keeps the pipes open.
        import subprocess
        print("printed before the hang", flush=True)
        grandchild = ("import subprocess, sys; p = subprocess.Popen([sys.executable, '-c', "
                      "'import time; time.sleep(20)']); "
                      f"open({str(project / 'orphan.pid')!r}, 'w').write(str(p.pid))")
        subprocess.run([sys.executable, "-c", grandchild])
    if scene and scene.endswith(("hang.tscn", "spawn.tscn", "orphan.tscn")):
        (project / "stub.pid").write_text(str(os.getpid()), encoding="utf-8")
        time.sleep(30)
    print("scene ready")
    if "--" in argv:
        print("user args: " + " ".join(argv[argv.index("--") + 1:]))
    main_gd = project / "main.gd"
    main_text = main_gd.read_text(encoding="utf-8") if main_gd.is_file() else ""
    if "PUSH_ERROR" in main_text:
        runtime_error("res://main.gd")
    if "PRINTERR" in main_text:
        sys.stderr.write("to stderr\n")
    if "CRASH" in main_text:
        sys.stderr.write("handle_crash: Program crashed with signal 11\n"
                         "Engine version: Godot Engine v4.7.2.stub\n"
                         "Dumping the backtrace. Please include this when reporting the bug.\n")
        return 139
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
