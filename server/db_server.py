#!/usr/bin/env python3
"""
HW3 DB Server (internal service)

- TCP + length-prefixed JSON frames (see hw3/common/framing.py)
- SQLite persistence (data survives restart)

This server is intentionally "thin": it exposes CRUD/query actions needed by
the Lobby Server and Developer Server.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional

from hw3.common.framing import FramingError, recv_frame, send_frame
from hw3.common.config import get_int, get_str, resolve_path, section


PBKDF2_ITER = 120_000
SALT_LEN = 16


def now_ts() -> int:
    return int(time.time())


def _pbkdf2_hash(password: str, salt: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITER)


def _gen_salt() -> bytes:
    return os.urandom(SALT_LEN)


def _verify_password(password: str, salt: bytes, pw_hash: bytes) -> bool:
    test = _pbkdf2_hash(password, salt)
    return hmac.compare_digest(test, pw_hash)


def _db_path() -> Path:
    default = Path(__file__).resolve().parent / "storage" / "hw3.sqlite3"
    env = (os.environ.get("NP_HW3_DB_PATH") or "").strip()
    if env:
        return Path(env)
    cfg = section("db")
    cfg_path = get_str(cfg, "sqlitePath")
    if cfg_path:
        return resolve_path(cfg_path)
    return default


DB_PATH = _db_path()
_CFG_DB = section("db")
HOST = (os.environ.get("NP_HW3_DB_HOST") or get_str(_CFG_DB, "bindHost") or "0.0.0.0")
PORT = int(os.environ.get("NP_HW3_DB_PORT") or get_int(_CFG_DB, "port") or 10101)


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = _get_conn()
    try:
        cur = conn.cursor()
        # Accounts (separated by role per spec)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS DevUser(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                pw_salt BLOB NOT NULL,
                pw_hash BLOB NOT NULL,
                createdAt INTEGER NOT NULL,
                lastLoginAt INTEGER
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS PlayerUser(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                pw_salt BLOB NOT NULL,
                pw_hash BLOB NOT NULL,
                createdAt INTEGER NOT NULL,
                lastLoginAt INTEGER
            )
            """
        )

        # Store / versions
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS Game(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gameId TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                developerId INTEGER NOT NULL,
                delisted INTEGER NOT NULL DEFAULT 0,
                createdAt INTEGER NOT NULL,
                updatedAt INTEGER NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS GameVersion(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gameFk INTEGER NOT NULL,
                version TEXT NOT NULL,
                changelog TEXT,
                uploadedAt INTEGER NOT NULL,
                fileName TEXT NOT NULL,
                sizeBytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL,
                zipPath TEXT NOT NULL,
                extractedPath TEXT NOT NULL,
                manifestJson TEXT NOT NULL,
                clientType TEXT NOT NULL,
                minPlayers INTEGER NOT NULL,
                maxPlayers INTEGER NOT NULL,
                UNIQUE(gameFk, version)
            )
            """
        )

        # Reviews
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS Review(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gameFk INTEGER NOT NULL,
                playerId INTEGER NOT NULL,
                rating INTEGER NOT NULL,
                comment TEXT NOT NULL,
                createdAt INTEGER NOT NULL,
                updatedAt INTEGER NOT NULL,
                UNIQUE(gameFk, playerId)
            )
            """
        )

        # Lobby rooms
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS Room(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                hostPlayerId INTEGER NOT NULL,
                gameFk INTEGER NOT NULL,
                gameVersionFk INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'waiting',
                createdAt INTEGER NOT NULL,
                updatedAt INTEGER NOT NULL
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS RoomMember(
                roomId INTEGER NOT NULL,
                playerId INTEGER NOT NULL,
                joinedAt INTEGER NOT NULL,
                PRIMARY KEY(roomId, playerId)
            )
            """
        )

        # Match history (for P4 eligibility and UX)
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS MatchLog(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                roomId INTEGER NOT NULL,
                gameFk INTEGER NOT NULL,
                gameVersionFk INTEGER NOT NULL,
                startedAt INTEGER NOT NULL,
                endedAt INTEGER NOT NULL,
                reason TEXT NOT NULL,
                winnerPlayerId INTEGER,
                resultsJson TEXT NOT NULL
            )
            """
        )

        conn.commit()
    finally:
        conn.close()


# -------------------------
# Dispatch helpers
# -------------------------
def _err(code: str, **extra: Any) -> Dict[str, Any]:
    return {"status": "ERR", "error": code, **extra}


def _ok(**extra: Any) -> Dict[str, Any]:
    return {"status": "OK", **extra}


def _fetchone_dict(cur: sqlite3.Cursor) -> Optional[dict]:
    row = cur.fetchone()
    return dict(row) if row else None


# -------------------------
# Collections
# -------------------------
def handle_dev_user(action: str, data: Dict[str, Any]) -> Dict[str, Any]:
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if action == "register":
        if not username or not password:
            return _err("missing_fields")
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM DevUser WHERE username=?", (username,))
            if cur.fetchone():
                return _err("username_exists")
            salt = _gen_salt()
            pw_hash = _pbkdf2_hash(password, salt)
            ts = now_ts()
            cur.execute(
                "INSERT INTO DevUser(username,pw_salt,pw_hash,createdAt,lastLoginAt) VALUES(?,?,?,?,?)",
                (username, salt, pw_hash, ts, 0),
            )
            conn.commit()
            return _ok(data={"developerId": cur.lastrowid, "username": username})

    if action == "login":
        if not username or not password:
            return _err("missing_fields")
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id,pw_salt,pw_hash FROM DevUser WHERE username=?", (username,))
            row = cur.fetchone()
            if not row:
                return _err("bad_credentials")
            if not _verify_password(password, row["pw_salt"], row["pw_hash"]):
                return _err("bad_credentials")
            ts = now_ts()
            cur.execute("UPDATE DevUser SET lastLoginAt=? WHERE id=?", (ts, row["id"]))
            conn.commit()
            return _ok(data={"developerId": int(row["id"]), "username": username})

    if action == "get_by_username":
        if not username:
            return _err("missing_fields")
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id,username,createdAt,lastLoginAt FROM DevUser WHERE username=?", (username,))
            row = _fetchone_dict(cur)
            if not row:
                return _err("not_found")
            return _ok(data=row)

    if action == "get_by_id":
        dev_id = int(data.get("developerId") or 0)
        if dev_id <= 0:
            return _err("missing_fields")
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id,username,createdAt,lastLoginAt FROM DevUser WHERE id=?", (dev_id,))
            row = _fetchone_dict(cur)
            if not row:
                return _err("not_found")
            return _ok(data=row)

    return _err("unknown_action")


def handle_player_user(action: str, data: Dict[str, Any]) -> Dict[str, Any]:
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if action == "register":
        if not username or not password:
            return _err("missing_fields")
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT 1 FROM PlayerUser WHERE username=?", (username,))
            if cur.fetchone():
                return _err("username_exists")
            salt = _gen_salt()
            pw_hash = _pbkdf2_hash(password, salt)
            ts = now_ts()
            cur.execute(
                "INSERT INTO PlayerUser(username,pw_salt,pw_hash,createdAt,lastLoginAt) VALUES(?,?,?,?,?)",
                (username, salt, pw_hash, ts, 0),
            )
            conn.commit()
            return _ok(data={"playerId": cur.lastrowid, "username": username})

    if action == "login":
        if not username or not password:
            return _err("missing_fields")
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id,pw_salt,pw_hash FROM PlayerUser WHERE username=?", (username,))
            row = cur.fetchone()
            if not row:
                return _err("bad_credentials")
            if not _verify_password(password, row["pw_salt"], row["pw_hash"]):
                return _err("bad_credentials")
            ts = now_ts()
            cur.execute("UPDATE PlayerUser SET lastLoginAt=? WHERE id=?", (ts, row["id"]))
            conn.commit()
            return _ok(data={"playerId": int(row["id"]), "username": username})

    if action == "get_by_username":
        if not username:
            return _err("missing_fields")
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id,username,createdAt,lastLoginAt FROM PlayerUser WHERE username=?", (username,))
            row = _fetchone_dict(cur)
            if not row:
                return _err("not_found")
            return _ok(data=row)

    return _err("unknown_action")


def handle_game(action: str, data: Dict[str, Any]) -> Dict[str, Any]:
    if action == "create":
        game_id = (data.get("gameId") or "").strip()
        name = (data.get("name") or "").strip()
        description = (data.get("description") or "").strip()
        developer_id = int(data.get("developerId") or 0)
        if not game_id or not name or not description or developer_id <= 0:
            return _err("missing_fields")
        ts = now_ts()
        with _get_conn() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    "INSERT INTO Game(gameId,name,description,developerId,delisted,createdAt,updatedAt) VALUES(?,?,?,?,?,?,?)",
                    (game_id, name, description, developer_id, 0, ts, ts),
                )
                conn.commit()
            except sqlite3.IntegrityError:
                return _err("game_exists")
            return _ok(data={"gameDbId": cur.lastrowid})

    if action == "get_by_gameId":
        game_id = (data.get("gameId") or "").strip()
        if not game_id:
            return _err("missing_fields")
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM Game WHERE gameId=?", (game_id,))
            row = _fetchone_dict(cur)
            if not row:
                return _err("not_found")
            return _ok(data=row)

    if action == "list_public":
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM Game WHERE delisted=0 ORDER BY updatedAt DESC, id DESC")
            return _ok(games=[dict(r) for r in cur.fetchall()])

    if action == "list_by_dev":
        developer_id = int(data.get("developerId") or 0)
        if developer_id <= 0:
            return _err("missing_fields")
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM Game WHERE developerId=? ORDER BY updatedAt DESC, id DESC", (developer_id,))
            return _ok(games=[dict(r) for r in cur.fetchall()])

    if action == "set_delisted":
        game_id = (data.get("gameId") or "").strip()
        delisted = 1 if data.get("delisted") else 0
        developer_id = int(data.get("developerId") or 0)
        if not game_id or developer_id <= 0:
            return _err("missing_fields")
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT developerId FROM Game WHERE gameId=?", (game_id,))
            row = cur.fetchone()
            if not row:
                return _err("not_found")
            if int(row["developerId"]) != developer_id:
                return _err("not_owner")
            ts = now_ts()
            cur.execute("UPDATE Game SET delisted=?, updatedAt=? WHERE gameId=?", (delisted, ts, game_id))
            conn.commit()
            return _ok()

    return _err("unknown_action")


def handle_game_version(action: str, data: Dict[str, Any]) -> Dict[str, Any]:
    if action == "create":
        game_db_id = int(data.get("gameDbId") or 0)
        version = (data.get("version") or "").strip()
        if game_db_id <= 0 or not version:
            return _err("missing_fields")

        required = (
            "fileName",
            "sizeBytes",
            "sha256",
            "zipPath",
            "extractedPath",
            "manifestJson",
            "clientType",
            "minPlayers",
            "maxPlayers",
        )
        if any(k not in data for k in required):
            return _err("missing_fields", missing=[k for k in required if k not in data])

        ts = now_ts()
        with _get_conn() as conn:
            cur = conn.cursor()
            try:
                cur.execute(
                    """
                    INSERT INTO GameVersion(
                        gameFk,version,changelog,uploadedAt,fileName,sizeBytes,sha256,
                        zipPath,extractedPath,manifestJson,clientType,minPlayers,maxPlayers
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        game_db_id,
                        version,
                        (data.get("changelog") or "").strip(),
                        ts,
                        data["fileName"],
                        int(data["sizeBytes"]),
                        data["sha256"],
                        data["zipPath"],
                        data["extractedPath"],
                        data["manifestJson"],
                        data["clientType"],
                        int(data["minPlayers"]),
                        int(data["maxPlayers"]),
                    ),
                )
                # bump game.updatedAt on any new upload
                cur.execute("UPDATE Game SET updatedAt=? WHERE id=?", (ts, game_db_id))
                conn.commit()
            except sqlite3.IntegrityError:
                return _err("version_exists")
            return _ok(data={"gameVersionId": cur.lastrowid})

    if action == "list_for_gameId":
        game_id = (data.get("gameId") or "").strip()
        if not game_id:
            return _err("missing_fields")
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id,delisted FROM Game WHERE gameId=?", (game_id,))
            g = cur.fetchone()
            if not g:
                return _err("not_found")
            game_fk = int(g["id"])
            cur.execute(
                """
                SELECT id,version,uploadedAt,changelog,fileName,sizeBytes,sha256,clientType,minPlayers,maxPlayers
                FROM GameVersion
                WHERE gameFk=?
                ORDER BY uploadedAt DESC, id DESC
                """,
                (game_fk,),
            )
            return _ok(versions=[dict(r) for r in cur.fetchall()])

    if action == "get_for_gameId_version":
        game_id = (data.get("gameId") or "").strip()
        version = (data.get("version") or "").strip()
        if not game_id or not version:
            return _err("missing_fields")
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id,delisted FROM Game WHERE gameId=?", (game_id,))
            g = cur.fetchone()
            if not g:
                return _err("not_found")
            if int(g["delisted"]) != 0:
                return _err("game_delisted")
            cur.execute(
                """
                SELECT gv.* FROM GameVersion gv
                WHERE gv.gameFk=? AND gv.version=?
                LIMIT 1
                """,
                (int(g["id"]), version),
            )
            row = _fetchone_dict(cur)
            if not row:
                return _err("no_such_version")
            return _ok(data=row)

    if action == "latest_for_gameId":
        game_id = (data.get("gameId") or "").strip()
        if not game_id:
            return _err("missing_fields")
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id,delisted FROM Game WHERE gameId=?", (game_id,))
            g = cur.fetchone()
            if not g:
                return _err("not_found")
            if int(g["delisted"]) != 0:
                return _err("game_delisted")
            cur.execute(
                """
                SELECT gv.* FROM GameVersion gv
                WHERE gv.gameFk=?
                ORDER BY gv.uploadedAt DESC, gv.id DESC
                LIMIT 1
                """,
                (int(g["id"]),),
            )
            row = _fetchone_dict(cur)
            if not row:
                return _err("no_version")
            return _ok(data=row)

    if action == "get_by_id":
        vid = int(data.get("gameVersionId") or 0)
        if vid <= 0:
            return _err("missing_fields")
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM GameVersion WHERE id=?", (vid,))
            row = _fetchone_dict(cur)
            if not row:
                return _err("not_found")
            return _ok(data=row)

    return _err("unknown_action")


def handle_review(action: str, data: Dict[str, Any]) -> Dict[str, Any]:
    if action == "upsert":
        game_id = (data.get("gameId") or "").strip()
        player_id = int(data.get("playerId") or 0)
        rating = int(data.get("rating") or 0)
        comment = (data.get("comment") or "").strip()
        if not game_id or player_id <= 0 or rating < 1 or rating > 5:
            return _err("bad_request")
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM Game WHERE gameId=?", (game_id,))
            g = cur.fetchone()
            if not g:
                return _err("not_found")
            ts = now_ts()
            cur.execute(
                """
                INSERT INTO Review(gameFk,playerId,rating,comment,createdAt,updatedAt)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(gameFk,playerId) DO UPDATE SET
                  rating=excluded.rating,
                  comment=excluded.comment,
                  updatedAt=excluded.updatedAt
                """,
                (int(g["id"]), player_id, rating, comment, ts, ts),
            )
            conn.commit()
            return _ok()

    if action == "list_for_gameId":
        game_id = (data.get("gameId") or "").strip()
        if not game_id:
            return _err("missing_fields")
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM Game WHERE gameId=?", (game_id,))
            g = cur.fetchone()
            if not g:
                return _err("not_found")
            cur.execute(
                """
                SELECT playerId,rating,comment,createdAt,updatedAt
                FROM Review
                WHERE gameFk=?
                ORDER BY updatedAt DESC, id DESC
                """,
                (int(g["id"]),),
            )
            rows = [dict(r) for r in cur.fetchall()]
            return _ok(reviews=rows)

    return _err("unknown_action")


def handle_room(action: str, data: Dict[str, Any]) -> Dict[str, Any]:
    if action == "create":
        host_player_id = int(data.get("hostPlayerId") or 0)
        game_db_id = int(data.get("gameDbId") or 0)
        game_version_id = int(data.get("gameVersionId") or 0)
        if host_player_id <= 0 or game_db_id <= 0 or game_version_id <= 0:
            return _err("missing_fields")
        ts = now_ts()
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO Room(hostPlayerId,gameFk,gameVersionFk,status,createdAt,updatedAt) VALUES(?,?,?,?,?,?)",
                (host_player_id, game_db_id, game_version_id, "waiting", ts, ts),
            )
            rid = cur.lastrowid
            cur.execute(
                "INSERT OR IGNORE INTO RoomMember(roomId,playerId,joinedAt) VALUES(?,?,?)",
                (rid, host_player_id, ts),
            )
            conn.commit()
            return _ok(data={"roomId": rid})

    if action == "has_playing_for_gameId":
        game_id = (data.get("gameId") or "").strip()
        if not game_id:
            return _err("missing_fields")
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT 1
                FROM Room r
                JOIN Game g ON g.id=r.gameFk
                WHERE g.gameId=? AND r.status='playing'
                LIMIT 1
                """,
                (game_id,),
            )
            return _ok(data={"playing": cur.fetchone() is not None})

    if action == "list":
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT r.id,r.hostPlayerId,r.status,r.createdAt,r.updatedAt,
                       g.gameId AS gameId, g.name AS gameName,
                       gv.version AS version
                FROM Room r
                JOIN Game g ON g.id=r.gameFk
                JOIN GameVersion gv ON gv.id=r.gameVersionFk
                ORDER BY r.updatedAt DESC, r.id DESC
                """
            )
            rooms = [dict(r) for r in cur.fetchall()]
            for room in rooms:
                cur.execute("SELECT playerId FROM RoomMember WHERE roomId=? ORDER BY joinedAt ASC", (int(room["id"]),))
                room["players"] = [int(x["playerId"]) for x in cur.fetchall()]
            return _ok(rooms=rooms)

    if action == "get":
        room_id = int(data.get("roomId") or 0)
        if room_id <= 0:
            return _err("missing_fields")
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT r.id,r.hostPlayerId,r.status,r.createdAt,r.updatedAt,
                       g.id AS gameDbId, g.gameId AS gameId, g.name AS gameName, g.delisted AS delisted,
                       gv.id AS gameVersionId, gv.version AS version, gv.clientType AS clientType,
                       gv.minPlayers AS minPlayers, gv.maxPlayers AS maxPlayers
                FROM Room r
                JOIN Game g ON g.id=r.gameFk
                JOIN GameVersion gv ON gv.id=r.gameVersionFk
                WHERE r.id=?
                """,
                (room_id,),
            )
            room = _fetchone_dict(cur)
            if not room:
                return _err("not_found")
            cur.execute("SELECT playerId FROM RoomMember WHERE roomId=? ORDER BY joinedAt ASC", (room_id,))
            room["players"] = [int(x["playerId"]) for x in cur.fetchall()]
            return _ok(data=room)

    if action == "add_member":
        room_id = int(data.get("roomId") or 0)
        player_id = int(data.get("playerId") or 0)
        if room_id <= 0 or player_id <= 0:
            return _err("missing_fields")
        ts = now_ts()
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "INSERT OR IGNORE INTO RoomMember(roomId,playerId,joinedAt) VALUES(?,?,?)",
                (room_id, player_id, ts),
            )
            cur.execute("UPDATE Room SET updatedAt=? WHERE id=?", (ts, room_id))
            conn.commit()
            return _ok()

    if action == "remove_member":
        room_id = int(data.get("roomId") or 0)
        player_id = int(data.get("playerId") or 0)
        if room_id <= 0 or player_id <= 0:
            return _err("missing_fields")
        ts = now_ts()
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("DELETE FROM RoomMember WHERE roomId=? AND playerId=?", (room_id, player_id))
            cur.execute("UPDATE Room SET updatedAt=? WHERE id=?", (ts, room_id))
            conn.commit()
            return _ok()

    if action == "set_status":
        room_id = int(data.get("roomId") or 0)
        status = (data.get("status") or "").strip()
        if room_id <= 0 or not status:
            return _err("missing_fields")
        ts = now_ts()
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE Room SET status=?, updatedAt=? WHERE id=?", (status, ts, room_id))
            conn.commit()
            return _ok()

    if action == "set_host":
        room_id = int(data.get("roomId") or 0)
        host_player_id = int(data.get("hostPlayerId") or 0)
        if room_id <= 0 or host_player_id <= 0:
            return _err("missing_fields")
        ts = now_ts()
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE Room SET hostPlayerId=?, updatedAt=? WHERE id=?", (host_player_id, ts, room_id))
            conn.commit()
            return _ok()

    if action == "delete_if_empty":
        room_id = int(data.get("roomId") or 0)
        if room_id <= 0:
            return _err("missing_fields")
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS n FROM RoomMember WHERE roomId=?", (room_id,))
            n = int(cur.fetchone()["n"])
            if n != 0:
                return _err("not_empty")
            cur.execute("DELETE FROM Room WHERE id=?", (room_id,))
            conn.commit()
            return _ok()

    return _err("unknown_action")


def handle_match_log(action: str, data: Dict[str, Any]) -> Dict[str, Any]:
    if action == "create":
        required = ("roomId", "gameDbId", "gameVersionId", "startedAt", "endedAt", "reason", "resultsJson")
        if any(k not in data for k in required):
            return _err("missing_fields", missing=[k for k in required if k not in data])
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO MatchLog(roomId,gameFk,gameVersionFk,startedAt,endedAt,reason,winnerPlayerId,resultsJson)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    int(data["roomId"]),
                    int(data["gameDbId"]),
                    int(data["gameVersionId"]),
                    int(data["startedAt"]),
                    int(data["endedAt"]),
                    str(data["reason"]),
                    int(data.get("winnerPlayerId") or 0) or None,
                    str(data["resultsJson"]),
                ),
            )
            conn.commit()
            return _ok(data={"matchLogId": cur.lastrowid})

    if action == "has_player_played":
        game_id = (data.get("gameId") or "").strip()
        player_id = int(data.get("playerId") or 0)
        if not game_id or player_id <= 0:
            return _err("missing_fields")
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT id FROM Game WHERE gameId=?", (game_id,))
            g = cur.fetchone()
            if not g:
                return _err("not_found")
            game_fk = int(g["id"])
            # Minimal eligibility check: does any match log contain this playerId marker?
            # (Lobby persists resultsJson with at least a players list.)
            cur.execute(
                """
                SELECT 1 FROM MatchLog
                WHERE gameFk=? AND resultsJson LIKE ?
                LIMIT 1
                """,
                (game_fk, f"%\"userId\": {player_id}%"),
            )
            played = cur.fetchone() is not None
            return _ok(data={"played": played})

    if action == "list_by_player":
        player_id = int(data.get("playerId") or 0)
        if player_id <= 0:
            return _err("missing_fields")
        with _get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT ml.*,
                       g.gameId AS gameId,
                       gv.version AS version
                FROM MatchLog ml
                JOIN Game g ON g.id=ml.gameFk
                JOIN GameVersion gv ON gv.id=ml.gameVersionFk
                WHERE resultsJson LIKE ?
                ORDER BY ml.endedAt DESC, ml.id DESC
                LIMIT 50
                """,
                (f"%\"userId\": {player_id}%",),
            )
            return _ok(logs=[dict(r) for r in cur.fetchall()])

    return _err("unknown_action")


def dispatch(req: Dict[str, Any]) -> Dict[str, Any]:
    collection = req.get("collection")
    action = req.get("action")
    data = req.get("data", {}) or {}

    if collection == "DevUser":
        return handle_dev_user(action, data)
    if collection == "PlayerUser":
        return handle_player_user(action, data)
    if collection == "Game":
        return handle_game(action, data)
    if collection == "GameVersion":
        return handle_game_version(action, data)
    if collection == "Review":
        return handle_review(action, data)
    if collection == "Room":
        return handle_room(action, data)
    if collection == "MatchLog":
        return handle_match_log(action, data)
    return _err("unknown_collection")


async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    try:
        while True:
            frame = await recv_frame(reader)
            if frame is None:
                break
            try:
                req = json.loads(frame.decode("utf-8"))
                resp = dispatch(req)
            except Exception:
                resp = _err("exception")
            await send_frame(writer, json.dumps(resp, separators=(",", ":")).encode("utf-8"))
    except FramingError:
        pass
    finally:
        writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()


async def main():
    init_db()
    server = await asyncio.start_server(handle, HOST, PORT)
    print(f"[DB] SQLite at {DB_PATH} | listen {HOST}:{PORT}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
