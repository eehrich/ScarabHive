"""Command security validation."""

import re
from typing import List, Optional, Tuple


class CommandSecurityValidator:
    """Guards against destructive accidents — NOT a security boundary.

    This tool runs arbitrary shell commands by design, so a pattern list can
    never contain what an agent is able to do: everything below is reachable
    through ``sh -c``, a pipe, ``xargs`` or ``python -c``. Measured on the
    previous rule set, ``curl … > /tmp/x; sh /tmp/x`` passed while writing a
    Markdown file with a code span was rejected — filtering *syntax* buys
    nothing and costs daily friction.

    The real boundary is whether an agent is granted the ``terminal`` tool at
    all (per-agent allow/deny lists). What remains here is a hand-brake against
    the handful of commands nobody types on purpose.
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
            allow_command_chains: Allow chained commands with &&, ||, ; (default: True)
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
            if not any(re.match(pat, command) for pat in self.whitelist_patterns):
                return False, "Command not in whitelist"

        # Check blacklist
        for pattern in self.blacklist_patterns:
            if re.search(pattern, command, re.IGNORECASE):
                return False, f"Command blocked by blacklist pattern '{pattern}'"

        # Check for shell injection attempts
        if self._has_injection_risk(command):
            return False, "Potential shell injection detected"

        return True, "OK"

    def _has_injection_risk(self, command: str) -> bool:
        """
        Check for shell injection patterns.

        Args:
            command: Command to check

        Returns:
            bool: True if injection risk detected
        """
        # Multiple commands chained (only if not allowed)
        if not self.allow_command_chains:
            if '&&' in command or '||' in command or ';' in command:
                # Check if it's a safe chain pattern
                if not self._is_safe_chain(command):
                    return True

        # NOTE: an "odd ASCII-quote count" check used to live here. It broke
        # the moment a non-ASCII quote appeared anywhere in the command — a
        # German „word" pairs one ASCII '"' with a non-counting „, so ONE
        # quoted phrase looked "unbalanced" and TWO looked fine again. It also
        # never understood escapes or heredocs. Real quoting mistakes are the
        # shell's job to reject, with a far better error message than this
        # ever gave.

        return False

    def _is_safe_chain(self, command: str) -> bool:
        """
        Check if command chain is safe.

        Safe patterns:
        - Command followed by simple status check: command && echo "done"
        - Simple directory navigation: cd dir && ls
        - Build chains: make && make test

        Args:
            command: Command to check

        Returns:
            bool: True if safe chain pattern
        """
        # For now, allow all chains if allow_command_chains is True
        # Future: Implement more sophisticated chain validation
        if self.allow_command_chains:
            return True

        # Simple heuristic: allow if no suspicious characters after chain operators
        suspicious_after_chain = re.search(r'(&&|\|\||;)\s*[`$]', command)
        return not suspicious_after_chain
