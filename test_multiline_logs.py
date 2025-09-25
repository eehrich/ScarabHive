#!/usr/bin/env python3
"""
Quick test script to create some multiline log entries for testing tooltip functionality
"""
import logging
import os

# Set up logging to write to the API log file
log_file = "logs/api.log"
os.makedirs("logs", exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    filename=log_file,
    filemode='a'
)

logger = logging.getLogger(__name__)

def create_test_entries():
    """Create test log entries with multiline content"""

    # Single line entries
    logger.info("Single line info message")
    logger.warning("Single line warning message")

    # Multiline entries that should trigger tooltips
    logger.error("""This is a multiline error message
that spans multiple lines
and contains important details
for debugging purposes""")

    logger.info("""Complex multiline information:
- First item in the list
- Second item with details
- Third item with more data

Additional context:
The system processed 1,234 records
Failed to process 5 records
Success rate: 99.6%""")

    logger.warning("""Database connection warning:
Connection pool exhausted
Current connections: 50/50
Waiting connections: 15
Recommended action: increase pool size""")

    # Another single line
    logger.debug("Debug message for testing")

    print("Test log entries created! Check the log viewer for multiline tooltips.")

if __name__ == "__main__":
    create_test_entries()