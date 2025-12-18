#!/usr/bin/env python3
"""
HW3 Developer Server

Responsibilities:
  - Developer register/login/logout (separate from Player accounts)
  - Manage games: create, list mine, delist
  - Upload game versions (zip) with a validated manifest.json

Transport:
  - TCP + length-prefixed JSON frames (hw3/common/framing.py)
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

from hw3.common.framing import FramingError, recv_frame, send_frame
from hw3.common.manifest import load_manifest_from_dir
from hw3.common.config import get_int, get_str, resolve_path, section
from hw3.server.db_rpc import db_call


_CFG_DEV = section("developerServer")
HOST = (
    os.environ.get("NP_HW3_DEV_HOST")
    or get_str(_CFG_DEV, "bindHost")
    or get_str(_CFG_DEV, "host")
    or "0.0.0.0"
)
PORT = int(os.environ.get("NP_HW3_DEV_PORT") or get_int(_CFG_DEV, "port") or 10102)

_default_upload_root = Path(__file__).resolve().parent / "uploaded_games"
_default_tmp_root = Path(__file__).resolve().parent / "storage" / "tmp_uploads"
UPLOAD_ROOT = Path(os.environ.get("NP_HW3_UPLOAD_ROOT") or get_str(_CFG_DEV, "uploadRoot") or str(_default_upload_root))
TMP_ROOT = Path(os.environ.get("NP_HW3_TMP_ROOT") or get_str(_CFG_DEV, "tmpRoot") or str(_default_tmp_root))
if not UPLOAD_ROOT.is_absolute():
    UPLOAD_ROOT = resolve_path(str(UPLOAD_ROOT))
if not TMP_ROOT.is_absolute():
    TMP_ROOT = resolve_path(str(TMP_ROOT))


def _ok(**kwargs):
    return {"ok": True, **kwargs}


def _err(msg: str, **kwargs):
    return {"ok": False, "error": msg, **kwargs}


async def _send(writer: asyncio.StreamWriter, obj: dict):
    await send_frame(writer, json.dumps(obj, separators=(",", ":")).encode("utf-8"))

_SLUG_RE = re.compile(r"[^a-z0-9]+")
_GAME_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _slugify(name: str) -> str:
    s = (name or "").strip().lower()
    s = _SLUG_RE.sub("_", s).strip("_")
    return s[:32] if s else ""


async def _reserve_unique_game_id(base: str, developer_id: int) -> str:
    """
    Generate a unique gameId by probing the DB. Keeps the ID stable-ish and readable
    while avoiding collisions.
    """
    base = base or f"game_{developer_id}"
    # Try base first, then add random suffixes.
    for i in range(20):
        gid = base if i == 0 else f"{base}_{secrets.token_hex(2)}"
        r = await db_call({"collection": "Game", "action": "get_by_gameId", "data": {"gameId": gid}})
        if r.get("status") != "OK":
            return gid
    return f"{base}_{secrets.token_hex(6)}"


def _safe_extract_zip(zip_path: Path, dst_dir: Path) -> None:
    """
    Extract zip into dst_dir while preventing Zip Slip.
    """
    dst_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            # Reject absolute paths and parent traversal.
            member_path = Path(member.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise ValueError("unsafe_zip_entry")
        zf.extractall(dst_dir)


@dataclass
class DevSession:
    developer_id: int
    username: str


@dataclass
class UploadSession:
    upload_id: str
    developer_id: int
    game_id: str
    version: str
    file_name: str
    expected_size: int
    expected_sha256: str
    temp_path: Path
    auto_created_game: bool = False
    received: int = 0
    next_seq: int = 0
    hasher: "hashlib._Hash" = field(default_factory=hashlib.sha256)


SESSIONS: Dict[asyncio.StreamWriter, DevSession] = {}
ONLINE_DEVS: Dict[int, asyncio.StreamWriter] = {}
UPLOADS: Dict[str, UploadSession] = {}


def _require_login(writer: asyncio.StreamWriter) -> Optional[DevSession]:
    return SESSIONS.get(writer)


async def handle_register(writer: asyncio.StreamWriter, data: dict):
    resp = await db_call({"collection": "DevUser", "action": "register", "data": data})
    if resp.get("status") != "OK":
        await _send(writer, _err(resp.get("error", "register_failed")))
        return
    await _send(writer, _ok(**(resp.get("data") or {})))


async def handle_login(writer: asyncio.StreamWriter, data: dict):
    resp = await db_call({"collection": "DevUser", "action": "login", "data": data})
    if resp.get("status") != "OK":
        await _send(writer, _err(resp.get("error", "login_failed")))
        return
    info = resp.get("data") or {}
    dev_id = int(info.get("developerId") or 0)
    username = str(info.get("username") or "")
    if dev_id <= 0:
        await _send(writer, _err("bad_db_user"))
        return
    if dev_id in ONLINE_DEVS:
        await _send(writer, _err("already_online"))
        return
    SESSIONS[writer] = DevSession(developer_id=dev_id, username=username)
    ONLINE_DEVS[dev_id] = writer
    await _send(writer, _ok(developerId=dev_id, username=username))


async def handle_logout(writer: asyncio.StreamWriter):
    sess = SESSIONS.pop(writer, None)
    if sess:
        ONLINE_DEVS.pop(sess.developer_id, None)
    await _send(writer, _ok(loggedOut=True))


async def handle_game_list_mine(writer: asyncio.StreamWriter):
    sess = _require_login(writer)
    if not sess:
        await _send(writer, _err("not_logged_in"))
        return
    resp = await db_call({"collection": "Game", "action": "list_by_dev", "data": {"developerId": sess.developer_id}})
    if resp.get("status") != "OK":
        await _send(writer, _err(resp.get("error", "list_failed")))
        return
    games = resp.get("games") or []
    out = []
    for g in games:
        gid = g.get("gameId")
        latest = await db_call({"collection": "GameVersion", "action": "latest_for_gameId", "data": {"gameId": gid}})
        g2 = dict(g)
        if latest.get("status") == "OK":
            v = latest.get("data") or {}
            g2["latestVersion"] = v.get("version")
            g2["clientType"] = v.get("clientType")
            g2["minPlayers"] = v.get("minPlayers")
            g2["maxPlayers"] = v.get("maxPlayers")
        else:
            g2["latestVersion"] = None
        out.append(g2)
    await _send(writer, _ok(games=out))


async def handle_game_delist(writer: asyncio.StreamWriter, data: dict):
    sess = _require_login(writer)
    if not sess:
        await _send(writer, _err("not_logged_in"))
        return
    game_id = str((data.get("gameId") or "")).strip()
    delisted = bool(data.get("delisted"))
    if delisted and game_id:
        active = await db_call({"collection": "Room", "action": "has_playing_for_gameId", "data": {"gameId": game_id}})
        if active.get("status") == "OK" and bool((active.get("data") or {}).get("playing")):
            await _send(writer, _err("game_in_progress"))
            return
    payload = {"gameId": game_id, "delisted": delisted, "developerId": sess.developer_id}
    resp = await db_call({"collection": "Game", "action": "set_delisted", "data": payload})
    if resp.get("status") != "OK":
        await _send(writer, _err(resp.get("error", "delist_failed")))
        return
    await _send(writer, _ok())

async def handle_game_versions(writer: asyncio.StreamWriter, data: dict):
    sess = _require_login(writer)
    if not sess:
        await _send(writer, _err("not_logged_in"))
        return
    game_id = str((data.get("gameId") or "")).strip()
    if not game_id:
        await _send(writer, _err("missing_fields"))
        return
    g = await db_call({"collection": "Game", "action": "get_by_gameId", "data": {"gameId": game_id}})
    if g.get("status") != "OK":
        await _send(writer, _err(g.get("error", "no_such_game")))
        return
    gdata = g.get("data") or {}
    if int(gdata.get("developerId") or 0) != sess.developer_id:
        await _send(writer, _err("not_owner"))
        return
    v = await db_call({"collection": "GameVersion", "action": "list_for_gameId", "data": {"gameId": game_id}})
    if v.get("status") != "OK":
        await _send(writer, _err(v.get("error", "list_failed")))
        return
    await _send(writer, _ok(gameId=game_id, versions=v.get("versions") or []))


async def handle_upload_init(writer: asyncio.StreamWriter, data: dict):
    sess = _require_login(writer)
    if not sess:
        await _send(writer, _err("not_logged_in"))
        return

    game_id = str((data.get("gameId") or "")).strip()
    version = str((data.get("version") or "")).strip()
    file_name = str((data.get("fileName") or "")).strip()
    expected_size = int(data.get("sizeBytes") or 0)
    expected_sha256 = str((data.get("sha256") or "")).strip().lower()

    if not version or expected_size <= 0 or not expected_sha256:
        await _send(writer, _err("missing_fields"))
        return
    if not _VERSION_RE.fullmatch(version):
        await _send(writer, _err("bad_version"))
        return
    if game_id and not _GAME_ID_RE.fullmatch(game_id):
        await _send(writer, _err("bad_game_id"))
        return

    # If gameId is omitted, auto-create a Game row based on provided metadata.
    auto_created = False
    if not game_id:
        name = str((data.get("name") or "")).strip()
        description = str((data.get("description") or "")).strip()
        if not name or not description:
            await _send(writer, _err("missing_fields"))
            return
        # clientType/minPlayers/maxPlayers are UX hints; actual version metadata comes from manifest at finish.
        client_type = str((data.get("clientType") or "cli")).strip().lower()
        min_players = int(data.get("minPlayers") or 2)
        max_players = int(data.get("maxPlayers") or 2)
        # Generate a server-assigned gameId, then create the game row.
        game_id = await _reserve_unique_game_id(_slugify(name), sess.developer_id)
        auto_created = True
        created = await db_call(
            {
                "collection": "Game",
                "action": "create",
                "data": {
                    "gameId": game_id,
                    "name": name,
                    "description": description,
                    "developerId": sess.developer_id,
                    "clientType": client_type,
                    "minPlayers": min_players,
                    "maxPlayers": max_players,
                },
            }
        )
        if created.get("status") != "OK":
            await _send(writer, _err(created.get("error", "create_failed")))
            return
    else:
        # Ensure the game exists and belongs to the developer; otherwise create it.
        g = await db_call({"collection": "Game", "action": "get_by_gameId", "data": {"gameId": game_id}})
        if g.get("status") == "OK":
            gdata = g.get("data") or {}
            if int(gdata.get("developerId") or 0) != sess.developer_id:
                await _send(writer, _err("not_owner"))
                return
        else:
            if str(g.get("error") or "") != "not_found":
                await _send(writer, _err(g.get("error", "no_such_game")))
                return
            name = str((data.get("name") or "")).strip()
            description = str((data.get("description") or "")).strip()
            if not name or not description:
                await _send(writer, _err("missing_fields"))
                return
            client_type = str((data.get("clientType") or "cli")).strip().lower()
            min_players = int(data.get("minPlayers") or 2)
            max_players = int(data.get("maxPlayers") or 2)
            auto_created = True
            created = await db_call(
                {
                    "collection": "Game",
                    "action": "create",
                    "data": {
                        "gameId": game_id,
                        "name": name,
                        "description": description,
                        "developerId": sess.developer_id,
                        "clientType": client_type,
                        "minPlayers": min_players,
                        "maxPlayers": max_players,
                    },
                }
            )
            if created.get("status") != "OK":
                # If the id raced into existence, re-check ownership.
                if str(created.get("error") or "") == "game_exists":
                    g2 = await db_call({"collection": "Game", "action": "get_by_gameId", "data": {"gameId": game_id}})
                    if g2.get("status") == "OK" and int((g2.get("data") or {}).get("developerId") or 0) == sess.developer_id:
                        auto_created = False
                    else:
                        await _send(writer, _err(created.get("error", "create_failed")))
                        return
                else:
                    await _send(writer, _err(created.get("error", "create_failed")))
                    return

    upload_id = secrets.token_hex(16)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    temp_path = TMP_ROOT / f"{upload_id}.zip.part"

    UPLOADS[upload_id] = UploadSession(
        upload_id=upload_id,
        developer_id=sess.developer_id,
        game_id=game_id,
        version=version,
        file_name=file_name,
        expected_size=expected_size,
        expected_sha256=expected_sha256,
        temp_path=temp_path,
        auto_created_game=auto_created,
        received=0,
        next_seq=0,
        hasher=hashlib.sha256(),
    )
    # Truncate/initialize temp file
    temp_path.write_bytes(b"")
    await _send(writer, _ok(uploadId=upload_id, gameId=game_id, created=auto_created))


async def handle_upload_chunk(writer: asyncio.StreamWriter, data: dict):
    sess = _require_login(writer)
    if not sess:
        await _send(writer, _err("not_logged_in"))
        return

    upload_id = str((data.get("uploadId") or "")).strip()
    seq = int(data.get("seq") or 0)
    chunk_b64 = data.get("dataB64") or ""

    up = UPLOADS.get(upload_id)
    if not up:
        await _send(writer, _err("no_such_upload"))
        return
    if up.developer_id != sess.developer_id:
        await _send(writer, _err("not_owner"))
        return
    if seq != up.next_seq:
        await _send(writer, _err("bad_seq", expected=up.next_seq))
        return

    try:
        chunk = base64.b64decode(chunk_b64.encode("ascii"), validate=True)
    except Exception:
        await _send(writer, _err("bad_base64"))
        return

    if not chunk:
        await _send(writer, _err("empty_chunk"))
        return

    if up.received + len(chunk) > up.expected_size:
        await _send(writer, _err("too_large"))
        return

    with up.temp_path.open("ab") as f:
        f.write(chunk)
    up.hasher.update(chunk)
    up.received += len(chunk)
    up.next_seq += 1
    await _send(writer, _ok(received=up.received, expected=up.expected_size))


async def handle_upload_finish(writer: asyncio.StreamWriter, data: dict):
    sess = _require_login(writer)
    if not sess:
        await _send(writer, _err("not_logged_in"))
        return

    upload_id = str((data.get("uploadId") or "")).strip()
    up = UPLOADS.get(upload_id)
    if not up:
        await _send(writer, _err("no_such_upload"))
        return
    if up.developer_id != sess.developer_id:
        await _send(writer, _err("not_owner"))
        return

    if up.received != up.expected_size:
        await _send(writer, _err("size_mismatch", received=up.received, expected=up.expected_size))
        return

    digest = up.hasher.hexdigest().lower()
    if digest != up.expected_sha256:
        await _send(writer, _err("hash_mismatch", got=digest, expected=up.expected_sha256))
        return

    # Move into uploaded_games and extract.
    game_dir = UPLOAD_ROOT / up.game_id / up.version
    zip_path = game_dir / "package.zip"
    extracted_path = game_dir / "extracted"
    game_dir.mkdir(parents=True, exist_ok=True)

    try:
        shutil.move(str(up.temp_path), str(zip_path))
        if extracted_path.exists():
            shutil.rmtree(extracted_path)
        _safe_extract_zip(zip_path, extracted_path)
    except Exception as e:
        await _send(writer, _err(f"extract_failed:{e}"))
        return

    # Locate package root: allow a single top-level directory inside the zip.
    package_root = extracted_path
    if not (package_root / "manifest.json").exists():
        children = [p for p in package_root.iterdir()]
        if len(children) == 1 and children[0].is_dir():
            package_root = children[0]

    # Validate manifest
    manifest, err, raw = load_manifest_from_dir(package_root)
    if not manifest:
        await _send(writer, _err(err or "bad_manifest"))
        return
    if manifest.gameId != up.game_id:
        await _send(writer, _err("manifest_gameId_mismatch", manifestGameId=manifest.gameId, expected=up.game_id))
        return
    if manifest.version != up.version:
        await _send(writer, _err("manifest_version_mismatch", manifestVersion=manifest.version, expected=up.version))
        return

    # Ensure entrypoint files exist
    srv_path = package_root / manifest.server.module
    cli_path = package_root / manifest.client.module
    if not srv_path.exists():
        await _send(writer, _err("missing_server_entry", path=str(manifest.server.module)))
        return
    if not cli_path.exists():
        await _send(writer, _err("missing_client_entry", path=str(manifest.client.module)))
        return

    # DB: look up gameDbId, create GameVersion record
    g = await db_call({"collection": "Game", "action": "get_by_gameId", "data": {"gameId": up.game_id}})
    if g.get("status") != "OK":
        await _send(writer, _err(g.get("error", "no_such_game")))
        return
    gdata = g.get("data") or {}
    game_db_id = int(gdata.get("id") or 0)
    if game_db_id <= 0:
        await _send(writer, _err("bad_game_row"))
        return

    gv = await db_call(
        {
            "collection": "GameVersion",
            "action": "create",
            "data": {
                "gameDbId": game_db_id,
                "version": up.version,
                "changelog": (data.get("changelog") or ""),
                "fileName": up.file_name,
                "sizeBytes": up.expected_size,
                "sha256": up.expected_sha256,
                "zipPath": str(zip_path),
                "extractedPath": str(package_root),
                "manifestJson": json.dumps(raw, ensure_ascii=False),
                "clientType": manifest.clientType,
                "minPlayers": manifest.minPlayers,
                "maxPlayers": manifest.maxPlayers,
            },
        }
    )
    if gv.get("status") != "OK":
        await _send(writer, _err(gv.get("error", "version_create_failed")))
        return

    UPLOADS.pop(upload_id, None)
    await _send(writer, _ok(gameVersionId=(gv.get("data") or {}).get("gameVersionId")))


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        while True:
            frame = await recv_frame(reader)
            if frame is None:
                break
            msg = json.loads(frame.decode("utf-8"))
            typ = msg.get("type")
            data = msg.get("data", {}) or {}

            if typ == "dev_register":
                await handle_register(writer, data)
            elif typ == "dev_login":
                await handle_login(writer, data)
            elif typ == "dev_logout":
                await handle_logout(writer)
            elif typ == "game_list_mine":
                await handle_game_list_mine(writer)
            elif typ == "game_delist":
                await handle_game_delist(writer, data)
            elif typ == "game_list_versions":
                await handle_game_versions(writer, data)
            elif typ == "game_upload_init":
                await handle_upload_init(writer, data)
            elif typ == "game_upload_chunk":
                await handle_upload_chunk(writer, data)
            elif typ == "game_upload_finish":
                await handle_upload_finish(writer, data)
            else:
                await _send(writer, _err("unknown_type"))
    except FramingError:
        pass
    except Exception:
        with contextlib.suppress(Exception):
            await _send(writer, _err("server_exception"))
    finally:
        # cleanup sessions bound to this writer
        sess = SESSIONS.pop(writer, None)
        if sess:
            ONLINE_DEVS.pop(sess.developer_id, None)
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def main():
    UPLOAD_ROOT.mkdir(parents=True, exist_ok=True)
    TMP_ROOT.mkdir(parents=True, exist_ok=True)
    server = await asyncio.start_server(handle, HOST, PORT)
    print(f"[DevServer] listen on {HOST}:{PORT} | upload_root={UPLOAD_ROOT}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
    if not file_name:
        file_name = f"{game_id}-{version}.zip"
