CREATE TABLE IF NOT EXISTS embed_assets (
    id TEXT PRIMARY KEY,
    file_id TEXT NOT NULL,
    channel_id TEXT NOT NULL,
    uploader_id TEXT NOT NULL,
    message_id TEXT,
    key_id TEXT,
    iv TEXT,
    encrypted INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    FOREIGN KEY (file_id) REFERENCES files (id) ON DELETE CASCADE,
    FOREIGN KEY (channel_id) REFERENCES channels (id) ON DELETE CASCADE,
    FOREIGN KEY (uploader_id) REFERENCES users (id) ON DELETE CASCADE,
    FOREIGN KEY (message_id) REFERENCES messages (id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_embed_assets_file ON embed_assets(file_id);
CREATE INDEX IF NOT EXISTS idx_embed_assets_message ON embed_assets(message_id);
CREATE INDEX IF NOT EXISTS idx_embed_assets_created ON embed_assets(created_at);
