#!/usr/bin/env python3
"""
HW3 Lobby + Store Server

Responsibilities (minimal scaffold):
  - Player register/login/logout (separate from Developer accounts)
  - Store: list games, show details, download latest game version (chunked)
  - Rooms: create/join/leave/list, start match (spawn uploaded game server)
  - Receive game result callback (post_result) and persist MatchLog

Transport:
  - TCP + length-prefixed JSON frames (hw3/common/framing.py)
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import secrets
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from hw3.common.config import get_int, get_str, load_config, resolve_path, section
from hw3.common.framing import FramingError, recv_frame, send_frame
from hw3.common.manifest import load_manifest_from_dir
from hw3.server.db_rpc import db_call


_CFG_LOBBY = section("lobbyServer")
_CFG_ROOT = load_config()
HOST = (
    os.environ.get("NP_HW3_LOBBY_HOST")
    or get_str(_CFG_LOBBY, "bindHost")
    or get_str(_CFG_LOBBY, "host")
    or "0.0.0.0"
)
PORT = int(os.environ.get("NP_HW3_LOBBY_PORT") or get_int(_CFG_LOBBY, "port") or 10103)

GAME_HOST_PUB = (
    os.environ.get("NP_HW3_GAME_HOST_PUB")
    or str(_CFG_ROOT.get("gameHostPublic") or "").strip()
    or get_str(_CFG_LOBBY, "host")
    or "127.0.0.1"
)

_default_internal = get_str(_CFG_LOBBY, "internalHost")
if not _default_internal:
    bind = get_str(_CFG_LOBBY, "bindHost") or ""
    if bind and bind not in ("0.0.0.0", "::"):
        _default_internal = bind
    else:
        _default_internal = get_str(_CFG_LOBBY, "host") or "127.0.0.1"
LOBBY_HOST_INTERNAL = os.environ.get("NP_HW3_LOBBY_HOST_INTERNAL") or _default_internal

GAME_PORT_MIN = int(os.environ.get("NP_HW3_GAME_PORT_MIN") or get_int(_CFG_LOBBY, "gamePortMin") or 10000)
GAME_PORT_MAX = int(os.environ.get("NP_HW3_GAME_PORT_MAX") or get_int(_CFG_LOBBY, "gamePortMax") or 20000)

_default_run_root = Path(__file__).resolve().parents[1] / ".run"
RUN_ROOT = Path(os.environ.get("NP_HW3_RUN_ROOT") or get_str(_CFG_LOBBY, "runRoot") or str(_default_run_root))
if not RUN_ROOT.is_absolute():
    RUN_ROOT = resolve_path(str(RUN_ROOT))


# Keep a comfortable margin under 64KiB after base64 + JSON overhead.
MAX_B64_CHUNK = 32 * 1024  # raw bytes per chunk (base64 expands)


def _ok(**kwargs):
    return {"ok": True, **kwargs}


def _err(msg: str, **kwargs):
    return {"ok": False, "error": msg, **kwargs}


async def _send(writer: asyncio.StreamWriter, obj: dict):
    await send_frame(writer, json.dumps(obj, separators=(",", ":")).encode("utf-8"))


async def _push_event(writer: asyncio.StreamWriter, name: str, **data):
    await _send(writer, {"type": "event", "name": name, "data": data})


def _select_free_port() -> int:
    for p in range(GAME_PORT_MIN, GAME_PORT_MAX + 1):
        with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("0.0.0.0", p))
                return p
            except OSError:
                continue
    raise RuntimeError("no_free_port")


def _fmt_argv(argv: List[str], mapping: dict) -> List[str]:
    out: List[str] = []
    for a in argv:
        try:
            out.append(str(a).format(**mapping))
        except KeyError as e:
            raise ValueError(f"bad_argv_template:{e}") from e
    return out


@dataclass
class PlayerSession:
    player_id: int
    username: str
    writer: asyncio.StreamWriter
    room_id: Optional[int] = None


@dataclass
class RoomLive:
    room_id: int
    host_player_id: int
    players: List[int] = field(default_factory=list)
    game_id: str = ""
    version: str = ""
    game_db_id: int = 0
    game_version_id: int = 0
    status: str = "waiting"  # "waiting" | "playing"
    token: Optional[str] = None
    game_port: Optional[int] = None
    game_proc: Optional[asyncio.subprocess.Process] = None


@dataclass
class DownloadSession:
    download_id: str
    zip_path: Path
    file_name: str
    size_bytes: int
    sha256: str
    game_id: str
    version: str


SESSIONS_BY_WRITER: Dict[asyncio.StreamWriter, PlayerSession] = {}
SESSIONS_BY_PLAYER_ID: Dict[int, PlayerSession] = {}

ROOMS: Dict[int, RoomLive] = {}
DOWNLOADS: Dict[str, DownloadSession] = {}


def _require_login(writer: asyncio.StreamWriter) -> Optional[PlayerSession]:
    return SESSIONS_BY_WRITER.get(writer)


async def _push_event_to_player(player_id: int, name: str, **data):
    sess = SESSIONS_BY_PLAYER_ID.get(player_id)
    if not sess:
        return
    with contextlib.suppress(Exception):
        await _push_event(sess.writer, name, **data)


async def _ensure_room_live(room_id: int) -> Optional[RoomLive]:
    live = ROOMS.get(room_id)
    if live:
        return live
    r = await db_call({"collection": "Room", "action": "get", "data": {"roomId": room_id}})
    if r.get("status") != "OK":
        return None
    d = r.get("data") or {}
    live = RoomLive(
        room_id=int(d["id"]),
        host_player_id=int(d["hostPlayerId"]),
        players=[int(x) for x in (d.get("players") or [])],
        game_id=str(d.get("gameId") or ""),
        version=str(d.get("version") or ""),
        game_db_id=int(d.get("gameDbId") or 0),
        game_version_id=int(d.get("gameVersionId") or 0),
        status=str(d.get("status") or "waiting"),
    )
    ROOMS[room_id] = live
    return live


# -------------------------
# Auth
# -------------------------
async def handle_player_register(writer: asyncio.StreamWriter, data: dict):
    resp = await db_call({"collection": "PlayerUser", "action": "register", "data": data})
    if resp.get("status") != "OK":
        await _send(writer, _err(resp.get("error", "register_failed")))
        return
    await _send(writer, _ok(**(resp.get("data") or {})))


async def handle_player_login(writer: asyncio.StreamWriter, data: dict):
    resp = await db_call({"collection": "PlayerUser", "action": "login", "data": data})
    if resp.get("status") != "OK":
        await _send(writer, _err(resp.get("error", "login_failed")))
        return
    info = resp.get("data") or {}
    pid = int(info.get("playerId") or 0)
    username = str(info.get("username") or "")
    if pid <= 0:
        await _send(writer, _err("bad_db_user"))
        return
    if pid in SESSIONS_BY_PLAYER_ID:
        await _send(writer, _err("already_online"))
        return
    sess = PlayerSession(player_id=pid, username=username, writer=writer)
    SESSIONS_BY_WRITER[writer] = sess
    SESSIONS_BY_PLAYER_ID[pid] = sess
    await _send(writer, _ok(playerId=pid, username=username))


async def handle_player_logout(writer: asyncio.StreamWriter):
    await _cleanup_connection(writer, notify_client=True)

async def handle_player_list(writer: asyncio.StreamWriter):
    """
    Minimal "lobby status" support: list currently online players.
    """
    players = []
    for pid, sess in SESSIONS_BY_PLAYER_ID.items():
        room = None
        if sess.room_id is not None:
            room = await _ensure_room_live(sess.room_id)
        players.append(
            {
                "playerId": int(pid),
                "username": sess.username,
                "roomId": sess.room_id,
                "roomStatus": (room.status if room else None),
                "gameId": (room.game_id if room else None),
                "version": (room.version if room else None),
            }
        )
    players.sort(key=lambda x: x["playerId"])
    await _send(writer, _ok(players=players))


# -------------------------
# Store browsing (P1)
# -------------------------
async def handle_store_list_games(writer: asyncio.StreamWriter):
    r = await db_call({"collection": "Game", "action": "list_public", "data": {}})
    if r.get("status") != "OK":
        await _send(writer, _err(r.get("error", "list_failed")))
        return
    games = r.get("games") or []
    out = []
    for g in games:
        gid = g.get("gameId")
        dev_id = int(g.get("developerId") or 0)
        dev_username: Optional[str] = None
        if dev_id > 0:
            dev = await db_call({"collection": "DevUser", "action": "get_by_id", "data": {"developerId": dev_id}})
            if dev.get("status") == "OK":
                dev_username = (dev.get("data") or {}).get("username")
        latest = await db_call({"collection": "GameVersion", "action": "latest_for_gameId", "data": {"gameId": gid}})
        if latest.get("status") == "OK":
            v = latest.get("data") or {}
            g2 = dict(g)
            g2["developerUsername"] = dev_username
            g2["latestVersion"] = v.get("version")
            g2["clientType"] = v.get("clientType")
            g2["minPlayers"] = v.get("minPlayers")
            g2["maxPlayers"] = v.get("maxPlayers")
            out.append(g2)
        else:
            g2 = dict(g)
            g2["developerUsername"] = dev_username
            g2["latestVersion"] = None
            out.append(g2)
    await _send(writer, _ok(games=out))


async def handle_store_game_detail(writer: asyncio.StreamWriter, data: dict):
    game_id = str((data.get("gameId") or "")).strip()
    if not game_id:
        await _send(writer, _err("missing_fields"))
        return
    g = await db_call({"collection": "Game", "action": "get_by_gameId", "data": {"gameId": game_id}})
    if g.get("status") != "OK":
        await _send(writer, _err(g.get("error", "not_found")))
        return
    game = g.get("data") or {}
    dev_id = int(game.get("developerId") or 0)
    dev_username: Optional[str] = None
    if dev_id > 0:
        dev = await db_call({"collection": "DevUser", "action": "get_by_id", "data": {"developerId": dev_id}})
        if dev.get("status") == "OK":
            dev_username = (dev.get("data") or {}).get("username")
    if dev_username:
        game = dict(game)
        game["developerUsername"] = dev_username
    latest = await db_call({"collection": "GameVersion", "action": "latest_for_gameId", "data": {"gameId": game_id}})
    reviews = await db_call({"collection": "Review", "action": "list_for_gameId", "data": {"gameId": game_id}})
    await _send(
        writer,
        _ok(
            game=game,
            latestVersion=(latest.get("data") if latest.get("status") == "OK" else None),
            reviews=(reviews.get("reviews") if reviews.get("status") == "OK" else []),
        ),
    )


# -------------------------
# Download (P2)
# -------------------------
async def handle_store_download_init(writer: asyncio.StreamWriter, data: dict):
    game_id = str((data.get("gameId") or "")).strip()
    if not game_id:
        await _send(writer, _err("missing_fields"))
        return
    req_version = str((data.get("version") or "")).strip()
    if req_version:
        latest = await db_call(
            {"collection": "GameVersion", "action": "get_for_gameId_version", "data": {"gameId": game_id, "version": req_version}}
        )
    else:
        latest = await db_call({"collection": "GameVersion", "action": "latest_for_gameId", "data": {"gameId": game_id}})
    if latest.get("status") != "OK":
        await _send(writer, _err(latest.get("error", "no_version")))
        return
    v = latest.get("data") or {}
    zip_path = Path(str(v.get("zipPath") or ""))
    if not zip_path.exists():
        await _send(writer, _err("missing_zip_on_server"))
        return
    download_id = secrets.token_hex(16)
    sess = DownloadSession(
        download_id=download_id,
        zip_path=zip_path,
        file_name=str(v.get("fileName") or zip_path.name),
        size_bytes=int(v.get("sizeBytes") or zip_path.stat().st_size),
        sha256=str(v.get("sha256") or ""),
        game_id=game_id,
        version=str(v.get("version") or ""),
    )
    DOWNLOADS[download_id] = sess
    await _send(
        writer,
        _ok(
            downloadId=download_id,
            gameId=sess.game_id,
            version=sess.version,
            fileName=sess.file_name,
            sizeBytes=sess.size_bytes,
            sha256=sess.sha256,
        ),
    )


async def handle_store_download_chunk(writer: asyncio.StreamWriter, data: dict):
    download_id = str((data.get("downloadId") or "")).strip()
    offset = int(data.get("offset") or 0)
    limit = int(data.get("limit") or MAX_B64_CHUNK)
    if not download_id or offset < 0:
        await _send(writer, _err("bad_request"))
        return
    sess = DOWNLOADS.get(download_id)
    if not sess:
        await _send(writer, _err("no_such_download"))
        return
    limit = max(1, min(limit, MAX_B64_CHUNK))

    try:
        with sess.zip_path.open("rb") as f:
            f.seek(offset)
            chunk = f.read(limit)
    except Exception:
        await _send(writer, _err("read_failed"))
        return

    done = (offset + len(chunk)) >= sess.size_bytes
    await _send(
        writer,
        _ok(
            downloadId=download_id,
            offset=offset,
            dataB64=base64.b64encode(chunk).decode("ascii"),
            done=done,
        ),
    )

    if done:
        DOWNLOADS.pop(download_id, None)


# -------------------------
# Rooms (P3)
# -------------------------
async def handle_room_list(writer: asyncio.StreamWriter):
    r = await db_call({"collection": "Room", "action": "list", "data": {}})
    if r.get("status") != "OK":
        await _send(writer, _err(r.get("error", "list_failed")))
        return
    await _send(writer, _ok(rooms=r.get("rooms") or []))

async def handle_room_detail(writer: asyncio.StreamWriter, data: dict):
    room_id = int(data.get("roomId") or 0)
    if room_id <= 0:
        await _send(writer, _err("bad_room_id"))
        return
    r = await db_call({"collection": "Room", "action": "get", "data": {"roomId": room_id}})
    if r.get("status") != "OK":
        await _send(writer, _err(r.get("error", "no_such_room")))
        return
    await _send(writer, _ok(room=(r.get("data") or {})))


async def handle_room_create(writer: asyncio.StreamWriter, data: dict):
    sess = _require_login(writer)
    if not sess:
        await _send(writer, _err("not_logged_in"))
        return
    if sess.room_id is not None:
        await _send(writer, _err("already_in_room", roomId=sess.room_id))
        return
    game_id = str((data.get("gameId") or "")).strip()
    if not game_id:
        await _send(writer, _err("missing_fields"))
        return

    g = await db_call({"collection": "Game", "action": "get_by_gameId", "data": {"gameId": game_id}})
    if g.get("status") != "OK":
        await _send(writer, _err(g.get("error", "not_found")))
        return
    gdata = g.get("data") or {}
    if int(gdata.get("delisted") or 0) != 0:
        await _send(writer, _err("game_delisted"))
        return

    latest = await db_call({"collection": "GameVersion", "action": "latest_for_gameId", "data": {"gameId": game_id}})
    if latest.get("status") != "OK":
        await _send(writer, _err(latest.get("error", "no_version")))
        return
    v = latest.get("data") or {}
    room = await db_call(
        {
            "collection": "Room",
            "action": "create",
            "data": {
                "hostPlayerId": sess.player_id,
                "gameDbId": int(gdata.get("id") or 0),
                "gameVersionId": int(v.get("id") or 0),
            },
        }
    )
    if room.get("status") != "OK":
        await _send(writer, _err(room.get("error", "room_create_failed")))
        return
    rid = int((room.get("data") or {}).get("roomId") or 0)
    live = await _ensure_room_live(rid)
    if live:
        sess.room_id = rid
    await _send(writer, _ok(roomId=rid, gameId=game_id, version=str(v.get("version") or "")))


async def handle_room_join(writer: asyncio.StreamWriter, data: dict):
    sess = _require_login(writer)
    if not sess:
        await _send(writer, _err("not_logged_in"))
        return
    if sess.room_id is not None:
        await _send(writer, _err("already_in_room", roomId=sess.room_id))
        return
    room_id = int(data.get("roomId") or 0)
    if room_id <= 0:
        await _send(writer, _err("bad_room_id"))
        return
    live = await _ensure_room_live(room_id)
    if not live:
        await _send(writer, _err("no_such_room"))
        return
    if live.status == "playing":
        await _send(writer, _err("room_playing"))
        return

    # Refresh from DB for authoritative player list
    r = await db_call({"collection": "Room", "action": "get", "data": {"roomId": room_id}})
    if r.get("status") != "OK":
        await _send(writer, _err(r.get("error", "no_such_room")))
        return
    rd = r.get("data") or {}
    players = [int(x) for x in (rd.get("players") or [])]
    max_players = int(rd.get("maxPlayers") or 2)
    if sess.player_id in players:
        sess.room_id = room_id
        await _send(writer, _ok(roomId=room_id, joined=True))
        return
    if len(players) >= max_players:
        await _send(writer, _err("room_full"))
        return

    add = await db_call({"collection": "Room", "action": "add_member", "data": {"roomId": room_id, "playerId": sess.player_id}})
    if add.get("status") != "OK":
        await _send(writer, _err(add.get("error", "join_failed")))
        return

    live.players = sorted(set(players + [sess.player_id]))
    sess.room_id = room_id
    await _send(writer, _ok(roomId=room_id, joined=True))
    # Notify room
    for u in live.players:
        if u != sess.player_id:
            await _push_event_to_player(u, "player_joined", roomId=room_id, playerId=sess.player_id)


async def _handle_room_leave(sess: PlayerSession, *, force: bool = False):
    rid = sess.room_id
    if rid is None:
        return
    live = await _ensure_room_live(rid)
    # If in playing state, disallow leave unless force (disconnect).
    if not force and live and live.status == "playing":
        return

    _ = await db_call({"collection": "Room", "action": "remove_member", "data": {"roomId": rid, "playerId": sess.player_id}})

    if live:
        if sess.player_id in live.players:
            live.players = [u for u in live.players if u != sess.player_id]

        # host reassignment
        if live.players and live.host_player_id == sess.player_id:
            new_host = live.players[0]
            live.host_player_id = new_host
            _ = await db_call({"collection": "Room", "action": "set_host", "data": {"roomId": rid, "hostPlayerId": new_host}})
            for u in live.players:
                await _push_event_to_player(u, "host_changed", roomId=rid, hostPlayerId=new_host)

        for u in live.players:
            await _push_event_to_player(u, "player_left", roomId=rid, playerId=sess.player_id)

        if not live.players:
            _ = await db_call({"collection": "Room", "action": "delete_if_empty", "data": {"roomId": rid}})
            ROOMS.pop(rid, None)

    sess.room_id = None


async def handle_room_leave(writer: asyncio.StreamWriter):
    sess = _require_login(writer)
    if not sess:
        await _send(writer, _err("not_logged_in"))
        return
    live = await _ensure_room_live(sess.room_id) if sess.room_id else None
    if live and live.status == "playing":
        await _send(writer, _err("room_playing"))
        return
    await _handle_room_leave(sess, force=False)
    await _send(writer, _ok(left=True))


async def _cleanup_connection(writer: asyncio.StreamWriter, *, notify_client: bool):
    sess = SESSIONS_BY_WRITER.pop(writer, None)
    if not sess:
        if notify_client:
            with contextlib.suppress(Exception):
                await _send(writer, _ok(loggedOut=True))
        return

    SESSIONS_BY_PLAYER_ID.pop(sess.player_id, None)

    # On disconnect, always leave room. If a match is running, end it.
    if sess.room_id is not None:
        live = await _ensure_room_live(sess.room_id)
        if live and live.status == "playing":
            await _finish_match(sess.room_id, result={"roomId": sess.room_id, "reason": "disconnect", "results": []})
        await _handle_room_leave(sess, force=True)

    if notify_client:
        with contextlib.suppress(Exception):
            await _send(writer, _ok(loggedOut=True))


async def _finish_match(room_id: int, *, result: Optional[dict] = None):
    live = await _ensure_room_live(room_id)
    if not live:
        return
    already_finished = live.status != "playing" and live.game_proc is None and live.token is None
    # Avoid double-finishing when both process watcher and post_result trigger.
    # If a late post_result arrives, still persist the match log but don't re-broadcast.
    if already_finished and not result:
        return

    # Best-effort stop game process
    if live.game_proc and live.game_proc.returncode is None:
        with contextlib.suppress(Exception):
            live.game_proc.terminate()
        with contextlib.suppress(Exception):
            await asyncio.wait_for(live.game_proc.wait(), timeout=2.0)
        if live.game_proc.returncode is None:
            with contextlib.suppress(Exception):
                live.game_proc.kill()

    # Persist match log if provided (even if already finished).
    if result:
        try:
            started_at = int(result.get("startedAt") or int(time.time()))
            ended_at = int(result.get("endedAt") or int(time.time()))
            reason = str(result.get("reason") or "finished")
            winner = result.get("winner")
            winner_pid = int(winner) if winner is not None else None
            # Store a consistent envelope that at least records participants so we can
            # later enforce "must have played before reviewing" (P4).
            results_json = json.dumps(
                {
                    "players": [{"userId": int(pid)} for pid in list(live.players)],
                    "results": result.get("results") or [],
                },
                ensure_ascii=False,
            )
            _ = await db_call(
                {
                    "collection": "MatchLog",
                    "action": "create",
                    "data": {
                        "roomId": room_id,
                        "gameDbId": live.game_db_id,
                        "gameVersionId": live.game_version_id,
                        "startedAt": started_at,
                        "endedAt": ended_at,
                        "reason": reason,
                        "winnerPlayerId": winner_pid,
                        "resultsJson": results_json,
                    },
                }
            )
        except Exception:
            pass

    if already_finished:
        return

    live.status = "waiting"
    live.token = None
    live.game_port = None
    live.game_proc = None
    _ = await db_call({"collection": "Room", "action": "set_status", "data": {"roomId": room_id, "status": "waiting"}})

    for u in list(live.players):
        await _push_event_to_player(u, "game_ready", roomId=room_id, result=(result or {}))


async def _watch_game(room_id: int, proc: asyncio.subprocess.Process):
    with contextlib.suppress(Exception):
        await proc.wait()
    live = await _ensure_room_live(room_id)
    if not live or live.status != "playing":
        return
    # Give the game process a brief window to post_result before we auto-finish.
    await asyncio.sleep(0.5)
    live = await _ensure_room_live(room_id)
    if not live or live.status != "playing":
        return
    await _finish_match(room_id, result={"roomId": room_id, "reason": "process_exit", "results": []})


async def handle_room_start(writer: asyncio.StreamWriter, data: dict):
    sess = _require_login(writer)
    if not sess:
        await _send(writer, _err("not_logged_in"))
        return
    rid = int(data.get("roomId") or (sess.room_id or 0))
    if rid <= 0:
        await _send(writer, _err("bad_room_id"))
        return
    live = await _ensure_room_live(rid)
    if not live:
        await _send(writer, _err("no_such_room"))
        return
    if sess.player_id != live.host_player_id:
        await _send(writer, _err("not_host"))
        return
    if live.status == "playing":
        # If the game process already exited but the room hasn't been reset yet,
        # auto-finish it so the host can start a new match immediately.
        if live.game_proc is None:
            await _finish_match(rid, result={"roomId": rid, "reason": "stale_state", "results": []})
        elif live.game_proc.returncode is not None:
            await _finish_match(rid, result={"roomId": rid, "reason": "process_exit", "results": []})
        else:
            await _send(writer, _err("already_playing"))
            return

    # Validate minPlayers
    rd = await db_call({"collection": "Room", "action": "get", "data": {"roomId": rid}})
    if rd.get("status") != "OK":
        await _send(writer, _err(rd.get("error", "no_such_room")))
        return
    room_row = rd.get("data") or {}
    players = [int(x) for x in (room_row.get("players") or [])]
    min_players = int(room_row.get("minPlayers") or 2)
    if len(players) < min_players:
        await _send(writer, _err("need_more_players", minPlayers=min_players))
        return

    gv = await db_call({"collection": "GameVersion", "action": "get_by_id", "data": {"gameVersionId": live.game_version_id}})
    if gv.get("status") != "OK":
        await _send(writer, _err(gv.get("error", "bad_game_version")))
        return
    v = gv.get("data") or {}
    extracted_path = Path(str(v.get("extractedPath") or ""))
    manifest, err, _raw = load_manifest_from_dir(extracted_path)
    if not manifest:
        await _send(writer, _err(err or "bad_manifest"))
        return

    try:
        port = _select_free_port()
    except Exception as e:
        await _send(writer, _err(f"no_port:{e}"))
        return
    token = secrets.token_hex(16)

    mapping = {
        "host": GAME_HOST_PUB,
        "port": port,
        "token": token,
        "roomId": rid,
        "gameId": live.game_id,
        "version": live.version,
        "lobbyHost": LOBBY_HOST_INTERNAL,
        "lobbyPort": PORT,
    }

    try:
        argv = _fmt_argv(manifest.server.argv, mapping)
    except Exception as e:
        await _send(writer, _err(f"bad_manifest_argv:{e}"))
        return

    env = os.environ.copy()
    env.update(
        {
            "HW3_LOBBY_HOST": mapping["lobbyHost"],
            "HW3_LOBBY_PORT": str(mapping["lobbyPort"]),
            "HW3_ROOM_ID": str(rid),
            "HW3_TOKEN": token,
            "HW3_GAME_ID": live.game_id,
            "HW3_VERSION": live.version,
            "HW3_EXPECTED_PLAYERS": str(len(players)),
            "PYTHONUNBUFFERED": "1",
        }
    )

    try:
        log_dir = RUN_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"game_room_{rid}.log"
        log_f = log_path.open("ab", buffering=0)
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-u",
            str((extracted_path / manifest.server.module)),
            *argv,
            cwd=str(extracted_path),
            stdout=log_f,
            stderr=log_f,
            env=env,
        )
    except Exception as e:
        await _send(writer, _err(f"spawn_failed:{e}"))
        return

    live.status = "playing"
    live.players = players
    live.token = token
    live.game_port = port
    live.game_proc = proc
    _ = await db_call({"collection": "Room", "action": "set_status", "data": {"roomId": rid, "status": "playing"}})

    # push game_info to all room members
    for u in players:
        await _push_event_to_player(
            u,
            "game_info",
            roomId=rid,
            gameId=live.game_id,
            version=live.version,
            host=GAME_HOST_PUB,
            port=port,
            token=token,
        )

    asyncio.create_task(_watch_game(rid, proc))
    await _send(writer, _ok(started=True, port=port))


# -------------------------
# Game -> Lobby callback
# -------------------------
async def handle_post_result(writer: asyncio.StreamWriter, data: dict):
    try:
        rid = int(data.get("roomId") or 0)
    except Exception:
        rid = 0
    if rid <= 0:
        await _send(writer, _err("bad_room_id"))
        return
    await _finish_match(rid, result=data)
    await _send(writer, _ok(posted=True))


# -------------------------
# Reviews (P4) – minimal plumbing
# -------------------------
async def handle_review_upsert(writer: asyncio.StreamWriter, data: dict):
    sess = _require_login(writer)
    if not sess:
        await _send(writer, _err("not_logged_in"))
        return
    game_id = str((data.get("gameId") or "")).strip()
    if not game_id:
        await _send(writer, _err("missing_fields"))
        return
    # Enforce spec P4: player must have actually played the game before reviewing.
    played = await db_call(
        {"collection": "MatchLog", "action": "has_player_played", "data": {"gameId": game_id, "playerId": sess.player_id}}
    )
    if played.get("status") != "OK":
        await _send(writer, _err(played.get("error", "eligibility_check_failed")))
        return
    if not bool((played.get("data") or {}).get("played")):
        await _send(writer, _err("not_played"))
        return
    payload = dict(data or {})
    payload["playerId"] = sess.player_id
    r = await db_call({"collection": "Review", "action": "upsert", "data": payload})
    if r.get("status") != "OK":
        await _send(writer, _err(r.get("error", "review_failed")))
        return
    await _send(writer, _ok())

async def handle_match_list_mine(writer: asyncio.StreamWriter):
    sess = _require_login(writer)
    if not sess:
        await _send(writer, _err("not_logged_in"))
        return
    r = await db_call({"collection": "MatchLog", "action": "list_by_player", "data": {"playerId": sess.player_id}})
    if r.get("status") != "OK":
        await _send(writer, _err(r.get("error", "list_failed")))
        return
    await _send(writer, _ok(logs=r.get("logs") or []))


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        await _send(writer, _ok(hello="hw3_lobby_ready"))
        while True:
            frame = await recv_frame(reader)
            if frame is None:
                break
            msg = json.loads(frame.decode("utf-8"))
            typ = msg.get("type")
            data = msg.get("data", {}) or {}

            if typ == "player_register":
                await handle_player_register(writer, data)
            elif typ == "player_login":
                await handle_player_login(writer, data)
            elif typ == "player_logout":
                await handle_player_logout(writer)
            elif typ == "player_list":
                await handle_player_list(writer)

            elif typ == "store_list_games":
                await handle_store_list_games(writer)
            elif typ == "store_game_detail":
                await handle_store_game_detail(writer, data)
            elif typ == "store_download_init":
                await handle_store_download_init(writer, data)
            elif typ == "store_download_chunk":
                await handle_store_download_chunk(writer, data)

            elif typ == "room_list":
                await handle_room_list(writer)
            elif typ == "room_detail":
                await handle_room_detail(writer, data)
            elif typ == "room_create":
                await handle_room_create(writer, data)
            elif typ == "room_join":
                await handle_room_join(writer, data)
            elif typ == "room_leave":
                await handle_room_leave(writer)
            elif typ == "room_start":
                await handle_room_start(writer, data)

            elif typ == "post_result":
                await handle_post_result(writer, data)

            elif typ == "review_create_or_update":
                await handle_review_upsert(writer, data)
            elif typ == "match_list_mine":
                await handle_match_list_mine(writer)
            else:
                await _send(writer, _err("unknown_type"))

    except FramingError:
        pass
    except Exception:
        with contextlib.suppress(Exception):
            await _send(writer, _err("server_exception"))
    finally:
        await _cleanup_connection(writer, notify_client=False)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def main():
    server = await asyncio.start_server(handle, HOST, PORT)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
