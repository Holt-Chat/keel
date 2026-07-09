CREATE TABLE webhooks (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL,
    name TEXT NOT NULL,
    pfp TEXT,
    token TEXT NOT NULL,
    created_by TEXT,
    created_at INTEGER NOT NULL,
    last_used_at INTEGER,
    FOREIGN KEY (channel_id) REFERENCES channels (id) ON DELETE CASCADE,
    FOREIGN KEY (created_by) REFERENCES users (id) ON DELETE SET NULL
);
ALTER TABLE messages ADD COLUMN webhook_id TEXT;
ALTER TABLE messages ADD COLUMN webhook_name TEXT;
ALTER TABLE messages ADD COLUMN webhook_pfp TEXT;
INSERT OR IGNORE INTO users (id, username, display_name, pfp, passkey, public_key, created_at) VALUES ('0', '__holt_webhooks_system_account_do_not_use__', 'System', NULL, 'system', 'system', 0);
CREATE INDEX idx_webhooks_channel_id ON webhooks (channel_id);
CREATE UNIQUE INDEX idx_webhooks_token ON webhooks (token);
