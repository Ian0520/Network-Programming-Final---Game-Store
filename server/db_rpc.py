from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import Any, Dict

from hw3.common.config import get_int, get_str, section
from hw3.common.framing import recv_frame, send_frame


_CFG_DB = section("db")
DB_HOST = (os.environ.get("NP_HW3_DB_HOST") or get_str(_CFG_DB, "host") or "127.0.0.1")
DB_PORT = int(os.environ.get("NP_HW3_DB_PORT") or get_int(_CFG_DB, "port") or 10101)


async def db_call(payload: Dict[str, Any]) -> Dict[str, Any]:
    reader, writer = await asyncio.open_connection(DB_HOST, DB_PORT)
    try:
        await send_frame(writer, json.dumps(payload, separators=(",", ":")).encode("utf-8"))
        data = await recv_frame(reader)
        if not data:
            return {"status": "ERR", "error": "db_no_response"}
        return json.loads(data.decode("utf-8"))
    except Exception as e:
        return {"status": "ERR", "error": f"db_error:{e}"}
    finally:
        with contextlib.suppress(Exception):
            writer.close()
            await writer.wait_closed()
