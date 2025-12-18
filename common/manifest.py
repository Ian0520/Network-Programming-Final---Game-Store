from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


class ManifestError(Exception):
    pass


@dataclass(frozen=True)
class Entrypoint:
    module: str
    argv: List[str]


@dataclass(frozen=True)
class GameManifest:
    gameId: str
    name: str
    version: str
    developer: str
    description: str
    clientType: str  # "cli" | "gui"
    minPlayers: int
    maxPlayers: int
    server: Entrypoint
    client: Entrypoint


def _require(d: Dict[str, Any], key: str) -> Any:
    if key not in d:
        raise ManifestError(f"missing:{key}")
    return d[key]


def parse_manifest(obj: Dict[str, Any]) -> GameManifest:
    game_id = str(_require(obj, "gameId")).strip()
    name = str(_require(obj, "name")).strip()
    version = str(_require(obj, "version")).strip()
    developer = str(_require(obj, "developer")).strip()
    description = str(_require(obj, "description")).strip()
    client_type = str(_require(obj, "clientType")).strip().lower()
    min_players = int(_require(obj, "minPlayers"))
    max_players = int(_require(obj, "maxPlayers"))

    entrypoints = _require(obj, "entrypoints")
    if not isinstance(entrypoints, dict):
        raise ManifestError("bad:entrypoints")
    srv = _require(entrypoints, "server")
    cli = _require(entrypoints, "client")
    if not isinstance(srv, dict) or not isinstance(cli, dict):
        raise ManifestError("bad:entrypoints")

    srv_module = str(_require(srv, "module")).strip()
    srv_argv = list(srv.get("argv") or [])
    cli_module = str(_require(cli, "module")).strip()
    cli_argv = list(cli.get("argv") or [])

    if client_type not in ("cli", "gui"):
        raise ManifestError("bad:clientType")
    if min_players <= 0 or max_players <= 0 or min_players > max_players:
        raise ManifestError("bad:playerRange")
    if not game_id or not name or not version or not developer:
        raise ManifestError("bad:identity")

    return GameManifest(
        gameId=game_id,
        name=name,
        version=version,
        developer=developer,
        description=description,
        clientType=client_type,
        minPlayers=min_players,
        maxPlayers=max_players,
        server=Entrypoint(module=srv_module, argv=srv_argv),
        client=Entrypoint(module=cli_module, argv=cli_argv),
    )


def load_manifest_from_dir(game_dir: Path) -> Tuple[Optional[GameManifest], Optional[str], Optional[dict]]:
    """
    Returns: (manifest, error, raw_obj)
    """
    try:
        manifest_path = game_dir / "manifest.json"
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None, "bad_manifest_json", None
        m = parse_manifest(raw)
        return m, None, raw
    except FileNotFoundError:
        return None, "missing_manifest", None
    except json.JSONDecodeError:
        return None, "bad_manifest_json", None
    except ManifestError as e:
        return None, str(e), None
    except Exception as e:
        return None, f"manifest_error:{e}", None

