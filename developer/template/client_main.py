#!/usr/bin/env python3
import argparse
import asyncio

from framing import recv_json, send_json


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", type=str, required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--token", type=str, required=True)
    ap.add_argument("--room-id", type=int, required=True)
    ap.add_argument("--user", type=int, required=True)
    return ap.parse_args()


async def main():
    args = parse_args()
    r, w = await asyncio.open_connection(args.host, args.port)
    await send_json(w, {"type": "HELLO", "roomId": args.room_id, "userId": args.user, "token": args.token})
    print(await recv_json(r))
    await send_json(w, {"type": "MSG", "data": {"hello": "world"}})
    print(await recv_json(r))
    w.close()
    await w.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())

