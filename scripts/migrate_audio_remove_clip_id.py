#!/usr/bin/env python3
"""
Migration script: Remove clip_id, use (scene_id, audio_version_id) as composite key.

This migration:
1. Adds UNIQUE constraint on (scene_id, audio_version_id) to scene_audio_clips
2. Updates scene_audio_srts to use scene_id+audio_version_id instead of clip_id
3. Updates scene_audio_generations to use scene_id+audio_version_id instead of clip_id
4. Updates reviews with target_type='audio_clip' to store scene_id+audio_version_id in target_id

The clip_id (scene_audio_clips.id) is kept for internal reference but is no longer
exposed in the API. All operations use scene_id + audio_version_id instead.

Run from project root:
    python scripts/migrate_audio_remove_clip_id.py

Options:
    --dry-run: Show what would be done without making changes
    --db-path: Custom database path (default: data/writer/books.db)
"""

import argparse
import sqlite3
import json
import sys
from datetime import datetime
from pathlib import Path


def get_connection(db_path: str) -> sqlite3.Connection:
    """Get database connection with row factory."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def check_current_state(conn: sqlite3.Connection) -> dict:
    """Check current database state."""
    cursor = conn.cursor()
    state = {}
    
    # Check scene_audio_clips
    cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='scene_audio_clips'")
    row = cursor.fetchone()
    state["clips_schema"] = row[0] if row else None
    
    # Check for UNIQUE constraint on (scene_id, audio_version_id)
    cursor.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name='scene_audio_clips'")
    indexes = cursor.fetchall()
    state["clips_indexes"] = [r[0] for r in indexes if r[0]]
    
    # Check if unique constraint exists
    state["has_unique_constraint"] = any(
        "scene_id" in (idx or "") and "audio_version_id" in (idx or "") and "UNIQUE" in (idx or "").upper()
        for idx in state["clips_indexes"]
    )
    
    # Check scene_audio_srts schema
    cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='scene_audio_srts'")
    row = cursor.fetchone()
    state["srts_schema"] = row[0] if row else None
    state["srts_has_audio_version_id"] = "audio_version_id" in (state["srts_schema"] or "")
    
    # Check scene_audio_generations schema
    cursor.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='scene_audio_generations'")
    row = cursor.fetchone()
    state["gens_schema"] = row[0] if row else None
    state["gens_has_audio_version_id"] = "audio_version_id" in (state["gens_schema"] or "")
    
    # Count records
    cursor.execute("SELECT COUNT(*) FROM scene_audio_clips")
    state["clips_count"] = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM scene_audio_srts")
    state["srts_count"] = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM scene_audio_generations")
    state["gens_count"] = cursor.fetchone()[0]
    
    cursor.execute("SELECT COUNT(*) FROM reviews WHERE target_type = 'audio_clip'")
    state["audio_reviews_count"] = cursor.fetchone()[0]
    
    return state


def migrate_srts(conn: sqlite3.Connection, dry_run: bool) -> int:
    """Migrate scene_audio_srts to use audio_version_id instead of clip_id."""
    cursor = conn.cursor()
    
    # Check if already migrated
    cursor.execute("PRAGMA table_info(scene_audio_srts)")
    columns = [row["name"] for row in cursor.fetchall()]
    
    if "audio_version_id" in columns:
        print("  scene_audio_srts already has audio_version_id column")
        return 0
    
    if dry_run:
        print("  Would add audio_version_id column to scene_audio_srts")
        print("  Would populate from clip's audio_version_id")
        return 0
    
    # Add audio_version_id column
    cursor.execute("ALTER TABLE scene_audio_srts ADD COLUMN audio_version_id INTEGER")
    
    # Populate from clips
    cursor.execute("""
        UPDATE scene_audio_srts 
        SET audio_version_id = (
            SELECT audio_version_id FROM scene_audio_clips 
            WHERE scene_audio_clips.id = scene_audio_srts.clip_id
        )
    """)
    updated = cursor.rowcount
    
    print(f"  Added audio_version_id to {updated} SRT records")
    return updated


def migrate_generations(conn: sqlite3.Connection, dry_run: bool) -> int:
    """Migrate scene_audio_generations to use audio_version_id."""
    cursor = conn.cursor()
    
    # Check if already migrated
    cursor.execute("PRAGMA table_info(scene_audio_generations)")
    columns = [row["name"] for row in cursor.fetchall()]
    
    if "audio_version_id" in columns:
        print("  scene_audio_generations already has audio_version_id column")
        return 0
    
    if dry_run:
        print("  Would add audio_version_id column to scene_audio_generations")
        print("  Would populate from clip's audio_version_id")
        return 0
    
    # Add audio_version_id column
    cursor.execute("ALTER TABLE scene_audio_generations ADD COLUMN audio_version_id INTEGER")
    
    # Populate from clips (where clip_id exists)
    cursor.execute("""
        UPDATE scene_audio_generations 
        SET audio_version_id = (
            SELECT audio_version_id FROM scene_audio_clips 
            WHERE scene_audio_clips.id = scene_audio_generations.clip_id
        )
        WHERE clip_id IS NOT NULL
    """)
    updated = cursor.rowcount
    
    print(f"  Added audio_version_id to {updated} generation records")
    return updated


def add_unique_constraint(conn: sqlite3.Connection, dry_run: bool) -> bool:
    """Add UNIQUE constraint on (scene_id, audio_version_id) to scene_audio_clips."""
    cursor = conn.cursor()
    
    # Check for duplicates first
    cursor.execute("""
        SELECT scene_id, audio_version_id, COUNT(*) as cnt 
        FROM scene_audio_clips 
        WHERE audio_version_id IS NOT NULL
        GROUP BY scene_id, audio_version_id 
        HAVING cnt > 1
    """)
    duplicates = cursor.fetchall()
    
    if duplicates:
        print("  ERROR: Found duplicate (scene_id, audio_version_id) combinations:")
        for dup in duplicates:
            print(f"    scene_id={dup['scene_id']}, audio_version_id={dup['audio_version_id']}, count={dup['cnt']}")
        print("  Cannot add UNIQUE constraint. Please resolve duplicates first.")
        return False
    
    # Check if index already exists
    cursor.execute("""
        SELECT name FROM sqlite_master 
        WHERE type='index' AND tbl_name='scene_audio_clips' 
        AND name='idx_clips_scene_version_unique'
    """)
    if cursor.fetchone():
        print("  UNIQUE index already exists")
        return True
    
    if dry_run:
        print("  Would add UNIQUE index on (scene_id, audio_version_id)")
        return True
    
    # Add unique index
    cursor.execute("""
        CREATE UNIQUE INDEX idx_clips_scene_version_unique 
        ON scene_audio_clips(scene_id, audio_version_id)
        WHERE audio_version_id IS NOT NULL
    """)
    
    print("  Added UNIQUE index on (scene_id, audio_version_id)")
    return True


def migrate_reviews(conn: sqlite3.Connection, dry_run: bool) -> int:
    """
    Migrate reviews with target_type='audio_clip'.
    
    Current: target_id contains clip_id (single int) or JSON array [clip_id, ...]
    New: target_id contains JSON with scene_ids and audio_version_id
         Format: {"scene_ids": [1, 2, 3], "audio_version_id": 5}
    """
    cursor = conn.cursor()
    
    # Get all audio_clip reviews
    cursor.execute("""
        SELECT id, target_id FROM reviews WHERE target_type = 'audio_clip'
    """)
    reviews = cursor.fetchall()
    
    if not reviews:
        print("  No audio_clip reviews to migrate")
        return 0
    
    migrated = 0
    errors = 0
    
    for review in reviews:
        review_id = review["id"]
        target_id = review["target_id"]
        
        # Parse current target_id (can be int, JSON array, or already migrated JSON object)
        try:
            parsed = json.loads(str(target_id))
            if isinstance(parsed, dict) and "scene_ids" in parsed:
                # Already migrated
                continue
            elif isinstance(parsed, list):
                clip_ids = parsed
            else:
                clip_ids = [int(parsed)]
        except (json.JSONDecodeError, ValueError):
            # Plain integer
            try:
                clip_ids = [int(target_id)]
            except ValueError:
                print(f"    WARNING: Cannot parse target_id for review {review_id}: {target_id}")
                errors += 1
                continue
        
        # Resolve clip_ids to scene_ids and audio_version_id
        if not clip_ids:
            continue
        
        placeholders = ",".join("?" * len(clip_ids))
        cursor.execute(f"""
            SELECT id, scene_id, audio_version_id 
            FROM scene_audio_clips 
            WHERE id IN ({placeholders})
        """, clip_ids)
        clips = cursor.fetchall()
        
        if not clips:
            print(f"    WARNING: No clips found for review {review_id} with clip_ids {clip_ids}")
            errors += 1
            continue
        
        # All clips should have same audio_version_id
        audio_version_ids = set(c["audio_version_id"] for c in clips if c["audio_version_id"])
        if len(audio_version_ids) > 1:
            print(f"    WARNING: Review {review_id} has clips from multiple audio versions: {audio_version_ids}")
        
        audio_version_id = clips[0]["audio_version_id"]
        scene_ids = [c["scene_id"] for c in clips]
        
        new_target = json.dumps({"scene_ids": scene_ids, "audio_version_id": audio_version_id})
        
        if dry_run:
            print(f"    Review {review_id}: {target_id} -> {new_target}")
        else:
            cursor.execute(
                "UPDATE reviews SET target_id = ? WHERE id = ?",
                (new_target, review_id)
            )
        
        migrated += 1
    
    if dry_run:
        print(f"  Would migrate {migrated} reviews ({errors} errors)")
    else:
        print(f"  Migrated {migrated} reviews ({errors} errors)")
    
    return migrated


def run_migration(db_path: str, dry_run: bool = False):
    """Run the full migration."""
    print(f"\n{'='*60}")
    print(f"Migration: Remove clip_id, use (scene_id, audio_version_id)")
    print(f"Database: {db_path}")
    print(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    print(f"{'='*60}\n")
    
    if not Path(db_path).exists():
        print(f"ERROR: Database not found: {db_path}")
        sys.exit(1)
    
    conn = get_connection(db_path)
    
    try:
        # Check current state
        print("1. Checking current state...")
        state = check_current_state(conn)
        print(f"   Clips: {state['clips_count']}")
        print(f"   SRTs: {state['srts_count']}")
        print(f"   Generations: {state['gens_count']}")
        print(f"   Audio reviews: {state['audio_reviews_count']}")
        print(f"   Has UNIQUE constraint: {state['has_unique_constraint']}")
        print(f"   SRTs have audio_version_id: {state['srts_has_audio_version_id']}")
        print(f"   Generations have audio_version_id: {state['gens_has_audio_version_id']}")
        
        # Step 2: Add UNIQUE constraint
        print("\n2. Adding UNIQUE constraint on (scene_id, audio_version_id)...")
        if not add_unique_constraint(conn, dry_run):
            print("   FAILED - stopping migration")
            sys.exit(1)
        
        # Step 3: Migrate SRTs
        print("\n3. Migrating scene_audio_srts...")
        migrate_srts(conn, dry_run)
        
        # Step 4: Migrate generations
        print("\n4. Migrating scene_audio_generations...")
        migrate_generations(conn, dry_run)
        
        # Step 5: Migrate reviews
        print("\n5. Migrating reviews...")
        migrate_reviews(conn, dry_run)
        
        if not dry_run:
            conn.commit()
            print("\n✅ Migration completed successfully!")
        else:
            print("\n✅ Dry run completed - no changes made")
        
    except Exception as e:
        conn.rollback()
        print(f"\n❌ Migration failed: {e}")
        raise
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Migrate audio tables to remove clip_id dependency")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without making changes")
    parser.add_argument("--db-path", default="data/writer/books.db", help="Database path")
    
    args = parser.parse_args()
    run_migration(args.db_path, args.dry_run)


if __name__ == "__main__":
    main()
