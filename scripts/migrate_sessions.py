"""
Migration Script: Convert in-memory sessions to persistent storage

This script migrates sessions from the old in-memory format (if any existed)
to the new JSON-based persistent storage format.

Usage:
    python -m scripts.migrate_sessions [--dry-run]
"""

import argparse
import asyncio
import json
import logging
from pathlib import Path
from datetime import datetime, timezone

from agent_system.services.session_manager import SessionManager
from agent_system.utils.id import short_id

logger = logging.getLogger(__name__)


async def migrate_sessions(dry_run: bool = False):
    """Migrate sessions to persistent storage.
    
    Args:
        dry_run: If True, only show what would be migrated without actually doing it
    """
    # Initialize SessionManager
    storage_path = Path(__file__).parents[1] / "data" / "sessions"
    session_manager = SessionManager(storage_path=str(storage_path))
    
    logger.info("Session migration started (dry_run=%s)", dry_run)
    logger.info("Storage path: %s", storage_path)
    
    # In the old system, sessions were stored in-memory in Agent._sessions dict
    # Since this was never persisted, we don't have old sessions to migrate
    # This script serves as a template for future migrations
    
    migrated_count = 0
    error_count = 0
    
    # Example migration (if we had old session data):
    # old_sessions = load_old_sessions()  # This would load from wherever old data was
    # for old_session in old_sessions:
    #     try:
    #         if not dry_run:
    #             await session_manager.create_session(
    #                 user_id=old_session.get("user_id", "anonymous"),
    #                 title=old_session.get("title", "Migrated Session"),
    #                 agent_name=old_session.get("agent_name", "basic_agent"),
    #                 llm_profile=old_session.get("llm_profile", "default"),
    #                 session_id=old_session.get("session_id")
    #             )
    #             # Then save messages
    #             session_data = await session_manager.load_session(
    #                 old_session["user_id"],
    #                 old_session["session_id"]
    #             )
    #             session_data["messages"] = old_session.get("messages", [])
    #             await session_manager.save_session(session_data)
    #         
    #         migrated_count += 1
    #         logger.info("Migrated session %s", old_session["session_id"])
    #     except Exception as e:
    #         error_count += 1
    #         logger.error("Failed to migrate session %s: %s", 
    #                     old_session.get("session_id", "unknown"), e)
    
    logger.info("Migration complete: %d sessions migrated, %d errors", 
                migrated_count, error_count)
    
    if dry_run:
        logger.info("DRY RUN: No changes were made")
    
    return migrated_count, error_count


async def create_sample_sessions(count: int = 3):
    """Create sample sessions for testing purposes.
    
    Args:
        count: Number of sample sessions to create
    """
    storage_path = Path(__file__).parents[1] / "data" / "sessions"
    session_manager = SessionManager(storage_path=str(storage_path))
    
    sample_messages = [
        [
            {"role": "user", "content": "Hello, can you help me?"},
            {"role": "assistant", "content": "Of course! I'm here to help. What do you need assistance with?"},
        ],
        [
            {"role": "user", "content": "What's the weather like?"},
            {"role": "assistant", "content": "I don't have access to real-time weather data, but I can help you find weather information!"},
            {"role": "user", "content": "How can I check it myself?"},
            {"role": "assistant", "content": "You can check weather websites like weather.com or your local weather service."},
        ],
        [
            {"role": "user", "content": "Tell me a joke"},
            {"role": "assistant", "content": "Why don't scientists trust atoms? Because they make up everything!"},
        ],
    ]
    
    titles = [
        "Getting Started",
        "Weather Discussion",
        "Quick Chat",
    ]
    
    for i in range(min(count, len(titles))):
        try:
            session = await session_manager.create_session(
                user_id="demo_user",
                title=titles[i],
                agent_name="basic_agent",
                llm_profile="default"
            )
            
            # Add messages
            session["messages"] = sample_messages[i]
            await session_manager.save_session(session)
            
            logger.info("Created sample session: %s", titles[i])
        except Exception as e:
            logger.error("Failed to create sample session: %s", e)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Migrate sessions to persistent storage"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be migrated without actually doing it"
    )
    parser.add_argument(
        "--create-samples",
        type=int,
        metavar="N",
        help="Create N sample sessions for testing (default: 3)"
    )
    parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Set logging level (default: INFO)"
    )
    
    args = parser.parse_args()
    
    # Setup logging
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )
    
    # Run migration or create samples
    if args.create_samples is not None:
        count = args.create_samples if args.create_samples > 0 else 3
        asyncio.run(create_sample_sessions(count))
    else:
        asyncio.run(migrate_sessions(dry_run=args.dry_run))


if __name__ == "__main__":
    main()
