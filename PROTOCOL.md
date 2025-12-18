# HW3 Wire Protocol (Draft)

All TCP messages use **length-prefixed framing**:

`[uint32_be length][payload bytes]`, with `0 < length <= 64KiB`.

Payloads are UTF-8 JSON objects.

## Common conventions

- Requests: `{"type": "<command>", "data": {...}}`
- Responses: `{"ok": true, ...}` or `{"ok": false, "error": "reason", ...}`
- Server-push events: `{"type": "event", "name": "<event_name>", "data": {...}}`

## D1/D2/D3 (Developer Server)

### Auth

- `dev_register`: `{username, password}`
- `dev_login`: `{username, password}`
- `dev_logout`: `{}`

### Game management

- `game_list_mine`: `{}` → `{ok:true, games:[{gameId, name, delisted, latestVersion, clientType, minPlayers, maxPlayers, ...}]}`
- `game_list_versions`: `{gameId}` → `{ok:true, versions:[{version, uploadedAt, clientType, minPlayers, maxPlayers, ...}]}`
- `game_delist`: `{gameId, delisted: true|false}`

### Upload a version (chunked)

1. `game_upload_init`: `{gameId, name, description, clientType, minPlayers, maxPlayers, version, changelog, fileName, sizeBytes, sha256}`
   - If `gameId` does not exist yet, the server creates the game entry (metadata) and then accepts the upload as the first version.
   - If `gameId` exists, the server treats the upload as a new version for that game.
2. server → response: `{ok:true, uploadId, gameId, created}`
3. client streams `game_upload_chunk`: `{uploadId, seq, dataB64}`
4. `game_upload_finish`: `{uploadId}`

Errors should be explicit (`bad_manifest`, `not_owner`, `version_exists`, `hash_mismatch`, …).

## P1/P2/P3/P4 (Lobby/Store Server)

### Auth

- `player_register`: `{username, password}`
- `player_login`: `{username, password}`
- `player_logout`: `{}`
- `player_list`: `{}` → `{ok:true, players:[{playerId, username, roomId|null, roomStatus|null, gameId|null, version|null}]}`

### Store browsing (P1)

- `store_list_games`: `{}`
- `store_game_detail`: `{gameId}`

Notes:
- Game objects may include `developerUsername` (the author name shown to players).

### Download latest version (P2)

1. `store_download_init`: `{gameId}` (or `{gameId, version}` if you support pinning)
2. server → response: `{ok:true, downloadId, version, fileName, sizeBytes, sha256}`
3. client pulls chunks via `store_download_chunk`: `{downloadId, offset, limit}`
4. server → response: `{ok:true, offset, dataB64, done}`

### Rooms and starting a match (P3)

- `room_create`: `{gameId}` (server pins to latest active version)
- `room_join`: `{roomId}`
- `room_leave`: `{}`
- `room_list`: `{}`
- `room_detail`: `{roomId}` → `{ok:true, room:{...}}`
- `room_start`: `{roomId}` (host-only)

When a match starts, lobby pushes:

- `event: game_info`: `{roomId, gameId, version, host, port, token}`

### Match result callback (from Game Server → Lobby)

- `post_result`: `{roomId, gameId, version, startedAt, endedAt, winner, reason, results}`

### Reviews (P4)

- `review_create_or_update`: `{gameId, rating1to5, comment}`
- `review_list`: `{gameId}`
- `match_list_mine`: `{}` → `{ok:true, logs:[{id, roomId, gameFk, gameVersionFk, startedAt, endedAt, reason, winnerPlayerId, resultsJson}]}`
  - Logs also include `gameId` and `version` for display.
