#!/usr/bin/env python3
"""
Bomb Pass pygame client (2-3 players).

This client intentionally avoids asyncio to prevent event loop shutdown warnings
when users close the window.

Requires: pygame installed.
"""

from __future__ import annotations

import argparse
import json
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import List, Optional

import pygame


HDR = struct.Struct("!I")
MAX_FRAME = 64 * 1024


def send_frame(sock: socket.socket, payload: bytes) -> None:
    if not payload or len(payload) > MAX_FRAME:
        return
    sock.sendall(HDR.pack(len(payload)) + payload)


def recv_exact(sock: socket.socket, n: int) -> Optional[bytes]:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf.extend(chunk)
    return bytes(buf)


def recv_frame(sock: socket.socket) -> Optional[bytes]:
    hdr = recv_exact(sock, HDR.size)
    if not hdr:
        return None
    (ln,) = HDR.unpack(hdr)
    if ln <= 0 or ln > MAX_FRAME:
        return None
    return recv_exact(sock, ln)


def send_json(sock: socket.socket, obj: dict) -> None:
    send_frame(sock, json.dumps(obj, separators=(",", ":")).encode("utf-8"))


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--room-id", type=int, required=True)
    ap.add_argument("--user", type=int, required=True)
    return ap.parse_args()


@dataclass
class UiState:
    players: List[int]
    holder: Optional[int]
    status: str
    last_event: str
    game_over: bool
    winner: Optional[int]


class NetClient:
    def __init__(self, host: str, port: int, token: str, room_id: int, user_id: int, state: UiState):
        self.host = host
        self.port = port
        self.token = token
        self.room_id = room_id
        self.user_id = user_id
        self.state = state

        self.sock: Optional[socket.socket] = None
        self.alive = False
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.lock = threading.Lock()

    def start(self):
        self.alive = True
        self.thread.start()

    def close(self):
        self.alive = False
        with self.lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None
        # Join briefly to let the thread exit cleanly.
        self.thread.join(timeout=1.0)

    def send_pass(self, target: int):
        with self.lock:
            if not self.sock:
                return
            try:
                send_json(self.sock, {"type": "PASS", "target": int(target)})
            except Exception:
                pass

    def _loop(self):
        try:
            self.sock = socket.create_connection((self.host, int(self.port)), timeout=5)
            # Use a short timeout so we can exit promptly on window close, but treat
            # timeouts as "no data yet" (bomb timer can be several seconds).
            self.sock.settimeout(0.5)
            self.state.status = "joined"
            send_json(self.sock, {"type": "HELLO", "roomId": self.room_id, "userId": self.user_id, "token": self.token})

            while self.alive and self.sock:
                try:
                    frame = recv_frame(self.sock)
                except socket.timeout:
                    continue
                if not frame:
                    break
                msg = json.loads(frame.decode("utf-8"))
                t = msg.get("type")
                if t == "WELCOME":
                    self.state.status = "connected"
                elif t == "START":
                    self.state.status = "started"
                elif t == "STATE":
                    self.state.players = list(msg.get("players") or [])
                    self.state.holder = msg.get("holder")
                elif t == "BOMB_ARMED":
                    self.state.last_event = f"bomb armed (~{float(msg.get('seconds') or 0):.1f}s)"
                elif t == "PASSED":
                    self.state.last_event = f"{msg.get('from')} -> {msg.get('to')}"
                elif t == "EXPLODE":
                    self.state.last_event = f"BOOM! victim {msg.get('victim')}"
                elif t == "PLAYER_JOINED":
                    self.state.last_event = f"player joined {msg.get('userId')}"
                elif t == "PLAYER_LEFT":
                    self.state.last_event = f"player left {msg.get('userId')}"
                elif t == "GAME_OVER":
                    self.state.winner = msg.get("winner")
                    self.state.last_event = f"game over ({msg.get('reason')})"
                    self.state.game_over = True
                    break
                elif t == "ERR":
                    self.state.last_event = f"error: {msg.get('error')}"
        except Exception:
            pass
        finally:
            self.state.status = "disconnected"
            with self.lock:
                if self.sock:
                    try:
                        self.sock.close()
                    except Exception:
                        pass
                    self.sock = None
            self.alive = False


def main():
    args = parse_args()

    pygame.init()
    screen = pygame.display.set_mode((520, 360))
    pygame.display.set_caption("Bomb Pass (HW3)")
    font = pygame.font.SysFont(None, 28)
    font_small = pygame.font.SysFont(None, 22)

    state = UiState(players=[], holder=None, status="connecting", last_event="", game_over=False, winner=None)
    net = NetClient(args.host, args.port, args.token, args.room_id, args.user, state)
    net.start()

    clock = pygame.time.Clock()
    running = True
    last_click_at = 0.0

    def draw_text(txt, x, y, col=(230, 230, 235), f=font):
        s = f.render(txt, True, col)
        screen.blit(s, (x, y))

    while running:
        clock.tick(60)
        click_pos = None
        for ev in pygame.event.get():
            if ev.type == pygame.QUIT:
                running = False
            elif ev.type == pygame.KEYDOWN and ev.key == pygame.K_ESCAPE:
                running = False
            elif ev.type == pygame.MOUSEBUTTONDOWN and ev.button == 1:
                click_pos = ev.pos

        screen.fill((18, 18, 24))
        draw_text(f"Room {args.room_id}  You: {args.user}", 20, 20)
        draw_text(f"Status: {state.status}", 20, 55, f=font_small)
        draw_text(f"Players: {state.players}", 20, 80, f=font_small)
        draw_text(f"Holder: {state.holder}", 20, 105, f=font_small)
        if state.last_event:
            draw_text(f"Event: {state.last_event}", 20, 130, f=font_small)

        if state.game_over:
            draw_text("GAME OVER", 20, 180, col=(240, 170, 90))
            draw_text(f"Winner: {state.winner}", 20, 210, col=(240, 170, 90), f=font_small)
        else:
            enabled = (state.holder == args.user) and (len(state.players) >= 2)
            draw_text("Pass bomb to:", 20, 170, f=font_small)
            y = 200
            for pid in state.players:
                if pid == args.user:
                    continue
                rect = pygame.Rect(20, y, 220, 36)
                mx, my = pygame.mouse.get_pos()
                hover = rect.collidepoint(mx, my)
                col = (70, 160, 90) if enabled else (70, 70, 70)
                if hover and enabled:
                    col = (90, 190, 110)
                pygame.draw.rect(screen, col, rect, border_radius=6)
                pygame.draw.rect(screen, (20, 20, 25), rect, 2, border_radius=6)
                label = font_small.render(f"Pass to {pid}", True, (10, 10, 10))
                screen.blit(label, (rect.centerx - label.get_width() // 2, rect.centery - label.get_height() // 2))
                if enabled and click_pos and rect.collidepoint(*click_pos):
                    now = time.time()
                    if (now - last_click_at) > 0.15:
                        net.send_pass(int(pid))
                        last_click_at = now
                y += 46

        pygame.display.flip()

    net.close()
    pygame.quit()


if __name__ == "__main__":
    main()
