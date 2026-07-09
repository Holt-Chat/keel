-- Migration v2: Add seq column to session table and change primary key, fix expires_at timestamps
-- Backup existing data
CREATE TABLE session_backup AS SELECT * FROM session;

-- Drop the old table
DROP TABLE session;

-- Create new session table with seq column
CREATE TABLE session (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    user TEXT NOT NULL,
    token_hash TEXT UNIQUE NOT NULL,
    id TEXT UNIQUE NOT NULL,
    device TEXT,
    browser TEXT,
    logged_in_at INTEGER NOT NULL,
    next_challenge INTEGER,
    FOREIGN KEY (user) REFERENCES users (id) ON DELETE CASCADE
);

-- Restore data to new table
INSERT INTO session (user, token_hash, id, device, browser, logged_in_at, next_challenge)
SELECT user, token_hash, id, device, browser, logged_in_at, next_challenge FROM session_backup;

-- Clean up backup table
DROP TABLE session_backup;

-- Recreate index
CREATE INDEX IF NOT EXISTS idx_session_user ON session (user);

-- Fix expires_at values in channels_keys_info (multiply by 1000)
UPDATE channels_keys_info SET expires_at = expires_at * 1000;