-- Migration v4: Add message signing support
PRAGMA foreign_keys=OFF;

ALTER TABLE messages ADD COLUMN signature TEXT;
ALTER TABLE messages ADD COLUMN signed_timestamp INTEGER;

PRAGMA foreign_keys=ON;
