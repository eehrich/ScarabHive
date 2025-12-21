#!/usr/bin/env python3
"""
Migration script: choice_text → choice_from_prev

Migrates all path scene_sequences in the database from the old 
choice_text field to the new choice_from_prev field.

Usage:
    python scripts/migrate_choice_text_to_choice_from_prev.py [--db-path PATH]
    
Arguments:
    --db-path: Path to books.db (default: data/writer/books.db)
    --dry-run: Show changes without applying them
"""

import argparse
import json
import sqlite3
from pathlib import Path


def migrate_database(db_path: str, dry_run: bool = False) -> None:
    """Migrate all paths from choice_text to choice_from_prev."""
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Get all paths
    cursor.execute("SELECT id, book_id, name, scene_sequence FROM paths")
    paths = cursor.fetchall()
    
    if not paths:
        print("No paths found in database")
        return
    
    print(f"Found {len(paths)} paths to migrate")
    print()
    
    migrated_count = 0
    unchanged_count = 0
    
    for path in paths:
        path_id = path["id"]
        book_id = path["book_id"]
        name = path["name"]
        scene_sequence_json = path["scene_sequence"]
        
        if not scene_sequence_json:
            print(f"⚠️  Path {path_id} ({name}) has empty scene_sequence, skipping")
            unchanged_count += 1
            continue
        
        scene_sequence = json.loads(scene_sequence_json)
        
        # Check if migration needed
        needs_migration = False
        for item in scene_sequence:
            if "choice_text" in item:
                needs_migration = True
                break
        
        if not needs_migration:
            print(f"✓ Path {path_id} ({name}): Already migrated")
            unchanged_count += 1
            continue
        
        # Perform migration
        migrated_items = []
        for item in scene_sequence:
            new_item = {"scene_id": item["scene_id"]}
            
            # Rename choice_text → choice_from_prev
            if "choice_text" in item:
                new_item["choice_from_prev"] = item["choice_text"]
            
            # Preserve other fields
            if "condition" in item:
                new_item["condition"] = item["condition"]
            
            migrated_items.append(new_item)
        
        migrated_json = json.dumps(migrated_items)
        
        print(f"→ Path {path_id} (book_id={book_id}, name={name})")
        print(f"  Migrated {sum(1 for i in scene_sequence if 'choice_text' in i)} choice_text fields")
        
        if not dry_run:
            cursor.execute(
                "UPDATE paths SET scene_sequence = ? WHERE id = ?",
                (migrated_json, path_id)
            )
            migrated_count += 1
        else:
            print(f"  [DRY RUN] Would update scene_sequence")
        
        print()
    
    if not dry_run:
        conn.commit()
    
    conn.close()
    
    print("=" * 60)
    print(f"Migration complete!")
    print(f"  Migrated: {migrated_count}")
    print(f"  Unchanged: {unchanged_count}")
    print(f"  Total: {len(paths)}")
    
    if dry_run:
        print()
        print("⚠️  DRY RUN MODE - No changes were made")
        print("   Run without --dry-run to apply changes")


def main():
    parser = argparse.ArgumentParser(
        description="Migrate choice_text to choice_from_prev in writer database"
    )
    parser.add_argument(
        "--db-path",
        type=str,
        default="data/writer/books.db",
        help="Path to books.db (default: data/writer/books.db)"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show changes without applying them"
    )
    
    args = parser.parse_args()
    
    db_path = Path(args.db_path)
    
    if not db_path.exists():
        print(f"❌ Database not found: {db_path}")
        print("   Make sure the path is correct")
        return 1
    
    print(f"Database: {db_path}")
    print(f"Mode: {'DRY RUN' if args.dry_run else 'LIVE MIGRATION'}")
    print()
    
    if not args.dry_run:
        response = input("⚠️  This will modify the database. Continue? [y/N]: ")
        if response.lower() != 'y':
            print("Migration cancelled")
            return 0
        print()
    
    migrate_database(str(db_path), dry_run=args.dry_run)
    return 0


if __name__ == "__main__":
    exit(main())
