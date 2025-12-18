#!/usr/bin/env python3
"""
Menu-driven Lobby Client (HW3).

Connects to Lobby Server and supports (minimal scaffold):
  - player register/login/logout
  - browse store (list games, details)
  - download/update latest game version into per-player downloads/
  - room create/join/leave/list
  - start match (host) and auto-launch the downloaded game client on game_info events
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import socket
import subprocess
import sys
import tempfile
import threading
import time
import shutil
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

from hw3.common.config import get_int, get_str, section
from hw3.common.framing import recv_json_sync, send_json_sync, safe_json_dumps
from hw3.common.manifest import load_manifest_from_dir


_CFG_LOBBY = section("lobbyServer")
DEFAULT_HOST = (os.environ.get("NP_HW3_LOBBY_HOST") or get_str(_CFG_LOBBY, "host") or "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("NP_HW3_LOBBY_PORT") or get_int(_CFG_LOBBY, "port") or 10103)

DOWNLOADS_ROOT = Path(os.environ.get("NP_HW3_DOWNLOADS_ROOT", str(Path(__file__).resolve().parent / "downloads")))
REVIEW_DRAFTS_ROOT = Path(os.environ.get("NP_HW3_REVIEW_DRAFTS_ROOT", str(DOWNLOADS_ROOT / "_review_drafts")))

ERROR_MESSAGES = {
    "missing_fields": "Missing required fields.",
    "bad_request": "Bad request.",
    "bad_credentials": "Wrong username or password.",
    "username_exists": "Username already in use.",
    "already_online": "This account is already logged in elsewhere.",
    "not_logged_in": "Please login first.",
    "not_host": "Only the room host can start the match.",
    "room_full": "Room is full.",
    "room_playing": "Room is currently playing.",
    "already_in_room": "You are already in a room.",
    "need_more_players": "Not enough players to start.",
    "game_delisted": "This game is delisted.",
    "no_version": "No downloadable version available for this game.",
    "missing_zip_on_server": "Server is missing the uploaded package for this game/version.",
    "not_played": "You must play the game before leaving a review.",
}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            b = f.read(64 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def _safe_extract_zip(zip_path: Path, dst_dir: Path) -> None:
    dst_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            p = Path(member.filename)
            if p.is_absolute() or ".." in p.parts:
                raise ValueError("unsafe_zip_entry")
        zf.extractall(dst_dir)


def _resolve_package_root(base_dir: Path) -> Path:
    """
    Allow either:
      - manifest.json at base_dir/
      - a single top-level directory containing manifest.json
    """
    if (base_dir / "manifest.json").exists():
        return base_dir
    children = []
    try:
        children = [p for p in base_dir.iterdir()]
    except FileNotFoundError:
        return base_dir
    if len(children) == 1 and children[0].is_dir() and (children[0] / "manifest.json").exists():
        return children[0]
    return base_dir


class LobbyClient:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sock: Optional[socket.socket] = None
        self.alive = False
        self.reader_thread: Optional[threading.Thread] = None
        self.lock = threading.Lock()

        self.responses: "queue.Queue[dict]" = queue.Queue()

        self.player_id: Optional[int] = None
        self.username: Optional[str] = None
        self.room_id: Optional[int] = None

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port))
        self.sock.settimeout(0.5)
        self.alive = True
        self.reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self.reader_thread.start()
        _ = self._wait_response(timeout=2.0)
        print(f"[LobbyClient] connected to {self.host}:{self.port}")

    def close(self):
        self.alive = False
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None

    def _reader_loop(self):
        assert self.sock
        while self.alive and self.sock:
            try:
                msg = recv_json_sync(self.sock)
                if msg is None:
                    break
                if msg.get("type") == "event":
                    self._handle_event(msg)
                else:
                    self.responses.put(msg)
            except socket.timeout:
                continue
            except Exception:
                time.sleep(0.2)
        self.alive = False

    def _wait_response(self, timeout: float = 5.0) -> Optional[dict]:
        try:
            return self.responses.get(timeout=timeout)
        except queue.Empty:
            return None

    def request(self, typ: str, data: dict | None = None, timeout: float = 10.0) -> dict:
        assert self.sock
        with self.lock:
            send_json_sync(self.sock, {"type": typ, "data": (data or {})})
        resp = self._wait_response(timeout=timeout)
        if resp is None:
            raise RuntimeError("timeout_or_disconnect")
        return resp

    # -------------------------
    # Events
    # -------------------------
    def _handle_event(self, msg: dict):
        name = msg.get("name")
        data = msg.get("data") or {}
        if name == "game_info":
            print(f"\n[EVENT] game_info: {data}")
            # Auto-launch performs blocking work (download + subprocess). Run it in a
            # separate thread so the reader loop can keep consuming messages.
            threading.Thread(target=self._auto_launch_game_safe, args=(data,), daemon=True).start()
        elif name == "game_ready":
            print(f"\n[EVENT] game_ready: room {data.get('roomId')}")
            try:
                rid = int(data.get("roomId") or 0)
                if rid > 0:
                    # Helpful UX: show current room status after a match ends.
                    self.do_room_detail(rid)
                    print("[EVENT] You can start another match now.", flush=True)
            except Exception:
                pass
        elif name == "player_joined":
            print(f"\n[EVENT] player_joined: {data}")
        elif name == "player_left":
            print(f"\n[EVENT] player_left: {data}")
        elif name == "host_changed":
            print(f"\n[EVENT] host_changed: {data}")
        else:
            print(f"\n[EVENT] {name}: {data}")

    def _auto_launch_game_safe(self, game_info: dict):
        try:
            self._auto_launch_game(game_info)
        except Exception as e:
            print(f"[EVENT] launch failed: {e}", flush=True)

    # -------------------------
    # Download + launch
    # -------------------------
    def _installed_dir(self, game_id: str, version: str) -> Path:
        assert self.username
        return DOWNLOADS_ROOT / self.username / game_id / version

    def _local_versions(self, game_id: str) -> list[str]:
        if not self.username:
            return []
        base = DOWNLOADS_ROOT / self.username / game_id
        try:
            versions = [p.name for p in base.iterdir() if p.is_dir()]
        except FileNotFoundError:
            return []
        return sorted(versions)

    def _best_local_version(self, game_id: str) -> Optional[str]:
        """
        Pick a reasonable "latest" among locally installed versions.
        Prefers semver-ish ordering when possible, otherwise falls back to lexicographic.
        """
        versions = self._local_versions(game_id)
        if not versions:
            return None

        def semver_key(v: str):
            parts = v.strip().lstrip("v").split(".")
            nums: list[int] = []
            for p in parts[:3]:
                try:
                    nums.append(int(p))
                except Exception:
                    return None
            while len(nums) < 3:
                nums.append(0)
            return tuple(nums)

        parsed = [(v, semver_key(v)) for v in versions]
        semver_only = [x for x in parsed if x[1] is not None]
        if semver_only:
            return max(semver_only, key=lambda x: x[1])[0]
        return max(versions)

    def ensure_downloaded(self, game_id: str, version: str | None = None, *, interactive: bool = True) -> Path:
        """
        Ensure the requested game is installed. If version is None, downloads latest.
        Returns the installed directory.
        """
        if not self.username:
            raise RuntimeError("not_logged_in")
        if version is not None:
            d = self._installed_dir(game_id, version)
            root = _resolve_package_root(d)
            if (root / "manifest.json").exists():
                return root

        # If a specific version is requested (e.g. from room_start), ask server for that exact version.
        init_req = {"gameId": game_id}
        if version is not None:
            init_req["version"] = version
        info = self.request("store_download_init", init_req)
        if not info.get("ok"):
            raise RuntimeError(info.get("error"))
        download_id = info["downloadId"]
        version = str(info["version"])
        size = int(info["sizeBytes"])
        sha = str(info["sha256"])

        # Only prompt for "update?" when user is manually downloading latest.
        if interactive and version is not None and "version" not in init_req:
            local_best = self._best_local_version(game_id)
            if local_best and local_best != version:
                ans = input(f"Local version is v{local_best}, server has v{version}. Update? (Y/n): ").strip().lower()
                if ans == "n":
                    existing = self._installed_dir(game_id, local_best)
                    root = _resolve_package_root(existing)
                    if (root / "manifest.json").exists():
                        print(f"[LobbyClient] keeping local v{local_best}")
                        return root

        install_dir = self._installed_dir(game_id, version)
        root = _resolve_package_root(install_dir)
        if (root / "manifest.json").exists():
            # already installed
            return root

        install_dir.parent.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory() as td:
            tmp_zip = Path(td) / f"{game_id}-{version}.zip"
            offset = 0
            with tmp_zip.open("wb") as f:
                while True:
                    r = self.request(
                        "store_download_chunk",
                        {"downloadId": download_id, "offset": offset, "limit": 32 * 1024},
                        timeout=10.0,
                    )
                    if not r.get("ok"):
                        raise RuntimeError(r.get("error"))
                    chunk = base64.b64decode((r.get("dataB64") or "").encode("ascii"))
                    f.write(chunk)
                    offset += len(chunk)
                    done = bool(r.get("done"))
                    print(f"\rDownloading... {offset}/{size} bytes", end="")
                    if done:
                        break
            print()
            if offset != size:
                raise RuntimeError(f"size_mismatch local={offset} server={size}")
            got = sha256_file(tmp_zip)
            if got.lower() != sha.lower():
                raise RuntimeError(f"hash_mismatch got={got} expected={sha}")

            # Extract atomically: extract to temp dir then rename.
            tmp_extract = Path(td) / "extracted"
            _safe_extract_zip(tmp_zip, tmp_extract)

            if install_dir.exists():
                shutil.rmtree(install_dir)
            install_dir.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp_extract), str(install_dir))

        root = _resolve_package_root(install_dir)
        if not (root / "manifest.json").exists():
            raise RuntimeError("bad_install_missing_manifest")

        return root

    def _ensure_installed_for_room(self, *, game_id: str, version: str) -> None:
        """
        Best-effort: before creating/joining a room, ensure the exact room version is installed.
        If it's missing, auto-download it and print a clear message after install.
        """
        if not self.username:
            print("[ERR] not_logged_in")
            return
        game_id = str(game_id or "").strip()
        version = str(version or "").strip()
        if not game_id or not version:
            return

        install_dir = self._installed_dir(game_id, version)
        root = _resolve_package_root(install_dir)
        if (root / "manifest.json").exists():
            return

        print(f"[LobbyClient] {game_id} v{version} is not installed. Auto-downloading now...")
        try:
            installed_root = self.ensure_downloaded(game_id, version=version, interactive=False)
        except Exception as e:
            print(f"[ERR] Auto-download failed: {e}")
            return
        print(f"[LobbyClient] Installed {game_id} v{version} at: {installed_root}")

    def _auto_launch_game(self, game_info: dict):
        if not self.player_id:
            raise RuntimeError("not_logged_in")
        game_id = str(game_info.get("gameId") or "").strip()
        version = str(game_info.get("version") or "").strip()
        host = str(game_info.get("host") or "").strip()
        port = int(game_info.get("port") or 0)
        token = str(game_info.get("token") or "").strip()
        room_id = int(game_info.get("roomId") or 0)
        if not game_id or not version or not host or not port or not token:
            raise RuntimeError("bad_game_info")

        # Auto-launch should be non-interactive (avoid prompting in reader thread).
        install_dir = self.ensure_downloaded(game_id, version=version, interactive=False)
        manifest, err, _raw = load_manifest_from_dir(install_dir)
        if not manifest:
            raise RuntimeError(err or "bad_manifest")

        mapping = {"host": host, "port": port, "token": token, "roomId": room_id, "userId": self.player_id}
        argv = []
        for a in manifest.client.argv:
            argv.append(str(a).format(**mapping))

        cmd = [sys.executable, str(install_dir / manifest.client.module), *argv]
        print(f"[LobbyClient] launching game client: {' '.join(cmd)} (cwd={install_dir})", flush=True)
        try:
            completed = subprocess.run(cmd, cwd=str(install_dir), check=False)
            print(f"[LobbyClient] game client exited with code={completed.returncode}", flush=True)
        except Exception as e:
            raise RuntimeError(f"launch_exception:{e}") from e

    # -------------------------
    # Menu actions
    # -------------------------
    def _choose_game_id(self, *, prompt: str = "Choose game: ") -> Optional[str]:
        """
        Fetch store list and let user pick a game by number.
        Returns gameId or None if canceled / no games.
        """
        while True:
            try:
                r = self.request("store_list_games")
            except Exception:
                print("[ERR] Failed to load game list (timeout/disconnect).")
                retry = input("Retry? (y/N): ").strip().lower()
                if retry in ("y", "yes"):
                    continue
                return None
            if r.get("ok"):
                break
            self._print_resp(r)
            print("[ERR] Failed to load game list.")
            retry = input("Retry? (y/N): ").strip().lower()
            if retry in ("y", "yes"):
                continue
            return None
        games = r.get("games") or []
        if not games:
            print("No available games.")
            return None
        print("Available games:")
        for i, g in enumerate(games, start=1):
            gid = g.get("gameId")
            name = g.get("name") or "(unnamed)"
            latest = g.get("latestVersion") or "-"
            players = ""
            if g.get("minPlayers") is not None and g.get("maxPlayers") is not None:
                players = f"{g.get('minPlayers')}-{g.get('maxPlayers')}"
            print(f"  {i}) {gid} | {name} | latest={latest} | players={players}")
        s = input(f"{prompt}(1-{len(games)} or blank to cancel): ").strip()
        if not s:
            return None
        try:
            idx = int(s)
        except Exception:
            print("[ERR] invalid selection")
            return None
        if idx < 1 or idx > len(games):
            print("[ERR] out of range")
            return None
        return str(games[idx - 1].get("gameId"))

    def show_games(self):
        while True:
            try:
                r = self.request("store_list_games")
            except Exception:
                print("[ERR] Failed to load game list (timeout/disconnect).")
                retry = input("Retry? (y/N): ").strip().lower()
                if retry in ("y", "yes"):
                    continue
                return
            self._print_resp(r)
            if not r.get("ok"):
                print("[ERR] Failed to load game list.")
                retry = input("Retry? (y/N): ").strip().lower()
                if retry in ("y", "yes"):
                    continue
                return
            games = r.get("games") or []
            if not games:
                print("No available games.")
                return
            break

        rows = []
        for g in games:
            rows.append(
                {
                    "gameId": str(g.get("gameId") or ""),
                    "name": str(g.get("name") or "(unnamed)"),
                    "latest": str(g.get("latestVersion") or "-"),
                    "type": str(g.get("clientType") or "-"),
                    "players": (
                        f"{g.get('minPlayers')}-{g.get('maxPlayers')}"
                        if g.get("minPlayers") is not None and g.get("maxPlayers") is not None
                        else "-"
                    ),
                    "author": str(g.get("developerUsername") or g.get("developerId") or "-"),
                }
            )

        def w(key: str, cap: int) -> int:
            return min(cap, max(len(key), max((len(r.get(key, "")) for r in rows), default=0)))

        w_gid = w("gameId", 24)
        w_name = w("name", 28)
        w_latest = w("latest", 12)
        w_type = w("type", 6)
        w_players = w("players", 9)
        w_author = w("author", 16)

        header = (
            f"{'#':>2}  {'gameId':<{w_gid}}  {'name':<{w_name}}  {'latest':<{w_latest}}  "
            f"{'type':<{w_type}}  {'players':<{w_players}}  {'author':<{w_author}}"
        )
        print(header)
        print("-" * len(header))
        for i, row in enumerate(rows, start=1):
            print(
                f"{i:>2}  {row['gameId']:<{w_gid}}  {row['name']:<{w_name}}  {row['latest']:<{w_latest}}  "
                f"{row['type']:<{w_type}}  {row['players']:<{w_players}}  {row['author']:<{w_author}}"
            )

    def show_game_detail(self):
        gid = self._choose_game_id(prompt="Game detail - ")
        if not gid:
            return
        r = self.request("store_game_detail", {"gameId": gid})
        self._print_resp(r)
        if not r.get("ok"):
            return
        game = r.get("game") or {}
        latest = r.get("latestVersion")
        reviews = r.get("reviews") or []
        author = game.get("developerUsername") or game.get("developerId") or "-"
        name = game.get("name") or "(unnamed)"
        print(f"Game: {game.get('gameId')} name={name} author={author}")
        desc = game.get("description")
        if not desc:
            desc = "No description provided."
        print(f"Desc: {desc}")
        if latest:
            print(f"Latest: v{latest.get('version')} type={latest.get('clientType')} players={latest.get('minPlayers')}-{latest.get('maxPlayers')}")
        else:
            print("Latest: (none)")
        print("Reviews:")
        if not reviews:
            print("(no reviews yet)")
            return
        for rv in reviews[:10]:
            comment = rv.get("comment")
            if not comment:
                comment = "-"
            print(f"  - player#{rv.get('playerId')} rating={rv.get('rating')} comment={comment}")

    def do_download(self):
        gid = self._choose_game_id(prompt="Download - ")
        if not gid:
            return
        d = self.ensure_downloaded(gid, version=None)
        print(f"Installed at: {d}")

    def do_register(self):
        u = input("username: ").strip()
        p = input("password: ").strip()
        r = self.request("player_register", {"username": u, "password": p})
        self._print_resp(r)

    def do_login(self):
        u = input("username: ").strip()
        p = input("password: ").strip()
        r = self.request("player_login", {"username": u, "password": p})
        if r.get("ok"):
            self.player_id = int(r.get("playerId"))
            self.username = str(r.get("username"))
        self._print_resp(r)

    def do_logout(self):
        r = self.request("player_logout", {})
        self.player_id = None
        self.username = None
        self.room_id = None
        self._print_resp(r)

    def do_player_list(self):
        r = self.request("player_list", {})
        self._print_resp(r)
        players = r.get("players") or []
        if not players:
            print("(no online players)")
            return
        print("Online players:")
        for p in players:
            rid = p.get("roomId")
            if rid:
                game = p.get("gameId") or "?"
                ver = p.get("version") or "?"
                room_status = p.get("roomStatus") or "?"
                status = f"room {rid} ({room_status}) game={game} v{ver}"
            else:
                status = "in lobby"
            print(f"  - #{p.get('playerId')} {p.get('username')} ({status})")

    def do_room_list(self):
        r = self.request("room_list", {})
        self._print_resp(r)
        rooms = r.get("rooms") or []
        if not rooms:
            return
        print("Rooms:")
        for rm in rooms:
            print(
                f"  - {rm.get('id')} | game={rm.get('gameId')} v{rm.get('version')} "
                f"| host={rm.get('hostPlayerId')} | status={rm.get('status')} | players={rm.get('players')}"
            )

    def do_room_create(self):
        gid = self._choose_game_id(prompt="Create room for - ")
        if not gid:
            return
        r = self.request("room_create", {"gameId": gid})
        if r.get("ok"):
            self.room_id = int(r.get("roomId"))
        self._print_resp(r)
        if r.get("ok"):
            room = self.do_room_detail(self.room_id)
            if room:
                self._ensure_installed_for_room(game_id=str(room.get("gameId")), version=str(room.get("version")))

    def do_room_join(self):
        rid = int(input("roomId: ").strip())
        r = self.request("room_join", {"roomId": rid})
        if r.get("ok"):
            self.room_id = rid
        self._print_resp(r)
        if r.get("ok"):
            room = self.do_room_detail(rid)
            if room:
                self._ensure_installed_for_room(game_id=str(room.get("gameId")), version=str(room.get("version")))

    def do_room_leave(self):
        r = self.request("room_leave", {})
        if r.get("ok"):
            self.room_id = None
        self._print_resp(r)

    def do_room_start(self):
        rid = self.room_id or int(input("roomId: ").strip())
        r = self.request("room_start", {"roomId": rid})
        self._print_resp(r)
        if (not r.get("ok")) and r.get("error") == "already_playing":
            # Helpful hint: room may be finishing; show status.
            self.do_room_detail(rid)

    def do_review(self):
        if not self.username or not self.player_id:
            print("[ERR] not_logged_in")
            return
        gid = self._choose_game_id(prompt="Review - ")
        if not gid:
            return
        if not gid:
            print("[ERR] missing gameId")
            return

        draft_path = REVIEW_DRAFTS_ROOT / self.username / f"{gid}.json"
        draft: Optional[dict] = None
        if draft_path.exists():
            try:
                draft = json.loads(draft_path.read_text(encoding="utf-8"))
            except Exception:
                draft = None

        if draft:
            use = input(f"Found saved draft for {gid}. Use it? (y/N): ").strip().lower()
            if use == "y":
                rating = int(draft.get("rating") or 0)
                comment = str(draft.get("comment") or "")
            else:
                rating = int(input("rating (1-5): ").strip())
                comment = input("comment: ").strip()
        else:
            rating = int(input("rating (1-5): ").strip())
            comment = input("comment: ").strip()

        # Best-effort: store draft before sending so failures don't lose input.
        try:
            draft_path.parent.mkdir(parents=True, exist_ok=True)
            draft_path.write_text(
                json.dumps({"gameId": gid, "rating": rating, "comment": comment}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

        r = self.request("review_create_or_update", {"gameId": gid, "rating": rating, "comment": comment})
        self._print_resp(r)
        if r.get("ok"):
            try:
                draft_path.unlink(missing_ok=True)
            except Exception:
                pass

    def do_room_detail(self, room_id: Optional[int] = None) -> Optional[dict]:
        rid = room_id or self.room_id
        if not rid:
            print("[ERR] no room selected")
            return None
        r = self.request("room_detail", {"roomId": int(rid)})
        self._print_resp(r)
        if not r.get("ok"):
            return None
        room = r.get("room") or {}
        print(
            f"Room {room.get('id')} | game={room.get('gameId')} v{room.get('version')} "
            f"| status={room.get('status')} | host={room.get('hostPlayerId')} | players={room.get('players')}"
        )
        return room

    def do_match_history(self):
        r = self.request("match_list_mine", {})
        self._print_resp(r)
        if not r.get("ok"):
            return
        logs = r.get("logs") or []
        if not logs:
            print("(no matches found)")
            return
        print("Recent matches:")
        for m in logs[:20]:
            print(
                f"  - match#{m.get('id')} game={m.get('gameId')} v{m.get('version')} "
                f"room={m.get('roomId')} endedAt={m.get('endedAt')} reason={m.get('reason')}"
            )

    def _print_resp(self, resp: dict):
        if resp.get("ok"):
            print("[OK]")
        else:
            err = resp.get("error")
            msg = ERROR_MESSAGES.get(str(err), str(err))
            print(f"[ERR] {msg}")
        if "--verbose" in sys.argv:
            print(safe_json_dumps(resp))

    def run(self):
        DOWNLOADS_ROOT.mkdir(parents=True, exist_ok=True)
        while True:
            print("\n=== Main Menu ===")
            print("1) Auth")
            print("2) Lobby status")
            print("3) Store")
            print("4) Rooms")
            print("5) Reviews")
            print("0) Quit")
            cmd = input("Choose: ").strip()
            try:
                if cmd == "1":
                    self._menu_auth()
                elif cmd == "2":
                    self._menu_lobby()
                elif cmd == "3":
                    self._menu_store()
                elif cmd == "4":
                    self._menu_rooms()
                elif cmd == "5":
                    self._menu_reviews()
                elif cmd == "0":
                    return
            except KeyboardInterrupt:
                print()
                return
            except Exception as e:
                print(f"[ERR] {e}")

    def _menu_auth(self):
        while True:
            print("\n=== Auth ===")
            print("1) Register")
            print("2) Login")
            print("3) Logout")
            print("0) Back")
            cmd = input("Choose: ").strip()
            if cmd == "1":
                self.do_register()
            elif cmd == "2":
                self.do_login()
            elif cmd == "3":
                self.do_logout()
            elif cmd == "0":
                return

    def _menu_lobby(self):
        while True:
            print("\n=== Lobby Status ===")
            print("1) List online players")
            print("0) Back")
            cmd = input("Choose: ").strip()
            if cmd == "1":
                self.do_player_list()
            elif cmd == "0":
                return

    def _menu_store(self):
        while True:
            print("\n=== Store ===")
            print("1) List games")
            print("2) Game detail")
            print("3) Download/update game")
            print("0) Back")
            cmd = input("Choose: ").strip()
            if cmd == "1":
                self.show_games()
            elif cmd == "2":
                self.show_game_detail()
            elif cmd == "3":
                self.do_download()
            elif cmd == "0":
                return

    def _menu_rooms(self):
        while True:
            print("\n=== Rooms ===")
            print("1) List rooms")
            print("2) Create room")
            print("3) Join room")
            print("4) Leave room")
            print("5) Start match (host)")
            print("0) Back")
            cmd = input("Choose: ").strip()
            if cmd == "1":
                self.do_room_list()
            elif cmd == "2":
                self.do_room_create()
            elif cmd == "3":
                self.do_room_join()
            elif cmd == "4":
                self.do_room_leave()
            elif cmd == "5":
                self.do_room_start()
            elif cmd == "0":
                return

    def _menu_reviews(self):
        while True:
            print("\n=== Reviews ===")
            print("1) Rate/comment a game")
            print("2) My match history")
            print("0) Back")
            cmd = input("Choose: ").strip()
            if cmd == "1":
                self.do_review()
            elif cmd == "2":
                self.do_match_history()
            elif cmd == "0":
                return


def main():
    host = sys.argv[1] if len(sys.argv) >= 2 else DEFAULT_HOST
    port = int(sys.argv[2]) if len(sys.argv) >= 3 else DEFAULT_PORT
    c = LobbyClient(host, port)
    c.connect()
    try:
        c.run()
    finally:
        c.close()


if __name__ == "__main__":
    main()
