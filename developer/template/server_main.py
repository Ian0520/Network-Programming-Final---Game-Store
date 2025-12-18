#!/usr/bin/env python3
import argparse
import asyncio
import contextlib
import os

from framing import recv_json, send_json


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--token", type=str, required=True)
    ap.add_argument("--room-id", type=int, required=True)
    return ap.parse_args()


async def lobby_post_result(room_id: int):
    host = os.environ.get("HW3_LOBBY_HOST", "127.0.0.1")
    port = int(os.environ.get("HW3_LOBBY_PORT", "10103"))
    try:
        r, w = await asyncio.open_connection(host, port)
        await send_json(w, {"type": "post_result", "data": {"roomId": room_id, "reason": "finished", "results": []}})
        with contextlib.suppress(Exception):
            w.close()
            await w.wait_closed()
    except Exception:
        pass


async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, token: str):
    hello = await recv_json(reader)
    if not hello or hello.get("type") != "HELLO" or (hello.get("token") != token):
        await send_json(writer, {"type": "BYE", "reason": "bad_token"})
        writer.close()
        return
    await send_json(writer, {"type": "WELCOME"})
    msg = await recv_json(reader)
    await send_json(writer, {"type": "ECHO", "data": msg})
    writer.close()


async def main():
    args = parse_args()
    server = await asyncio.start_server(lambda r, w: handle_client(r, w, args.token), "0.0.0.0", args.port)
    print(f"[__GAME_ID__] listen 0.0.0.0:{args.port} room={args.room_id}")
    async with server:
        await asyncio.sleep(5)
    await lobby_post_result(args.room_id)


if __name__ == "__main__":
    asyncio.run(main())

