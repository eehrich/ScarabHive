"""Command security validation."""

import re
from typing import List, Optional, Tuple

# What starts, joins or feeds a second command on the shell line. With chains
# off, and on every instance with a whitelist, the command is refused when any
# of these appears ANYWHERE in it -- lexically, without parsing quotes:
# `echo "a;b"` is refused too. That errs on the safe side on purpose; an
# instance without a whitelist that needs such a character turns chains on. A
# line break is a separator for `bash -c` like `;`, and `|` and `&` cover `||`,
# `&&`, pipes and a trailing `&` alike. This keeps the shell line to one command;
# it does not see a command that runs another from its own arguments (an
# evaluating builtin, a nested shell, an evaluating expansion). The command is
# exactly one only together with a whitelist that names the program.
_SECOND_COMMAND = ("\n", "\r", ";", "|", "&", "`", "$(", "<(", ">(")

# Control characters: everything below 0x20 except tab, and DEL. No command a
# whitelist names needs one, and a pattern is easy to write so that one slips
# through (`\s` matches a line break, `$` matches before a final one).
_CONTROL_CHARACTER = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")


class CommandSecurityValidator:
    """Guards against destructive accidents — NOT a security boundary, with one
    exception named at the end.

    This tool runs arbitrary shell commands by design, so a pattern list can
    never contain what an agent is able to do: everything below is reachable
    through ``sh -c``, a pipe, ``xargs`` or ``python -c``. Measured on the
    previous rule set, ``curl … > /tmp/x; sh /tmp/x`` passed while writing a
    Markdown file with a code span was rejected — filtering *syntax* buys
    nothing and costs daily friction.

    The real boundary is whether an agent is granted the ``terminal`` tool at
    all (per-agent allow/deny lists). What remains here is a hand-brake against
    the handful of commands nobody types on purpose.

    The exception: a whitelist whose patterns name the program, anchored at
    both ends. The whitelist alone keeps the shell line to one command,
    whatever the chain setting (chains off on such an instance is belt and
    braces); no control character reaches a pattern, and the command is one
    the configuration names (plus, in the executor, its configured directory
    and environment). That is a boundary, and the ``user`` gate of
    ``state_graph_agent`` relies on it: its terminal starts one analysis
    script, nothing else.
    """

    # Commands that are almost never intended, and unrecoverable when they are
    # a slip. Deliberately specific: a pattern that also matches ordinary work
    # gets worked around, and a rule that gets worked around protects nothing.
    DANGEROUS_PATTERNS = [
        r'rm\s+-rf\s+/',           # Recursive delete from root
        r'dd\s+if=.*of=/dev/',     # Disk operations
        r':\(\)\{.*\};:',          # Fork bomb
        r'mkfs\.',                 # Format filesystem
        r'chmod\s+-R\s+777',       # Dangerous permissions
        r'chown\s+-R\s+root',      # Owner changes
        r'wget.*\|.*sh',           # Download and execute
        r'curl.*\|.*bash',         # Download and execute
    ]
    # Removed on purpose, do not restore without reading the docstring above:
    #   r'\$\(.*\)'  — blocked every command substitution, including
    #                  `echo "$(git branch --show-current)"`. Its own comment
    #                  already said "too broad".
    #   r'`.*`'      — matched ANY two backticks in the whole string, so a
    #                  heredoc writing Markdown ("use `git status`") or a code
    #                  fence was rejected, even though the shell never
    #                  evaluates a quoted heredoc.

    def __init__(
        self,
        whitelist: Optional[List[str]] = None,
        blacklist: Optional[List[str]] = None,
        allow_command_chains: bool = True,
        extra_dangerous_patterns: Optional[List[str]] = None,
    ):
        """
        Initialize security validator.

        Args:
            whitelist: List of regex patterns for allowed commands (if set, only these are allowed)
            blacklist: List of regex patterns for blocked commands (always blocked)
            allow_command_chains: Allow more than one command per shell line (default:
                True). False keeps the line to one command, lexically (see
                _second_command); a whitelist does the same whatever this says.
                Exactly one command takes a whitelist that names the program.
            extra_dangerous_patterns: Additional built-in-style patterns from the
                operator's config, so tightening this list is a deployment
                decision rather than a code change.
        """
        self.whitelist_patterns = whitelist or []
        self.blacklist_patterns = blacklist or []
        self.allow_command_chains = allow_command_chains
        self.dangerous_patterns = list(self.DANGEROUS_PATTERNS) + list(extra_dangerous_patterns or [])

    def validate_command(self, command: str) -> Tuple[bool, str]:
        """
        Validate command is safe to execute.

        Args:
            command: Shell command to validate

        Returns:
            Tuple[bool, str]: (is_valid, error_message)
        """
        # Check for dangerous patterns
        for pattern in self.dangerous_patterns:
            if re.search(pattern, command, re.IGNORECASE):
                return False, f"Command blocked: matches dangerous pattern '{pattern}'"

        # Check whitelist if configured
        if self.whitelist_patterns:
            # Before any pattern is asked, whatever the chain setting: a
            # whitelist names commands, and none of them spans lines.
            if _CONTROL_CHARACTER.search(command):
                return False, ("Command blocked: this terminal runs only the commands its "
                               "configuration allows, and those contain no control characters "
                               "(line breaks included)")
            if not any(re.match(pat, command) for pat in self.whitelist_patterns):
                return False, "Command not in whitelist"

        # Check blacklist
        for pattern in self.blacklist_patterns:
            if re.search(pattern, command, re.IGNORECASE):
                return False, f"Command blocked by blacklist pattern '{pattern}'"

        # One command per shell line, where chains are off or a whitelist is set
        second = self._second_command(command)
        if second is not None:
            why = "its whitelist names single commands" if self.whitelist_patterns else "command chains are off"
            return False, (f"Command blocked: this terminal runs one command per call, and {second!r} "
                           f"starts or joins another one ({why})")

        # NOTE: an "odd ASCII-quote count" check used to live here. It broke
        # the moment a non-ASCII quote appeared anywhere in the command — a
        # German „word" pairs one ASCII '"' with a non-counting „, so ONE
        # quoted phrase looked "unbalanced" and TWO looked fine again. It also
        # never understood escapes or heredocs. Real quoting mistakes are the
        # shell's job to reject, with a far better error message than this
        # ever gave.

        return True, "OK"

    def _second_command(self, command: str) -> Optional[str]:
        """What in *command* starts, joins or feeds a second command on the shell
        line, or None.

        Asked where chains are off, and on every instance with a whitelist
        whatever the chain setting: there, a pattern alone would be all that
        stands between the command it names and a second one behind it. Lexical
        on purpose (see _SECOND_COMMAND): a quoted ``;`` or ``|`` is refused as
        well, because telling a quoted one from a live one means parsing the
        shell's grammar, and a mistake there lets a second command through.

        This keeps the shell line to one command. Whether that command runs
        another is up to the command; exactly one takes a whitelist that names
        the program as well.

        The check that stood here before looked only at ``&&``, ``||`` and
        ``;`` and then let every such chain through unless a backtick or ``$``
        followed the operator; pipes, a trailing ``&`` and line breaks were not
        looked at at all, although ``bash -c`` runs every line.
        """
        if self.allow_command_chains and not self.whitelist_patterns:
            return None
        for marker in _SECOND_COMMAND:
            if marker in command:
                return marker
        return None
