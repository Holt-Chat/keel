CREATE TABLE IF NOT EXISTS interaction_history (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    user_username TEXT NOT NULL,
    user_display TEXT,
    command TEXT NOT NULL,
    bot_username TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    FOREIGN KEY (channel_id) REFERENCES channels (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_interaction_history_channel ON interaction_history(channel_id, timestamp);
