# HW3 Requirements Status (Spec vs Current Scaffold)

This document tracks major requirements described in `hw3/spec.txt` and whether
the current code under `hw3/` implements them.

Legend:
- ✅ Implemented (core behavior present)
- ⚠️ Partial (present but missing important parts / UX)
- ❌ Not implemented

## Account System (global rules)

| Requirement (from spec) | Status | Notes / Where |
|---|---:|---|
| Developer and Player accounts are separate | ✅ | Separate DB tables `DevUser` and `PlayerUser` in `hw3/server/db_server.py` |
| Register with username + password; prevent duplicates | ✅ | Returns `username_exists` on conflict in DB server |
| Login verifies existence + password correctness | ✅ | Returns `bad_credentials` on mismatch |
| Avoid duplicate logins (one active session per account) | ✅ | Servers reject second login with `already_online` |

## Developer Platform (D1–D3)

| Use case / requirement | Status | Notes / Where |
|---|---:|---|
| Provide a `developer/template/` skeleton + one-command project scaffold script | ✅ | `hw3/developer/template/` + `hw3/developer/create_game_template.py` |
| D1: Create a new game entry (metadata) | ✅ | Auto-created during upload (`game_upload_init` with blank `gameId`) |
| D1: Upload game package to server | ✅ | Chunked upload: `game_upload_init/chunk/finish` |
| D1: Enforce “dev can only manage own games” | ✅ | Ownership checks (`not_owner`) |
| D1: Validate uploaded package follows a unified spec | ✅ | Requires `manifest.json`, validates entrypoints exist |
| D2: Update an existing game by adding a new version | ✅ | Implemented as uploading a new `version` for existing `gameId` |
| D2: Show/track “current version” and versions list in Dev UI | ✅ | Dev client lists versions and shows latest version in “my games” |
| D3: Delist game so it disappears from public store | ✅ | `game_delist` + store filters `delisted=0` |
| D3: Delist policy for in-progress rooms/matches | ✅ | Developer server blocks delist if any room for the game is `playing` |

## Player / Lobby / Store (P1–P4)

| Use case / requirement | Status | Notes / Where |
|---|---:|---|
| P1: List available games (store) | ✅ | `store_list_games` + latest version lookup |
| P1: View game detail (name/author/version/description/etc.) | ✅ | Store now includes `developerUsername` (author) in game detail |
| P2: Download latest game version | ✅ | `store_download_init/chunk` with sha256 + size verification client-side |
| P2: Per-player isolated downloads directory | ✅ | `downloads/<username>/<gameId>/<version>/...` |
| P2: Robustness against partial/corrupt downloads | ✅ | Hash + size checks; atomic extract; zip-slip guard |
| P2: “Update” UX (compare local vs server, prompt) | ✅ | Lobby client compares local vs server version and prompts to update |
| P3: Create/join/leave/list rooms | ✅ | Room CRUD + membership events |
| P3: Start match spawns game server and informs players | ✅ | Spawns subprocess, pushes `event: game_info` |
| P3: Auto-launch game client and connect to spawned server | ✅ | Lobby client launches installed package client entrypoint on `game_info` |
| P3: Show “lobby status” (players list + rooms list + games list) | ✅ | Player list shows room/game/status; rooms and games lists available |
| P4: Create/update reviews | ✅ | Review upsert + list stored in DB |
| P4: Only allow reviews if player actually played | ✅ | Lobby enforces eligibility via `MatchLog` before accepting reviews |
| P4: Preserve typed review if server post fails | ✅ | Lobby client saves a local draft per user/game and reuses it on retry |
| P4: Provide “my records” / play history view | ✅ | Lobby client lists recent matches including `gameId` and `version` |

## Uploaded Game Storage / Version Management

| Requirement | Status | Notes / Where |
|---|---:|---|
| Server restart should not lose data | ✅ | SQLite persistence in DB server |
| Uploaded games stored in a unified server-managed area | ✅ | `hw3/server/uploaded_games/` (configurable via env) |
| Uploaded games should be treated as immutable download source | ⚠️ | Stored under server; no explicit integrity lock/ACL beyond server filesystem |
| Player downloads should not be arbitrarily modified | ⚠️ | Client installs under `downloads/`; no enforcement beyond filesystem permissions |
| Lobby can download correct version from uploaded area | ✅ | Lobby reads `zipPath` from DB and streams chunks |
| Lobby can spawn correct version’s game server | ✅ | Lobby reads `extractedPath` from DB and spawns entrypoint |
| Release resources after game ends and return to lobby/room | ✅ | Lobby stops process, resets room, emits `game_ready` (`hw3/server/lobby_server.py`) |

## Menu-driven Interface / Demo constraints

| Requirement | Status | Notes / Where |
|---|---:|---|
| Client is menu-driven (no hidden shell commands during demo) | ✅ | Both clients are menu-driven; game auto-launch is internal |
| Menu depth/UX guidance (keep menus small, clear flow) | ✅ | Lobby + Developer clients use submenus to avoid an overloaded single screen |
| Clear, user-friendly error messages (not just codes) | ⚠️ | Clients now map common error codes to friendly messages; server still returns codes |
| README must explain how to start clients/servers and set host/port | ✅ | `hw3/README.md` includes manual and `make -C hw3` quickstart |
| Provide quickstart automation (Makefile/script/Docker/venv) | ✅ | `hw3/Makefile` + `hw3/scripts/start_all.sh`/`stop_all.sh` |
| Provide a way to clear/reset test data before demo | ✅ | `hw3/scripts/reset_demo.sh` (also `make reset` from `hw3/`) |

## Extra Credit: Plugins (PL1–PL4)

| Requirement | Status | Notes / Where |
|---|---:|---|
| PL1: List available plugins | ❌ | Not present |
| PL2: Install/remove plugin | ❌ | Not present |
| PL3: Installed plugin adds room functionality (e.g., chat UI) | ❌ | Not present |
| PL4: No-plugin players still work normally | ❌ | Not applicable until plugin system exists |

## Game Implementation (grading milestones)

| Requirement | Status | Notes / Where |
|---|---:|---|
| At least one playable 2-player CLI game (end-to-end match) | ✅ | `developer/games/bomb_pass_cli` |
| GUI game support (basic window/UI, no CLI-only control) | ✅ | `developer/games/bomb_pass_gui` (pygame) |
| 3+ player same-match gameplay (multiplayer) | ✅ | Bomb Pass supports up to 3 players |
| Example uploadable HW2 game package included | ✅ | `hw3/developer/games/hw2_tetris_duel/` (pygame client) |
