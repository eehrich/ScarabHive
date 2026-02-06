#!/usr/bin/env python
"""
Migration script: Add audio_versions support to existing database.

This script:
1. Creates the audio_versions table if it doesn't exist
2. Adds audio_version_id column to scene_audio_clips
3. Creates a "DE-Clemens" version for each book with existing clips
4. Updates existing clips to reference the new version
5. Removes the old language column from scene_audio_clips
6. Removes language column from scene_audio_srts
"""

import sqlite3
import sys
from pathlib import Path


def migrate(db_path: str, dry_run: bool = False) -> None:
    """Run the migration."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    print(f"Migrating database: {db_path}")
    print(f"Dry run: {dry_run}")
    print()
    
    # Step 1: Check current state
    cursor.execute("PRAGMA table_info(scene_audio_clips)")
    columns = {col[1] for col in cursor.fetchall()}
    has_language = "language" in columns
    has_audio_version_id = "audio_version_id" in columns
    
    print(f"Current state:")
    print(f"  - has 'language' column: {has_language}")
    print(f"  - has 'audio_version_id' column: {has_audio_version_id}")
    
    if has_audio_version_id and not has_language:
        print("\nDatabase already migrated!")
        return
    
    # Step 2: Create audio_versions table if not exists
    print("\nStep 1: Creating audio_versions table...")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS audio_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            language TEXT NOT NULL,
            voice_config JSON NOT NULL DEFAULT '{}',
            description TEXT,
            is_default BOOLEAN DEFAULT FALSE,
            status TEXT DEFAULT 'active' CHECK(status IN ('active', 'archived')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE,
            UNIQUE(book_id, name)
        )
    """)
    print("  audio_versions table ready")
    
    # Step 3: Find books with existing clips
    cursor.execute("""
        SELECT DISTINCT s.book_id, b.title
        FROM scene_audio_clips c
        JOIN scenes s ON c.scene_id = s.id
        JOIN books b ON s.book_id = b.id
    """)
    books_with_clips = cursor.fetchall()
    
    print(f"\nStep 2: Creating audio versions for {len(books_with_clips)} books...")
    
    version_ids = {}
    for book in books_with_clips:
        book_id = book["book_id"]
        book_title = book["title"]
        
        # Check if version already exists
        cursor.execute(
            "SELECT id FROM audio_versions WHERE book_id = ? AND name = ?",
            (book_id, "DE-Clemens")
        )
        existing = cursor.fetchone()
        
        if existing:
            version_ids[book_id] = existing["id"]
            print(f"  Book {book_id} ({book_title}): version already exists (ID: {existing['id']})")
        else:
            cursor.execute("""
                INSERT INTO audio_versions 
                (book_id, name, language, voice_config, description, is_default, status)
                VALUES (?, 'DE-Clemens', 'de', '{"narrator": "Clemens", "default": "Clemens"}', 
                        'German version with Clemens as narrator', TRUE, 'active')
            """, (book_id,))
            version_ids[book_id] = cursor.lastrowid
            print(f"  Book {book_id} ({book_title}): created version (ID: {version_ids[book_id]})")
    
    # Step 4: Add audio_version_id column if not exists
    if not has_audio_version_id:
        print("\nStep 3: Adding audio_version_id column to scene_audio_clips...")
        cursor.execute("ALTER TABLE scene_audio_clips ADD COLUMN audio_version_id INTEGER")
        print("  Column added")
    
    # Step 5: Update existing clips with version_id
    print("\nStep 4: Updating existing clips with version IDs...")
    for book_id, version_id in version_ids.items():
        cursor.execute("""
            UPDATE scene_audio_clips 
            SET audio_version_id = ?
            WHERE scene_id IN (SELECT id FROM scenes WHERE book_id = ?)
        """, (version_id, book_id))
        print(f"  Book {book_id}: updated {cursor.rowcount} clips")
    
    # Step 6: Remove language column (SQLite doesn't support DROP COLUMN before 3.35)
    # We need to recreate the table
    if has_language:
        print("\nStep 5: Removing language column from scene_audio_clips...")
        
        # Create new table without language
        cursor.execute("""
            CREATE TABLE scene_audio_clips_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scene_id INTEGER NOT NULL,
                audio_version_id INTEGER,
                speaker_name TEXT,
                file_path TEXT NOT NULL,
                duration_seconds REAL,
                sample_rate INTEGER DEFAULT 44100,
                format TEXT DEFAULT 'flac',
                status TEXT DEFAULT 'draft' CHECK(status IN ('draft', 'approved', 'rejected')),
                metadata JSON,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (scene_id) REFERENCES scenes(id) ON DELETE CASCADE,
                FOREIGN KEY (audio_version_id) REFERENCES audio_versions(id) ON DELETE SET NULL
            )
        """)
        
        # Copy data
        cursor.execute("""
            INSERT INTO scene_audio_clips_new 
            (id, scene_id, audio_version_id, speaker_name, file_path, duration_seconds, 
             sample_rate, format, status, metadata, created_at, updated_at)
            SELECT id, scene_id, audio_version_id, speaker_name, file_path, duration_seconds,
                   sample_rate, format, status, metadata, created_at, updated_at
            FROM scene_audio_clips
        """)
        
        # Drop old table
        cursor.execute("DROP TABLE scene_audio_clips")
        
        # Rename new table
        cursor.execute("ALTER TABLE scene_audio_clips_new RENAME TO scene_audio_clips")
        
        print("  Language column removed")
    
    # Step 7: Remove language from scene_audio_srts
    cursor.execute("PRAGMA table_info(scene_audio_srts)")
    srt_columns = {col[1] for col in cursor.fetchall()}
    
    if "language" in srt_columns:
        print("\nStep 6: Removing language column from scene_audio_srts...")
        
        cursor.execute("""
            CREATE TABLE scene_audio_srts_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                clip_id INTEGER NOT NULL,
                scene_id INTEGER NOT NULL,
                srt_content TEXT NOT NULL,
                word_count INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (clip_id) REFERENCES scene_audio_clips(id) ON DELETE CASCADE,
                FOREIGN KEY (scene_id) REFERENCES scenes(id) ON DELETE CASCADE
            )
        """)
        
        cursor.execute("""
            INSERT INTO scene_audio_srts_new 
            (id, clip_id, scene_id, srt_content, word_count, created_at)
            SELECT id, clip_id, scene_id, srt_content, word_count, created_at
            FROM scene_audio_srts
        """)
        
        cursor.execute("DROP TABLE scene_audio_srts")
        cursor.execute("ALTER TABLE scene_audio_srts_new RENAME TO scene_audio_srts")
        
        print("  Language column removed from scene_audio_srts")
    
    # Step 8: Create indexes
    print("\nStep 7: Creating indexes...")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audio_versions_book ON audio_versions(book_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_audio_clips_version ON scene_audio_clips(audio_version_id)")
    print("  Indexes created")
    
    if dry_run:
        print("\n[DRY RUN] Rolling back changes...")
        conn.rollback()
    else:
        print("\nCommitting changes...")
        conn.commit()
    
    # Verify
    print("\nVerification:")
    cursor.execute("SELECT COUNT(*) FROM audio_versions")
    print(f"  Audio versions: {cursor.fetchone()[0]}")
    
    cursor.execute("SELECT COUNT(*) FROM scene_audio_clips WHERE audio_version_id IS NOT NULL")
    print(f"  Clips with version: {cursor.fetchone()[0]}")
    
    cursor.execute("SELECT COUNT(*) FROM scene_audio_clips WHERE audio_version_id IS NULL")
    print(f"  Clips without version: {cursor.fetchone()[0]}")
    
    conn.close()
    print("\nMigration complete!")


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Migrate audio_versions")
    parser.add_argument("db_path", nargs="?", default="data/writer/books.db", help="Path to database")
    parser.add_argument("--dry-run", action="store_true", help="Don't commit changes")
    args = parser.parse_args()
    migrate(args.db_path, args.dry_run)
