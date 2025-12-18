#!/usr/bin/env python3
"""
Bomb Pass (2-3 players) game server for HW3.

Rules:
- One player holds the bomb.
- Bomb explodes after a random timer window; the holder at explosion is eliminated.
- Remaining players continue until one winner remains.

Server posts result back to HW3 Lobby via `post_result`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import random
import time
from dataclasses import dataclass
from typing import Dict, Optional, Set, Tuple

from framing import recv_json, send_json


def now_ts() -> int:
    return int(time.time())


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--token", type=str, required=True)
    ap.add_argument("--room-id", type=int, required=True)
    return ap.parse_args()


LOBBY_HOST = os.environ.get("HW3_LOBBY_HOST", "127.0.0.1")
LOBBY_PORT = int(os.environ.get("HW3_LOBBY_PORT", "10103"))

BOMB_MIN_SEC = float(os.environ.get("BOMB_PASS_MIN_SEC", "3.0"))
BOMB_MAX_SEC = float(os.environ.get("BOMB_PASS_MAX_SEC", "6.0"))
WAIT_ALL_SEC = float(os.environ.get("BOMB_PASS_WAIT_ALL_SEC", "15.0"))


async def lobby_post_result(room_id: int, *, started_at: int, ended_at: int, winner: Optional[int], reason: str, results: list):
    payload = {
        "type": "post_result",
        "data": {
            "roomId": room_id,
            "startedAt": started_at,
            "endedAt": ended_at,
            "winner": winner,
            "reason": reason,
            "results": results,
        },
    }
    try:
        _r, w = await asyncio.wait_for(asyncio.open_connection(LOBBY_HOST, LOBBY_PORT), timeout=1.0)
        try:
            await asyncio.wait_for(send_json(w, payload), timeout=1.0)
        finally:
            with contextlib.suppress(Exception):
                w.close()
                await w.wait_closed()
    except Exception:
        pass


@dataclass
class ClientConn:
    user_id: int
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter


class BombPassServer:
    def __init__(self, port: int, room_id: int, token: str):
        self.port = port
        self.room_id = room_id
        self.token = token

        self.server: Optional[asyncio.base_events.Server] = None

        self.clients: Dict[int, ClientConn] = {}
        self.alive_players: Set[int] = set()
        self.holder: Optional[int] = None
        self.started_at: Optional[int] = None
        self.ended_at: Optional[int] = None
        self.bomb_task: Optional[asyncio.Task] = None
        self.game_over = False
        self.stop_event = asyncio.Event()
        self.boot_monotonic = time.monotonic()
        self.expected_players = max(2, int(os.environ.get("HW3_EXPECTED_PLAYERS", "2") or 2))

        self.pass_count: Dict[int, int] = {}
        self.eliminated_order: list[int] = []
        self.last_client_seen = time.monotonic()

    async def broadcast(self, msg: dict):
        for uid, c in list(self.clients.items()):
            with contextlib.suppress(Exception):
                await send_json(c.writer, msg)

    async def send_to(self, uid: int, msg: dict):
        c = self.clients.get(uid)
        if not c:
            return
        with contextlib.suppress(Exception):
            await send_json(c.writer, msg)

    def _choose_new_holder(self) -> Optional[int]:
        if not self.alive_players:
            return None
        return random.choice(sorted(self.alive_players))

    async def _arm_bomb(self):
        delay = random.uniform(BOMB_MIN_SEC, BOMB_MAX_SEC)
        await self.broadcast({"type": "BOMB_ARMED", "seconds": delay})
        await asyncio.sleep(delay)
        if self.game_over:
            return
        holder = self.holder
        if holder is None or holder not in self.alive_players:
            holder = self._choose_new_holder()
            self.holder = holder
        if holder is None:
            await self.finish(reason="no_players")
            return
        await self._explode(holder)

    async def _explode(self, victim: int):
        if victim in self.alive_players:
            self.alive_players.remove(victim)
            self.eliminated_order.append(victim)
        await self.broadcast({"type": "EXPLODE", "victim": victim})
        self.holder = self._choose_new_holder()
        await self.broadcast({"type": "STATE", "players": sorted(self.alive_players), "holder": self.holder})

        if len(self.alive_players) <= 1:
            await self.finish(reason="finished")
            return
        # Re-arm for next round
        self.bomb_task = asyncio.create_task(self._arm_bomb())

    async def finish(self, *, reason: str):
        if self.game_over:
            return
        self.game_over = True
        # Ensure the server process will exit even if notifications block.
        self.stop_event.set()
        if self.bomb_task and not self.bomb_task.done():
            self.bomb_task.cancel()
        self.ended_at = now_ts()
        winner = next(iter(self.alive_players), None)
        results = []
        for uid in sorted(self.clients.keys()):
            results.append(
                {
                    "userId": uid,
                    "passes": int(self.pass_count.get(uid, 0)),
                    "eliminated": uid in self.eliminated_order,
                }
            )
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.broadcast({"type": "GAME_OVER", "winner": winner, "reason": reason}), timeout=1.0)
        with contextlib.suppress(Exception):
            await lobby_post_result(
                self.room_id,
                started_at=self.started_at or self.ended_at or now_ts(),
                ended_at=self.ended_at or now_ts(),
                winner=winner,
                reason=reason,
                results=results,
            )
        # Best-effort close connections; do not block shutdown.
        for _uid, c in list(self.clients.items()):
            with contextlib.suppress(Exception):
                c.writer.close()

    async def start_game_if_ready(self):
        if self.started_at is not None:
            return
        # Prefer waiting for all expected players (from lobby), but fall back after a grace window.
        if len(self.alive_players) < self.expected_players:
            if len(self.alive_players) < 2:
                return
            if (time.monotonic() - self.boot_monotonic) < WAIT_ALL_SEC:
                return
        self.started_at = now_ts()
        self.holder = self._choose_new_holder()
        await self.broadcast({"type": "START", "roomId": self.room_id})
        await self.broadcast({"type": "STATE", "players": sorted(self.alive_players), "holder": self.holder})
        self.bomb_task = asyncio.create_task(self._arm_bomb())

    async def _watchdog(self):
        """
        Prevent stuck matches:
        - If a game never reaches start (not enough clients connect) and everyone disconnects, stop.
        - If game started and players drop below 2, finish.
        """
        while not self.game_over:
            await asyncio.sleep(0.5)
            if self.started_at is None:
                # Re-check start conditions (handles the "wait for 3 players" grace window).
                with contextlib.suppress(Exception):
                    await self.start_game_if_ready()
                # Give clients a grace window to connect after spawn.
                if (time.monotonic() - self.boot_monotonic) < 3.0:
                    continue
                if len(self.alive_players) == 0 and len(self.clients) == 0:
                    await self.finish(reason="no_players")
                    return
            else:
                # If everyone closed the game client windows, end the match so the lobby can restart.
                if len(self.clients) == 0 and (time.monotonic() - self.last_client_seen) > 1.0:
                    await self.finish(reason="no_clients")
                    return
                if len(self.alive_players) <= 1:
                    await self.finish(reason="disconnect")
                    return

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        user_id: Optional[int] = None
        try:
            hello = await recv_json(reader)
            if not hello or hello.get("type") != "HELLO":
                await send_json(writer, {"type": "BYE", "reason": "expected_hello"})
                return
            if str(hello.get("token") or "") != self.token:
                await send_json(writer, {"type": "BYE", "reason": "bad_token"})
                return
            user_id = int(hello.get("userId") or 0)
            if user_id <= 0:
                await send_json(writer, {"type": "BYE", "reason": "bad_user"})
                return

            if user_id in self.clients:
                await send_json(writer, {"type": "BYE", "reason": "already_connected"})
                return

            self.clients[user_id] = ClientConn(user_id=user_id, reader=reader, writer=writer)
            self.alive_players.add(user_id)
            self.pass_count.setdefault(user_id, 0)
            self.last_client_seen = time.monotonic()

            await send_json(writer, {"type": "WELCOME", "roomId": self.room_id, "userId": user_id})
            await self.broadcast({"type": "PLAYER_JOINED", "userId": user_id})
            # If the game already started (e.g. other players connected first),
            # sync this late-connecting player to current state.
            if self.started_at is not None:
                await send_json(writer, {"type": "START", "roomId": self.room_id})
                await send_json(writer, {"type": "STATE", "players": sorted(self.alive_players), "holder": self.holder})
                await self.broadcast({"type": "STATE", "players": sorted(self.alive_players), "holder": self.holder})
            await self.start_game_if_ready()

            while not self.game_over:
                msg = await recv_json(reader)
                if msg is None:
                    break
                typ = msg.get("type")
                if typ == "PASS":
                    target = int(msg.get("target") or 0)
                    if user_id != self.holder:
                        await self.send_to(user_id, {"type": "ERR", "error": "not_holder"})
                        continue
                    if target not in self.alive_players:
                        await self.send_to(user_id, {"type": "ERR", "error": "bad_target"})
                        continue
                    if target == user_id:
                        await self.send_to(user_id, {"type": "ERR", "error": "cannot_pass_to_self"})
                        continue
                    self.holder = target
                    self.pass_count[user_id] = int(self.pass_count.get(user_id, 0)) + 1
                    await self.broadcast({"type": "PASSED", "from": user_id, "to": target})
                    await self.broadcast({"type": "STATE", "players": sorted(self.alive_players), "holder": self.holder})
                    self.last_client_seen = time.monotonic()
                elif typ == "PING":
                    await send_json(writer, {"type": "PONG"})
        except Exception:
            pass
        finally:
            if user_id is not None:
                self.clients.pop(user_id, None)
                if user_id in self.alive_players:
                    self.alive_players.remove(user_id)
                    self.eliminated_order.append(user_id)
                    await self.broadcast({"type": "PLAYER_LEFT", "userId": user_id})
                    if not self.game_over:
                        # If a match is running and fewer than 2 remain, finish.
                        if len(self.alive_players) <= 1 and self.started_at is not None:
                            await self.finish(reason="disconnect")
                if not self.clients:
                    self.last_client_seen = time.monotonic()
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def run(self):
        self.server = await asyncio.start_server(self.handle_client, "0.0.0.0", self.port)
        asyncio.create_task(self._watchdog())
        async with self.server:
            await self.stop_event.wait()
        # ensure closed
        self.server.close()
        await self.server.wait_closed()


async def amain():
    args = parse_args()
    srv = BombPassServer(args.port, args.room_id, args.token)
    await srv.run()


if __name__ == "__main__":
    asyncio.run(amain())
