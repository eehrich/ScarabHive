"""One `--attach` for every kind of file, sorted the way /attach sorts.

The command line used to have three flags -- --images, --audio, --text -- and
the person had to know which bucket a file belongs in. The chat never asked:
`/attach <path>` reads the extension and puts the file where it belongs. A file
in the wrong bucket does not fail politely either: a .txt handed to --images is
base64-encoded as a picture, a .png handed to --text is read as text.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import agent_system.agent_cli as cli
import agent_system.agent_run as agent_run
from agent_system.cli_utils.attachments import sort_attachments
from agent_system.config.models import (
    AgentConfig,
    AgentSystemConfig,
    LLMModelConfig,
    LLMProfile,
    LLMSystemConfig,
    MCPConfig,
    PluginsConfig,
)


@pytest.fixture
def files(tmp_path):
    """One real file of each kind, plus one of a kind nothing can send."""
    made = {}
    for name in ("picture.png", "voice.mp3", "notes.md", "archive.zip"):
        path = tmp_path / name
        path.write_bytes(b"x")
        made[name] = path
    return made


class TestSorting:
    def test_each_file_lands_where_it_belongs(self, files):
        kinds, problems = sort_attachments(
            [files["notes.md"], files["picture.png"], files["voice.mp3"]])
        assert problems == []
        assert kinds["image"] == [str(files["picture.png"])]
        assert kinds["audio"] == [str(files["voice.mp3"])]
        assert kinds["text"] == [str(files["notes.md"])]

    def test_the_order_within_a_kind_is_the_order_typed(self, tmp_path):
        first, second = tmp_path / "a.png", tmp_path / "b.png"
        for p in (first, second):
            p.write_bytes(b"x")
        kinds, _ = sort_attachments([second, first])
        assert kinds["image"] == [str(second), str(first)]

    def test_nothing_attached_is_not_a_problem(self):
        kinds, problems = sort_attachments([])
        assert problems == []
        assert kinds == {"image": [], "audio": [], "text": []}

    def test_a_missing_file_is_named_not_dropped(self, tmp_path):
        kinds, problems = sort_attachments([tmp_path / "nope.png"])
        assert kinds["image"] == []
        assert len(problems) == 1 and "nope.png" in problems[0]

    def test_a_kind_nothing_can_send_is_named_not_dropped(self, files):
        kinds, problems = sort_attachments([files["archive.zip"]])
        assert all(not v for v in kinds.values())
        assert len(problems) == 1 and "archive.zip" in problems[0]

    def test_the_good_files_are_still_sorted_next_to_a_bad_one(self, files,
                                                               tmp_path):
        # The bad ones come FIRST on purpose: with the good file in front, a
        # sorter that gives up at the first problem still looks healthy.
        kinds, problems = sort_attachments(
            [files["archive.zip"], tmp_path / "nope.png", files["picture.png"]])
        assert kinds["image"] == [str(files["picture.png"])]
        assert len(problems) == 2

    def test_a_directory_is_not_a_file(self, tmp_path):
        _, problems = sort_attachments([tmp_path])
        assert len(problems) == 1 and "Not a file" in problems[0]


# --------------------------------------------------------------------------
# The wiring: what the flag hands to the message builder.
#
# A text attachment on purpose -- it is the one kind that needs no model
# capability, so the run reaches the builder on a stub model and the test
# measures the routing instead of the capability gate.
# --------------------------------------------------------------------------

AGENT = "attach_agent"


class _DummyAgent:
    def __init__(self, *args, **kwargs):
        # The real Agent carries a name and agent_run reads it; a fake that
        # can do less than the original hides exactly the lines that touch it.
        self.name = AGENT
        self.agent_config = AgentConfig(system_prompt="x")
        self.registry = None
        self.llm = SimpleNamespace(model="m")
        self._session_tracker = SimpleNamespace(
            set_session_messages=lambda sid, messages: None,
            set_session_metadata=lambda sid, meta: None)

    async def run_events(self, task, **kwargs):
        _sent.append(task)
        yield {"type": "final", "summary": "done"}
        yield {"type": "end"}


_sent: list = []


@pytest.fixture
def booted(tmp_path, monkeypatch):
    """Both entry points, far enough to reach the message builder."""
    _sent.clear()
    config = AgentSystemConfig(
        llm_system=LLMSystemConfig(
            models={"m": LLMModelConfig(provider="openai", model="m")},
            profiles={"normal": LLMProfile(model_ref="m")},
        ))
    config.plugins = PluginsConfig(servers={
        AGENT: MCPConfig(type="agent", enabled=True,
                         agent_config=AgentConfig(system_prompt="x")),
    })
    config.default_agent = AGENT

    from agent_system.mcp.base import MCPRegistry
    from agent_system.services.initialization_service import InitializationService
    from agent_system.services.session_manager import SessionManager
    from agent_system.services.session_service import SessionService

    manager = SessionManager(storage_path=str(tmp_path / "sessions"))
    service = SessionService(manager)

    def fake_init(self):
        self._session_manager = manager
        return MCPRegistry(), service

    monkeypatch.setattr(cli, "load_settings", lambda path=None: config)
    monkeypatch.setattr(agent_run, "load_settings", lambda path=None: config)
    monkeypatch.setattr(InitializationService, "initialize_for_cli", fake_init)
    monkeypatch.setattr("agent_system.servers.agent.server.Agent", _DummyAgent)
    monkeypatch.setattr("agent_system.agent_cli.Agent", _DummyAgent)

    async def fake_initialize_system(cfg):
        return MCPRegistry(), service

    async def fake_create_agent(cfg, registry, agent_name, session_service=None):
        return _DummyAgent()

    async def fake_request(agent, request, session_id, llm_override=None,
                           llm_profile_info=None):
        _sent.append(request)
        return {"summary": "done", "calls": []}

    async def fake_save(**kwargs):
        return True

    monkeypatch.setattr(agent_run, "initialize_system", fake_initialize_system)
    monkeypatch.setattr(agent_run, "create_agent", fake_create_agent)
    monkeypatch.setattr(agent_run, "run_agent_request", fake_request)
    monkeypatch.setattr(service, "save_session", fake_save)
    return SimpleNamespace(sent=_sent, config=config)


def _text_of(message) -> str:
    """Everything the built message carries, as one searchable string."""
    parts = getattr(message, "content", message)
    if isinstance(parts, str):
        return parts
    return " ".join(str(getattr(p, "text", "") or p) for p in parts)


class TestAgentCliWiring:
    def test_a_text_file_reaches_the_agent_through_attach(self, booted, tmp_path,
                                                          monkeypatch):
        notes = tmp_path / "notes.md"
        notes.write_text("die geheime zahl ist 47", encoding="utf-8")

        monkeypatch.setattr("sys.argv", ["agent-cli", "--raw", "run",
                                         "fasse zusammen", "--attach", str(notes)])
        cli.main()

        assert booted.sent, "the agent was never called"
        assert "die geheime zahl ist 47" in _text_of(booted.sent[0]), (
            "the attachment did not reach the message")

    def test_the_old_flag_still_works_and_is_sorted_by_kind(self, booted, tmp_path,
                                                            monkeypatch):
        # --images used to force the image bucket; a text file there was
        # base64-encoded as a picture. It now lands where it belongs.
        notes = tmp_path / "notes.md"
        notes.write_text("aus der alten flagge", encoding="utf-8")

        monkeypatch.setattr("sys.argv", ["agent-cli", "--raw", "run", "was steht da",
                                         "--images", str(notes)])
        cli.main()

        assert "aus der alten flagge" in _text_of(booted.sent[0])

    def test_a_missing_file_stops_the_run(self, booted, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("sys.argv", ["agent-cli", "--raw", "run", "los",
                                         "--attach", str(tmp_path / "weg.png")])
        with pytest.raises(SystemExit) as exit_info:
            cli.main()

        assert exit_info.value.code == 1
        assert "weg.png" in capsys.readouterr().err
        assert not booted.sent, "the run went on without the attachment"


class TestAgentRunWiring:
    def _run(self, **kwargs):
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(agent_run.main_async("fasse zusammen", **kwargs))
        finally:
            loop.close()

    def test_a_text_file_reaches_the_agent_through_attach(self, booted, tmp_path):
        notes = tmp_path / "notes.md"
        notes.write_text("die geheime zahl ist 47", encoding="utf-8")

        self._run(attachments=[str(notes)])

        assert booted.sent, "the agent was never called"
        assert "die geheime zahl ist 47" in _text_of(booted.sent[0])

    def test_a_missing_file_stops_the_run(self, booted, tmp_path, capsys):
        # main_async turns a ValueError into the user-friendly stderr line and
        # exit 1 -- the same shape agent-cli uses, and what publish_pipeline
        # reads as a hard failure.
        with pytest.raises(SystemExit) as exit_info:
            self._run(attachments=[str(tmp_path / "weg.png")])

        assert exit_info.value.code == 1
        assert "weg.png" in capsys.readouterr().err
        assert not booted.sent, "the run went on without the attachment"


class TestTheChatQueue:
    """The one moment the REPL tests next door cannot reach.

    tests/cli/test_plugin_commands.py::TestAttachCommand drives the real REPL
    through /attach and pins what a queued file becomes. What it cannot stage
    is a file that changes BETWEEN /attach and Enter, because the lines go in
    as one sequence -- so that case is exercised here, one level down.
    """

    @staticmethod
    def _ctx(attachments):
        return SimpleNamespace(
            attachments=list(attachments),
            llm_override=None,
            agent=SimpleNamespace(llm=SimpleNamespace(model="m")))

    @staticmethod
    def _renderer():
        return SimpleNamespace(_colored=lambda text, code: text)

    def test_a_file_that_vanished_since_it_was_queued_stops_the_send(
            self, tmp_path, capsys):
        from agent_system.cli_utils.chat import _task_with_attachments

        gone = tmp_path / "weg.png"
        gone.write_bytes(b"x")
        ctx = self._ctx([str(gone)])
        gone.unlink()

        message = _task_with_attachments(ctx, "und?", self._renderer())

        assert message is None, "a file that is no longer there was sent anyway"
        # "Not sent: Not a file", not just the name: the OLD code reached the
        # same dead end through the builder and printed "Attachment failed,
        # nothing sent: ...weg.png", so asserting the name alone measured
        # nothing about the sorter (measured green against the old loop).
        assert "Not sent: Not a file" in capsys.readouterr().out
        assert ctx.attachments, (
            "the queue was emptied, so the person has to attach everything again")


class TestEveryImageTheBuilderCanReadIsSortedAsOne:
    """The detector's set decides, the builder never asked.

    validate_image_file opens the file with PIL and builds the data URL from
    PIL's format -- the extension is not consulted. So --images screenshot.jfif
    (Chrome's "save image as" default) produced a working data:image/jpeg
    message, while the new sorter called it unknown and exited 1. The same
    bytes as .tiff were an image and as .tif were not.

    This walks the real builder, so it cannot go stale against a hand-written
    list of "what should work".
    """

    @pytest.mark.parametrize("name,pil_format", [
        ("shot.jfif", "JPEG"), ("shot.jpe", "JPEG"), ("shot.jpg", "JPEG"),
        ("scan.tif", "TIFF"), ("scan.tiff", "TIFF"), ("bild.png", "PNG"),
    ])
    def test_a_file_the_builder_encodes_is_sorted_as_an_image(self, tmp_path,
                                                              name, pil_format):
        from PIL import Image
        from agent_system.utils.multimodal_processor import (
            create_multimodal_message_extended)

        target = tmp_path / name
        Image.new("RGB", (2, 2), "red").save(target, pil_format)

        # What the builder does with it -- the reference the sorter has to match.
        message = create_multimodal_message_extended(text="was ist das",
                                                     image_paths=[target])
        assert [c.type for c in message.content] == ["text", "image_url"], (
            "fixture is wrong: the builder cannot read this file at all")

        kinds, problems = sort_attachments([target])
        assert problems == [], f"the sorter refuses what the builder encodes: {problems}"
        assert kinds["image"] == [str(target)]


class TestPathsThatBite:
    """Two shapes that turned a listed problem into a crash or a silent pass."""

    def test_a_tilde_name_that_is_not_a_home_is_a_problem_not_a_crash(
            self, monkeypatch):
        # Path.expanduser() RAISES RuntimeError for a ~name it cannot resolve
        # -- on POSIX for an unknown user, on Windows only when USERNAME
        # differs from the profile directory. It does differ on the machine
        # this was found on, so the setenv changes nothing HERE; it is what
        # makes the test measure the same thing on a machine where the two
        # match, and there it is the difference between red and green.
        monkeypatch.setenv("USERNAME", "jemand_ganz_anderes")
        kinds, problems = sort_attachments(["~$notes.md"])

        assert all(not v for v in kinds.values())
        assert len(problems) == 1 and "Not a file" in problems[0]

    def test_a_real_home_path_is_still_expanded(self, monkeypatch, tmp_path):
        home = tmp_path / "home"
        home.mkdir()
        note = home / "notiz.md"
        note.write_text("x", encoding="utf-8")
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))

        kinds, problems = sort_attachments(["~/notiz.md"])

        assert problems == []
        assert kinds["text"] == [str(note)]


class TestAFailedAttachmentIsAFailedRun:
    """A file that exists but cannot be processed used to leave with exit 0.

    Callers that read the exit code -- the writer runners and
    publish_pipeline do -- took the empty stdout for a successful run.
    """

    def test_agent_cli_exits_non_zero_on_an_unreadable_image(self, booted,
                                                             tmp_path, monkeypatch,
                                                             capsys):
        broken = tmp_path / "kaputt.png"
        broken.write_bytes(b"this is not a picture")

        monkeypatch.setattr("sys.argv", ["agent-cli", "--raw", "run", "was ist das",
                                         "--attach", str(broken)])
        with pytest.raises(SystemExit) as exit_info:
            cli.main()

        assert exit_info.value.code == 1
        assert "error processing image" in capsys.readouterr().err.lower()
        assert not booted.sent

    def test_agent_run_exits_non_zero_on_an_unreadable_image(self, booted, tmp_path,
                                                             capsys):
        broken = tmp_path / "kaputt.png"
        broken.write_bytes(b"this is not a picture")

        loop = asyncio.new_event_loop()
        try:
            with pytest.raises(SystemExit) as exit_info:
                loop.run_until_complete(
                    agent_run.main_async("was ist das", attachments=[str(broken)]))
        finally:
            loop.close()

        assert exit_info.value.code == 1
        assert "error processing image" in capsys.readouterr().err.lower()
        assert not booted.sent


class TestEveryBucketReachesTheBuilder:
    """image, audio and text -- each one on its own.

    Only image and text were exercised, so `has_audio = []` in agent-cli and
    `audio_paths = []` in agent-run both survived the whole suite (measured).
    """

    @staticmethod
    def _wav(tmp_path):
        import wave

        target = tmp_path / "ton.wav"
        with wave.open(str(target), "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(b"\x00\x00" * 8)
        return target

    @staticmethod
    def _png(tmp_path):
        from PIL import Image

        target = tmp_path / "bild.png"
        Image.new("RGB", (2, 2), "red").save(target, "PNG")
        return target

    @staticmethod
    def _md(tmp_path):
        target = tmp_path / "notiz.md"
        target.write_text("die geheime zahl ist 47", encoding="utf-8")
        return target

    @pytest.mark.parametrize("maker,part_type", [
        ("_png", "image_url"), ("_wav", "audio"), ("_md", "text_file")])
    def test_agent_cli_hands_each_kind_to_its_own_argument(
            self, booted, tmp_path, monkeypatch, maker, part_type):
        target = getattr(self, maker)(tmp_path)

        monkeypatch.setattr("sys.argv", ["agent-cli", "--raw", "run", "sieh dir das an",
                                         "--attach", str(target)])
        cli.main()

        message = booted.sent[0]
        assert [getattr(c, "type", None) for c in message.content] == [
            "text", part_type]

    @pytest.mark.parametrize("maker,part_type", [
        ("_png", "image_url"), ("_wav", "audio"), ("_md", "text_file")])
    def test_agent_run_hands_each_kind_to_its_own_argument(
            self, booted, tmp_path, monkeypatch, maker, part_type):
        target = getattr(self, maker)(tmp_path)

        monkeypatch.setattr("sys.argv", ["agent-run", "sieh dir das an",
                                         "--attach", str(target)])
        agent_run.main()

        message = booted.sent[0]
        assert [getattr(c, "type", None) for c in message.content] == [
            "text", part_type]


class TestTheFlagItself:
    """What argparse does with the flag -- measured through main(), not below it.

    TestAgentRunWiring calls main_async directly, which is below the parser:
    renaming agent-run's dest left every test in this file green (measured).
    """

    @pytest.mark.parametrize("entry", ["agent-run", "agent-cli"])
    @pytest.mark.parametrize("flag", ["--attach", "--images", "--audio",
                                      "--text", "--files"])
    def test_every_flag_feeds_the_same_list(self, booted, tmp_path, monkeypatch,
                                            flag, entry):
        # Both entry points: agent-cli's own alias list was only ever driven
        # with --images and --text, so dropping --audio and --files from it
        # stayed green (measured).
        notes = tmp_path / "notes.md"
        notes.write_text("egal durch welche flagge", encoding="utf-8")

        argv = ([entry, "was steht da", flag, str(notes)] if entry == "agent-run"
                else [entry, "--raw", "run", "was steht da", flag, str(notes)])
        monkeypatch.setattr("sys.argv", argv)
        (agent_run.main if entry == "agent-run" else cli.main)()

        assert "egal durch welche flagge" in _text_of(booted.sent[0])

    # BOTH orders: with only one order tested, turning the FIRST argument into
    # a plain "store" stays invisible -- the second one still extends the list
    # it left behind (measured green).
    @pytest.mark.parametrize("order", [("--attach", "--images"),
                                       ("--images", "--attach")])
    def test_two_flags_accumulate_instead_of_overwriting(self, booted, tmp_path,
                                                         monkeypatch, order):
        first = tmp_path / "eins.md"
        second = tmp_path / "zwei.md"
        first.write_text("erste datei", encoding="utf-8")
        second.write_text("zweite datei", encoding="utf-8")

        monkeypatch.setattr("sys.argv", ["agent-run", "fasse zusammen",
                                         order[0], str(first),
                                         order[1], str(second)])
        agent_run.main()

        sent = _text_of(booted.sent[0])
        assert "erste datei" in sent and "zweite datei" in sent

    @pytest.mark.parametrize("order", [("--attach", "--text"),
                                       ("--text", "--attach")])
    def test_agent_cli_two_flags_accumulate_too(self, booted, tmp_path,
                                                monkeypatch, order):
        first = tmp_path / "eins.md"
        second = tmp_path / "zwei.md"
        first.write_text("erste datei", encoding="utf-8")
        second.write_text("zweite datei", encoding="utf-8")

        monkeypatch.setattr("sys.argv", ["agent-cli", "--raw", "run", "fasse zusammen",
                                         order[0], str(first),
                                         order[1], str(second)])
        cli.main()

        sent = _text_of(booted.sent[0])
        assert "erste datei" in sent and "zweite datei" in sent


class TestNoAttachmentErrorLeavesWithZero:
    """An anti-drift guard for a class, not a case.

    Nine except-branches across the two entry points printed an error and
    `return`ed, so the process left with 0 and a caller reading the exit code
    took a failed run for an empty answer. Two of the nine are pinned by a
    test that runs them; the other seven would need seven near-identical
    fixtures. This reads the source instead and holds the whole class --
    including main_async's own two handlers, which wrap the builder too.
    """

    @staticmethod
    def _attachment_handlers(path):
        """EVERY try that wraps the builder, not the first one walk() finds.

        Taking the first one measured main_async's OUTER try, whose two
        handlers exit anyway -- so the guard passed with all four attachment
        branches broken (measured).
        """
        import ast

        tree = ast.parse(path.read_text(encoding="utf-8"))
        handlers = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            calls = [c for b in node.body for c in ast.walk(b)
                     if isinstance(c, ast.Call)]
            names = {getattr(c.func, "id", None) or getattr(c.func, "attr", None)
                     for c in calls}
            if "create_multimodal_message_extended" in names:
                handlers.extend(node.handlers)
        return handlers

    @pytest.mark.parametrize("module", ["agent_cli.py", "agent_run.py"])
    def test_every_branch_ends_the_process(self, module):
        from pathlib import Path as _P

        source = _P(__file__).parents[2] / "src" / "agent_system" / module
        handlers = self._attachment_handlers(source)
        assert handlers, f"no attachment try/except found in {module}"

        for handler in handlers:
            last = handler.body[-1]
            import ast
            exits_non_zero = (
                isinstance(last, ast.Expr)
                and isinstance(last.value, ast.Call)
                and getattr(last.value.func, "attr", None) == "exit"
                # The CODE, not just the call: sys.exit(0) is precisely the
                # "left like a success" this guard forbids, and it passed.
                and any(isinstance(a, ast.Constant) and a.value not in (0, None)
                        for a in last.value.args))
            assert isinstance(last, ast.Raise) or exits_non_zero, (
                f"{module}:{last.lineno} an attachment error leaves with the "
                f"exit code of a successful run")


class TestAFinishedRunAlwaysPrintsItsResult:
    """A tool value that is not plain JSON must not end a finished run with exit 1.

    Live run 2026-09-11: a tool_script failure report carried a set. The story
    was already written when agent-cli died in json.dumps of its --verbose
    output, and the runner saw exit 1. This class only reuses the
    two-entry-point harness above; it has nothing to do with attachments.
    """

    SET_RESULT = {"status": "error", "variables": {"seen": {"B01"}}}

    @classmethod
    def _agent_returning_a_set(cls, monkeypatch):
        class _SetResultAgent(_DummyAgent):
            async def run_events(self, task, **kwargs):
                _sent.append(task)
                # Call before result, as the real agent emits them: the --raw
                # collector only attaches a result to its preceding call.
                yield {"type": "mcp_call", "step": 1, "server": "pipe",
                       "action": "pipe", "params": {}}
                yield {"type": "mcp_result", "step": 1, "server": "pipe",
                       "action": "pipe", "result": cls.SET_RESULT}
                yield {"type": "final", "summary": "done"}
                yield {"type": "end"}

        monkeypatch.setattr("agent_system.servers.agent.server.Agent", _SetResultAgent)
        monkeypatch.setattr("agent_system.agent_cli.Agent", _SetResultAgent)

    @pytest.mark.parametrize("flag", ["--raw", "--verbose"])
    def test_agent_cli(self, booted, monkeypatch, capsys, flag):
        self._agent_returning_a_set(monkeypatch)
        monkeypatch.setattr("sys.argv", ["agent-cli", flag, "run", "was steht da"])

        cli.main()

        assert booted.sent, "the agent was never called -- nothing measured"
        assert "B01" in capsys.readouterr().out

    def test_agent_run(self, booted, monkeypatch, capsys):
        async def fake_request(agent, request, session_id, llm_override=None,
                               llm_profile_info=None):
            _sent.append(request)
            # No summary: agent-run then prints the whole result as JSON.
            return {"summary": "", "calls": [{"result": self.SET_RESULT}]}

        monkeypatch.setattr(agent_run, "run_agent_request", fake_request)
        monkeypatch.setattr("sys.argv", ["agent-run", "was steht da"])

        agent_run.main()

        assert booted.sent, "the agent was never called -- nothing measured"
        assert "B01" in capsys.readouterr().out


class TestTheCapabilityCheckSeesTheModelTheRunUses:
    """--llm decides the model, so it decides whether an attachment may go.

    agent-cli built the --llm override about 500 lines after the capability
    check, and agent-run built it in time but still asked the agent: both
    measured `--llm <profile> --attach pic.png` against the agent's default
    model -- refused although the chosen model takes images, let through
    although it does not.
    """

    CHOSEN = "x-chosen"

    @pytest.fixture
    def chosen_profile(self, booted, monkeypatch):
        booted.config.llm_system.models[self.CHOSEN] = LLMModelConfig(
            provider="openai", model=self.CHOSEN)
        booted.config.llm_system.profiles["chosen"] = LLMProfile(model_ref=self.CHOSEN)
        # The override client is built for real; building it needs a key to exist.
        monkeypatch.setenv("OPENAI_API_KEY", "test-key-never-sent")
        return booted

    @staticmethod
    def _only_this_model_takes_images(monkeypatch, model_that_can):
        import agent_system.llm.capabilities as caps
        checked = []

        def fake(model, **counts):
            checked.append(model)
            return None if model == model_that_can else f"model {model} cannot take images"

        monkeypatch.setattr(caps, "ensure_model_supports", fake)
        return checked

    @staticmethod
    def _run(entry, png, monkeypatch):
        argv = ([entry, "--raw", "run", "was siehst du", "--llm", "chosen", "--attach", str(png)]
                if entry == "agent-cli"
                else [entry, "was siehst du", "--llm", "chosen", "--attach", str(png)])
        monkeypatch.setattr("sys.argv", argv)
        (cli.main if entry == "agent-cli" else agent_run.main)()

    @pytest.mark.parametrize("entry", ["agent-cli", "agent-run"])
    def test_a_chosen_model_that_takes_images_lets_the_picture_through(
            self, chosen_profile, monkeypatch, tmp_path, entry):
        checked = self._only_this_model_takes_images(monkeypatch, self.CHOSEN)
        png = TestEveryBucketReachesTheBuilder._png(tmp_path)

        self._run(entry, png, monkeypatch)

        assert checked == [self.CHOSEN]
        assert chosen_profile.sent, "refused against the agent's default model"

    @pytest.mark.parametrize("entry", ["agent-cli", "agent-run"])
    def test_a_chosen_model_without_images_refuses_the_picture(
            self, chosen_profile, monkeypatch, tmp_path, entry):
        # Only the agent's default model ("m") takes images here.
        checked = self._only_this_model_takes_images(monkeypatch, "m")
        png = TestEveryBucketReachesTheBuilder._png(tmp_path)

        with pytest.raises(SystemExit) as leaving:
            self._run(entry, png, monkeypatch)

        assert leaving.value.code == 1
        assert checked == [self.CHOSEN]
        assert not chosen_profile.sent, "sent although the chosen model cannot take images"
