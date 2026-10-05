"""Size guards of the sandbox, the print cap and the reported line.

One big-int C call holds the GIL for its whole run: measured before these
guards, ``pow(3, 10**6000, 10**6000 + 7)`` stopped the event loop of the whole
process for 9 s, ``x = 10**2000000; x * x`` for 7.7 s. Texts and lists could be
grown to any size with ``+``, ``ljust``, ``replace``, ``join``, ``extend`` or a
format width. Every refused case must answer at once; ordinary work must not
notice the guards.
"""

from __future__ import annotations

import time

import pytest

from plugins.script_interpreter.config import ScriptInterpreterConfig
from plugins.script_interpreter.executor import ScriptExecutor


def run(code: str, **config):
    return ScriptExecutor(ScriptInterpreterConfig(**config)).execute(code)


@pytest.mark.parametrize("code", [
    "r = pow(3, 10 ** 700, 10 ** 700 + 7)",       # exponent/modulus over 2048 bits
    "r = pow(3, 5, 10 ** 700)",                    # modulus alone
    "x = 10 ** 100000",                            # passed the old '**' guard
    # 2 ** 90000 passes the '**' guard; its square is the largest int allowed.
    "x = 2 ** 90000\ny = x * x\nz = y * x",        # int '*'
    "x = 2 ** 90000\ny = x * x\ny *= x",           # augmented path
    "x = 2 ** 90000\ny = x * x\nz = y % (3 ** 60000)",   # quadratic '%'
    "x = 2 ** 90000\ny = x * x\nz = y // (3 ** 60000)",
    "r = sum([[1]] * 1000, [])",                   # quadratic list concatenation
    "r = sorted([0] * 2000000)",                   # 6.5M items sorted for 0.8 s
    "l = [0] * 2000000\nl.sort()",
    "l = [0] * 2000000\nf = l.sort\nf()",
], ids=["powmod-exp", "powmod-mod", "pow", "mul", "mul-aug", "mod", "floordiv", "sum-lists",
        "sorted", "sort", "sort-value"])
def test_a_blocking_arithmetic_call_is_refused_at_once(code):
    t = time.perf_counter()
    res = run(code)
    elapsed = time.perf_counter() - t
    assert not res["success"], "refused nothing"
    message = res["error"]["message"]
    assert "too large" in message or "sum() adds numbers" in message, message
    assert elapsed < 0.5, f"took {elapsed:.2f}s"


@pytest.mark.parametrize("code", [
    's = "a" * 4000000\nt = s + s',
    's = "a" * 4000000\ns += s',
    "l = [0] * 4000000\nl.extend(l)",
    "l = [0] * 4000000\nf = l.extend\nf(l)",       # method taken as a value
    'r = "a".ljust(100000000)',
    'r = "a".rjust(100000000)',
    'r = "a".center(100000000)',
    'r = "a".zfill(100000000)',
    'f = "a".ljust\nr = f(100000000)',
    's = "ab" * 1000\nr = s.replace("a", "x" * 10000)',
    'r = ",".join(["x" * 1000000] * 7)',
    'r = f"{1:>200000000}"',
    'r = f"{1.5:.200000000f}"',
    'r = format(1, ">200000000")',
], ids=["plus", "plus-aug", "extend", "extend-value", "ljust", "rjust", "center", "zfill",
        "ljust-value", "replace", "join", "fstring-width", "fstring-precision", "format"])
def test_a_huge_text_or_list_is_refused_at_once(code):
    t = time.perf_counter()
    res = run(code)
    elapsed = time.perf_counter() - t
    assert not res["success"], "refused nothing"
    assert "too large" in res["error"]["message"]
    assert elapsed < 0.5, f"took {elapsed:.2f}s"


@pytest.mark.parametrize("code,expected", [
    ("r = 10 ** 50 * 10 ** 50", 10 ** 100),
    ("r = 12345678901234567890 % 97", 12345678901234567890 % 97),
    ("r = 2 ** 4096 // 3 > 0", True),
    ("r = pow(3, 1000, 1009)", pow(3, 1000, 1009)),
    ('r = "a".ljust(5, ".")', "a...."),
    ('r = "abc".replace("a", "xx")', "xxbc"),
    ('r = ",".join(["a", "b"])', "a,b"),
    ('r = f"{3.14159:.2f}|{7:>4}"', "3.14|   7"),
    ('r = format(255, "08b")', "11111111"),
    ("r = [1] + [2]", [1, 2]),
    ("l = [1]\nl.extend([2, 3])\nr = l", [1, 2, 3]),
    ("r = sum([1, 2], 10)", 13),
    ("r = sorted([3, 1, 2], reverse=True)", [3, 2, 1]),
    ("l = [3, 1, 2]\nl.sort()\nr = l", [1, 2, 3]),
])
def test_ordinary_work_is_not_refused(code, expected):
    res = run(code)
    assert res["success"], res["error"]
    assert res["variables"]["r"] == expected


def test_the_print_cap_cannot_be_caught_and_stops_the_buffer():
    # Appended before the check and raised as a RuntimeError: a script's
    # `except Exception:` printed on and the buffer grew past the cap.
    res = run(
        "for i in range(50):\n"
        "    try:\n"
        '        print("x" * 30)\n'
        "    except Exception:\n"
        "        pass\n"
        "r = 1",
        max_output_length=100)
    assert not res["success"], "the cap was caught"
    assert len(res["output"]) <= 100


@pytest.mark.parametrize("code,line", [
    ('x = 1\nd = {}\nfor i in range(2):\n    y = d["k"]\n', 4),        # inside a loop
    ('def f():\n    return {}["k"]\nx = 1\nf()\n', 2),                  # inside a function
    ('x = 1\nraise ValueError("bad value at line 7")\n', 2),            # "line 7" is text
    ("x = 1\ny = 2\nimport os\n", 3),                                   # unsupported node
])
def test_the_error_names_the_line_it_happened_on(code, line):
    res = run(code)
    assert not res["success"]
    assert res["error"]["line_number"] == line, res["error"]


# ---------------------------------------------------------------------------
# Round 3: text conversion, '%', round/int, aggregates, the memory fuse
# ---------------------------------------------------------------------------

# 20,000 items of a 4,000-digit int: str() of it took 3.57 s in one C call.
BIG_LIST = "b = 10 ** 4000\nx = [b] * 20000\n"


@pytest.mark.parametrize("code", [
    BIG_LIST + "print(x)",
    BIG_LIST + "s = str(x)",
    BIG_LIST + 's = f"{x}"',
    BIG_LIST + "s = format(x)",
    BIG_LIST + 's = "%s" % (x,)',
    BIG_LIST + 's = "%s"\ns %= (x,)',
    BIG_LIST + "t = tuple(x)\nd = {}\ny = d[t]",                    # KeyError text
    BIG_LIST + "t = tuple(x)\ntry:\n    y = {}[t]\nexcept KeyError as e:\n    r = e",
], ids=["print", "str", "fstring", "format", "percent", "percent-aug", "error-text",
        "except-as"])
def test_turning_a_big_container_into_text_is_refused_at_once(code):
    t = time.perf_counter()
    res = ScriptExecutor(ScriptInterpreterConfig(max_execution_time=60)).execute(code)
    elapsed = time.perf_counter() - t
    assert elapsed < 1.0, f"took {elapsed:.2f}s"
    if res["success"]:  # the except-as case: the handler got a short text
        assert "too large to show" in res["variables"]["r"]
    else:
        assert len(res["error"]["message"]) < 1000


@pytest.mark.parametrize("code", [
    'r = "%050000000d" % 1',
    'r = "%*d" % (50000000, 1)',
    'r = "%.*f" % (50000000, 1.0)',
    'r = "%(n)50000000s" % {"n": 1}',
], ids=["width", "star-width", "star-precision", "named"])
def test_a_huge_percent_width_is_refused_at_once(code):
    t = time.perf_counter()
    res = run(code)
    assert not res["success"], "refused nothing"
    assert "too large" in res["error"]["message"]
    assert time.perf_counter() - t < 0.5


@pytest.mark.parametrize("code", [
    "r = round(1, -10000000)",
    'r = int("1" * 6000000, 2)',
    "r = median([0] * 2000000)",
    "b = 10 ** 60000\nx = [b] * 1000\nr = sum(x)",
    "b = 10 ** 60000\nx = [b] * 1000\nr = max(x)",
    "b = 10 ** 60000\nx = [b] * 1000\nr = max(x, x)",           # several arguments
    "b = 10 ** 60000\nx = [b] * 1000\nr = min(x)",
    "b = 10 ** 60000\nx = [b] * 1000\nr = set(x)",
    "b = 10 ** 60000\nx = [b] * 1000\nr = sorted(x)",
    "x = [10 ** 300] * 120000\nr = mean(x)",  # works without the guard: only it refuses
], ids=["round", "int-base", "median", "sum", "max", "max-args", "min", "set", "sorted", "mean"])
def test_a_blocking_call_on_numbers_or_containers_is_refused_at_once(code):
    t = time.perf_counter()
    res = run(code)
    assert not res["success"], "refused nothing"
    assert "too large" in res["error"]["message"] or "too small" in res["error"]["message"], \
        res["error"]["message"]
    assert time.perf_counter() - t < 0.5


@pytest.mark.parametrize("code,expected", [
    ('r = "%5d|%-3s|%.2f" % (42, "a", 3.14159)', "   42|a  |3.14"),
    ('r = "%(n)s!" % {"n": 1}', "1!"),
    ('r = "100%%" % ()', "100%"),
    ("r = round(1234, -2)", 1200),
    ('r = int("ff", 16)', 255),
    ("r = median([3, 1, 2])", 2),
    ("r = sum(range(1000))", 499500),
    ("r = str([1, 'a'])", "[1, 'a']"),
    ("r = (2 ** 100001) > 0", True),       # the '**' estimate doubled for base 2
    ("r = (3 ** 70000) > 0", True),
])
def test_ordinary_work_is_not_refused_round_3(code, expected):
    res = run(code)
    assert res["success"], res["error"]
    assert res["variables"]["r"] == expected


def test_the_memory_fuse_aborts_and_cannot_be_caught():
    ex = ScriptExecutor(ScriptInterpreterConfig(max_execution_time=30, loop_timeout_seconds=30))
    reads = []

    def fake_rss():
        # +2 GB on the second reading only: an abort a script could catch
        # would let it finish normally after that.
        reads.append(1)
        return 10**9 + (2 * 1024**3 if len(reads) == 2 else 0)

    ex.safe_executor._read_rss = fake_rss
    res = ex.execute(
        "try:\n    n = 0\n    while n < 100000:\n        n += 1\n"
        "except Exception:\n    r = 'caught'")
    assert len(reads) >= 2, "the loop ended before a second reading -- nothing measured"
    assert not res["success"], res["variables"].get("r")
    assert "Memory grew" in res["error"]["message"]


def test_many_prints_stay_linear():
    # print summed the whole buffer on every call: 20,000 prints took seconds.
    t = time.perf_counter()
    res = run("for i in range(20000):\n    print('')", max_output_length=10**6,
              max_execution_time=60, loop_timeout_seconds=60)
    assert res["success"], res["error"]
    assert time.perf_counter() - t < 3.0


def test_the_tool_lists_variables_shortly():
    import asyncio
    from unittest.mock import Mock
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig
    from plugins.script_interpreter.server import ScriptInterpreterServer

    class _Status:
        async def progress(self, *a, **k): pass
        async def error(self, *a, **k): pass
        async def end(self, *a, **k): pass

    server = ScriptInterpreterServer("script_interpreter", Mock(spec=AgentSystemConfig),
                                     ToolServerConfig(type="script_interpreter", enabled=True))
    res = asyncio.run(server.call("script_interpreter_execute", {
        "code": 'x = 10 ** 5000\ny = "a" * 6000000\nz = 7', "_status": _Status(), "_session_id": "v"}))
    assert "error" not in res, res
    assert "omitted" in res["result"] and "z=7" in res["result"]
    assert len(res["result"]) < 1000


# ---------------------------------------------------------------------------
# Round 4: aliases, views and bytes, the fast content path, float sizes,
# min/max keywords
# ---------------------------------------------------------------------------

BIG_DICT = "b = 10 ** 4000\nd = {i: b for i in range(20000)}\n"


@pytest.mark.parametrize("code", [
    BIG_LIST + "f = str\ns = f(x)",                               # alias of str
    BIG_LIST + "f = set\ns = f(x)",                               # alias of set
    'f = int\nr = f("1" * 6000000, 2)',                            # alias of int
    BIG_LIST + "f = dict\nr = f([(b, 1)] * 20000)",                # alias of dict
    BIG_DICT + 's = f"{d.values()}"',                             # dict views
    BIG_DICT + 's = f"{d.items()}"',
    BIG_DICT + "r = max(d.values())",
    'x = b"\\x00" * 6000000\ns = str(x)',                         # bytes repr is 4x
], ids=["alias-str", "alias-set", "alias-int", "alias-dict", "values-view", "items-view",
        "max-view", "bytes"])
def test_aliases_views_and_bytes_are_guarded(code):
    t = time.perf_counter()
    res = ScriptExecutor(ScriptInterpreterConfig(max_execution_time=60, loop_timeout_seconds=60)).execute(code)
    elapsed = time.perf_counter() - t
    assert not res["success"], "refused nothing"
    assert "too large" in res["error"]["message"], res["error"]["message"]
    assert elapsed < 1.0, f"took {elapsed:.2f}s"


@pytest.mark.parametrize("values,body", [
    ([i * 0.5 + 0.25 for i in range(3000)], "d.append(x / max(nums))"),
    ([2 ** 100 + i for i in range(3000)], "d.append(max(nums) > x)"),  # past 64 bits
    ([f"name{i}" for i in range(3000)], "d.append(max(nums))"),
    ([i * 0.5 for i in range(2999)] + [None], "d.append(len(set(nums)))"),
], ids=["floats", "ints", "strings", "with-none"])
def test_an_aggregate_in_a_loop_stays_near_its_own_cost(values, body):
    # The Python walk made `x / max(nums)` in a 6000-step loop about 15 times
    # slower (over 4 s); before the guard it took ~0.3 s, with the C-speed
    # path ~0.6 s. At 3000 items the fast path stays well under the limit
    # and the walk (quadratic in the loop) far over it.
    ex = ScriptExecutor(ScriptInterpreterConfig(max_execution_time=60, loop_timeout_seconds=60))
    ex.safe_executor.variables["nums"] = values
    t = time.perf_counter()
    res = ex.execute(f"d = []\nfor x in nums:\n    {body}\nr = len(d)")
    assert res["success"], res["error"]
    assert time.perf_counter() - t < 1.5


def test_flat_sequences_are_measured_without_the_walk(monkeypatch):
    # The item-by-item walk is what made aggregates in a loop slow; a flat
    # sequence of one kind must be measured at C speed.
    import plugins.script_interpreter.safe_executor as se

    def no_walk(*args):
        raise AssertionError("walked item by item")

    monkeypatch.setattr(se, "estimate_text_size", no_walk)
    assert se.estimate_content_size(["ab", "cde"], 100) == 9
    assert se.estimate_content_size((0.5, None, True), 100) == 6
    assert se.estimate_content_size({1, 2 ** 40}, 100) == 4
    assert se.estimate_content_size([1, 2 ** 70], 100) == 2 * (int(71 * 0.30103) + 2)


def test_the_estimate_of_floats_follows_their_text():
    # Counted as 24 characters each, 900 rounded prices (6,138 chars of JSON)
    # were "over 20000".
    from plugins.script_interpreter.safe_executor import estimate_text_size
    value = [round(i * 0.37, 2) for i in range(900)]
    assert abs(estimate_text_size(value, 10**9) - len(str(value))) < 0.05 * len(str(value))


@pytest.mark.parametrize("code,expected", [
    ('r = max([{"n": 2}, {"n": 5}], key=lambda d: d["n"])["n"]', 5),
    ('r = min([{"n": 2}, {"n": 5}], key=lambda d: d["n"])["n"]', 2),
    ("r = max([], default=0)", 0),
    ("r = sum([0.5] * 1150000)", 575000.0),                        # F5: 1.15M floats
    ("r = sum([999] * 6500000)", 999 * 6500000),                   # small ints, at the length cap
])
def test_min_max_keywords_and_big_float_sums_work(code, expected):
    res = ScriptExecutor(ScriptInterpreterConfig(max_execution_time=60)).execute(code)
    assert res["success"], res["error"]
    assert res["variables"]["r"] == expected


def test_the_tool_decides_a_listed_value_by_its_own_length():
    # [999] * 40 is exactly 200 characters; its estimate is higher.
    import asyncio
    from unittest.mock import Mock
    from agent_system.config.models import AgentSystemConfig, ToolServerConfig
    from plugins.script_interpreter.server import ScriptInterpreterServer

    class _Status:
        async def progress(self, *a, **k): pass
        async def error(self, *a, **k): pass
        async def end(self, *a, **k): pass

    server = ScriptInterpreterServer("script_interpreter", Mock(spec=AgentSystemConfig),
                                     ToolServerConfig(type="script_interpreter", enabled=True))
    res = asyncio.run(server.call("script_interpreter_execute", {
        "code": "v = [999] * 40\nb = 10 ** 4000\nd = {i: b for i in range(20000)}\nw = d.values()",
        "_status": _Status(), "_session_id": "v4"}))
    assert "error" not in res, res
    assert "v=[999, 999" in res["result"]
    assert "w=<dict_values" in res["result"] and len(res["result"]) < 1000
