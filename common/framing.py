"""
Length-prefixed framing helpers (TCP).

Wire format:
  [4-byte length (uint32, network byte order)] [payload bytes]

Constraints:
  - 0 < length <= 64 KiB
"""

from __future__ import annotations

import asyncio
import json
import socket
import struct
from typing import Any, Optional


HDR = struct.Struct("!I")
MAX_FRAME = 64 * 1024


class FramingError(Exception):
    pass


# ---------------------------
# asyncio StreamReader/Writer
# ---------------------------
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


# ---------------------------
# blocking socket helpers
# ---------------------------
def send_frame_sync(sock: socket.socket, payload: bytes) -> None:
    if not payload or len(payload) > MAX_FRAME:
        raise FramingError("bad frame size")
    sock.sendall(HDR.pack(len(payload)))
    sock.sendall(payload)


def _recv_exact_sync(sock: socket.socket, n: int) -> Optional[bytes]:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def recv_frame_sync(sock: socket.socket) -> Optional[bytes]:
    hdr = _recv_exact_sync(sock, HDR.size)
    if hdr is None:
        return None
    (length,) = HDR.unpack(hdr)
    if length == 0 or length > MAX_FRAME:
        raise FramingError("bad length")
    return _recv_exact_sync(sock, length)


def send_json_sync(sock: socket.socket, obj: dict) -> None:
    send_frame_sync(sock, json.dumps(obj, separators=(",", ":")).encode("utf-8"))


def recv_json_sync(sock: socket.socket) -> Optional[dict]:
    frame = recv_frame_sync(sock)
    if frame is None:
        return None
    return json.loads(frame.decode("utf-8"))


def safe_json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True)

