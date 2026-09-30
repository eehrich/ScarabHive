"""Platform detection for bash executable."""

import os
import platform
import shutil
from typing import Tuple


class PlatformDetector:
    """Detects and configures platform-specific shell."""

    def detect_bash(self) -> Tuple[str, str]:
        """
        Detect bash executable path.

        Returns:
            Tuple[str, str]: (bash_path, shell_name)

        Raises:
            RuntimeError: If no bash executable found
        """
        system = platform.system()

        if system == "Windows":
            return self._detect_windows_bash()
        else:
            return self._detect_unix_bash()

    def _detect_windows_bash(self) -> Tuple[str, str]:
        """Detect bash on Windows: Git Bash, else the first bash on PATH."""
        # Try Git Bash first (preferred on Windows)
        git_bash = r"C:\Program Files\Git\bin\bash.exe"
        if os.path.exists(git_bash):
            return git_bash, "Git Bash"

        # Try bash in PATH (might be Git Bash or other)
        bash = shutil.which("bash")
        if bash:
            return bash, "bash"

        # No wsl.exe fallback: every command is spawned as `<bash> -c
        # <command>`, and wsl.exe answers that with "invalid command line
        # argument: -c" (measured) -- a terminal that loads and runs nothing.
        # WSL's own bash.exe in System32 takes -c and is found on PATH above.
        raise RuntimeError(
            "No bash executable found on Windows. "
            "Please install Git Bash (https://git-scm.com/download/win), "
            "or name a bash in the terminal's platform.bash_path"
        )

    def _detect_unix_bash(self) -> Tuple[str, str]:
        """Detect bash on Linux/macOS."""
        bash = shutil.which("bash") or "/bin/bash"
        if os.path.exists(bash):
            return bash, "bash"

        raise RuntimeError("No bash executable found. Please install bash.")
