CREATE TABLE IF NOT EXISTS message_reactions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    message_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    content TEXT NOT NULL,
    key TEXT,
    iv TEXT,
    signature TEXT,
    signed_timestamp INTEGER,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (message_id) REFERENCES messages (id) ON DELETE CASCADE,
    FOREIGN KEY (channel_id) REFERENCES channels (id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_message_reactions_message_id ON message_reactions(message_id);
CREATE INDEX IF NOT EXISTS idx_message_reactions_channel_id ON message_reactions(channel_id);
CREATE INDEX IF NOT EXISTS idx_message_reactions_message_id_user_id ON message_reactions(message_id, user_id);
UPDATE members SET permissions = permissions | 128 WHERE permissions IS NOT NULL AND permissions & 4 != 0;
UPDATE channels SET permissions = permissions | 128 WHERE type IN (1, 2, 3);
