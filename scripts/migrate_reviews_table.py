#!/usr/bin/env python3
"""
Migrate reviews table to allow NULL book_id (for story_id support).
"""
import sqlite3
from pathlib import Path

def migrate():
    db_path = Path(__file__).parent.parent / "data" / "writer" / "books.db"
    
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    
    print("Migrating reviews table to allow NULL book_id...")
    
    # 1. Create new table with correct schema
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reviews_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            book_id INTEGER,
            story_id INTEGER,
            target_type TEXT NOT NULL,
            target_id TEXT NOT NULL,
            reviewer TEXT NOT NULL,
            score INTEGER,
            feedback TEXT,
            approved BOOLEAN DEFAULT FALSE,
            status TEXT DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE,
            FOREIGN KEY (story_id) REFERENCES stories(id) ON DELETE CASCADE
        )
    """)
    
    # 2. Copy data from old table
    cur.execute("""
        INSERT INTO reviews_new (id, book_id, story_id, target_type, target_id, reviewer, score, feedback, approved, status, created_at, updated_at)
        SELECT id, book_id, story_id, target_type, target_id, reviewer, score, feedback, approved, status, created_at, updated_at
        FROM reviews
    """)
    
    # 3. Get counts
    cur.execute("SELECT COUNT(*) FROM reviews")
    old_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM reviews_new")
    new_count = cur.fetchone()[0]
    print(f"Copied {new_count} reviews (was {old_count})")
    
    # 4. Drop old table
    cur.execute("DROP TABLE reviews")
    
    # 5. Rename new table
    cur.execute("ALTER TABLE reviews_new RENAME TO reviews")
    
    # 6. Recreate indexes
    cur.execute("CREATE INDEX IF NOT EXISTS idx_reviews_book_target ON reviews(book_id, target_type, target_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_reviews_story_target ON reviews(story_id, target_type, target_id)")
    
    conn.commit()
    print("Migration complete!")
    
    # Verify
    cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='reviews'")
    print("\nNew schema:")
    print(cur.fetchone()[0])
    
    conn.close()

if __name__ == "__main__":
    migrate()
