ALTER TABLE messages ADD COLUMN expires_at INTEGER;
CREATE INDEX IF NOT EXISTS idx_messages_expires_at ON messages(expires_at);
