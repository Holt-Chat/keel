-- Migration v5: Add WebRTC calls support for DMs
PRAGMA foreign_keys=OFF;

CREATE TABLE IF NOT EXISTS calls (
    channel_id TEXT PRIMARY KEY,
    started_by TEXT NOT NULL,
    started_at INTEGER NOT NULL,
    FOREIGN KEY (channel_id) REFERENCES channels (id) ON DELETE CASCADE,
    FOREIGN KEY (started_by) REFERENCES users (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS call_participants (
    channel_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    joined_at INTEGER NOT NULL,
    left_at INTEGER,
    PRIMARY KEY (channel_id, user_id),
    FOREIGN KEY (channel_id) REFERENCES calls (channel_id) ON DELETE CASCADE,
    FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_call_participants_channel ON call_participants(channel_id);
CREATE INDEX IF NOT EXISTS idx_call_participants_user ON call_participants(user_id);

PRAGMA foreign_keys=ON;
