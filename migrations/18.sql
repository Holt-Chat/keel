ALTER TABLE interaction_history RENAME TO interaction_history_old;
CREATE TABLE interaction_history (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    id TEXT UNIQUE NOT NULL,
    channel_id TEXT NOT NULL,
    user_username TEXT NOT NULL,
    user_display TEXT,
    command TEXT NOT NULL,
    bot_username TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    FOREIGN KEY (channel_id) REFERENCES channels (id) ON DELETE CASCADE
);
INSERT INTO interaction_history (id, channel_id, user_username, user_display, command, bot_username, timestamp)
SELECT id, channel_id, user_username, user_display, command, bot_username, timestamp FROM interaction_history_old ORDER BY timestamp ASC;
DROP TABLE interaction_history_old;
CREATE INDEX IF NOT EXISTS idx_interaction_history_channel ON interaction_history(channel_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_interaction_history_channel_seq ON interaction_history(channel_id, seq);
ALTER TABLE members ADD COLUMN interaction_seq INTEGER NOT NULL DEFAULT 0;
UPDATE members SET interaction_seq=(SELECT COALESCE(MAX(seq), 0) FROM interaction_history WHERE channel_id=members.channel_id);
