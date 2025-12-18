#!/usr/bin/env python3
"""
Bomb Pass CLI client (2-3 players).

Controls:
- type a target userId and press enter to pass the bomb (only works if you hold it)
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
from typing import Optional

from framing import recv_json, send_json


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--room-id", type=int, required=True)
    ap.add_argument("--user", type=int, required=True)
    return ap.parse_args()


async def ainput(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


async def main():
    args = parse_args()
    reader, writer = await asyncio.open_connection(args.host, args.port)
    await send_json(writer, {"type": "HELLO", "roomId": args.room_id, "userId": args.user, "token": args.token})
    msg = await recv_json(reader)
    if not msg or msg.get("type") != "WELCOME":
        print("Failed to join:", msg)
        return
    print("Joined Bomb Pass. Waiting for others...")

    holder: Optional[int] = None
    players: list[int] = []
    game_over = False

    async def reader_loop():
        nonlocal holder, players, game_over
        while True:
            m = await recv_json(reader)
            if m is None:
                game_over = True
                return
            t = m.get("type")
            if t == "START":
                print("Game started! (2-3 players)")
            elif t == "STATE":
                players = list(m.get("players") or [])
                holder = m.get("holder")
                print(f"[STATE] players={players} holder={holder}")
            elif t == "BOMB_ARMED":
                print(f"[BOMB] armed (next explosion in ~{float(m.get('seconds') or 0):.1f}s)")
            elif t == "PASSED":
                print(f"[PASS] {m.get('from')} -> {m.get('to')}")
            elif t == "EXPLODE":
                print(f"[BOOM] victim={m.get('victim')}")
            elif t == "PLAYER_JOINED":
                print(f"[JOIN] user={m.get('userId')}")
            elif t == "PLAYER_LEFT":
                print(f"[LEFT] user={m.get('userId')}")
            elif t == "GAME_OVER":
                print(f"[GAME_OVER] winner={m.get('winner')} reason={m.get('reason')}")
                game_over = True
                return
            elif t == "ERR":
                print(f"[ERR] {m.get('error')}")

    async def input_loop():
        nonlocal game_over
        while not game_over:
            if holder != args.user:
                await asyncio.sleep(0.2)
                continue
            s = (await ainput("You have the bomb! Pass to userId: ")).strip()
            if not s:
                continue
            try:
                target = int(s)
            except Exception:
                print("Please enter a numeric userId.")
                continue
            await send_json(writer, {"type": "PASS", "target": target})

    t1 = asyncio.create_task(reader_loop())
    t2 = asyncio.create_task(input_loop())
    await t1
    t2.cancel()
    with contextlib.suppress(Exception):
        writer.close()
        await writer.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
