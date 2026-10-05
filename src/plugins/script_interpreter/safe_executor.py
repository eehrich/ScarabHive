"""
Alternative execution approach using Python's ast module with safety restrictions.
This allows for loops and if statements while maintaining security.
"""

import ast
import time
import builtins
import math
import re
from .errors import UnsupportedFeatureError

try:
    import psutil
except ImportError:  # pragma: no cover - psutil is a core dependency
    psutil = None

#: Seeded into every sandbox's variable table so ``isinstance(x, int)`` works.
#: They are NOT user variables: anything that counts or lists variables has to
#: subtract them, or a script that assigned one name reports nine -- which is
#: what the status line and the "Variables:" output block both did.
_SEEDED_TYPES = {
    'int': int,
    'str': str,
    'float': float,
    'bool': bool,
    'list': list,
    'dict': dict,
    'tuple': tuple,
    'set': set,
}
SEEDED_TYPE_NAMES = frozenset(_SEEDED_TYPES)
#: The seeded types by identity: `f = set; f(big)` must meet the same guard
#: as `set(big)` -- keyed on the called NAME, the alias walked past it.
_SEEDED_BY_TYPE = {t: n for n, t in _SEEDED_TYPES.items()}

#: Largest int an arithmetic operation may produce or work on, in bits (about
#: 60,000 decimal digits). One big-int C call holds the GIL for its whole run:
#: measured, a 100,000-bit square takes ~1.5 ms, a 2,000,000-bit one 160 ms,
#: and `%` grows quadratically from there -- the event loop of the whole
#: process stood still for seconds.
_MAX_INT_BITS = 200_000
#: Three-argument pow(): the exponent's and the modulus's bit lengths. 1024/1024
#: took ~2 ms, 8192/8192 over 700 ms.
_MAX_POWMOD_BITS = 2048
#: Items one sort may order.
_MAX_SORT_LEN = 1_000_000
#: Text methods whose result can be much longer than their inputs.
_GROWING_STR_METHODS = frozenset({"ljust", "rjust", "center", "zfill", "replace", "join"})
#: List methods guarded the same way.
_GUARDED_LIST_METHODS = frozenset({"extend", "sort"})
#: round(int, -n) computes 10**n in C.
_MAX_ROUND_DIGITS = 60_000
#: Estimated text size of what one aggregate C call (sum, min, max, set,
#: sorted, the statistics) walks: summing or hashing 6.5 million copies of a
#: 200,000-bit int is one call of many seconds.
_MAX_CONTENT_CHARS = 30_000_000
#: Growth of the process's resident memory during one script, and how often
#: it is read. A loop that keeps what it builds grew by ~6.4 GB/s.
_MEMORY_FUSE_MB = 1024
_MEMORY_CHECK_SECONDS = 0.05


def estimate_text_size(value, limit):
    """About how many characters ``str``/``repr``/JSON of ``value`` takes.

    Walks containers iteratively and stops as soon as the count passes
    ``limit`` (the result is then > limit): turning a container into text is
    one C call, and ``str([10**4000] * 6_500_000)`` would take ~19 minutes and
    26 GB before any check after it. A cycle only adds up and ends the walk.
    """
    total = 0
    stack = [value]
    while stack:
        v = stack.pop()
        if isinstance(v, str):
            total += len(v) + 2
        elif isinstance(v, bool) or v is None:
            total += 5
        elif isinstance(v, int):
            total += int(v.bit_length() * 0.30103) + 2
        elif isinstance(v, float):
            total += len(repr(v))
        elif isinstance(v, (bytes, bytearray)):
            total += 4 * len(v) + 3
        elif isinstance(v, dict):
            total += 2 + 4 * len(v)
            if total > limit:
                return total
            stack.extend(v.keys())
            stack.extend(v.values())
        elif isinstance(v, (list, tuple, set, frozenset)):
            total += 2 + 2 * len(v)
            if total > limit:
                return total
            stack.extend(v)
        elif not isinstance(v, type) and hasattr(v, "__len__") and hasattr(v, "__iter__"):
            # Anything else sized and iterable -- dict views above all -- is
            # walked like a list: counted as 64, `f"{d.values()}"` built
            # 80 M characters.
            try:
                total += 2 + 2 * len(v)
                if total > limit:
                    return total
                stack.extend(v)
            except Exception:
                total += 64
        else:
            total += 64
        if total > limit:
            return total
    return total


_SCALAR_TYPES = frozenset({float, bool, type(None)})
_NUMBER_TYPES = frozenset({int, float, bool})
_FLAT_SEQUENCES = (list, tuple, set, frozenset,
                   type({}.keys()), type({}.values()))


def estimate_content_size(value, limit):
    """How much an aggregate C call (sum, min, max, set, sorted, ...) has
    to walk, in characters of content -- floats, bools and None count 2
    each: the call does constant work per item for them.

    A flat sequence of one kind is measured at C speed: the Python walk made
    `x / max(nums)` in a loop about 15 times slower than before the guard.
    Only a mixed or nested value, or one the quick bound puts over the
    limit, is walked item by item.
    """
    if isinstance(value, _FLAT_SEQUENCES):
        kinds = set(map(type, value))
        n = len(value)
        if kinds <= _SCALAR_TYPES:
            return 2 * n
        if kinds <= _NUMBER_TYPES:
            largest = max(map(abs, value), default=0)
            if not isinstance(largest, int) or largest.bit_length() <= 64:
                return 2 * n  # machine-size numbers: constant work per item
            quick = n * (int(largest.bit_length() * 0.30103) + 2)
            if quick <= limit:
                return quick
        elif kinds == {str}:
            return sum(map(len, value)) + 2 * n
    return estimate_text_size(value, limit)


def _rss_bytes():
    """This process's resident memory, or None without psutil."""
    if psutil is None:  # pragma: no cover
        return None
    return psutil.Process().memory_info().rss


class OutputLimitExceeded(BaseException):
    """print() went past max_output_length. A BaseException, so a script's
    ``except Exception:`` cannot catch it and print on."""


class MemoryLimitExceeded(BaseException):
    """The process grew by more than the memory fuse during the script.
    Not catchable, like OutputLimitExceeded."""


class FunctionReturn(Exception):
    """Exception used to handle function returns in SafeExecutor."""
    def __init__(self, value):
        self.value = value
        super().__init__()

class SafeExecutor:
    """Safe Python executor that allows more AST features while maintaining security."""
    
    def __init__(self, config):
        self.config = config
        self.allowed_functions = set(config.allowed_functions)
        # Seeded so isinstance() works; SEEDED_TYPE_NAMES is the same set,
        # for anyone who REPORTS variables.
        self.variables = dict(_SEEDED_TYPES)
        self.user_functions = {}  # Store user-defined functions
        self.output_buffer = []
        self._output_len = 0  # length of "\n".join(output_buffer), kept running
        self.start_time = None
        self.loop_count = 0
        # Memory fuse: the RSS reader is an attribute so a test can fake it.
        self._read_rss = _rss_bytes
        self._rss_start = None
        self._next_memory_check = 0.0
        self._memory_fuse_bytes = _MEMORY_FUSE_MB * 1024 * 1024

        # DoS guards for single uninterruptible C-level operations. A `**` or a
        # sequence `*` int runs as ONE C call, so check_timeout() (only evaluated
        # at AST-node boundaries) cannot interrupt it and it can block the
        # interpreter / exhaust memory. We bound the RESULT size before computing
        # it. (resource.setrlimit would be the OS-level alternative but is
        # Unix-only.) Pow is bounded by compute time (~1 MB result is sub-100ms);
        # sequence-multiply by the configured memory budget (worst-case 8 B/elem).
        self._max_pow_result_bits = _MAX_INT_BITS
        # One cap on the length of any text or list an operation builds
        # (`*`, `+`, extend, ljust/center/replace/join, format widths).
        self._max_seq_len = max(1, int(getattr(self.config, "max_memory_mb", 50)) * 1024 * 1024 // 8)

    def _refuse_length(self, what, length):
        if length > self._max_seq_len:
            raise ValueError(
                f"{what} result too large ({length} elements exceeds the "
                f"{self._max_seq_len}-element limit); rejected"
            )

    def _guard_binop_size(self, op, left, right):
        """Reject an operation whose result would be huge, before it runs.

        Raises ValueError (formatted into a clean error result by execute()).
        Covers `**`, and `*`, `//`, `%` on big ints (one C call each, holding
        the GIL), `*`/`+` building a long text or list, and `%` formatting.
        """
        if isinstance(op, ast.Mod) and isinstance(left, str):
            self._guard_percent(left, right)
            return
        if isinstance(op, ast.Pow):
            if (isinstance(left, int) and isinstance(right, int)
                    and right > 0 and left not in (-1, 0, 1)):
                est_bits = (right * math.log2(abs(left)) if right.bit_length() < 64
                            else math.inf)
                if est_bits > self._max_pow_result_bits:
                    raise ValueError(
                        f"'**' result too large (~{est_bits} bits exceeds the "
                        f"{self._max_pow_result_bits}-bit limit); rejected to "
                        f"avoid blocking the interpreter"
                    )
            return
        if (isinstance(op, (ast.Mult, ast.FloorDiv, ast.Mod))
                and isinstance(left, int) and isinstance(right, int)):
            bits = left.bit_length() + right.bit_length()
            if bits > _MAX_INT_BITS:
                raise ValueError(
                    f"int operands too large ({bits} bits exceeds the "
                    f"{_MAX_INT_BITS}-bit limit); rejected to avoid blocking "
                    f"the interpreter"
                )
            return
        if isinstance(op, ast.Mult):
            seq, n = None, None
            if isinstance(left, (str, bytes, bytearray, list, tuple)) and isinstance(right, int):
                seq, n = left, right
            elif isinstance(right, (str, bytes, bytearray, list, tuple)) and isinstance(left, int):
                seq, n = right, left
            if seq is not None and n > 0:
                self._refuse_length("'*'", len(seq) * n)
        elif isinstance(op, ast.Add):
            if (isinstance(left, (str, list, tuple)) and isinstance(right, (str, list, tuple))):
                self._refuse_length("'+'", len(left) + len(right))

    def _guard_method_size(self, obj, name, args, kwargs):
        """Reject a text or list method whose result would be huge."""
        if isinstance(obj, str) and name in ("ljust", "rjust", "center", "zfill"):
            width = args[0] if args else kwargs.get("width")
            if isinstance(width, int):
                self._refuse_length(f"'{name}'", width)
        elif isinstance(obj, str) and name == "replace" and len(args) >= 2:
            old, new = args[0], args[1]
            if isinstance(old, str) and isinstance(new, str) and len(new) > len(old):
                n = obj.count(old) if old else len(obj) + 1
                if len(args) > 2 and isinstance(args[2], int) and args[2] >= 0:
                    n = min(n, args[2])
                self._refuse_length("'replace'", len(obj) + n * (len(new) - len(old)))
        elif isinstance(obj, str) and name == "join" and args and hasattr(args[0], "__len__"):
            items = args[0]
            chars = len(items) if isinstance(items, str) else sum(
                len(i) for i in items if isinstance(i, str))
            self._refuse_length("'join'", chars + len(obj) * max(len(items) - 1, 0))
        elif isinstance(obj, list) and name == "extend" and args and hasattr(args[0], "__len__"):
            self._refuse_length("'extend'", len(obj) + len(args[0]))
        elif isinstance(obj, list) and name == "sort":
            self._refuse_sort(len(obj))

    def _refuse_sort(self, length):
        """A sort is one C call that holds the GIL: 6.5 million items took
        0.8 s, one million about a tenth of that."""
        if length > _MAX_SORT_LEN:
            raise ValueError(
                f"sort too large ({length} items exceeds the {_MAX_SORT_LEN}-item "
                f"limit); rejected to avoid blocking the interpreter")

    def _guard_text(self, value, extra=0):
        """Reject turning ``value`` into a text longer than the length cap."""
        size = estimate_text_size(value, self._max_seq_len) + extra
        if size > self._max_seq_len:
            raise ValueError(
                f"text result too large (over {self._max_seq_len} characters); rejected")

    def _guard_content(self, value):
        """Reject an aggregate C call (sum, min, max, set, sorted, the
        statistics) over more content than it can walk in milliseconds."""
        if estimate_content_size(value, _MAX_CONTENT_CHARS) > _MAX_CONTENT_CHARS:
            raise ValueError(
                "argument too large for one call (over "
                f"{_MAX_CONTENT_CHARS} characters of content); rejected")

    def _guard_type_call(self, func_name, args):
        """Size guards of the type constructors. ``int``, ``str``, ``set`` and
        ``dict`` are seeded as variables, so a script's call reaches the type
        itself, not safe_builtin_function -- the guards must run on that path."""
        if func_name == "str" and args:
            self._guard_text(args[0])
        elif func_name in ("set", "dict") and args:
            self._guard_content(args[0])
        elif (func_name == "int" and len(args) == 2 and isinstance(args[0], str)
                and isinstance(args[1], int)):
            # Base 10 has CPython's 4300-digit limit; power-of-two bases do
            # not: int("1" * 6_000_000, 2) built a 6M-bit int.
            bits = len(args[0]) * math.log2(args[1] if args[1] >= 2 else 36)
            if bits > _MAX_INT_BITS:
                raise ValueError(
                    f"int() result too large (~{int(bits)} bits exceeds the "
                    f"{_MAX_INT_BITS}-bit limit); rejected")

    def _guard_percent(self, fmt, values):
        """Reject ``fmt % values`` whose widths, precisions or values would
        build a huge text. A ``*`` width is taken from the values, in order."""
        self._guard_text(values, len(fmt))
        args = list(values) if isinstance(values, tuple) else [values]
        i = 0
        for m in re.finditer(r"%(?:\([^)]*\))?[-+ #0]*(\*|\d+)?(?:\.(\*|\d+))?([a-zA-Z%])", fmt):
            if m.group(3) == "%":
                continue
            for field in (m.group(1), m.group(2)):
                if field == "*":
                    n = args[i] if i < len(args) else 0
                    i += 1
                elif field:
                    n = int(field) if len(field) <= 9 else self._max_seq_len + 1
                else:
                    continue
                if isinstance(n, int) and abs(n) > self._max_seq_len:
                    raise ValueError(
                        f"'%' width/precision too large ({n}); rejected")
            i += 1

    def _exception_text(self, error):
        """``str(error)``, unless its arguments would make a huge text."""
        if estimate_text_size(error.args, self._max_seq_len) > self._max_seq_len:
            return f"{type(error).__name__} with a message too large to show"
        return str(error)

    def _guard_format_spec(self, spec):
        """Reject a format spec whose width or precision would build a huge text."""
        for digits in re.findall(r"\d+", str(spec)):
            if len(digits) > 9 or int(digits) > self._max_seq_len:
                raise ValueError(
                    f"format width/precision too large ({digits[:12]}); rejected")

    def safe_print(self, *args):
        """Safe print function that captures output."""
        # Checked BEFORE appending, and not catchable: appended first and
        # raised as a RuntimeError, a script's `except Exception:` kept
        # printing and the buffer kept growing. The size is estimated before
        # str() runs -- str() of a big container is one long C call.
        separator = 1 if self.output_buffer else 0
        remaining = self.config.max_output_length - self._output_len - separator
        estimate = sum(len(a) if isinstance(a, str) else estimate_text_size(a, remaining)
                       for a in args) + max(len(args) - 1, 0)
        # A container's estimate may run a little over its text; twice the
        # remaining room refuses only what is clearly too large, the exact
        # check below does the rest.
        if estimate > 2 * remaining + 64:
            raise OutputLimitExceeded("Output exceeds maximum allowed length")
        output = " ".join(str(arg) for arg in args)
        if len(output) > remaining:
            raise OutputLimitExceeded("Output exceeds maximum allowed length")
        self.output_buffer.append(output)
        self._output_len += separator + len(output)
    
    def check_timeout(self):
        """Check if execution has timed out -- and the memory fuse."""
        now = time.time()
        if self.start_time and now - self.start_time > self.config.max_execution_time:
            raise TimeoutError(f"Execution exceeded {self.config.max_execution_time} seconds")
        if now >= self._next_memory_check:
            self._next_memory_check = now + _MEMORY_CHECK_SECONDS
            self._check_memory()

    def _check_memory(self):
        rss = self._read_rss()
        if rss is None:
            return
        if self._rss_start is None:
            self._rss_start = rss
        elif rss - self._rss_start > self._memory_fuse_bytes:
            raise MemoryLimitExceeded(
                f"Memory grew by more than {self._memory_fuse_bytes // (1024 * 1024)} MB "
                f"during the script; aborted")
    
    def check_loop_timeout(self, loop_start_time):
        """Check if a loop has timed out."""
        if time.time() - loop_start_time > self.config.loop_timeout_seconds:
            raise TimeoutError(f"Loop exceeded {self.config.loop_timeout_seconds} seconds")
    
    def safe_builtin_function(self, func_name, args, kwargs=None):
        """Execute allowed builtin functions safely."""
        if kwargs is None:
            kwargs = {}
        if func_name not in self.allowed_functions:
            raise RuntimeError(f"Function '{func_name}' is not allowed")
            
        # Statistics functions
        if func_name == "mean":
            if len(args) != 1:
                raise ValueError("mean() takes exactly one argument")
            values = args[0]
            self._guard_content(values)
            if not values:
                raise ValueError("mean() requires non-empty sequence")
            return sum(values) / len(values)
        elif func_name == "median":
            if len(args) != 1:
                raise ValueError("median() takes exactly one argument")
            if hasattr(args[0], "__len__"):
                self._refuse_sort(len(args[0]))
            self._guard_content(args[0])
            values = sorted(args[0])
            n = len(values)
            if n == 0:
                raise ValueError("median() requires non-empty sequence")
            if n % 2 == 1:
                return values[n // 2]
            else:
                return (values[n // 2 - 1] + values[n // 2]) / 2
        elif func_name == "mode":
            if len(args) != 1:
                raise ValueError("mode() takes exactly one argument")
            values = args[0]
            self._guard_content(values)
            if not values:
                raise ValueError("mode() requires non-empty sequence")
            counts = {}
            for value in values:
                counts[value] = counts.get(value, 0) + 1
            max_count = max(counts.values())
            modes = [k for k, v in counts.items() if v == max_count]
            return modes[0]
        elif func_name == "stdev":
            if len(args) != 1:
                raise ValueError("stdev() takes exactly one argument")
            values = args[0]
            self._guard_content(values)
            if len(values) < 2:
                raise ValueError("stdev() requires at least 2 values")
            mean_val = sum(values) / len(values)
            variance = sum((x - mean_val) ** 2 for x in values) / (len(values) - 1)
            return variance ** 0.5
        # Regular builtins
        elif func_name == "print":
            self.safe_print(*args)
            return None
        elif func_name in ("min", "max"):
            # key= (a lambda) and default= pass through; they were dropped,
            # so max(rows, key=...) failed.
            extra = {k: kwargs[k] for k in ("key", "default") if k in kwargs}
            if len(args) == 1:
                self._guard_content(args[0])
                return getattr(builtins, func_name)(args[0], **extra)
            self._guard_content(args)
            return getattr(builtins, func_name)(args, **extra)
        elif func_name == "sum":
            self._guard_content(args[0] if args else args)
            if len(args) == 1:
                return sum(args[0])
            elif len(args) == 2:
                # A list or text start makes sum() a quadratic concatenation
                # of everything in one C call.
                if not isinstance(args[1], (int, float)):
                    raise ValueError("sum() adds numbers; use extend() for lists "
                                     "or join() for text")
                return sum(args[0], args[1])
            else:
                return sum(args[0])
        elif func_name == "len":
            return len(args[0])
        elif func_name == "range":
            # Prevent huge ranges
            try:
                if len(args) == 1 and isinstance(args[0], int):
                    length = args[0]
                elif len(args) == 2 and all(isinstance(a, int) for a in args):
                    length = max(0, args[1] - args[0])
                elif len(args) == 3 and all(isinstance(a, int) for a in args):
                    start, stop, step = args
                    if step == 0:
                        raise ValueError("range() step argument cannot be zero")
                    length = max(0, (stop - start + (step - 1)) // step)
                else:
                    raise ValueError("Dynamic range() arguments not allowed")
                    
                if length > self.config.max_loop_iterations:
                    raise ValueError(f"range() too large: size {length} exceeds maximum {self.config.max_loop_iterations}")
                    
                return list(range(*args))
            except Exception as e:
                raise ValueError(f"Error evaluating range(): {e}")
        elif func_name in ["round", "abs", "int", "float", "str", "bool", "sorted"]:
            if func_name == "sorted" and args and hasattr(args[0], "__len__"):
                self._refuse_sort(len(args[0]))
                self._guard_content(args[0])
            elif func_name in ("str", "int"):
                self._guard_type_call(func_name, args)
            elif (func_name == "round" and len(args) == 2 and isinstance(args[0], int)
                    and isinstance(args[1], int) and args[1] < -_MAX_ROUND_DIGITS):
                # CPython computes 10**-ndigits for an int: round(1, -10**7)
                # stood the event loop still for 5.75 s.
                raise ValueError(
                    f"round() ndigits too small ({args[1]}); rejected to avoid "
                    f"blocking the interpreter")
            return getattr(builtins, func_name)(*args, **kwargs)
        elif func_name == "list":
            if len(args) == 0:
                return []
            elif len(args) == 1:
                return list(args[0])
            else:
                raise ValueError("list() takes at most 1 argument")
        elif func_name == "tuple":
            if len(args) == 0:
                return ()
            elif len(args) == 1:
                return tuple(args[0])
            else:
                raise ValueError("tuple() takes at most 1 argument")
        elif func_name == "dict":
            if len(args) == 0:
                return {}
            elif len(args) == 1:
                return dict(args[0])
            else:
                raise ValueError("dict() takes at most 1 argument")
        elif func_name == "set":
            if len(args) == 0:
                return set()
            elif len(args) == 1:
                self._guard_type_call("set", args)
                return set(args[0])
            else:
                raise ValueError("set() takes at most 1 argument")
        elif func_name == "enumerate":
            if len(args) == 1:
                # enumerate(iterable) - start from 0
                iterable = args[0]
                return [(i, value) for i, value in enumerate(iterable)]
            elif len(args) == 2:
                # enumerate(iterable, start) - start from given value
                iterable, start = args
                if not isinstance(start, int):
                    raise TypeError("enumerate() start must be an integer")
                return [(i, value) for i, value in enumerate(iterable, start)]
            else:
                raise ValueError("enumerate() takes 1 or 2 arguments")
        # Exception constructors
        elif func_name == "ValueError":
            if len(args) == 0:
                return ValueError()
            elif len(args) == 1:
                return ValueError(args[0])
            else:
                return ValueError(*args)
        elif func_name == "RuntimeError":
            if len(args) == 0:
                return RuntimeError()
            elif len(args) == 1:
                return RuntimeError(args[0])
            else:
                return RuntimeError(*args)
        elif func_name == "TypeError":
            if len(args) == 0:
                return TypeError()
            elif len(args) == 1:
                return TypeError(args[0])
            else:
                return TypeError(*args)
        elif func_name == "type":
            if len(args) == 1:
                return type(args[0])
            else:
                raise ValueError("type() takes exactly 1 argument")
        elif func_name == "isinstance":
            if len(args) == 2:
                obj, class_or_tuple = args
                return isinstance(obj, class_or_tuple)
            else:
                raise ValueError("isinstance() takes exactly 2 arguments")
        # Math constants
        elif func_name == "pi":
            if len(args) != 0:
                raise ValueError("pi() takes no arguments")
            return math.pi
        elif func_name == "e":
            if len(args) != 0:
                raise ValueError("e() takes no arguments")
            return math.e
        # Math functions (single argument)
        elif func_name == "sqrt":
            if len(args) != 1:
                raise ValueError("sqrt() takes exactly one argument")
            return math.sqrt(args[0])
        elif func_name == "sin":
            if len(args) != 1:
                raise ValueError("sin() takes exactly one argument")
            return math.sin(args[0])
        elif func_name == "cos":
            if len(args) != 1:
                raise ValueError("cos() takes exactly one argument")
            return math.cos(args[0])
        elif func_name == "tan":
            if len(args) != 1:
                raise ValueError("tan() takes exactly one argument")
            return math.tan(args[0])
        elif func_name == "asin":
            if len(args) != 1:
                raise ValueError("asin() takes exactly one argument")
            return math.asin(args[0])
        elif func_name == "acos":
            if len(args) != 1:
                raise ValueError("acos() takes exactly one argument")
            return math.acos(args[0])
        elif func_name == "atan":
            if len(args) != 1:
                raise ValueError("atan() takes exactly one argument")
            return math.atan(args[0])
        elif func_name == "sinh":
            if len(args) != 1:
                raise ValueError("sinh() takes exactly one argument")
            return math.sinh(args[0])
        elif func_name == "cosh":
            if len(args) != 1:
                raise ValueError("cosh() takes exactly one argument")
            return math.cosh(args[0])
        elif func_name == "tanh":
            if len(args) != 1:
                raise ValueError("tanh() takes exactly one argument")
            return math.tanh(args[0])
        elif func_name == "log":
            if len(args) == 1:
                return math.log(args[0])
            elif len(args) == 2:
                return math.log(args[0], args[1])
            else:
                raise ValueError("log() takes 1 or 2 arguments")
        elif func_name == "log10":
            if len(args) != 1:
                raise ValueError("log10() takes exactly one argument")
            return math.log10(args[0])
        elif func_name == "exp":
            if len(args) != 1:
                raise ValueError("exp() takes exactly one argument")
            return math.exp(args[0])
        elif func_name == "floor":
            if len(args) != 1:
                raise ValueError("floor() takes exactly one argument")
            return math.floor(args[0])
        elif func_name == "ceil":
            if len(args) != 1:
                raise ValueError("ceil() takes exactly one argument")
            return math.ceil(args[0])
        elif func_name == "pow":
            if len(args) == 2:
                return math.pow(args[0], args[1])
            elif len(args) == 3:
                # pow(base, exp, mod) - Python builtin version; one C call.
                if any(isinstance(a, int) and abs(a).bit_length() > _MAX_POWMOD_BITS
                       for a in args[1:]):
                    raise ValueError(
                        f"pow() exponent or modulus too large (over "
                        f"{_MAX_POWMOD_BITS} bits); rejected to avoid blocking "
                        f"the interpreter")
                return pow(args[0], args[1], args[2])
            else:
                raise ValueError("pow() takes 2 or 3 arguments")
        elif func_name == "degrees":
            if len(args) != 1:
                raise ValueError("degrees() takes exactly one argument")
            return math.degrees(args[0])
        elif func_name == "radians":
            if len(args) != 1:
                raise ValueError("radians() takes exactly one argument")
            return math.radians(args[0])
        # String/number formatting functions
        elif func_name == "format":
            if len(args) < 1 or len(args) > 2:
                raise ValueError("format() takes 1 or 2 arguments")
            self._guard_text(args[0])
            if len(args) == 1:
                return format(args[0])
            else:
                self._guard_format_spec(args[1])
                return format(args[0], args[1])
        elif func_name == "hex":
            if len(args) != 1:
                raise ValueError("hex() takes exactly one argument")
            return hex(args[0])
        elif func_name == "bin":
            if len(args) != 1:
                raise ValueError("bin() takes exactly one argument")
            return bin(args[0])
        elif func_name == "oct":
            if len(args) != 1:
                raise ValueError("oct() takes exactly one argument")
            return oct(args[0])
        elif func_name == "chr":
            if len(args) != 1:
                raise ValueError("chr() takes exactly one argument")
            return chr(args[0])
        elif func_name == "ord":
            if len(args) != 1:
                raise ValueError("ord() takes exactly one argument")
            return ord(args[0])
        else:
            raise RuntimeError(f"Function '{func_name}' not implemented")
    
    def validate_ast_security(self, node):
        """Recursively validate that an AST node doesn't contain dangerous operations."""
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                func_name = node.func.id
                if func_name in ['__import__', 'eval', 'exec', 'compile', 'open', 'input']:
                    raise RuntimeError(f"Function '{func_name}' is not allowed in this context")
        elif isinstance(node, ast.Name):
            if node.id in ['__builtins__', '__import__', '__name__', '__file__']:
                if node.id == '__name__':
                    raise RuntimeError(f"Variable '{node.id}' is not allowed. Instead of 'if __name__ == \"__main__\":', call your functions directly at the module level.")
                else:
                    raise RuntimeError(f"Access to '{node.id}' is not allowed")
        elif isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in ['__builtins__', 'sys', 'os']:
                raise RuntimeError(f"Access to '{node.value.id}.{node.attr}' is not allowed")
        
        # Recursively check child nodes
        for child in ast.iter_child_nodes(node):
            self.validate_ast_security(child)

    def assign_target(self, target, value):
        """Assign a value to a potentially complex target (supports unpacking)"""
        if isinstance(target, ast.Name):
            # Simple variable assignment with security check
            var_name = target.id
            # Allow single underscore '_' as throwaway variable, but block other underscore patterns
            if (var_name.startswith('_') and var_name != '_') or var_name in ['__builtins__', '__import__', 'eval', 'exec']:
                raise RuntimeError(f"Variable name '{var_name}' is not allowed")
            self.variables[var_name] = value
            
        elif isinstance(target, (ast.Tuple, ast.List)):
            # Sequence unpacking (with optional starred expression)
            if not hasattr(value, '__iter__') or isinstance(value, (str, bytes)):
                raise RuntimeError(f"Cannot unpack non-sequence {type(value).__name__}")
            
            value_list = list(value)
            
            # Check if there's a starred expression
            starred_index = None
            for i, elt in enumerate(target.elts):
                if isinstance(elt, ast.Starred):
                    if starred_index is not None:
                        raise RuntimeError("Multiple starred expressions in assignment")
                    starred_index = i
            
            if starred_index is not None:
                # Handle starred unpacking: a, *b, c = [1, 2, 3, 4, 5]
                num_regular = len(target.elts) - 1  # All except the starred one
                if len(value_list) < num_regular:
                    raise RuntimeError(f"Not enough values to unpack (expected at least {num_regular}, got {len(value_list)})")
                
                # Assign values before starred
                for i in range(starred_index):
                    self.assign_target(target.elts[i], value_list[i])
                
                # Assign starred portion (middle values)
                starred_count = len(value_list) - num_regular
                starred_values = value_list[starred_index:starred_index + starred_count]
                starred_target = target.elts[starred_index].value  # ast.Starred has a .value attribute
                self.assign_target(starred_target, starred_values)
                
                # Assign values after starred  
                for i in range(starred_index + 1, len(target.elts)):
                    value_index = len(value_list) - (len(target.elts) - i)
                    self.assign_target(target.elts[i], value_list[value_index])
                
            else:
                # Regular unpacking without stars
                if len(target.elts) != len(value_list):
                    raise RuntimeError(f"Too many values to unpack (expected {len(target.elts)}, got {len(value_list)})")
                
                for target_elt, val in zip(target.elts, value_list):
                    # Recursively handle nested unpacking
                    self.assign_target(target_elt, val)
                    
        elif isinstance(target, ast.Subscript):
            # Subscript assignment: obj[key] = value
            obj = self.eval_expression(target.value)
            key = self.eval_expression(target.slice)
            
            if isinstance(obj, dict):
                obj[key] = value
            elif isinstance(obj, list):
                if isinstance(key, int):
                    obj[key] = value
                else:
                    raise RuntimeError("List indices must be integers")
            else:
                raise RuntimeError(f"Subscript assignment not supported for {type(obj).__name__}")
        else:
            raise RuntimeError(f"Unsupported assignment target: {ast.dump(target)}")
    
    def execute_ast_node(self, node):
        """Execute an AST node safely."""
        self.check_timeout()
        
        if isinstance(node, ast.Assign):
            # Check if variable assignment is enabled
            if not self.config.enable_variables:
                raise RuntimeError("Variable assignment is disabled")
                
            # Variable assignment - handle both simple and complex assignment patterns
            value = self.eval_expression(node.value)
            
            for target in node.targets:
                self.assign_target(target, value)
            
            return value
        
        elif isinstance(node, ast.AugAssign):
            # Augmented assignment (+=, -=, *=, etc.)
            target_name = None
            if isinstance(node.target, ast.Name):
                target_name = node.target.id
                current_value = self.variables.get(target_name, 0)
            elif isinstance(node.target, ast.Subscript):
                # Handle subscript augmented assignment like list[0] += 1
                obj = self.eval_expression(node.target.value)
                key = self.eval_expression(node.target.slice)
                if isinstance(obj, dict):
                    current_value = obj.get(key, 0)
                elif isinstance(obj, list) and isinstance(key, int):
                    current_value = obj[key]
                else:
                    raise RuntimeError("Unsupported subscript augmented assignment")
            else:
                raise RuntimeError(f"Unsupported augmented assignment target: {ast.dump(node.target)}")
            
            # Get the right-hand side value
            rhs_value = self.eval_expression(node.value)
            
            # Perform the operation
            self._guard_binop_size(node.op, current_value, rhs_value)
            if isinstance(node.op, ast.Add):
                new_value = current_value + rhs_value
            elif isinstance(node.op, ast.Sub):
                new_value = current_value - rhs_value
            elif isinstance(node.op, ast.Mult):
                new_value = current_value * rhs_value
            elif isinstance(node.op, ast.Div):
                new_value = current_value / rhs_value
            elif isinstance(node.op, ast.FloorDiv):
                new_value = current_value // rhs_value
            elif isinstance(node.op, ast.Mod):
                new_value = current_value % rhs_value
            elif isinstance(node.op, ast.Pow):
                new_value = current_value ** rhs_value
            else:
                raise RuntimeError(f"Unsupported augmented assignment operator: {type(node.op).__name__}")
            
            # Assign the new value back
            if isinstance(node.target, ast.Name):
                self.variables[target_name] = new_value
            elif isinstance(node.target, ast.Subscript):
                obj = self.eval_expression(node.target.value)
                key = self.eval_expression(node.target.slice)
                if isinstance(obj, dict):
                    obj[key] = new_value
                elif isinstance(obj, list) and isinstance(key, int):
                    obj[key] = new_value
            
            return new_value
                
        elif isinstance(node, ast.Expr):
            # Expression statement
            return self.eval_expression(node.value)
            
        elif isinstance(node, ast.If):
            # If statement
            condition = self.eval_expression(node.test)
            if condition:
                result = None
                for stmt in node.body:
                    result = self.execute_ast_node(stmt)
                return result
            elif node.orelse:
                result = None
                for stmt in node.orelse:
                    result = self.execute_ast_node(stmt)
                return result
            return None
            
        elif isinstance(node, ast.For):
            # For loop with timeout protection
            loop_start_time = time.time()
            
            iterable = self.eval_expression(node.iter)
            result = None
            iteration_count = 0
            
            for item in iterable:
                # Check timeouts
                self.check_timeout()
                self.check_loop_timeout(loop_start_time)
                
                iteration_count += 1
                if iteration_count > self.config.max_loop_iterations:
                    raise RuntimeError(f"Loop exceeded maximum iterations: {self.config.max_loop_iterations}")
                
                # Set loop variable(s) - supports tuple unpacking
                self.assign_target(node.target, item)
                
                # Execute loop body
                for stmt in node.body:
                    result = self.execute_ast_node(stmt)
            
            return result
            
        elif isinstance(node, ast.While):
            # While loop with timeout protection
            # Check for infinite 'while True:' loops
            if isinstance(node.test, ast.Constant) and node.test.value is True:
                raise RuntimeError("Infinite 'while True' loops are not allowed")
            
            loop_start_time = time.time()
            result = None
            iteration_count = 0
            
            while self.eval_expression(node.test):
                # Check timeouts
                self.check_timeout()
                self.check_loop_timeout(loop_start_time)
                
                iteration_count += 1
                if iteration_count > self.config.max_loop_iterations:
                    raise RuntimeError(f"Loop exceeded maximum iterations: {self.config.max_loop_iterations}")
                
                # Execute loop body
                for stmt in node.body:
                    result = self.execute_ast_node(stmt)
            
            return result
            
        elif isinstance(node, ast.FunctionDef):
            # Function definition
            func_name = node.name
            if func_name.startswith('_') or func_name in ['__builtins__', '__import__', 'eval', 'exec']:
                raise RuntimeError(f"Function name '{func_name}' is not allowed")
            
            # Store function definition (AST node and parameter names with defaults)
            param_names = [arg.arg for arg in node.args.args]
            # Handle default values for parameters
            defaults = []
            if node.args.defaults:
                # Evaluate default values
                for default_node in node.args.defaults:
                    defaults.append(self.eval_expression(default_node))
            
            self.user_functions[func_name] = {
                'node': node,
                'params': param_names,
                'defaults': defaults,
                'num_required': len(param_names) - len(defaults)  # number of required parameters
            }
            return None
            
        elif isinstance(node, ast.Return):
            # Return statement - raise special exception to handle function returns
            if node.value is not None:
                value = self.eval_expression(node.value)
                raise FunctionReturn(value)
            else:
                raise FunctionReturn(None)
                
        elif isinstance(node, ast.Pass):
            # Pass statement - do nothing
            return None
            
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            # Import statements are not allowed
            if isinstance(node, ast.Import):
                modules = [alias.name for alias in node.names]
                raise UnsupportedFeatureError(f"Import statements are not allowed in sandbox: import {', '.join(modules)}")
            else:  # ast.ImportFrom
                module = node.module or ''
                names = [alias.name for alias in node.names]
                raise UnsupportedFeatureError(f"Import statements are not allowed in sandbox: from {module} import {', '.join(names)}")
                
        elif isinstance(node, ast.Try):
            # Try/except statement
            result = None
            exception_caught = False
            
            try:
                # Execute try block
                for stmt in node.body:
                    result = self.execute_ast_node(stmt)
                    
            except FunctionReturn:
                # Don't catch function returns - let them pass through
                raise
            except Exception as e:
                # Handle exceptions - check except handlers
                exception_caught = True
                
                for handler in node.handlers:
                    # Check if this handler matches the exception
                    if handler.type is None:  # bare except:
                        should_handle = True
                    else:
                        # For simplicity, we'll handle common built-in exception types by name
                        handler_type_name = None
                        if isinstance(handler.type, ast.Name):
                            handler_type_name = handler.type.id
                        
                        # Map common exceptions
                        exception_name = type(e).__name__
                        should_handle = (
                            handler_type_name == exception_name or
                            handler_type_name == 'Exception' or  # Catch all
                            (handler_type_name == 'ValueError' and isinstance(e, (ValueError, RuntimeError))) or
                            (handler_type_name == 'TypeError' and isinstance(e, TypeError)) or
                            (handler_type_name == 'ZeroDivisionError' and isinstance(e, ZeroDivisionError))
                        )
                    
                    if should_handle:
                        # Bind exception to variable if specified
                        if handler.name:
                            self.variables[handler.name] = self._exception_text(e)
                        
                        # Execute except block
                        try:
                            for stmt in handler.body:
                                result = self.execute_ast_node(stmt)
                        except FunctionReturn:
                            # Don't catch function returns - let them pass through
                            raise
                        
                        exception_caught = False  # Exception was handled
                        break
                
                # If no handler caught the exception, re-raise it
                if exception_caught:
                    raise
            
            else:
                # Execute else block if no exception occurred
                if node.orelse:
                    try:
                        for stmt in node.orelse:
                            result = self.execute_ast_node(stmt)
                    except FunctionReturn:
                        # Don't catch function returns - let them pass through
                        raise
            
            finally:
                # Execute finally block (but allow FunctionReturn to pass through)
                if node.finalbody:
                    try:
                        for stmt in node.finalbody:
                            self.execute_ast_node(stmt)
                    except FunctionReturn:
                        # Don't catch function returns - let them pass through  
                        raise
            
            return result
            
        elif isinstance(node, ast.Raise):
            # Raise statement
            if node.exc is None:
                # Re-raise current exception
                raise
            else:
                # Evaluate exception expression
                exc_value = self.eval_expression(node.exc)
                if isinstance(exc_value, str):
                    # Convert string to RuntimeError
                    raise RuntimeError(exc_value)
                elif callable(exc_value):
                    # Call exception constructor
                    if node.cause is not None:
                        raise exc_value() from self.eval_expression(node.cause)
                    else:
                        raise exc_value()
                else:
                    raise exc_value
            
        else:
            # Answered as an unsupported_feature error
            raise UnsupportedFeatureError(f"Unsupported AST node type: {type(node).__name__}")
    
    def eval_expression(self, node):
        """Evaluate an expression node safely."""
        self.check_timeout()
        
        if isinstance(node, ast.Constant):
            return node.value
        elif isinstance(node, ast.Name):
            # Check for special blocked variables first
            if node.id in ['__name__', '__file__', '__builtins__', '__import__']:
                if node.id == '__name__':
                    raise RuntimeError(f"Variable '{node.id}' is not allowed. Instead of 'if __name__ == \"__main__\":', call your functions directly at the module level.")
                else:
                    raise RuntimeError(f"Access to '{node.id}' is not allowed")
            
            if node.id in self.variables:
                return self.variables[node.id]
            else:
                raise NameError(f"Name '{node.id}' is not defined")
        elif isinstance(node, ast.List):
            return [self.eval_expression(elem) for elem in node.elts]
        elif isinstance(node, ast.Tuple):
            return tuple(self.eval_expression(elem) for elem in node.elts)
        elif isinstance(node, ast.Dict):
            return {
                self.eval_expression(key): self.eval_expression(value)
                for key, value in zip(node.keys, node.values)
            }
        elif isinstance(node, ast.BinOp):
            left = self.eval_expression(node.left)
            right = self.eval_expression(node.right)
            self._guard_binop_size(node.op, left, right)

            if isinstance(node.op, ast.Add):
                return left + right
            elif isinstance(node.op, ast.Sub):
                return left - right
            elif isinstance(node.op, ast.Mult):
                return left * right
            elif isinstance(node.op, ast.Div):
                return left / right
            elif isinstance(node.op, ast.Mod):
                return left % right
            elif isinstance(node.op, ast.Pow):
                return left ** right
            elif isinstance(node.op, ast.FloorDiv):
                return left // right
            elif isinstance(node.op, ast.BitOr):
                # Set union: {1, 2} | {2, 3} or bitwise OR for integers
                if isinstance(left, set) and isinstance(right, set):
                    return left | right
                else:
                    return left | right  # Bitwise OR for integers
            elif isinstance(node.op, ast.BitAnd):
                # Set intersection: {1, 2} & {2, 3} or bitwise AND for integers
                if isinstance(left, set) and isinstance(right, set):
                    return left & right
                else:
                    return left & right  # Bitwise AND for integers
            elif isinstance(node.op, ast.BitXor):
                # Set symmetric difference: {1, 2} ^ {2, 3} or bitwise XOR for integers
                if isinstance(left, set) and isinstance(right, set):
                    return left ^ right
                else:
                    return left ^ right  # Bitwise XOR for integers
            else:
                raise RuntimeError(f"Unsupported binary operator: {type(node.op).__name__}")
                
        elif isinstance(node, ast.UnaryOp):
            operand = self.eval_expression(node.operand)
            if isinstance(node.op, ast.UAdd):
                return +operand
            elif isinstance(node.op, ast.USub):
                return -operand
            elif isinstance(node.op, ast.Not):
                return not operand
            else:
                raise RuntimeError(f"Unsupported unary operator: {type(node.op).__name__}")

        elif isinstance(node, ast.BoolOp):
            # and/or with Python short-circuit semantics: return the deciding
            # operand's VALUE (not a bool), later operands stay unevaluated.
            if isinstance(node.op, ast.And):
                value = True
                for operand in node.values:
                    value = self.eval_expression(operand)
                    if not value:
                        return value
                return value
            else:  # ast.Or
                value = False
                for operand in node.values:
                    value = self.eval_expression(operand)
                    if value:
                        return value
                return value
                
        elif isinstance(node, ast.Compare):
            left = self.eval_expression(node.left)
            for op, comparator in zip(node.ops, node.comparators):
                right = self.eval_expression(comparator)
                
                if isinstance(op, ast.Eq):
                    result = left == right
                elif isinstance(op, ast.NotEq):
                    result = left != right
                elif isinstance(op, ast.Lt):
                    result = left < right
                elif isinstance(op, ast.LtE):
                    result = left <= right
                elif isinstance(op, ast.Gt):
                    result = left > right
                elif isinstance(op, ast.GtE):
                    result = left >= right
                elif isinstance(op, ast.Is):
                    result = left is right
                elif isinstance(op, ast.IsNot):
                    result = left is not right
                elif isinstance(op, ast.In):
                    result = left in right
                elif isinstance(op, ast.NotIn):
                    result = left not in right
                else:
                    raise RuntimeError(f"Unsupported comparison operator: {type(op).__name__}")
                
                if not result:
                    return False
                left = right
            return True
            
        elif isinstance(node, ast.Call):
            # Handle method calls like list.append()
            if isinstance(node.func, ast.Attribute):
                obj = self.eval_expression(node.func.value)
                method_name = node.func.attr
                args = [self.eval_expression(arg) for arg in node.args]
                kwargs = {}
                for keyword in node.keywords:
                    kwargs[keyword.arg] = self.eval_expression(keyword.value)
                self._guard_method_size(obj, method_name, args, kwargs)

                # Allow safe list methods
                if isinstance(obj, list) and method_name in [
                    "append", "extend", "insert", "remove", "pop", "clear", "count", "index",
                    "reverse", "sort", "copy"
                ]:
                    result = getattr(obj, method_name)(*args, **kwargs)
                    return result
                # Allow safe string methods  
                elif isinstance(obj, str) and method_name in [
                    "upper", "lower", "strip", "lstrip", "rstrip", "replace", "split", "join",
                    "find", "rfind", "index", "rindex", "startswith", "endswith", "count",
                    "isalpha", "isdigit", "isalnum", "isspace", "islower", "isupper",
                    "capitalize", "title", "swapcase", "center", "ljust", "rjust", "zfill",
                    "partition", "rpartition", "splitlines", "casefold"
                ]:
                    return getattr(obj, method_name)(*args, **kwargs)
                # Allow safe dict methods
                elif isinstance(obj, dict) and method_name in [
                    "items", "keys", "values", "get", "pop", "clear", "update", 
                    "setdefault", "popitem", "copy"
                ]:
                    return getattr(obj, method_name)(*args, **kwargs)
                # Allow safe set methods
                elif isinstance(obj, set) and method_name in [
                    "add", "remove", "discard", "pop", "clear", "copy", "update",
                    "union", "intersection", "difference", "symmetric_difference",
                    "issubset", "issuperset", "isdisjoint"
                ]:
                    return getattr(obj, method_name)(*args, **kwargs)
                else:
                    raise RuntimeError(f"Method '{method_name}' not allowed on {type(obj).__name__}")
            
            # Handle regular function calls
            func_name = node.func.id if isinstance(node.func, ast.Name) else None
            if not func_name:
                raise RuntimeError("Only simple function calls are supported")
            
            args = [self.eval_expression(arg) for arg in node.args]
            kwargs = {}
            for keyword in node.keywords:
                kwargs[keyword.arg] = self.eval_expression(keyword.value)
            
            # Check if it's a user-defined function
            if func_name in self.user_functions:
                return self.call_user_function(func_name, args)
            # Check if it's a lambda or callable variable
            elif func_name in self.variables:
                func_obj = self.variables[func_name]
                seeded = _SEEDED_BY_TYPE.get(func_obj) if isinstance(func_obj, type) else None
                if seeded is not None:
                    self._guard_type_call(seeded, args)
                if callable(func_obj):
                    return func_obj(*args, **kwargs)
                else:
                    raise RuntimeError(f"'{func_name}' object is not callable")
            else:
                return self.safe_builtin_function(func_name, args, kwargs)
            
        elif isinstance(node, ast.IfExp):
            # Inline if expression: a if condition else b
            condition = self.eval_expression(node.test)
            if condition:
                return self.eval_expression(node.body)
            else:
                return self.eval_expression(node.orelse)
                
        elif isinstance(node, ast.Subscript):
            # Array/list subscript: arr[index] — including slices arr[1:3],
            # s[:100], x[::-1]. Slices only read (bounded by the value's own
            # size), so no extra guards are needed beyond normal type errors.
            value = self.eval_expression(node.value)
            if isinstance(node.slice, ast.Slice):
                lower = self.eval_expression(node.slice.lower) if node.slice.lower else None
                upper = self.eval_expression(node.slice.upper) if node.slice.upper else None
                step = self.eval_expression(node.slice.step) if node.slice.step else None
                return value[slice(lower, upper, step)]
            slice_value = self.eval_expression(node.slice)
            return value[slice_value]
            
        elif isinstance(node, ast.JoinedStr):
            # F-string support: f"Hello {name}!"
            result_parts = []
            for value in node.values:
                if isinstance(value, ast.Constant):
                    # String literal part
                    result_parts.append(str(value.value))
                elif isinstance(value, ast.FormattedValue):
                    # Formatted expression part like {name} or {x:.2f}
                    expr_value = self.eval_expression(value.value)
                    self._guard_text(expr_value)
                    
                    # Handle format specification if present
                    if value.format_spec:
                        format_spec = self.eval_expression(value.format_spec)
                        if isinstance(format_spec, ast.JoinedStr):
                            # Nested f-string in format spec - evaluate it
                            format_spec = self.eval_expression(format_spec)
                        self._guard_format_spec(format_spec)
                        try:
                            formatted = format(expr_value, str(format_spec))
                        except (ValueError, TypeError) as e:
                            raise RuntimeError(f"Invalid format specification: {e}")
                    else:
                        formatted = str(expr_value)
                    
                    result_parts.append(formatted)
                else:
                    raise RuntimeError(f"Unsupported f-string component: {type(value).__name__}")
            
            return ''.join(result_parts)
            
        elif isinstance(node, ast.ListComp):
            # List comprehension: [expr for target in iter if condition]
            result = []
            
            # Handle nested generators
            def eval_comprehension(generators, current_result):
                if not generators:
                    # Base case - evaluate the element expression
                    element_value = self.eval_expression(node.elt)
                    current_result.append(element_value)
                    return
                
                # Process current generator
                gen = generators[0]
                remaining_gens = generators[1:]
                
                # Get the iterable
                iterable = self.eval_expression(gen.iter)
                
                # Save current target variable state(s)
                saved_vars = {}
                if isinstance(gen.target, ast.Name):
                    target_names = [gen.target.id]
                elif isinstance(gen.target, (ast.Tuple, ast.List)):
                    target_names = []
                    for elt in gen.target.elts:
                        if isinstance(elt, ast.Name):
                            target_names.append(elt.id)
                        else:
                            raise RuntimeError("Complex unpacking targets not supported in comprehensions")
                else:
                    raise RuntimeError("Unsupported comprehension target")
                
                for name in target_names:
                    if name in self.variables:
                        saved_vars[name] = self.variables[name]
                
                try:
                    for item in iterable:
                        self.check_timeout()
                        
                        # Set loop variable(s) - supports unpacking
                        self.assign_target(gen.target, item)
                        
                        # Check all conditions for this generator
                        all_conditions_met = True
                        for condition in gen.ifs:
                            if not self.eval_expression(condition):
                                all_conditions_met = False
                                break
                        
                        if all_conditions_met:
                            eval_comprehension(remaining_gens, current_result)
                
                finally:
                    # Restore target variables
                    for name in target_names:
                        if name in saved_vars:
                            self.variables[name] = saved_vars[name]
                        elif name in self.variables:
                            del self.variables[name]
            
            eval_comprehension(node.generators, result)
            return result
            
        elif isinstance(node, ast.DictComp):
            # Dict comprehension: {key_expr: value_expr for target in iter if condition}
            result = {}
            
            def eval_dict_comprehension(generators, current_result):
                if not generators:
                    # Base case - evaluate key and value expressions
                    key_value = self.eval_expression(node.key)
                    value_value = self.eval_expression(node.value)
                    current_result[key_value] = value_value
                    return
                
                gen = generators[0]
                remaining_gens = generators[1:]
                
                iterable = self.eval_expression(gen.iter)
                
                # Save current target variable state(s)  
                saved_vars = {}
                if isinstance(gen.target, ast.Name):
                    target_names = [gen.target.id]
                elif isinstance(gen.target, (ast.Tuple, ast.List)):
                    target_names = []
                    for elt in gen.target.elts:
                        if isinstance(elt, ast.Name):
                            target_names.append(elt.id)
                        else:
                            raise RuntimeError("Complex unpacking targets not supported in comprehensions")
                else:
                    raise RuntimeError("Unsupported comprehension target")
                
                for name in target_names:
                    if name in self.variables:
                        saved_vars[name] = self.variables[name]
                
                try:
                    for item in iterable:
                        self.check_timeout()
                        self.assign_target(gen.target, item)
                        
                        # Check conditions
                        all_conditions_met = True
                        for condition in gen.ifs:
                            if not self.eval_expression(condition):
                                all_conditions_met = False
                                break
                        
                        if all_conditions_met:
                            eval_dict_comprehension(remaining_gens, current_result)
                
                finally:
                    # Restore target variables  
                    for name in target_names:
                        if name in saved_vars:
                            self.variables[name] = saved_vars[name]
                        elif name in self.variables:
                            del self.variables[name]
            
            eval_dict_comprehension(node.generators, result)
            return result
            
        elif isinstance(node, ast.Set):
            # Set literal: {1, 2, 3}
            elements = [self.eval_expression(elem) for elem in node.elts]
            return set(elements)
            
        elif isinstance(node, ast.SetComp):
            # Set comprehension: {expr for target in iter if condition}
            result = set()
            
            def eval_set_comprehension(generators, current_result):
                if not generators:
                    # Base case - evaluate expression and add to set
                    element = self.eval_expression(node.elt)
                    result.add(element)
                    return
                
                gen = generators[0]
                remaining_gens = generators[1:]
                
                iterable = self.eval_expression(gen.iter)
                
                # Save current target variable state(s)  
                saved_vars = {}
                if isinstance(gen.target, ast.Name):
                    target_names = [gen.target.id]
                elif isinstance(gen.target, (ast.Tuple, ast.List)):
                    target_names = []
                    for elt in gen.target.elts:
                        if isinstance(elt, ast.Name):
                            target_names.append(elt.id)
                        else:
                            raise RuntimeError("Complex unpacking targets not supported in comprehensions")
                else:
                    raise RuntimeError("Unsupported comprehension target")
                
                for name in target_names:
                    if name in self.variables:
                        saved_vars[name] = self.variables[name]
                
                try:
                    for item in iterable:
                        self.check_timeout()
                        self.assign_target(gen.target, item)
                        
                        # Check conditions
                        all_conditions_met = True
                        for condition in gen.ifs:
                            if not self.eval_expression(condition):
                                all_conditions_met = False
                                break
                        
                        if all_conditions_met:
                            eval_set_comprehension(remaining_gens, current_result)
                
                finally:
                    # Restore target variables  
                    for name in target_names:
                        if name in saved_vars:
                            self.variables[name] = saved_vars[name]
                        elif name in self.variables:
                            del self.variables[name]
            
            eval_set_comprehension(node.generators, result)
            return result
            
        elif isinstance(node, ast.GeneratorExp):
            # Generator expression: (expr for target in iter if condition)
            # We'll convert it to a list for simplicity (no lazy evaluation)
            result = []
            
            def eval_generator_expression(generators, current_result):
                if not generators:
                    # Base case - evaluate expression and add to result
                    element = self.eval_expression(node.elt)
                    result.append(element)
                    return
                
                gen = generators[0]
                remaining_gens = generators[1:]
                
                iterable = self.eval_expression(gen.iter)
                
                # Save current target variable state(s)  
                saved_vars = {}
                if isinstance(gen.target, ast.Name):
                    target_names = [gen.target.id]
                elif isinstance(gen.target, (ast.Tuple, ast.List)):
                    target_names = []
                    for elt in gen.target.elts:
                        if isinstance(elt, ast.Name):
                            target_names.append(elt.id)
                        else:
                            raise RuntimeError("Complex unpacking targets not supported in comprehensions")
                else:
                    raise RuntimeError("Unsupported comprehension target")
                
                for name in target_names:
                    if name in self.variables:
                        saved_vars[name] = self.variables[name]
                
                try:
                    for item in iterable:
                        self.check_timeout()
                        self.assign_target(gen.target, item)
                        
                        # Check conditions
                        all_conditions_met = True
                        for condition in gen.ifs:
                            if not self.eval_expression(condition):
                                all_conditions_met = False
                                break
                        
                        if all_conditions_met:
                            eval_generator_expression(remaining_gens, current_result)
                
                finally:
                    # Restore target variables  
                    for name in target_names:
                        if name in saved_vars:
                            self.variables[name] = saved_vars[name]
                        elif name in self.variables:
                            del self.variables[name]
            
            eval_generator_expression(node.generators, result)
            return result
            
        elif isinstance(node, ast.Lambda):
            # Lambda function: lambda x, y: x + y
            param_names = [arg.arg for arg in node.args.args]
            
            # Validate lambda body for security before creating the function
            self.validate_ast_security(node.body)
            
            # Store lambda as a callable object
            class LambdaFunction:
                def __init__(self, executor, param_names, body):
                    self.executor = executor
                    self.param_names = param_names
                    self.body = body
                
                def __call__(self, *args):
                    if len(args) != len(self.param_names):
                        raise RuntimeError(f"Lambda takes {len(self.param_names)} arguments but {len(args)} were given")
                    
                    # Save current state
                    saved_vars = self.executor.variables.copy()
                    
                    try:
                        # Set parameters
                        for param_name, arg_value in zip(self.param_names, args):
                            self.executor.variables[param_name] = arg_value
                        
                        # Evaluate body expression
                        return self.executor.eval_expression(self.body)
                    
                    finally:
                        # Restore state
                        self.executor.variables = saved_vars
            
            return LambdaFunction(self, param_names, node.body)
            
        elif isinstance(node, ast.Attribute):
            # Attribute access: obj.attr
            obj = self.eval_expression(node.value)
            attr_name = node.attr

            # A method taken as a value (`f = s.ljust`) is size-guarded when
            # called, like a direct method call.
            if ((isinstance(obj, str) and attr_name in _GROWING_STR_METHODS)
                    or (isinstance(obj, list) and attr_name in _GUARDED_LIST_METHODS)):
                def guarded(*args, **kwargs):
                    self._guard_method_size(obj, attr_name, args, kwargs)
                    return getattr(obj, attr_name)(*args, **kwargs)
                return guarded

            # Safe attribute access for common types
            if isinstance(obj, dict):
                if attr_name == "items":
                    return lambda: obj.items()
                elif attr_name == "keys":
                    return lambda: obj.keys()
                elif attr_name == "values":
                    return lambda: obj.values()
                elif attr_name == "get":
                    return lambda key, default=None: obj.get(key, default)
                elif attr_name == "pop":
                    return lambda key, default=None: obj.pop(key, default) if default is not None else obj.pop(key)
                elif attr_name == "popitem":
                    return lambda: obj.popitem()
                elif attr_name == "clear":
                    return lambda: obj.clear()
                elif attr_name == "copy":
                    return lambda: obj.copy()
                elif attr_name == "update":
                    return lambda other: obj.update(other)
                elif attr_name == "setdefault":
                    return lambda key, default=None: obj.setdefault(key, default)
                else:
                    raise RuntimeError(f"Method '{attr_name}' not allowed on dict")
            elif isinstance(obj, list):
                if attr_name == "append":
                    return lambda item: obj.append(item)
                elif attr_name == "extend":
                    return lambda items: obj.extend(items)
                elif attr_name == "insert":
                    return lambda index, item: obj.insert(index, item)
                elif attr_name == "pop":
                    return lambda index=-1: obj.pop(index)
                elif attr_name == "remove":
                    return lambda value: obj.remove(value)
                elif attr_name == "clear":
                    return lambda: obj.clear()
                elif attr_name == "copy":
                    return lambda: obj.copy()
                elif attr_name == "count":
                    return lambda value: obj.count(value)
                elif attr_name == "index":
                    return lambda value, start=0, stop=None: obj.index(value, start, stop) if stop is not None else obj.index(value, start)
                elif attr_name == "reverse":
                    return lambda: obj.reverse()
                elif attr_name == "sort":
                    return lambda key=None, reverse=False: obj.sort(key=key, reverse=reverse)
                else:
                    raise RuntimeError(f"Method '{attr_name}' not allowed on list")
            elif isinstance(obj, str):
                if attr_name == "split":
                    return lambda sep=None, maxsplit=-1: obj.split(sep, maxsplit) if maxsplit != -1 else obj.split(sep)
                elif attr_name == "join":
                    return lambda items: obj.join(items)
                elif attr_name == "upper":
                    return lambda: obj.upper()
                elif attr_name == "lower":
                    return lambda: obj.lower()
                elif attr_name == "strip":
                    return lambda chars=None: obj.strip(chars)
                elif attr_name == "lstrip":
                    return lambda chars=None: obj.lstrip(chars)
                elif attr_name == "rstrip":
                    return lambda chars=None: obj.rstrip(chars)
                elif attr_name == "replace":
                    return lambda old, new, count=-1: obj.replace(old, new, count) if count != -1 else obj.replace(old, new)
                elif attr_name == "find":
                    return lambda sub, start=0, end=None: obj.find(sub, start, end) if end is not None else obj.find(sub, start)
                elif attr_name == "rfind":
                    return lambda sub, start=0, end=None: obj.rfind(sub, start, end) if end is not None else obj.rfind(sub, start)
                elif attr_name == "index":
                    return lambda sub, start=0, end=None: obj.index(sub, start, end) if end is not None else obj.index(sub, start)
                elif attr_name == "rindex":
                    return lambda sub, start=0, end=None: obj.rindex(sub, start, end) if end is not None else obj.rindex(sub, start)
                elif attr_name == "count":
                    return lambda sub, start=0, end=None: obj.count(sub, start, end) if end is not None else obj.count(sub, start)
                elif attr_name == "startswith":
                    return lambda prefix, start=0, end=None: obj.startswith(prefix, start, end) if end is not None else obj.startswith(prefix, start)
                elif attr_name == "endswith":
                    return lambda suffix, start=0, end=None: obj.endswith(suffix, start, end) if end is not None else obj.endswith(suffix, start)
                elif attr_name == "isalpha":
                    return lambda: obj.isalpha()
                elif attr_name == "isdigit":
                    return lambda: obj.isdigit()
                elif attr_name == "isalnum":
                    return lambda: obj.isalnum()
                elif attr_name == "isspace":
                    return lambda: obj.isspace()
                elif attr_name == "isupper":
                    return lambda: obj.isupper()
                elif attr_name == "islower":
                    return lambda: obj.islower()
                elif attr_name == "title":
                    return lambda: obj.title()
                elif attr_name == "capitalize":
                    return lambda: obj.capitalize()
                elif attr_name == "swapcase":
                    return lambda: obj.swapcase()
                elif attr_name == "center":
                    return lambda width, fillchar=" ": obj.center(width, fillchar)
                elif attr_name == "ljust":
                    return lambda width, fillchar=" ": obj.ljust(width, fillchar)
                elif attr_name == "rjust":
                    return lambda width, fillchar=" ": obj.rjust(width, fillchar)
                elif attr_name == "zfill":
                    return lambda width: obj.zfill(width)
                else:
                    raise RuntimeError(f"Method '{attr_name}' not allowed on str")
            elif isinstance(obj, set):
                if attr_name == "add":
                    return lambda item: obj.add(item)
                elif attr_name == "remove":
                    return lambda item: obj.remove(item)
                elif attr_name == "discard":
                    return lambda item: obj.discard(item)
                elif attr_name == "pop":
                    return lambda: obj.pop()
                elif attr_name == "clear":
                    return lambda: obj.clear()
                elif attr_name == "copy":
                    return lambda: obj.copy()
                elif attr_name == "union":
                    return lambda other: obj.union(other)
                elif attr_name == "intersection":
                    return lambda other: obj.intersection(other)
                elif attr_name == "difference":
                    return lambda other: obj.difference(other)
                elif attr_name == "symmetric_difference":
                    return lambda other: obj.symmetric_difference(other)
                elif attr_name == "update":
                    return lambda other: obj.update(other)
                elif attr_name == "intersection_update":
                    return lambda other: obj.intersection_update(other)
                elif attr_name == "difference_update":
                    return lambda other: obj.difference_update(other)
                elif attr_name == "symmetric_difference_update":
                    return lambda other: obj.symmetric_difference_update(other)
                elif attr_name == "issubset":
                    return lambda other: obj.issubset(other)
                elif attr_name == "issuperset":
                    return lambda other: obj.issuperset(other)
                elif attr_name == "isdisjoint":
                    return lambda other: obj.isdisjoint(other)
                else:
                    raise RuntimeError(f"Method '{attr_name}' not allowed on set")
            elif isinstance(obj, type):
                # Allow access to common type attributes
                if attr_name == "__name__":
                    return obj.__name__
                else:
                    raise RuntimeError(f"Attribute '{attr_name}' not allowed on type")
            else:
                raise RuntimeError(f"Attribute access not allowed on {type(obj).__name__}")
            
        else:
            # Answered as an unsupported_feature error
            raise UnsupportedFeatureError(f"Unsupported expression type: {type(node).__name__}")
    
    def execute(self, code):
        """Execute Python code with loop and if statement support."""
        self.start_time = time.time()
        self.output_buffer.clear()
        self._output_len = 0
        self._rss_start = None
        self._next_memory_check = 0.0
        
        try:
            # Parse the code
            tree = ast.parse(code)
            
            # Execute each statement
            last_result = None
            for i, node in enumerate(tree.body):
                result = self.execute_ast_node(node)
                # If this is the last statement and it's an expression, capture its result
                if i == len(tree.body) - 1 and isinstance(node, ast.Expr) and result is not None:
                    last_result = result
            
            # If we have a single expression that produced a result, print it
            if (len(tree.body) == 1 and isinstance(tree.body[0], ast.Expr) and 
                last_result is not None and not self.output_buffer):
                self.safe_print(last_result)
            
            return {
                "success": True,
                "output": "\n".join(self.output_buffer),
                "variables": dict(self.variables),
                "execution_time": time.time() - self.start_time,
                "error": None
            }
            
        except (Exception, OutputLimitExceeded, MemoryLimitExceeded) as e:
            from .errors import format_error_for_llm, SyntaxError as ScriptSyntaxError

            # str() of an error whose argument is a huge container (a KeyError
            # on a tuple key, ValueError(big_list)) is the same long C call as
            # str() of the container.
            if estimate_text_size(e.args, self._max_seq_len) > self._max_seq_len:
                e = ValueError(self._exception_text(e)).with_traceback(e.__traceback__)
            
            # By type only: matching words in the message ("expected",
            # "incomplete") made `raise ValueError("expected a number")` and
            # an unpacking error syntax errors without a line.
            if isinstance(e, (SyntaxError, ScriptSyntaxError)):
                error_info = format_error_for_llm(ScriptSyntaxError(str(e)), code)
            else:
                error_info = format_error_for_llm(e, code)
                
            return {
                "success": False,
                "output": "\n".join(self.output_buffer),
                "variables": dict(self.variables),
                "execution_time": time.time() - self.start_time if self.start_time else 0,
                "error": error_info
            }
    
    def call_user_function(self, func_name, args):
        """Call a user-defined function safely."""
        if func_name not in self.user_functions:
            raise RuntimeError(f"Function '{func_name}' is not defined")
        
        func_def = self.user_functions[func_name]
        func_node = func_def['node']
        param_names = func_def['params']
        defaults = func_def.get('defaults', [])
        num_required = func_def.get('num_required', len(param_names))
        
        # Check argument count - must have at least required args, at most total params
        if len(args) < num_required or len(args) > len(param_names):
            if defaults:
                raise RuntimeError(f"Function '{func_name}' takes {num_required}-{len(param_names)} arguments but {len(args)} were given")
            else:
                raise RuntimeError(f"Function '{func_name}' takes {len(param_names)} arguments but {len(args)} were given")
        
        # Save current variable state
        saved_vars = self.variables.copy()
        
        try:
            # Set function parameters as local variables
            # First set provided arguments
            for i, arg_value in enumerate(args):
                self.variables[param_names[i]] = arg_value
            
            # Then set default values for remaining parameters
            if len(args) < len(param_names):
                for i in range(len(args), len(param_names)):
                    default_index = i - num_required
                    if default_index >= 0 and default_index < len(defaults):
                        self.variables[param_names[i]] = defaults[default_index]
            
            # Execute function body
            for stmt in func_node.body:
                try:
                    self.execute_ast_node(stmt)
                except FunctionReturn as ret:
                    # Function returned a value
                    return ret.value
            
            # If no return statement was executed, return None
            return None
            
        finally:
            # Restore previous variable state
            self.variables = saved_vars
    
    def reset(self):
        """Reset the executor state."""
        self.variables.clear()
        # Re-add built-in types after clearing
        self.variables.update({
            'int': int,
            'str': str,
            'float': float,
            'bool': bool,
            'list': list,
            'dict': dict,
            'tuple': tuple,
            'set': set,
        })
        self.user_functions.clear()
        self.output_buffer.clear()
        self._output_len = 0
        self.start_time = None