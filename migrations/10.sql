ALTER TABLE users ADD COLUMN is_bot INTEGER NOT NULL DEFAULT 0;
ALTER TABLE users ADD COLUMN owner_id TEXT REFERENCES users (id) ON DELETE CASCADE;
CREATE TABLE bot_tokens (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    bot_id TEXT NOT NULL,
    token_hash TEXT UNIQUE NOT NULL,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (bot_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE INDEX idx_bot_tokens_bot_id ON bot_tokens (bot_id);
CREATE INDEX idx_users_owner_id ON users (owner_id);
