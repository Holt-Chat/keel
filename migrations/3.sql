-- Migration v3: Fix files table UNIQUE constraint to allow same hash for different file_types
-- Disable foreign key constraints during migration
PRAGMA foreign_keys=OFF;

-- Backup existing data
CREATE TABLE files_backup AS SELECT * FROM files;

-- Drop the old table
DROP TABLE files;

-- Create new files table with composite UNIQUE constraint on (hash, file_type)
CREATE TABLE files (
    id TEXT PRIMARY KEY,
    filename TEXT,
    hash TEXT NOT NULL,
    size INTEGER NOT NULL,
    mimetype TEXT,
    file_type TEXT NOT NULL CHECK (file_type IN ('attachment', 'pfp')),
    UNIQUE (hash, file_type)
);

-- Restore data to new table
INSERT INTO files (id, filename, hash, size, mimetype, file_type)
SELECT id, filename, hash, size, mimetype, file_type FROM files_backup;

-- Clean up backup table
DROP TABLE files_backup;

-- Recreate indexes
CREATE INDEX IF NOT EXISTS idx_files_file_type ON files (file_type);

-- Re-enable foreign key constraints
PRAGMA foreign_keys=ON;
