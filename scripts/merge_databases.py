#!/usr/bin/env python3
"""
Merge books_from_production.db (latest content) with books_w_audio.db (audio data)
into books.db.

Strategy:
1. Copy production DB as base (has latest scenes, paths, books)
2. Add missing columns for audio support
3. Import all audio data from audio DB
4. Merge audio-related reviews
"""
import shutil
import sqlite3
from pathlib import Path


def main():
    data_dir = Path(__file__).parent.parent / "data" / "writer"
    
    prod_db = data_dir / "books_from_production.db"
    audio_db = data_dir / "books_w_audio.db"
    target_db = data_dir / "books.db"
    backup_db = data_dir / "books_backup_before_merge.db"
    
    # Validate source files
    if not prod_db.exists():
        raise FileNotFoundError(f"Production DB not found: {prod_db}")
    if not audio_db.exists():
        raise FileNotFoundError(f"Audio DB not found: {audio_db}")
    
    # Backup existing books.db if it exists
    if target_db.exists():
        print(f"Backing up existing books.db to {backup_db}")
        shutil.copy2(target_db, backup_db)
    
    # Step 1: Copy production as base
    print(f"Copying {prod_db} -> {target_db}")
    shutil.copy2(prod_db, target_db)
    
    # Connect to databases
    target = sqlite3.connect(target_db)
    audio = sqlite3.connect(audio_db)
    target.row_factory = sqlite3.Row
    audio.row_factory = sqlite3.Row
    tc = target.cursor()
    ac = audio.cursor()
    
    # Step 2: Add missing columns for audio support
    print("\nAdding missing columns...")
    
    # player_sessions.audio_version_id
    try:
        tc.execute("ALTER TABLE player_sessions ADD COLUMN audio_version_id INTEGER")
        print("  Added player_sessions.audio_version_id")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            print("  player_sessions.audio_version_id already exists")
        else:
            raise
    
    # scene_audio_generations.audio_version_id
    try:
        tc.execute("ALTER TABLE scene_audio_generations ADD COLUMN audio_version_id INTEGER")
        print("  Added scene_audio_generations.audio_version_id")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            print("  scene_audio_generations.audio_version_id already exists")
        else:
            raise
    
    # scene_audio_srts.audio_version_id
    try:
        tc.execute("ALTER TABLE scene_audio_srts ADD COLUMN audio_version_id INTEGER")
        print("  Added scene_audio_srts.audio_version_id")
    except sqlite3.OperationalError as e:
        if "duplicate column" in str(e).lower():
            print("  scene_audio_srts.audio_version_id already exists")
        else:
            raise
    
    target.commit()
    
    # Step 3: Import audio_versions
    print("\nImporting audio_versions...")
    ac.execute("SELECT * FROM audio_versions")
    audio_versions = ac.fetchall()
    for av in audio_versions:
        tc.execute("""
            INSERT OR REPLACE INTO audio_versions 
            (id, book_id, name, language, voice_config, description, is_default, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (av['id'], av['book_id'], av['name'], av['language'], av['voice_config'], 
              av['description'], av['is_default'], av['status'], av['created_at'], av['updated_at']))
    print(f"  Imported {len(audio_versions)} audio versions")
    target.commit()
    
    # Step 4: Import scene_audio_clips
    print("\nImporting scene_audio_clips...")
    ac.execute("SELECT * FROM scene_audio_clips")
    clips = ac.fetchall()
    imported_clips = 0
    skipped_clips = 0
    for clip in clips:
        # Check if scene exists in target
        tc.execute("SELECT id FROM scenes WHERE id = ?", (clip['scene_id'],))
        if tc.fetchone():
            tc.execute("""
                INSERT OR REPLACE INTO scene_audio_clips
                (id, scene_id, audio_version_id, speaker_name, file_path, duration_seconds, 
                 sample_rate, format, metadata, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (clip['id'], clip['scene_id'], clip['audio_version_id'], clip['speaker_name'],
                  clip['file_path'], clip['duration_seconds'], clip['sample_rate'], clip['format'],
                  clip['metadata'], clip['created_at'], clip['updated_at']))
            imported_clips += 1
        else:
            skipped_clips += 1
    print(f"  Imported {imported_clips} clips, skipped {skipped_clips} (scene not found)")
    target.commit()
    
    # Step 5: Import scene_audio_generations
    print("\nImporting scene_audio_generations...")
    ac.execute("SELECT * FROM scene_audio_generations")
    gens = ac.fetchall()
    imported_gens = 0
    skipped_gens = 0
    for gen in gens:
        tc.execute("SELECT id FROM scenes WHERE id = ?", (gen['scene_id'],))
        if tc.fetchone():
            tc.execute("""
                INSERT OR REPLACE INTO scene_audio_generations
                (id, scene_id, clip_id, workflow_params, success, error_message, created_at, audio_version_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (gen['id'], gen['scene_id'], gen['clip_id'], gen['workflow_params'],
                  gen['success'], gen['error_message'], gen['created_at'], gen['audio_version_id']))
            imported_gens += 1
        else:
            skipped_gens += 1
    print(f"  Imported {imported_gens} generations, skipped {skipped_gens} (scene not found)")
    target.commit()
    
    # Step 6: Import scene_audio_srts
    print("\nImporting scene_audio_srts...")
    ac.execute("SELECT * FROM scene_audio_srts")
    srts = ac.fetchall()
    imported_srts = 0
    skipped_srts = 0
    for srt in srts:
        tc.execute("SELECT id FROM scenes WHERE id = ?", (srt['scene_id'],))
        if tc.fetchone():
            tc.execute("""
                INSERT OR REPLACE INTO scene_audio_srts
                (id, clip_id, scene_id, srt_content, word_count, created_at, audio_version_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (srt['id'], srt['clip_id'], srt['scene_id'], srt['srt_content'],
                  srt['word_count'], srt['created_at'], srt['audio_version_id'], srt['updated_at']))
            imported_srts += 1
        else:
            skipped_srts += 1
    print(f"  Imported {imported_srts} SRTs, skipped {skipped_srts} (scene not found)")
    target.commit()
    
    # Step 7: Merge audio-related reviews
    print("\nMerging audio-related reviews...")
    ac.execute("SELECT * FROM reviews WHERE target_type LIKE '%audio%'")
    audio_reviews = ac.fetchall()
    imported_reviews = 0
    skipped_reviews = 0
    for rev in audio_reviews:
        # Check if review already exists
        tc.execute("SELECT id FROM reviews WHERE id = ?", (rev['id'],))
        if not tc.fetchone():
            tc.execute("""
                INSERT INTO reviews
                (id, book_id, target_type, target_id, reviewer, score, feedback, approved, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (rev['id'], rev['book_id'], rev['target_type'], rev['target_id'], rev['reviewer'],
                  rev['score'], rev['feedback'], rev['approved'], rev['status'], rev['created_at'], rev['updated_at']))
            imported_reviews += 1
        else:
            skipped_reviews += 1
    print(f"  Imported {imported_reviews} audio reviews, skipped {skipped_reviews} (already exist)")
    target.commit()
    
    # Step 8: Update sqlite_sequence for auto-increment
    print("\nUpdating sqlite_sequence...")
    tables_to_update = ['audio_versions', 'scene_audio_clips', 'scene_audio_generations', 'scene_audio_srts', 'reviews']
    for table in tables_to_update:
        tc.execute(f"SELECT MAX(id) FROM {table}")
        max_id = tc.fetchone()[0] or 0
        tc.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = ?", (max_id, table))
        if tc.rowcount == 0:
            tc.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, max_id))
        print(f"  {table}: max_id = {max_id}")
    target.commit()
    
    # Final summary
    print("\n" + "="*60)
    print("MERGE COMPLETE")
    print("="*60)
    
    tc.execute("SELECT COUNT(*) FROM audio_versions")
    print(f"  audio_versions: {tc.fetchone()[0]}")
    tc.execute("SELECT COUNT(*) FROM scene_audio_clips")
    print(f"  scene_audio_clips: {tc.fetchone()[0]}")
    tc.execute("SELECT COUNT(*) FROM scene_audio_generations")
    print(f"  scene_audio_generations: {tc.fetchone()[0]}")
    tc.execute("SELECT COUNT(*) FROM scene_audio_srts")
    print(f"  scene_audio_srts: {tc.fetchone()[0]}")
    tc.execute("SELECT COUNT(*) FROM reviews WHERE target_type LIKE '%audio%'")
    print(f"  audio reviews: {tc.fetchone()[0]}")
    tc.execute("SELECT COUNT(*) FROM reviews")
    print(f"  total reviews: {tc.fetchone()[0]}")
    
    audio.close()
    target.close()
    
    print(f"\nMerged database: {target_db}")


if __name__ == "__main__":
    main()
