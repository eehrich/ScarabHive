"""Progress bar utilities for terminal output."""

import time


class ProgressBar:
    """Simple progress bar for terminal output."""

    def __init__(self, total: int, description: str = "", width: int = 50):
        self.total = total
        self.current = 0
        self.description = description
        self.width = width
        self.start_time = time.time()

    def update(self, n: int = 1) -> None:
        """Update progress by n steps."""
        self.current += n
        self._display()

    def _display(self) -> None:
        """Display the progress bar."""
        if self.total == 0:
            return

        percentage = min(100, (self.current / self.total) * 100)
        filled = int(self.width * (self.current / self.total))
        bar = "█" * filled + "░" * (self.width - filled)

        elapsed = time.time() - self.start_time
        if self.current > 0:
            eta = elapsed * (self.total - self.current) / self.current
            eta_str = f" ETA {eta:.1f}s"
        else:
            eta_str = ""

        print(f"\r{self.description} [{bar}] {percentage:.1f}% ({self.current}/{self.total}){eta_str}", end="", flush=True)

        if self.current >= self.total:
            print()  # New line when complete
