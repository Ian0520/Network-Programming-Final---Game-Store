# HW3 Game Package Manifest (Draft)

Each uploaded game version is a **zip file** that contains a folder with a required
`manifest.json` at its root.

Note on `gameId`:
- The platform uses `manifest.json`’s `gameId` as the store identifier.
- Keep it stable across versions (changing it creates a new game entry).
- Recommended format: letters/numbers/`_`/`-` only.

## Required fields

```json
{
  "gameId": "connect4",
  "name": "Connect 4",
  "version": "1.0.0",
  "developer": "alice",
  "description": "A classic 2-player game.",

  "clientType": "cli",
  "minPlayers": 2,
  "maxPlayers": 2,

  "entrypoints": {
    "server": {"module": "server_main.py", "argv": ["--port", "{port}", "--token", "{token}"]},
    "client": {"module": "client_main.py", "argv": ["--host", "{host}", "--port", "{port}", "--token", "{token}", "--user", "{userId}"]}
  }
}
```

## Token handshake (recommended)

- Client connects to the game server and sends:
  - `{"type":"HELLO","roomId":..., "userId":..., "token":"..."}`
- Server replies:
  - `{"type":"WELCOME","role":"P1|P2|SPEC", ...}`

Your platform only needs to guarantee: **lobby provides (host,port,token)** and
the game server enforces the token.
