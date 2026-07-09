-- Migration v6: Add nonce field to messages
PRAGMA foreign_keys=OFF;

ALTER TABLE messages ADD COLUMN nonce TEXT;

PRAGMA foreign_keys=ON;
