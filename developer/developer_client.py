#!/usr/bin/env python3
"""
Menu-driven Developer Client (HW3).

Connects to Developer Server and supports:
  - register/login/logout
  - list my games
  - upload a game version (zip, chunked)
  - delist/relist a game
"""

from __future__ import annotations

import base64
import hashlib
import os
import socket
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Optional, Tuple

from hw3.common.config import get_int, get_str, section
from hw3.common.framing import recv_json_sync, send_json_sync, safe_json_dumps
from hw3.common.manifest import load_manifest_from_dir


_CFG_DEV = section("developerServer")
DEFAULT_HOST = (os.environ.get("NP_HW3_DEV_HOST") or get_str(_CFG_DEV, "host") or "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("NP_HW3_DEV_PORT") or get_int(_CFG_DEV, "port") or 10102)

RAW_CHUNK = 32 * 1024

ERROR_MESSAGES = {
    "missing_fields": "Missing required fields.",
    "username_exists": "Username already in use.",
    "bad_credentials": "Wrong username or password.",
    "already_online": "This account is already logged in elsewhere.",
    "not_logged_in": "Please login first.",
    "not_owner": "You don't have permission to modify this game.",
    "game_exists": "A game with this gameId already exists.",
    "version_exists": "This version already exists for the game.",
    "game_in_progress": "Cannot delist while a match is currently in progress for this game.",
    "bad_manifest": "Invalid game package (manifest).",
    "bad_manifest_json": "Invalid manifest.json (not valid JSON).",
    "missing_manifest": "Missing manifest.json in the uploaded package.",
    "manifest_gameId_mismatch": "manifest.json gameId does not match the upload gameId.",
    "manifest_version_mismatch": "manifest.json version does not match the upload version.",
    "hash_mismatch": "Upload corrupted (sha256 mismatch).",
    "size_mismatch": "Upload corrupted (size mismatch).",
    "bad_game_id": "Invalid gameId (use only letters/numbers/_/-).",
    "bad_version": "Invalid version string.",
}


def sha256_file(path: Path) -> Tuple[str, int]:
    h = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        while True:
            b = f.read(64 * 1024)
            if not b:
                break
            h.update(b)
            size += len(b)
    return h.hexdigest(), size


def zip_dir(src_dir: Path, out_zip: Path) -> None:
    with zipfile.ZipFile(out_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for p in src_dir.rglob("*"):
            if p.is_dir():
                continue
            rel = p.relative_to(src_dir)
            zf.write(p, rel.as_posix())


class DevClient:
    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sock: Optional[socket.socket] = None
        self.developer_id: Optional[int] = None
        self.username: Optional[str] = None

    def connect(self):
        self.sock = socket.create_connection((self.host, self.port))
        print(f"[DevClient] connected to {self.host}:{self.port}")

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None

    def req(self, typ: str, data: dict | None = None) -> dict:
        assert self.sock
        send_json_sync(self.sock, {"type": typ, "data": (data or {})})
        resp = recv_json_sync(self.sock)
        if resp is None:
            raise RuntimeError("server_closed")
        return resp

    def _print_resp(self, resp: dict):
        if resp.get("ok"):
            print("[OK]")
        else:
            err = resp.get("error")
            msg = ERROR_MESSAGES.get(str(err), str(err))
            print(f"[ERR] {msg}")
        if "--verbose" in sys.argv:
            print(safe_json_dumps(resp))

    def do_register(self):
        u = input("username: ").strip()
        p = input("password: ").strip()
        r = self.req("dev_register", {"username": u, "password": p})
        self._print_resp(r)

    def do_login(self):
        u = input("username: ").strip()
        p = input("password: ").strip()
        r = self.req("dev_login", {"username": u, "password": p})
        if r.get("ok"):
            self.developer_id = int(r.get("developerId"))
            self.username = r.get("username")
        self._print_resp(r)

    def do_logout(self):
        r = self.req("dev_logout", {})
        self.developer_id = None
        self.username = None
        self._print_resp(r)

    def _choose_my_game_id(self, *, prompt: str, delisted: Optional[bool] = None) -> Optional[str]:
        r = self.req("game_list_mine", {})
        if not r.get("ok"):
            self._print_resp(r)
            return None
        games = r.get("games") or []
        if delisted is not None:
            games = [g for g in games if bool(g.get("delisted")) == delisted]
        if not games:
            if delisted is True:
                print("(no delisted games)")
            elif delisted is False:
                print("(no listed games)")
            else:
                print("(no games)")
            return None
        print(f"\n{prompt}")
        for i, g in enumerate(games, 1):
            latest = g.get("latestVersion") or "-"
            delisted = "delisted" if bool(g.get("delisted")) else "listed"
            print(f"{i}) {g.get('name')} (gameId={g.get('gameId')}, {delisted}, latest={latest})")
        while True:
            choice = input(f"Choose (1-{len(games)}, 0 to cancel): ").strip()
            if choice == "0":
                return None
            try:
                idx = int(choice)
            except ValueError:
                print("[ERR] invalid choice")
                continue
            if 1 <= idx <= len(games):
                gid = str(games[idx - 1].get("gameId") or "").strip()
                if gid:
                    return gid
            print("[ERR] invalid choice")

    def do_list_mine(self):
        r = self.req("game_list_mine", {})
        self._print_resp(r)
        games = r.get("games") or []
        if not games:
            return
        print("My games:")
        for g in games:
            latest = g.get("latestVersion")
            extra = f" latest=v{latest}" if latest else " latest=(none)"
            players = ""
            if g.get("minPlayers") is not None and g.get("maxPlayers") is not None:
                players = f" players={g.get('minPlayers')}-{g.get('maxPlayers')}"
            ctype = f" type={g.get('clientType')}" if g.get("clientType") else ""
            print(
                f"  - {g.get('gameId')} name={g.get('name')} delisted={bool(g.get('delisted'))}"
                f"{extra}{ctype}{players}"
            )

    def do_delist(self, delisted: bool):
        if delisted:
            game_id = self._choose_my_game_id(prompt="Choose a game to delist", delisted=False)
        else:
            game_id = self._choose_my_game_id(prompt="Choose a game to relist", delisted=True)
        if not game_id:
            return
        if delisted:
            print("\nDelisting impact:")
            print("- Players will no longer be able to download this game.")
            print("- Players will not be able to create new rooms for it.")
            confirm = input("Confirm delist? (y/N): ").strip().lower()
            if confirm not in ("y", "yes"):
                print("(cancelled)")
                return
        else:
            confirm = input("Confirm relist? (y/N): ").strip().lower()
            if confirm not in ("y", "yes"):
                print("(cancelled)")
                return
        r = self.req("game_delist", {"gameId": game_id, "delisted": delisted})
        self._print_resp(r)
        if r.get("ok"):
            if delisted:
                print(f"[DevClient] delisted gameId={game_id}")
            else:
                print(f"[DevClient] relisted gameId={game_id}")

    def do_list_versions(self):
        game_id = self._choose_my_game_id(prompt="Choose a game to list versions")
        if not game_id:
            return
        r = self.req("game_list_versions", {"gameId": game_id})
        self._print_resp(r)
        if not r.get("ok"):
            return
        versions = r.get("versions") or []
        if not versions:
            print("(no versions)")
            return
        print(f"Versions for {game_id}:")
        for v in versions:
            print(
                f"  - v{v.get('version')} uploadedAt={v.get('uploadedAt')} "
                f"type={v.get('clientType')} players={v.get('minPlayers')}-{v.get('maxPlayers')}"
            )

    def _choose_local_game_dir(self) -> Optional[Path]:
        base_games_dir = Path(__file__).resolve().parent / "games"
        if not base_games_dir.exists():
            print("[ERR] missing developer/games/ directory")
            return None

        candidates: list[Path] = []
        for p in sorted(base_games_dir.iterdir(), key=lambda x: x.name):
            if not p.is_dir():
                continue
            if p.name.startswith(".") or p.name == "__pycache__":
                continue
            if not (p / "manifest.json").exists():
                continue
            candidates.append(p)

        if not candidates:
            print("[ERR] no game folders found under developer/games/ (missing manifest.json)")
            return None

        print("\nChoose a local game folder to upload (from developer/games/):")
        for i, p in enumerate(candidates, 1):
            m, _err, _raw = load_manifest_from_dir(p)
            if m:
                print(f"{i}) {p.name}  (name={m.name}, version={m.version})")
            else:
                print(f"{i}) {p.name}  (invalid manifest)")
        print("0) Cancel")

        while True:
            choice = input(f"Choose (0-{len(candidates)}): ").strip()
            if choice == "0":
                return None
            try:
                idx = int(choice)
            except ValueError:
                print("[ERR] invalid choice")
                continue
            if 1 <= idx <= len(candidates):
                return candidates[idx - 1]
            print("[ERR] invalid choice")

    def do_upload(self):
        src_dir = self._choose_local_game_dir()
        if not src_dir:
            return
        changelog = input("changelog (optional): ").strip()
        if not src_dir.exists() or not src_dir.is_dir():
            print("[ERR] invalid directory")
            return
        root = src_dir
        manifest, err, _raw = load_manifest_from_dir(root)
        if not manifest:
            print(f"[ERR] {err or 'bad_manifest'}")
            return

        game_id = str(manifest.gameId or "").strip()
        if not game_id:
            print("[ERR] missing gameId in manifest.json")
            return
        version = manifest.version

        with tempfile.TemporaryDirectory() as td:
            zip_base = (game_id or manifest.gameId or "game").strip() or "game"
            zip_path = Path(td) / f"{zip_base}-{version}.zip"
            zip_dir(src_dir, zip_path)
            digest, size = sha256_file(zip_path)

            init_data = {
                "gameId": game_id,
                "version": version,
                "fileName": zip_path.name,
                "sizeBytes": size,
                "sha256": digest,
                # Required if the server needs to create the game entry.
                "name": manifest.name,
                "description": manifest.description,
                "clientType": manifest.clientType,
                "minPlayers": manifest.minPlayers,
                "maxPlayers": manifest.maxPlayers,
            }

            init = self.req(
                "game_upload_init",
                init_data,
            )
            if not init.get("ok"):
                self._print_resp(init)
                return
            upload_id = init.get("uploadId")
            assigned_game_id = init.get("gameId") or game_id
            if bool(init.get("created")):
                print(f"[DevClient] created gameId: {assigned_game_id}")
            else:
                print(f"[DevClient] uploading new version for gameId: {assigned_game_id}")

            seq = 0
            sent = 0
            with zip_path.open("rb") as f:
                while True:
                    chunk = f.read(RAW_CHUNK)
                    if not chunk:
                        break
                    b64 = base64.b64encode(chunk).decode("ascii")
                    r = self.req("game_upload_chunk", {"uploadId": upload_id, "seq": seq, "dataB64": b64})
                    if not r.get("ok"):
                        self._print_resp(r)
                        return
                    seq += 1
                    sent += len(chunk)
                    print(f"\rUploading... {sent}/{size} bytes", end="")
            print()
            fin = self.req("game_upload_finish", {"uploadId": upload_id, "changelog": changelog})
            self._print_resp(fin)

    def run(self):
        while True:
            print("\n=== Main Menu ===")
            print("1) Auth")
            print("2) Games")
            print("3) Updates/Uploads")
            print("0) Quit")
            cmd = input("Choose: ").strip()
            try:
                if cmd == "1":
                    self._menu_auth()
                elif cmd == "2":
                    self._menu_games()
                elif cmd == "3":
                    self._menu_uploads()
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

    def _menu_games(self):
        while True:
            print("\n=== Games ===")
            print("1) List my games")
            print("2) List versions for a game")
            print("3) Delist game")
            print("4) Relist game")
            print("0) Back")
            cmd = input("Choose: ").strip()
            if cmd == "1":
                self.do_list_mine()
            elif cmd == "2":
                self.do_list_versions()
            elif cmd == "3":
                self.do_delist(True)
            elif cmd == "4":
                self.do_delist(False)
            elif cmd == "0":
                return

    def _menu_uploads(self):
        while True:
            print("\n=== Updates/Uploads ===")
            print("Tip: Place your game folder under `developer/games/` (must contain `manifest.json`).")
            print("1) Upload game folder (create/update by manifest gameId)")
            print("0) Back")
            cmd = input("Choose: ").strip()
            if cmd == "1":
                self.do_upload()
            elif cmd == "0":
                return


def main():
    host = sys.argv[1] if len(sys.argv) >= 2 else DEFAULT_HOST
    port = int(sys.argv[2]) if len(sys.argv) >= 3 else DEFAULT_PORT
    c = DevClient(host, port)
    c.connect()
    try:
        c.run()
    finally:
        c.close()


if __name__ == "__main__":
    main()
