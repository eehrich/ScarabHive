from __future__ import annotations

from typing import Dict, Any, Tuple
import logging
from functools import lru_cache
from pathlib import Path
from jinja2 import FileSystemLoader
from jinja2.exceptions import SecurityError
from jinja2.sandbox import SandboxedEnvironment
from datetime import datetime, timedelta
import pytz


logger = logging.getLogger(__name__)

#: Most characters, items or bits a ``*`` or ``**`` in a prompt may build.
_MAX_BINOP_SIZE = 100_000


class PromptSandbox(SandboxedEnvironment):
    """Jinja's sandbox, plus a size limit on ``*`` and ``**``.

    The sandbox keeps a template away from Python internals, not from cost: ``{{ 9 ** 99999999 }}`` is folded
    at compile time in one C call that holds the GIL and freezes the whole process, and ``'a' * 10**9`` fills
    the memory. Intercepted operators are not constant-folded, so both go through :meth:`call_binop`.
    Not bounded: memory a template builds through string methods and filters (``|center``, ``.ljust``) or by
    doubling a string in a loop. Only a process limit bounds that; prompts that are not trusted with resources
    belong in a separate process, not in this renderer.
    """

    intercepted_binops = frozenset({"*", "**"})

    def call_binop(self, context, operator, left, right):
        if _binop_size(operator, left, right) > _MAX_BINOP_SIZE:
            raise SecurityError(f"'{operator}' in a prompt would build more than {_MAX_BINOP_SIZE} characters, "
                                "items or bits")
        return super().call_binop(context, operator, left, right)


def _binop_size(operator: str, left: Any, right: Any) -> int:
    """Size of what ``left <operator> right`` would build: length for a repeated sequence, bits for an int."""
    if operator == "*":
        if isinstance(left, int) and isinstance(right, (str, list, tuple)):
            left, right = right, left
        if isinstance(left, (str, list, tuple)) and isinstance(right, int):
            return len(left) * right
        if isinstance(left, int) and isinstance(right, int):
            return left.bit_length() + right.bit_length()
    elif isinstance(left, int) and isinstance(right, int) and right > 0:
        return left.bit_length() * right
    return 0


#: Prompts are rendered in Jinja's sandbox: a template can read its variables and call their harmless methods,
#: but not reach Python internals (``{{ cycler.__init__.__globals__.os }}`` and the like). A prompt is text an
#: author -- or one day a model proposing a variant -- writes; without the sandbox, writing a prompt was running
#: code in the API process.
#: The environment for prompt text without a file (an agent's inline ``system_prompt``): no loader, so no
#: ``{% include %}``, as with the bare ``Template()`` it replaces.
PROMPT_ENV = PromptSandbox(autoescape=False)

#: Repo-level shared prompt library. Partials placed here are includable from
#: ANY prompt, which is how the same rule can live in one file instead of being
#: copy-pasted into every pipeline's prompts (v4 / v5b / v6 ...).
SHARED_PROMPT_DIR = Path("config/prompts")

def strip_prompt_comments(source: str, origin: str = "prompt") -> str:
    """The template source without its ``<!-- ... -->`` comments.

    An HTML comment in a prompt file is a note for whoever edits the prompt,
    and every agent that edits one assumes it never reaches the model. A
    comment that fills its line takes the line with it, so no blank line is
    left behind.

    Applied to the SOURCE, before Jinja renders it: text that comes in through
    a variable (a chapter, a test case) keeps whatever it contains.

    A plain scan with ``str.find`` rather than a regex: a regex rescans to the
    end of the text from every unclosed opener, which is quadratic on a prompt
    that renders on every LLM call.
    """
    out = []
    pos = 0
    while (start := source.find("<!--", pos)) != -1:
        end = source.find("-->", start + 4)
        if end == -1:
            # Keeping the rest is safer than dropping it, but it breaks the
            # promise above -- say so.
            logger.warning("Unclosed '<!--' in %s at offset %d: everything after "
                           "it is sent to the model", origin, start)
            break
        end += 3
        # Look only as far as the text not yet handled, in both directions:
        # searching the whole line again for every comment is quadratic on
        # one long line full of them.
        newline = source.rfind("\n", pos, start)
        line_start = newline + 1 if newline != -1 else pos
        at_line_start = newline != -1 or pos == 0 or source[pos - 1] == "\n"
        line_end = end
        while line_end < len(source) and source[line_end] in " \t\r":
            line_end += 1
        if (at_line_start and not source[line_start:start].strip(" \t")
                and (line_end == len(source) or source[line_end] == "\n")):
            out.append(source[pos:line_start])
            pos = line_end + 1
        else:
            out.append(source[pos:start])
            pos = end
    out.append(source[pos:])
    return "".join(out)


class _CommentStrippingLoader(FileSystemLoader):
    """FileSystemLoader whose templates arrive without HTML comments, so an
    ``{% include %}`` partial is stripped like the prompt that includes it."""

    def get_source(self, environment, template):
        source, filename, uptodate = super().get_source(environment, template)
        return strip_prompt_comments(source, filename), filename, uptodate


def get_datetime_context(timezone_str: str = "UTC", location: str = "Unknown") -> Dict[str, Any]:
    """Generate current datetime context for prompt templates."""
    try:
        if timezone_str.upper() == "UTC":
            tz: Any = pytz.UTC  # pytz types are complex, use Any
        else:
            tz = pytz.timezone(timezone_str)
        
        now = datetime.now(tz)
        tomorrow = now + timedelta(days=1)
        
        return {
            "current_date": now.strftime("%Y-%m-%d"),
            "current_time": now.strftime("%H:%M:%S"),
            "current_datetime": now.isoformat(),
            "current_timezone": timezone_str,
            "current_location": location,
            "tomorrow_date": tomorrow.strftime("%Y-%m-%d"),
            "current_weekday": now.strftime("%A"),
            "current_month": now.strftime("%B"),
            "current_year": now.year,
            "unix_timestamp": int(now.timestamp())
        }
    except Exception:
        # Fallback to basic info
        now = datetime.now()
        tomorrow = now + timedelta(days=1)
        return {
            "current_date": now.strftime("%Y-%m-%d"),
            "current_time": now.strftime("%H:%M:%S"),
            "current_datetime": now.isoformat(),
            "current_timezone": "Local",
            "current_location": location,
            "tomorrow_date": tomorrow.strftime("%Y-%m-%d"),
            "current_weekday": now.strftime("%A"),
            "current_month": now.strftime("%B"),
            "current_year": now.year,
            "unix_timestamp": int(now.timestamp())
        }


def _is_text_template(template_path: str) -> bool:
    """Check if file is a plain text/markdown template."""
    lower_path = template_path.lower()
    return lower_path.endswith(('.md', '.txt', '.markdown'))


def _include_search_paths(template_path: str) -> Tuple[str, ...]:
    """Directories that ``{% include %}`` may read from, most specific first.

    1. The template's own directory — ``{% include "shared/tone.md" %}`` resolves
       next to the prompt itself.
    2. ``config/prompts`` — the shared library, includable from every prompt.

    Jinja's FileSystemLoader confines lookups to these roots, so a template
    cannot escape them via ``..``.
    """
    paths = [str(Path(template_path).parent)]
    if SHARED_PROMPT_DIR.is_dir():
        shared = str(SHARED_PROMPT_DIR)
        if shared not in paths:
            paths.append(shared)
    return tuple(paths)


@lru_cache(maxsize=64)
def _get_env(search_paths: Tuple[str, ...]) -> PromptSandbox:
    """Jinja environment for a set of include roots.

    Cached because prompts render on EVERY LLM call — the previous bare
    ``Template()`` also reused a cached environment internally, so building a
    fresh one per render would have been a regression. Jinja environments are
    documented as thread-safe once configured, and FileSystemLoader keeps
    ``auto_reload`` on, so edited partials are still picked up without a
    restart.

    Settings mirror the previous bare ``Template()``: autoescape off (these are
    markdown prompts, not HTML) and undefined variables render empty. Sandboxed, as PROMPT_ENV.
    """
    return PromptSandbox(
        loader=_CommentStrippingLoader(list(search_paths), encoding="utf-8"),
        autoescape=False,
    )


def _render_text_template(template_path: str, context: Dict[str, Any]) -> Dict[str, str]:
    """Render a plain text or markdown file as a single system_prompt section.

    The entire file content is treated as the system_prompt and rendered
    with Jinja2 template substitution. ``{% include %}`` is supported and
    resolves against :func:`_include_search_paths`, so a rule shared by several
    prompts lives in ONE partial instead of being copy-pasted (and drifting).

    Args:
        template_path: Path to the text/markdown file
        context: Template context variables for Jinja2 rendering

    Returns:
        Dict with single 'system_prompt' key containing the rendered content
    """
    with open(template_path, "r", encoding="utf-8") as f:
        raw_content = strip_prompt_comments(f.read(), template_path)

    try:
        # An Environment (not a bare Template) is what gives templates a loader —
        # without one, `{% include %}` raises "no loader for this environment".
        env = _get_env(_include_search_paths(template_path))
        rendered = env.from_string(raw_content).render(**context)
    except Exception as e:
        # Includes are resolved here, so a missing/broken partial lands in this
        # branch. Falling back to the raw text would ship literal Jinja tags to
        # the model — log loudly enough that it is not mistaken for prose.
        logger.warning(
            "Failed to render prompt template %s: %s "
            "(includes are resolved from %s; line numbers count without "
            "comment lines) — using unrendered content",
            template_path, e, _include_search_paths(template_path),
        )
        rendered = raw_content  # Fallback to unrendered content

    return {"system_prompt": rendered}


def render_prompts(template_path: str, context: Dict[str, Any], auto_datetime: bool = True, timezone: str = "UTC", location: str = "Unknown") -> Dict[str, str]:
    """Render a markdown/text prompt template into a single ``system_prompt``.

    The entire file is the system prompt, rendered with Jinja2 (e.g.
    ``{{ current_date }}``, ``{{ tools }}``, ``{{ max_steps }}``). Templates
    are ``.md`` / ``.txt`` / ``.markdown``.

    The former multi-section YAML format (``system_prompt`` / ``tools_prompt`` /
    ``general_instructions_prompt`` keys) was removed — everything is one
    markdown document now. A ``.yaml``/``.yml`` path raises a clear error with
    migration guidance rather than silently embedding raw YAML.

    Returns:
        Dict with a single ``system_prompt`` key.
    """
    if auto_datetime:
        datetime_context = get_datetime_context(timezone, location)
        context = {**context, **datetime_context}

    if not _is_text_template(template_path):
        raise ValueError(
            f"Prompt template '{template_path}' is not markdown. YAML prompt "
            f"templates are no longer supported — convert it to a single "
            f"markdown (.md) file (Jinja2 variables still work).")

    logger.debug(f"Rendering markdown prompt template: {template_path}")
    return _render_text_template(template_path, context)
