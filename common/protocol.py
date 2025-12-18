"""
Shared protocol conventions for HW3.

This module intentionally stays small: it defines message shapes and constants
used by server and client code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional, TypedDict


Role = Literal["developer", "player"]


class Request(TypedDict, total=False):
    type: str
    data: dict


class Response(TypedDict, total=False):
    ok: bool
    error: str
    data: dict


class Event(TypedDict, total=False):
    type: Literal["event"]
    name: str
    data: dict


@dataclass(frozen=True)
class ServerPorts:
    db: int = 10101
    developer: int = 10102
    lobby: int = 10103


MAX_NAME_LEN = 32
MAX_DESC_LEN = 500

