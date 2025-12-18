from __future__ import annotations

import asyncio
import json
import struct
from typing import Optional


HDR = struct.Struct("!I")
MAX_FRAME = 64 * 1024


class FramingError(Exception):
    pass


async def send_frame(writer: asyncio.StreamWriter, payload: bytes) -> None:
    if not payload or len(payload) > MAX_FRAME:
        raise FramingError("bad frame size")
    writer.write(HDR.pack(len(payload)))
    writer.write(payload)
    await writer.drain()


async def _read_exact(reader: asyncio.StreamReader, n: int) -> Optional[bytes]:
    buf = bytearray()
    while len(buf) < n:
        chunk = await reader.read(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


async def recv_frame(reader: asyncio.StreamReader) -> Optional[bytes]:
    hdr = await _read_exact(reader, HDR.size)
    if hdr is None:
        return None
    (length,) = HDR.unpack(hdr)
    if length == 0 or length > MAX_FRAME:
        raise FramingError("bad length")
    return await _read_exact(reader, length)


async def send_json(writer: asyncio.StreamWriter, obj: dict) -> None:
    await send_frame(writer, json.dumps(obj, separators=(",", ":")).encode("utf-8"))


async def recv_json(reader: asyncio.StreamReader) -> Optional[dict]:
    frame = await recv_frame(reader)
    if frame is None:
        return None
    return json.loads(frame.decode("utf-8"))

