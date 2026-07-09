CREATE TABLE IF NOT EXISTS component_interactions (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    bot_id TEXT NOT NULL,
    custom_id TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    responded INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY (channel_id) REFERENCES channels (id) ON DELETE CASCADE,
    FOREIGN KEY (message_id) REFERENCES messages (id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_comp_interact_bot ON component_interactions(bot_id, responded);
