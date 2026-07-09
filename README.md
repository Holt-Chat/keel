<div align="center">
  <img src="https://raw.githubusercontent.com/Holt-Chat/shore/main/media/holt.png" width="96" alt="Holt Chat logo">

  # Keel

  **The backend for [Holt Chat](https://github.com/Holt-Chat)**, a self-hostable, end-to-end encrypted chat platform.

  [![License: MIT](https://img.shields.io/badge/license-MIT-blue?style=flat-square)](LICENSE.md)
  [![FastAPI](https://img.shields.io/badge/backend-FastAPI-009688?style=flat-square)](https://fastapi.tiangolo.com)
  [![E2EE](https://img.shields.io/badge/encryption-E2EE-6f42c1?style=flat-square)](#)
</div>

---

Keel is a FastAPI app backed by SQLite. It can serve the [Shore](https://github.com/Holt-Chat/shore) frontend directly, or run headless behind your own reverse proxy.

## Features

| | |
|---|---|
| **Messaging** | End-to-end encrypted DMs and group channels (RSA + AES-GCM); unencrypted broadcast channels also supported |
| **Channels** | Members, roles/permissions, bans, pins, interactions |
| **Bots** | Token auth and a full bot API, see [holt-sdk](https://github.com/Holt-Chat/holt-sdk) for client libraries |
| **Integrations** | Webhooks and OAuth (SSO) apps, so users can register apps that log in through Holt |
| **Calls** | WebRTC (mesh topology), configurable STUN/TURN |
| **Realtime** | Server-Sent Events stream, presence, typing indicators |
| **Notifications** | Web Push |

## Running it

**Docker (recommended)**

```sh
docker compose up -d
```

This starts Keel plus an nginx sidecar. Edit `config.toml` first, in particular `server.port`, `frontend.frontend_directory`, and anything under `[instance]` / `[calls]`.

**Directly**

```sh
pip install -r requirements.txt
python main.py
```

Add `--dev` for a dev environment. Default settings live in `default_config.toml` (don't edit it, it's overwritten on update); your instance's actual config is `config.toml`, generated on first run.

## Configuration

All server settings are in `config.toml`. Notable sections:

- `[frontend]` points `frontend_directory` at a checkout of Shore to have Keel serve it, or set `hosted=false` to run the API standalone
- `[instance]` optional signup password, auto-invite channel, and creation/deletion locks
- `[calls]` enable/disable calls, participant cap, STUN/TURN servers
- `[max_members]`, `[max_file_size]`, `[messages]` instance limits

## CLI

`cli.py` provides basic admin commands (list users, delete a channel/user):

```sh
python cli.py list-users
python cli.py delete-channel <channel_id>
python cli.py delete-user <username>
```

Under Docker: `docker compose run --rm keel python cli.py list-users`.

## License

MIT, see [LICENSE.md](LICENSE.md).
