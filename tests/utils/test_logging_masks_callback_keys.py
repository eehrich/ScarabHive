"""No log keeps the key of a callback URL: whoever holds one sends its run's event (stategraph's callback activity).

Every line of the app reaches the handlers setup_logging hangs at the root. The access log does not: started as
`agent-api`, it is written nowhere (setup_logging keeps it from the root); started as `uvicorn ... --factory`, through
handlers uvicorn sets up itself. security.log has its own handler (tests/auth/test_security_audit.py).
"""
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from uvicorn.logging import AccessFormatter

from agent_system.utils import profiling
from agent_system.utils.logging import KeyInPathFilter, setup_logging

KEY = "k3y-only-its-holder-may-use-0123456789abcdef"
URL = f"/plugins/stategraph/callback/{KEY}"


@pytest.fixture
def app_log(tmp_path):
    root = logging.getLogger()
    before, level = list(root.handlers), root.level
    servers = {name: (logging.getLogger(name).level, logging.getLogger(name).propagate, list(logging.getLogger(name).filters))
               for name in ("uvicorn.access", "uvicorn.error")}
    path = tmp_path / "api.log"
    setup_logging(enabled=True, level="INFO", file_path=str(path), rotation_enabled=False)
    yield path
    for handler in list(root.handlers):
        root.removeHandler(handler)
        if handler not in before:
            handler.close()
    for handler in before:
        root.addHandler(handler)
    root.setLevel(level)
    for name, (lvl, propagate, filters) in servers.items():
        logger = logging.getLogger(name)
        logger.level, logger.propagate, logger.filters = lvl, propagate, filters


def _written(path):
    for handler in logging.getLogger().handlers:
        handler.flush()
    return path.read_text(encoding="utf-8")


def _access_line(tmp_path, path):
    """One access log line, written the way `uvicorn --factory` writes it: its own handler, its AccessFormatter."""
    access = logging.getLogger("uvicorn.access")
    own = logging.FileHandler(tmp_path / "access.log", encoding="utf-8")
    own.setFormatter(AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=False))
    access.addHandler(own)
    try:
        access.info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:50000", "POST", path, "1.1", 404)
    finally:
        access.removeHandler(own)
        own.close()
    return (tmp_path / "access.log").read_text(encoding="utf-8")


def test_the_app_log_masks_the_key(app_log):
    logging.getLogger("agent_system.auth.enforcement").info(f"[SECURITY] Unauthorized access attempt: GET {URL}")
    logging.getLogger("agent_system").info("sent %s to %s", "the event", URL)

    text = _written(app_log)

    assert "Unauthorized access attempt: GET /plugins/stategraph/callback/***" in text, text
    assert "sent the event to /plugins/stategraph/callback/***" in text, text
    assert KEY not in text


def test_the_access_log_masks_the_key_whichever_handler_writes_it(app_log, tmp_path):
    text = _access_line(tmp_path, f"{URL}?x=1")

    assert '"POST /plugins/stategraph/callback/***?x=1 HTTP/1.1" 404' in text, text  # the rest of the line stays
    assert KEY not in text


@pytest.mark.parametrize("path,shown", [
    (f"/plugins/stategraph/callback?token={KEY}", "/plugins/stategraph/callback?token=***"),
    (f"/plugins/stategraph/callback?x=1&token={KEY}&y=2", "/plugins/stategraph/callback?x=1&token=***&y=2"),
    (f"/plugins/stategraph/callback/?token={KEY}", "/plugins/stategraph/callback/?token=***"),  # redirected, logged
    # a path whose ? and = came encoded: uvicorn writes it so, re-quoted
    (f"/plugins/stategraph/callback%3fToken%3D{KEY}", "/plugins/stategraph/callback%3fToken%3D***"),
    (f"/plugins/stategraph/callback%3Fx%3D1%26token%3D{KEY}%26y%3D2",
     "/plugins/stategraph/callback%3Fx%3D1%26token%3D***%26y%3D2"),
    # every token parameter: the route reads the last one, a sender may add its own before or after the real one
    (f"/plugins/stategraph/callback?token={KEY}&Token=x", "/plugins/stategraph/callback?token=***&Token=***"),
    (f"/plugins/stategraph/callback?token=x&token={KEY}", "/plugins/stategraph/callback?token=***&token=***"),
    (f"/plugins/stategraph/callback?next=/x&token={KEY}", "/plugins/stategraph/callback?next=/x&token=***"),
    (f"/plugins/stategraph/callback?next=%2Fplugins%2Fb%2Fcallback%2F{KEY}&token=x",
     "/plugins/stategraph/callback?next=%2Fplugins%2Fb%2Fcallback%2F***&token=***"),
])
def test_the_key_in_the_query_of_a_callback_url_is_masked_too(app_log, tmp_path, path, shown):
    """stategraph puts the key in the query: network.remote_paths lists exact paths, a key in the path never fits."""
    logging.getLogger("agent_system").info("sent the event to https://hive.example%s", path)
    access = _access_line(tmp_path, path)
    logging.getLogger("httpx").info("HTTP Request: GET %s", "https://hook.example/relay?to=%2Fplugins%2Fstategraph"
                                                          f"%2Fcallback%3Fx%3D1%26token%3D{KEY}%26y%3D2&z=3")

    text = _written(app_log)

    assert f'"POST {shown} HTTP/1.1" 404' in access, access
    assert f"sent the event to https://hive.example{shown}" in text, text
    assert "%2Fcallback%3Fx%3D1%26token%3D***%26y%3D2&z=3" in text, text
    assert KEY not in text + access


@pytest.mark.parametrize("repeated", ["/plugins/a/callback?x&", "%2Fplugins%2Fa%2Fcallback%3Fx%26",
                                      "/plugins/a/callback%3Fx%26"])
def test_a_line_full_of_callback_urls_costs_linear_time(repeated):
    """Anyone may send such a path, and the access log masks it on the event loop: quadratic, this took seconds."""
    import time

    from agent_system.utils.logging import loggable_path

    line = "GET /x?" + repeated * (200_000 // len(repeated))
    start = time.perf_counter()
    loggable_path(line)

    assert time.perf_counter() - start < 1.0


def test_a_traceback_and_a_url_passed_on_encoded_keep_no_key(app_log):
    try:
        raise RuntimeError(f"the machine could not reach {URL}")
    except RuntimeError:
        logging.getLogger("agent_system").exception("a call failed")
    logging.getLogger("httpx").info("HTTP Request: GET %s", "https://hook.example/relay?to=%2Fplugins%2Fstategraph"
                                                          f"%2Fcallback%2F{KEY}&x=1")

    text = _written(app_log)

    assert "could not reach /plugins/stategraph/callback/***" in text, text
    assert "%2Fcallback%2F***&x=1" in text, text
    assert KEY not in text


def test_a_callback_segment_deeper_in_a_path_is_no_key(app_log, tmp_path):
    """Only the segment right after /plugins/<plugin>/callback/ is a key; a route further down keeps its path."""
    logging.getLogger("agent_system").info("%d of %s", 3, "/plugins/stategraph/api/callback/list")

    assert "3 of /plugins/stategraph/api/callback/list" in _written(app_log)
    assert '"POST /plugins/stategraph/api/callback/list HTTP/1.1"' in _access_line(
        tmp_path, "/plugins/stategraph/api/callback/list")


def test_an_argument_that_cannot_be_shown_is_the_handlers_to_report_not_the_filters():
    """A filter's exception reaches the caller (Handler.handle does not catch it); the handler's is reported.

    Tested on the filter itself: under pytest the capture handler raises what a handler would report."""
    class Unprintable:
        def __str__(self):
            raise RuntimeError("no text")

    def record(msg, args):
        return logging.LogRecord("agent_system", logging.WARNING, __file__, 1, msg, args, None)

    beside = record("value %s at %s", (Unprintable(), URL))
    broken_merge = record(f"relay to %2Fplugins%2Fstategraph%2Fcallback%2F{KEY} for %s", ("x",))  # %2F: a format
    unprintable_message = record(Unprintable(), ())

    assert KeyInPathFilter().filter(beside) is True
    assert KeyInPathFilter().filter(broken_merge) is True
    assert KeyInPathFilter().filter(unprintable_message) is True
    assert beside.args[1] == "/plugins/stategraph/callback/***"  # what the handler's report on stderr shows
    assert KEY not in broken_merge.msg


def test_a_record_without_a_key_keeps_its_arguments(app_log):
    from types import MappingProxyType
    headers = MappingProxyType({"accept": "*/*", "host": "hive"})  # a Mapping, not a dict: logging keeps it whole

    logging.getLogger("agent_system").info("headers: %s", headers)
    plain = logging.LogRecord("agent_system", logging.INFO, __file__, 1, "%s of %s", ("one", ["two"]), None)
    kept = plain.args
    KeyInPathFilter().filter(plain)

    assert "headers: {'accept': '*/*', 'host': 'hive'}" in _written(app_log)
    assert plain.args is kept  # the very objects, not copies


def test_a_mapping_that_is_no_dict_is_masked_too(app_log):
    from types import MappingProxyType

    logging.getLogger("agent_system").info("%(method)s %(referer)s",
                                           MappingProxyType({"method": "POST", "referer": URL}))

    text = _written(app_log)

    assert "POST /plugins/stategraph/callback/***" in text, text
    assert KEY not in text


def test_with_logging_switched_off_the_access_log_still_masks_the_key(tmp_path):
    access = logging.getLogger("uvicorn.access")
    kept, kept_last = list(access.filters), list(logging.lastResort.filters)
    try:
        setup_logging(enabled=False, level="INFO", file_path=str(tmp_path / "unused.log"))
        text = _access_line(tmp_path, URL)
        # a process without any handler (agent-cli with logging off) writes WARNING+ through logging's last resort
        last_resort_masks = any(isinstance(f, KeyInPathFilter) for f in logging.lastResort.filters)
    finally:
        access.filters, logging.lastResort.filters = kept, kept_last

    assert '"POST /plugins/stategraph/callback/*** HTTP/1.1" 404' in text, text
    assert last_resort_masks


def test_the_profiling_report_keeps_no_key(monkeypatch):
    monkeypatch.setattr(profiling, "PROFILING_ENABLED", True)
    app = FastAPI()

    @app.get("/plugins/stategraph/callback/{token}")
    async def callback(token: str):
        return {}

    profiling.add_profiling_middleware(app)
    TestClient(app).get(URL)

    assert profiling.get_profiler()._completed_requests[-1].path == "/plugins/stategraph/callback/***"
