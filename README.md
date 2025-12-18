# Final Game Store + Lobby (Python)

This folder is a runnable scaffold for HW3 (Game Store + Lobby). Everything is
menu-driven once the servers/clients are started.

Docs:
- `PROTOCOL.md`: wire messages
- `MANIFEST.md`: uploaded game package format

## Server

Edit `config.json` as needed (see `config.example.json` for reference).

Start all servers:

`make start`

Stop servers:

`make stop`

Check status / logs:

- `make status`
- `make logs` (or see `./.run/logs/`)

### Connection settings (IP / Port)

Edit `config.json`:

- `db.bindHost` / `db.port` / `db.sqlitePath`
- `developerServer.bindHost` / `developerServer.port`
- `lobbyServer.bindHost` / `lobbyServer.port`
- `gameHostPublic` (IMPORTANT: the host/IP that clients use to connect to spawned game servers)

Client connection defaults come from:

- `developerServer.host` / `developerServer.port`
- `lobbyServer.host` / `lobbyServer.port`

Optional overrides (env vars still work):

- `NP_HW3_DB_HOST`, `NP_HW3_DB_PORT`, `NP_HW3_DB_PATH`
- `NP_HW3_DEV_HOST`, `NP_HW3_DEV_PORT`
- `NP_HW3_LOBBY_HOST`, `NP_HW3_LOBBY_PORT`
- `NP_HW3_GAME_HOST_PUB`
- `NP_HW3_CONFIG` (use a different config file path)

## Player

Run the Lobby Client (use 1 terminal per player):

`PYTHONPATH=.. python3 -m hw3.player.lobby_client`

### GUI dependency (pygame)

If you want to play GUI games (e.g. `bomb_pass_gui`), install `pygame` on the player machine:

`python3 -m pip install pygame`

### Connection settings (IP / Port)

Defaults come from `config.json` (`lobbyServer.host` / `lobbyServer.port`). You can override with:

- Env vars: `NP_HW3_LOBBY_HOST`, `NP_HW3_LOBBY_PORT`
- Args: `PYTHONPATH=.. python3 -m hw3.player.lobby_client <LOBBY_HOST> <LOBBY_PORT>`

Downloads:

- Default: `hw3/player/downloads/<username>/...`
- Override root: `NP_HW3_DOWNLOADS_ROOT`

## Developer

Run the Developer Client:

`PYTHONPATH=.. python3 -m hw3.developer.developer_client`

### Connection settings (IP / Port)

Defaults come from `config.json` (`developerServer.host` / `developerServer.port`). You can override with:

- Env vars: `NP_HW3_DEV_HOST`, `NP_HW3_DEV_PORT`
- Args: `PYTHONPATH=.. python3 -m hw3.developer.developer_client <DEV_HOST> <DEV_PORT>`

### Upload / Update flow

- Put your local game folders under `developer/games/` (must contain `manifest.json`)
- In the menu: `Updates/Uploads` → `Upload game folder (create/update by manifest gameId)`
- To publish a new version: bump `version` in `manifest.json` and upload again (same `gameId`)

## Reset demo data (destructive)

`make reset`
