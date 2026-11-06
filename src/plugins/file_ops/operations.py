"""Safe file operation implementations with atomic writes and error handling."""

from __future__ import annotations

import asyncio
import bisect
import codecs
import contextlib
import difflib
import errno
import functools
import logging
import os
import secrets
import weakref
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

import aiofiles

from agent_system.utils.path_sandbox import holds_secrets, is_link, is_secret

from . import textsearch


logger = logging.getLogger(__name__)


#: Typographic characters a model types plainly (and the other way round), each mapped to ONE character, so a
#: position in the translated text is the same position in the original.
_PLAIN_TYPOGRAPHY = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"', "«": '"', "»": '"',
    "‐": "-", "‑": "-", "‒": "-", "–": "-", "—": "-", "−": "-",
    " ": " ", " ": " ", " ": " ", " ": " ",
})

#: Beyond this many lines the closest-line hint is skipped: difflib compares every line.
_CLOSEST_LINES_MAX_FILE_LINES = 20000


def _lf_span_in_original(content: str, start: int, end: int) -> tuple[int, int]:
    """Where [start, end) of ``content`` with every CRLF read as LF lies in ``content`` itself."""
    collapsed: list[int] = []      # each CRLF's position in the LF view
    index = content.find("\r\n")
    while index != -1:
        collapsed.append(index - len(collapsed))
        index = content.find("\r\n", index + 2)
    # A CRLF before a position adds its \r; one AT the end position belongs to the span only if its \n does.
    return start + bisect.bisect_left(collapsed, start), end + bisect.bisect_left(collapsed, end)


def _line_ending_at(content: str, start: int, end: int) -> str:
    """The line ending new text takes where it replaces content[start:end]: that of the span, else of its line,
    else of the file."""
    span = content[start:end]
    if "\n" in span:
        return "\r\n" if "\r\n" in span else "\n"
    after = content.find("\n", end)
    if after != -1:
        return "\r\n" if after > 0 and content[after - 1] == "\r" else "\n"
    return "\r\n" if "\r\n" in content else "\n"


def _utf16_codec(path: Path) -> Optional[str]:
    """The codec of a file that starts with a UTF-16 BOM -- the one that keeps the BOM as a character, so a
    rewrite keeps the byte order -- else None."""
    try:
        with open(path, "rb") as handle:
            head = handle.read(2)
    except OSError:
        return None
    return {codecs.BOM_UTF16_LE: "utf-16-le", codecs.BOM_UTF16_BE: "utf-16-be"}.get(head)


def _occurrences(text: str, part: str) -> int:
    """How often ``part`` occurs in ``text``, overlapping matches included (str.count skips them)."""
    if not part:
        return len(text) + 1   # as str.count: an empty oldString is ambiguous
    count, at = 0, text.find(part)
    while at != -1:
        count += 1
        at = text.find(part, at + 1)
    return count


def _closest_lines(content: str, wanted: str, count: int = 3) -> list[str]:
    """The lines of ``content`` most like the first non-blank line of ``wanted``, as "line N: text".

    Lines split at \\n only: splitlines() also splits at form feeds and U+2028, and its numbers were not
    read_file's."""
    probe = next((line.strip() for line in wanted.split("\n") if line.strip()), "")
    lines = content.split("\n")
    if not probe or len(lines) > _CLOSEST_LINES_MAX_FILE_LINES:
        return []
    stripped = [line.strip() for line in lines]
    found = difflib.get_close_matches(probe, stripped, n=count, cutoff=0.6)
    hits = []
    for text in dict.fromkeys(found):
        number = stripped.index(text) + 1
        hits.append(f"line {number}: {lines[number - 1][:200]}")
    return hits


#: Bytes of the file's name a temp name keeps (".<name>.<12 hex>.tmp"): at most
#: 218 bytes, under the 255 a file system allows for one name.
_TEMP_NAME_KEEP = 200


def _secrets_refused(*paths: Path) -> Optional[Dict[str, Any]]:
    """The error for a delete, move or rename that would touch a private file
    (config/*.env, data/users.db, data/sessions), else None."""
    for path in paths:
        if is_secret(path) or (not is_link(path) and holds_secrets(path)):
            return {
                "status": "error",
                "error": f"Access to the installation's private files (secrets, user store, sessions) is refused: {path}",
                "error_type": "PermissionError",
                "path": str(path),
            }
    return None


def _temp_beside(path: Path) -> Path:
    """A name next to ``path`` that no file has yet.

    The writes used ``<name>.tmp``: a file of that name the person kept beside
    ``<name>`` was overwritten, then deleted with the temp -- and a checkpoint of
    the write, which names only ``<name>``, could not bring it back.
    """
    # The name only shortened: a temp name longer than the file's own failed on
    # names near the file system's limit (255 bytes) that the plain write took.
    kept = os.fsencode(path.name)[:_TEMP_NAME_KEEP].decode("utf-8", "ignore")
    return path.with_name(f".{kept}.{secrets.token_hex(6)}.tmp")


#: One lock per file that is being written, while anyone holds or waits for it.
_WRITERS: "weakref.WeakValueDictionary[str, asyncio.Lock]" = weakref.WeakValueDictionary()


def _one_writer_per_path(written=lambda path, *rest: (path,)):
    """The decorated method one call at a time per file it writes (``written`` names them from its
    arguments), in this process.

    The tool calls of one step run side by side (one task each): two replace_string_in_file on one
    file both read it, each wrote the file whole with its own edit, and both said success -- in a
    one-shot run 1 of 5 edits stayed (measured: of 3 parallel edits one survives, 20 runs of
    20). A move or rename holds its target too: a create of that name checked "not there", the move
    landed, and the create's temp replaced it. Module-wide, not per instance: instances may share a
    tree. Several locks are taken in one order, so two calls never wait for each other's.
    """
    def wrap(method):
        @functools.wraps(method)
        async def held(self, *args, **kwargs):
            keys = sorted({os.path.normcase(os.path.abspath(p)) for p in written(*args)})
            locks = [_WRITERS.get(k) or _WRITERS.setdefault(k, asyncio.Lock()) for k in keys]
            async with contextlib.AsyncExitStack() as stack:
                for lock in locks:
                    await stack.enter_async_context(lock)
                return await method(self, *args, **kwargs)
        return held
    return wrap


async def _write_through_temp(path: Path, content: str, encoding: str, keep_mode: bool) -> None:
    """Replace ``path`` whole in one rename, through a temp file of its own.

    ``keep_mode``: the replacement is a new file, and without the old mode an
    executable script comes back without its bit, a 0600 file world-readable --
    for an edit as much as for an overwrite.
    """
    temp_path = _temp_beside(path)
    # Claimed here, in this thread and with O_EXCL: a name that exists after all
    # is an error -- never a file overwritten, and not deleted either -- and once
    # it exists this call knows it is its own. Then opened by NAME with 'r+',
    # which creates nothing: an open still under way in the thread pool when a
    # cancel cleans up finds the name gone, instead of making the temp again.
    # (Created there, a cancel during the open left the temp behind.)
    os.close(os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666))
    try:
        async with aiofiles.open(temp_path, 'r+', encoding=encoding, newline='') as f:
            await f.write(content)
        if keep_mode:
            try:
                temp_path.chmod(path.stat().st_mode)
            except OSError as exc:
                logger.warning(f"Could not carry the mode of {path} over: {exc}")
        temp_path.replace(path)
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError as exc:  # Windows: a handle the thread pool still holds
            logger.warning(f"Could not remove the temp file {temp_path}: {exc}")


class FileOperations:
    """Safe file operation implementations."""

    def __init__(self, max_unpaginated_kb: int = 100, default_line_limit: int = 500):
        """Initialize file operations.
        
        Args:
            max_unpaginated_kb: Max file size in KB to read without pagination
            default_line_limit: Default number of lines for paginated reads
        """
        self.max_unpaginated_size = max_unpaginated_kb * 1024  # Convert to bytes
        self.default_line_limit = default_line_limit

    async def read_file_safe(
        self,
        path: Path,
        offset: int = 0,
        limit: Optional[int] = None,
        encoding: str = "utf-8"
    ) -> Dict[str, Any]:
        """
        Read file contents with pagination support.
        
        Large files (>100KB) are automatically paginated to prevent memory issues.
        Use offset and limit parameters for explicit pagination control.

        Args:
            path: File path to read
            offset: Starting line number (0-indexed)
            limit: Maximum number of lines to read (default: 500 for large files)
            encoding: Text encoding

        Returns:
            Dict with status, content, total_lines, lines_read, offset, encoding, file_path
        """
        try:
            # Check file size first
            file_size = path.stat().st_size

            # The first bytes decide. A UTF-16 file (its BOM: PowerShell's Out-File, .reg) is read as UTF-16; a
            # binary is refused -- read with errors='replace' it came back as a page of replacement characters.
            # An unreadable file is no binary: the read below says what it is.
            try:
                with open(path, "rb") as handle:
                    head = handle.read(textsearch.SNIFF_BYTES)
            except OSError:
                head = b""
            if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
                encoding = "utf-16"
            elif head and not textsearch.looks_like_text(head):
                return {
                    "status": "error",
                    "error": f"Not a text file: {path.name} ({file_size} bytes of binary content).",
                    "error_type": "BinaryFileError",
                    "file_path": str(path)
                }

            # If no pagination (offset=0 and no limit), read entire file preserving original line endings
            # BUT only if file is small enough
            if offset == 0 and limit is None and file_size <= self.max_unpaginated_size:
                async with aiofiles.open(path, 'r', encoding=encoding, errors='replace', newline='') as f:
                    content = await f.read()

                # Count lines for metadata
                total_lines = content.count('\n') + (1 if content and not content.endswith('\n') else 0)

                return {
                    "status": "success",
                    "content": content,
                    "total_lines": total_lines,
                    "lines_read": total_lines,
                    "offset": 0,
                    "encoding": encoding,
                    "file_path": str(path)
                }

            # Large file without explicit limit - apply default pagination
            if limit is None and file_size > self.max_unpaginated_size:
                limit = self.default_line_limit
                logger.info(
                    f"Large file ({file_size} bytes), applying default pagination: "
                    f"limit={limit} lines. Use offset/limit for more control."
                )

            # Pagination mode: need to process line by line. A page also stops at the size an unpaginated read
            # may have: 500 lines of minified code or JSON were megabytes in one answer.
            # Counted in UTF-8 bytes, as the answer goes out: in characters a page of umlauts or CJK was
            # three times the size the note named.
            lines = []
            total_lines = 0
            page_bytes = 0
            capped = cut = False

            async with aiofiles.open(path, 'r', encoding=encoding, errors='replace', newline='') as f:
                async for line in f:
                    if total_lines >= offset and (limit is None or len(lines) < limit) and not capped:
                        size = len(line.encode("utf-8", "replace"))
                        if page_bytes + size > self.max_unpaginated_size:
                            capped = True
                            if not lines:   # one line longer than a whole page: its start, said so
                                start = line.encode("utf-8", "replace")[:self.max_unpaginated_size]
                                lines.append(start.decode("utf-8", "ignore"))
                                cut = True
                        else:
                            # Preserve line endings - don't strip them
                            lines.append(line)
                            page_bytes += size
                    total_lines += 1

            # Join without adding extra newlines (they're already in the lines)
            content = ''.join(lines)

            result = {
                "status": "success",
                "content": content,
                "total_lines": total_lines,
                "lines_read": len(lines),
                "offset": offset,
                "encoding": encoding,
                "file_path": str(path)
            }
            if capped:
                # offset is 1-indexed for the model (read_file converts it)
                next_offset = offset + len(lines) + 1
                result["note"] = (
                    f"Line {offset + 1} is longer than {self.max_unpaginated_size // 1024} KB: only its start "
                    f"is shown. The next line starts at offset={next_offset}."
                    if cut else
                    f"Page stopped at {self.max_unpaginated_size // 1024} KB after {len(lines)} lines. "
                    f"Continue with offset={next_offset}.")
                result["line_cut"] = cut
            return result

        except UnicodeDecodeError as e:
            logger.warning(f"Encoding error reading {path}: {e}")
            return {
                "status": "error",
                "error": f"File encoding error. Try a different encoding (current: {encoding})",
                "error_type": "EncodingError",
                "file_path": str(path)
            }
        except Exception as e:
            logger.error(f"Error reading file {path}: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__,
                "file_path": str(path)
            }

    @_one_writer_per_path()
    async def create_file_safe(
        self,
        path: Path,
        content: str,
        overwrite: bool = False,
        create_dirs: bool = True,
        encoding: str = "utf-8"
    ) -> Dict[str, Any]:
        """
        Create a file atomically, or replace one whole with overwrite.

        Args:
            path: File path to create
            content: File content
            overwrite: Replace the file if it exists (its mode is carried over)
            create_dirs: Auto-create parent directories
            encoding: Text encoding

        Returns:
            Dict with status, file_path, bytes_written, created_dirs, replaced
            (whether a file was there before -- the caller asked, this answers)
        """
        try:
            # A directory first: overwrite is no way out of it, so advising it would mislead.
            if path.is_dir():
                return {
                    "status": "error",
                    "error": f"Is a directory, not a file: {path}",
                    "error_type": "IsADirectoryError",
                    "file_path": str(path)
                }
            existed = path.exists()
            if existed and not overwrite:
                return {
                    "status": "error",
                    "error": f"File already exists: {path}. Use overwrite=true to replace it, "
                             f"or replace_string_in_file to edit part of it.",
                    "error_type": "FileExistsError",
                    "file_path": str(path)
                }

            # Create parent directories if needed
            created_dirs = []
            if create_dirs and not path.parent.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                created_dirs.append(str(path.parent))

            await _write_through_temp(path, content, encoding, keep_mode=existed)
            bytes_written = path.stat().st_size

            return {
                "status": "success",
                "file_path": str(path),
                "bytes_written": bytes_written,
                "created_dirs": created_dirs,
                "replaced": existed
            }

        except Exception as e:
            logger.error(f"Error creating file {path}: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__,
                "file_path": str(path)
            }

    @_one_writer_per_path()
    async def edit_file_safe(
        self,
        path: Path,
        old_string: str,
        new_string: str,
        encoding: str = "utf-8"
    ) -> Dict[str, Any]:
        """
        Replace text in an existing file — the one edit primitive.

        Line-number modes (insert, replace_lines) and append used to live here
        with no tool exposing them. They were removed rather than exposed: a
        line number goes stale after the first edit above it, insert counted
        from 0 while read_file counts from 1, and a replace anchored on real
        text fails loudly instead of editing the wrong line.

        Args:
            path: File to edit
            old_string: String to replace
            new_string: Replacement string
            encoding: Text encoding

        Returns:
            Dict with status, file_path, changes
        """
        try:
            if not path.exists():
                return {
                    "status": "error",
                    "error": f"File not found: {path}",
                    "error_type": "FileNotFoundError",
                    "file_path": str(path)
                }

            # A UTF-16 file is edited in its own byte order, its BOM kept as the first character: read_file
            # shows it as text, and a strict UTF-8 read refused the edit -- the next try was an overwrite
            # that turned the file into UTF-8.
            encoding = _utf16_codec(path) or encoding
            # newline='' keeps the file's own line endings. The default
            # translation turned every CRLF file into LF on its first edit —
            # the whole file rewritten, whatever the edit was.
            async with aiofiles.open(path, 'r', encoding=encoding, newline='') as f:
                current_content = await f.read()

            if old_string is None or new_string is None:
                return {
                    "status": "error",
                    "error": "Parameters 'old_string' and 'new_string' are required",
                    "error_type": "ValidationError"
                }

            # Try exact match first. Overlapping matches count: str.count saw one in "q\nq\nq\n" for "\nq\n"
            # and the edit went to the first of two places.
            replacements = _occurrences(current_content, old_string)
            if replacements == 1:
                # The new text in the line endings of where it lands: the model writes \n, and pasted as it
                # came it put LF lines into a CRLF file.
                at = current_content.find(old_string)
                end = at + len(old_string)
                # A match that starts on the \n of a CRLF takes its \r along: left in place it stood before the
                # new text's own line ending (\r\r\n), or alone where the new text joins the lines.
                if old_string.startswith('\n') and at > 0 and current_content[at - 1] == '\r':
                    at -= 1
                ending = _line_ending_at(current_content, at, end)
                new_content = (current_content[:at] + new_string.replace('\r\n', '\n').replace('\n', ending)
                               + current_content[end:])

            elif replacements == 0:
                # Flexible matching: the model writes \n, the file may hold \r\n
                normalized_content = current_content.replace('\r\n', '\n')
                normalized_old_string = old_string.replace('\r\n', '\n')
                normalized_new_string = new_string.replace('\r\n', '\n')
                replacements = _occurrences(normalized_content, normalized_old_string)

                if replacements == 1:
                    logger.info("Using normalized line endings for replacement (CRLF/LF flexibility)")
                    # Only the matched span changes, in that span's own line endings: rewriting the whole
                    # text in one style turned a file with mixed endings into all-CRLF on one edit.
                    at = normalized_content.find(normalized_old_string)
                    start, end = _lf_span_in_original(current_content, at, at + len(normalized_old_string))
                    ending = _line_ending_at(current_content, start, end)
                    new_content = (current_content[:start] + normalized_new_string.replace('\n', ending)
                                   + current_content[end:])
                elif replacements == 0:
                    # Check if it's only whitespace difference (informative error)
                    stripped_content = normalized_content.replace(' ', '').replace('\t', '')
                    stripped_old_string = normalized_old_string.replace(' ', '').replace('\t', '')

                    if stripped_old_string in stripped_content:
                        # Content matches but whitespace differs - provide detailed analysis
                        # Find the position where content matches
                        pos = stripped_content.find(stripped_old_string)

                        # Reconstruct position in original content
                        char_count = 0
                        for i, char in enumerate(normalized_content):
                            if char not in (' ', '\t'):
                                if char_count == pos:
                                    # Found start position - extract a few lines for comparison
                                    lines_before = normalized_content[:i].count('\n')
                                    start_line = max(0, lines_before)

                                    # Get the relevant lines from file
                                    file_lines = normalized_content.splitlines()
                                    search_lines = normalized_old_string.splitlines()

                                    if start_line < len(file_lines) and search_lines:
                                        # Build corrected oldString with proper whitespace
                                        corrected_lines = []
                                        for j, search_line in enumerate(search_lines):
                                            file_idx = start_line + j
                                            if file_idx < len(file_lines):
                                                file_line = file_lines[file_idx]
                                                # Extract leading whitespace from file
                                                file_leading = len(file_line) - len(file_line.lstrip())
                                                file_ws = file_line[:file_leading]
                                                # Apply to search line content
                                                search_content = search_line.lstrip()
                                                corrected_lines.append(file_ws + search_content)
                                            else:
                                                corrected_lines.append(search_line)

                                        corrected_old_string = '\n'.join(corrected_lines)

                                        # Show first mismatched line with detailed whitespace info
                                        file_line = file_lines[start_line]
                                        search_line = search_lines[0]

                                        # Analyze whitespace
                                        file_leading = len(file_line) - len(file_line.lstrip())
                                        search_leading = len(search_line) - len(search_line.lstrip())

                                        file_ws = file_line[:file_leading]
                                        search_ws = search_line[:search_leading]

                                        ws_details = f"Line {start_line + 1}: "
                                        if file_leading != search_leading:
                                            ws_details += f"Expected {file_leading} leading whitespace chars, got {search_leading}. "

                                        file_tabs = file_ws.count('\t')
                                        file_spaces = file_ws.count(' ')
                                        search_tabs = search_ws.count('\t')
                                        search_spaces = search_ws.count(' ')

                                        if file_tabs != search_tabs or file_spaces != search_spaces:
                                            ws_details += f"File has {file_tabs} tabs + {file_spaces} spaces, search has {search_tabs} tabs + {search_spaces} spaces."

                                        logger.warning(f"String found with whitespace differences in {path}: {ws_details}")
                                        return {
                                            "status": "error",
                                            "error": f"String not found with exact whitespace. {ws_details}",
                                            "error_type": "WhitespaceMatchError",
                                            "file_path": str(path),
                                            "file_sample": file_line,
                                            "search_sample": search_line,
                                            "corrected_old_string": corrected_old_string,
                                            "hint": "Whitespace (spaces/tabs) doesn't match exactly. Use the 'corrected_old_string' value for oldString parameter."
                                        }
                                    break
                                char_count += 1

                        # Fallback if detailed analysis fails
                        logger.warning(f"String found with whitespace differences in {path}")
                        return {
                            "status": "error",
                            "error": "String not found with exact whitespace. Content matches but spaces/tabs differ.",
                            "error_type": "WhitespaceMatchError",
                            "file_path": str(path),
                            "hint": "Whitespace (spaces/tabs) doesn't match exactly. Copy the exact indentation from the file."
                        }

                    # Typographic quotes, dashes or spaces where oldString has plain ones (or the other way
                    # round). Not applied: the replacement would silently swap the file's characters.
                    plain_content = normalized_content.translate(_PLAIN_TYPOGRAPHY)
                    plain_old = normalized_old_string.translate(_PLAIN_TYPOGRAPHY)
                    if plain_old and _occurrences(plain_content, plain_old) == 1:
                        at = plain_content.find(plain_old)
                        return {
                            "status": "error",
                            "error": "String not found with exact characters: the file has different quotes, "
                                     "dashes or spaces there.",
                            "error_type": "CharacterMatchError",
                            "file_path": str(path),
                            "corrected_old_string": normalized_content[at:at + len(plain_old)],
                            "hint": "Use 'corrected_old_string' as oldString, and keep the file's characters "
                                    "in newString."
                        }

                    # True not found. difflib compares every line: off the event loop.
                    closest = await asyncio.to_thread(_closest_lines, normalized_content, normalized_old_string)
                    result = {
                        "status": "error",
                        "error": f"String not found in file: {old_string[:50]}...",
                        "error_type": "StringNotFoundError",
                        "file_path": str(path),
                        "hint": "String does not exist in file. Check spelling and ensure you have the correct content."
                    }
                    if closest:
                        result["closest_lines"] = closest
                        result["hint"] = ("String does not exist in file. The closest lines are in "
                                          "'closest_lines': read around them and copy the exact text.")
                    # A retry of an edit that went through (a timeout, a lost answer) finds only its result. Said
                    # beside "not found", never instead of it, and only for a newString that is more than a word.
                    new_text = normalized_new_string.strip()
                    if (new_text != normalized_old_string.strip() and new_text in normalized_content
                            and ('\n' in new_text or len(new_text) >= 12)):
                        result["new_string_present"] = True
                        result["hint"] = ("newString is already in the file: the edit may have been made already. "
                                          "Read the file before editing again.")
                    return result

            if replacements > 1:
                # The schema promises ONE occurrence and asks for a unique
                # oldString. Replacing all of them edited places the model never
                # looked at.
                return {
                    "status": "error",
                    "error": (f"oldString occurs {replacements} times in the file. "
                              "Include more surrounding lines so it matches exactly once."),
                    "error_type": "AmbiguousMatchError",
                    "file_path": str(path),
                    "occurrences": replacements
                }

            # Where the new text now stands: first and last line, 1-indexed.
            # Compared line by line instead, every line after an inserted one
            # counted as modified -- measured up to 53k characters of line
            # numbers in one answer, 28 of 133 answers over 2k.
            flat = current_content.replace('\r\n', '\n')
            added = new_string.replace('\r\n', '\n')
            # Newlines the new text opens with end the line before it: it
            # starts that many lines further down. A closing newline ends its
            # last line, it does not start another.
            lead = len(added) - len(added.lstrip('\n'))
            first = flat.count('\n', 0, max(flat.find(old_string.replace('\r\n', '\n')), 0)) + 1 + lead
            added = added[lead:]
            added = added[:-1] if added.endswith('\n') else added
            changes = {
                "replacements": replacements,
                "lines": [first, first + added.count('\n')]
            }


            await _write_through_temp(path, new_content, encoding, keep_mode=True)

            return {
                "status": "success",
                "file_path": str(path),
                "mode": "replace",
                "changes": changes
            }

        except Exception as e:
            logger.error(f"Error editing file {path}: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__,
                "file_path": str(path)
            }

    @_one_writer_per_path()
    async def delete_file_safe(self, path: Path) -> Dict[str, Any]:
        """
        Delete a file safely.

        Args:
            path: File to delete

        Returns:
            Dict with status, file_path
        """
        try:
            if not path.exists():
                return {
                    "status": "error",
                    "error": f"File not found: {path}",
                    "error_type": "FileNotFoundError",
                    "file_path": str(path)
                }

            if not path.is_file():
                return {
                    "status": "error",
                    "error": f"Path is not a file: {path}",
                    "error_type": "NotAFileError",
                    "file_path": str(path)
                }

            path.unlink()

            return {
                "status": "success",
                "file_path": str(path),
                "message": "File deleted successfully"
            }

        except Exception as e:
            logger.error(f"Error deleting file {path}: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__,
                "file_path": str(path)
            }

    @_one_writer_per_path()
    async def delete_path_safe(self, path: Path, recursive: bool = False) -> Dict[str, Any]:
        """
        Delete a file or directory safely.

        Args:
            path: Path to delete (file or directory)
            recursive: If True, delete non-empty directories recursively

        Returns:
            Dict with status, path, type (file/directory)
        """
        import shutil
        
        refused = _secrets_refused(path)
        if refused:
            return refused

        try:
            # A link goes, never what it points to: rmtree would refuse a
            # directory link, is_file would follow one to its target.
            if is_link(path):
                os.unlink(path)
                return {
                    "status": "success",
                    "path": str(path),
                    "type": "link",
                    "message": "Link deleted; what it pointed to is untouched"
                }

            if not path.exists():
                return {
                    "status": "error",
                    "error": f"Path not found: {path}",
                    "error_type": "FileNotFoundError",
                    "path": str(path)
                }

            if path.is_file():
                path.unlink()
                return {
                    "status": "success",
                    "path": str(path),
                    "type": "file",
                    "message": "File deleted successfully"
                }
            elif path.is_dir():
                if recursive:
                    shutil.rmtree(path)
                    return {
                        "status": "success",
                        "path": str(path),
                        "type": "directory",
                        "message": "Directory deleted recursively"
                    }
                else:
                    # Try to remove empty directory
                    try:
                        path.rmdir()
                        return {
                            "status": "success",
                            "path": str(path),
                            "type": "directory",
                            "message": "Empty directory deleted"
                        }
                    except OSError:
                        # Asked of the directory, not of the message: Windows
                        # words it in the system language ("ist nicht leer").
                        if any(path.iterdir()):
                            return {
                                "status": "error",
                                "error": f"Directory not empty: {path}. Use recursive=true to delete non-empty directories.",
                                "error_type": "DirectoryNotEmptyError",
                                "path": str(path)
                            }
                        raise
            else:
                return {
                    "status": "error",
                    "error": f"Unknown path type: {path}",
                    "error_type": "UnknownPathTypeError",
                    "path": str(path)
                }

        except Exception as e:
            logger.error(f"Error deleting path {path}: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__,
                "path": str(path)
            }

    @_one_writer_per_path(lambda source, destination: (source, destination))
    async def move_path_safe(self, source: Path, destination: Path) -> Dict[str, Any]:
        """
        Move a file or directory to a new location.

        Args:
            source: Source path
            destination: Destination path

        Returns:
            Dict with status, source, destination, type
        """
        import shutil
        
        refused = _secrets_refused(source, destination)
        if refused:
            return refused

        try:
            if not (source.exists() or source.is_symlink()):  # a dangling link moves too
                return {
                    "status": "error",
                    "error": f"Source not found: {source}",
                    "error_type": "FileNotFoundError",
                    "source": str(source)
                }

            if destination.exists():
                return {
                    "status": "error",
                    "error": f"Destination already exists: {destination}",
                    "error_type": "FileExistsError",
                    "destination": str(destination)
                }

            # Create parent directories if needed
            destination.parent.mkdir(parents=True, exist_ok=True)

            link = is_link(source)
            path_type = "link" if link else "file" if source.is_file() else "directory"

            if path_type == "file":
                shutil.move(str(source), str(destination))  # copies across drives
            else:
                # A rename, never shutil.move's copy fallback: across drives it
                # copytree's a junction -- islink() is False for one -- and so
                # copied what it points to, from outside, into the sandbox. A
                # folder may hold such a junction too.
                try:
                    os.rename(source, destination)
                except OSError as e:
                    return {
                        "status": "error",
                        "error": f"Cannot move this {path_type} to {destination}: {e}."
                                 + (" A link or a folder only moves within one drive."
                                    if e.errno == errno.EXDEV else ""),
                        "error_type": type(e).__name__,
                        "source": str(source),
                        "destination": str(destination)
                    }

            return {
                "status": "success",
                "source": str(source),
                "destination": str(destination),
                "type": path_type,
                "message": f"{path_type.capitalize()} moved successfully"
            }

        except Exception as e:
            logger.error(f"Error moving {source} to {destination}: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__,
                "source": str(source),
                "destination": str(destination)
            }

    @_one_writer_per_path(lambda path, new_name: (path, path.parent / new_name))
    async def rename_path_safe(self, path: Path, new_name: str) -> Dict[str, Any]:
        """
        Rename a file or directory (same directory).

        Args:
            path: Path to rename
            new_name: New name (without path)

        Returns:
            Dict with status, old_path, new_path, type
        """
        refused = _secrets_refused(path, path.parent / new_name)
        if refused:
            return refused

        try:
            if not (path.exists() or path.is_symlink()):  # a dangling link is renamed too
                return {
                    "status": "error",
                    "error": f"Path not found: {path}",
                    "error_type": "FileNotFoundError",
                    "path": str(path)
                }

            # Validate new_name doesn't contain path separators
            if "/" in new_name or "\\" in new_name:
                return {
                    "status": "error",
                    "error": f"new_name must not contain path separators: {new_name}",
                    "error_type": "ValidationError"
                }

            new_path = path.parent / new_name

            if new_path.exists():
                return {
                    "status": "error",
                    "error": f"Target already exists: {new_path}",
                    "error_type": "FileExistsError",
                    "new_path": str(new_path)
                }

            link = is_link(path)
            path_type = "link" if link else "file" if path.is_file() else "directory"
            path.rename(new_path)

            return {
                "status": "success",
                "old_path": str(path),
                "new_path": str(new_path),
                "type": path_type,
                "message": f"{path_type.capitalize()} renamed successfully"
            }

        except Exception as e:
            logger.error(f"Error renaming {path} to {new_name}: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__,
                "path": str(path)
            }

    async def list_directory_safe(
        self,
        dir_path: Path,
        recursive: bool = False,
        pattern: Optional[str] = None,
        include_hidden: bool = False,
        max_results: int = 200,
        include_ignored: bool = False,
        excludes: Sequence[str] = textsearch.DEFAULT_EXCLUDES,
    ) -> Dict[str, Any]:
        """
        List directory contents with filtering, capped at max_results entries.

        The listing itself lives in ``textsearch.list_directory`` and runs in
        a worker thread: a recursive walk is blocking disk I/O.

        Args:
            dir_path: Directory to list
            recursive: Recursively list subdirectories
            pattern: Glob pattern to filter entries
            include_hidden: Include hidden files
            max_results: Maximum number of entries (files + directories)
            include_ignored: Also list .gitignored, excluded and hidden entries
            excludes: Exclude globs pruned by a recursive listing

        Returns:
            Dict with status, dir_path, files, directories, truncated, skipped
        """
        try:
            if not dir_path.exists():
                return {
                    "status": "error",
                    "error": f"Directory not found: {dir_path}",
                    "error_type": "DirectoryNotFoundError",
                    "dir_path": str(dir_path)
                }

            if not dir_path.is_dir():
                return {
                    "status": "error",
                    "error": f"Path is not a directory: {dir_path}",
                    "error_type": "NotADirectoryError",
                    "dir_path": str(dir_path)
                }

            return await asyncio.to_thread(
                textsearch.list_directory,
                dir_path,
                recursive=recursive,
                pattern=pattern,
                max_results=max_results,
                excludes=excludes,
                include_hidden=include_hidden,
                include_ignored=include_ignored,
            )

        except Exception as e:
            logger.error(f"Error listing directory {dir_path}: {e}", exc_info=True)
            return {
                "status": "error",
                "error": str(e),
                "error_type": type(e).__name__,
                "dir_path": str(dir_path)
            }
