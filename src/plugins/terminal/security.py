"""Command security validation."""

import re
from typing import List, Optional, Tuple


class CommandSecurityValidator:
    """Validates commands against security rules."""

    # Dangerous command patterns to block
    DANGEROUS_PATTERNS = [
        r'rm\s+-rf\s+/',           # Recursive delete from root
        r'dd\s+if=.*of=/dev/',     # Disk operations
        r':\(\)\{.*\};:',          # Fork bomb
        r'mkfs\.',                 # Format filesystem
        r'chmod\s+-R\s+777',       # Dangerous permissions
        r'chown\s+-R\s+root',      # Owner changes
        r'wget.*\|.*sh',           # Download and execute
        r'curl.*\|.*bash',         # Download and execute
        r'\$\(.*\)',               # Command substitution abuse (too broad, see below)
        r'`.*`',                   # Backtick injection
    ]

    def __init__(
        self,
        whitelist: Optional[List[str]] = None,
        blacklist: Optional[List[str]] = None,
        allow_command_chains: bool = True
    ):
        """
        Initialize security validator.

        Args:
            whitelist: List of regex patterns for allowed commands (if set, only these are allowed)
            blacklist: List of regex patterns for blocked commands (always blocked)
            allow_command_chains: Allow chained commands with &&, ||, ; (default: True)
        """
        self.whitelist_patterns = whitelist or []
        self.blacklist_patterns = blacklist or []
        self.allow_command_chains = allow_command_chains

    def validate_command(self, command: str) -> Tuple[bool, str]:
        """
        Validate command is safe to execute.

        Args:
            command: Shell command to validate

        Returns:
            Tuple[bool, str]: (is_valid, error_message)
        """
        # Check for dangerous patterns
        for pattern in self.DANGEROUS_PATTERNS:
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

        # Unescaped quotes (odd number of quotes)
        if command.count('"') % 2 != 0 or command.count("'") % 2 != 0:
            return True

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
