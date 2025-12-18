#!/usr/bin/env python3
"""
Scaffold a minimal HW3 game package (manifest + placeholder server/client).

This is not a graded game; it's a "smoke test" package to validate:
  - developer upload
  - player download
  - lobby start match (spawns server)
  - lobby client launch (spawns client)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


TEMPLATE_DIR = Path(__file__).resolve().parent / "template"


def _render_template(s: str, *, game_id: str) -> str:
    return s.replace("__GAME_ID__", game_id)


def _copy_template(out: Path, *, game_id: str) -> None:
    """
    Copy files from developer/template into the new game directory.
    """
    files = ["manifest.json", "framing.py", "server_main.py", "client_main.py"]
    for fn in files:
        src = TEMPLATE_DIR / fn
        if not src.exists():
            raise FileNotFoundError(f"missing_template:{fn}")
        dst = out / fn
        dst.write_text(_render_template(src.read_text(encoding="utf-8"), game_id=game_id), encoding="utf-8")


def main():
    if len(sys.argv) < 2:
        print("Usage: create_game_template.py <gameId> [dest_dir]")
        sys.exit(2)
    game_id = sys.argv[1].strip()
    dest = Path(sys.argv[2]).expanduser().resolve() if len(sys.argv) >= 3 else Path.cwd()
    out = dest / game_id
    out.mkdir(parents=True, exist_ok=True)

    _copy_template(out, game_id=game_id)

    os.chmod(out / "server_main.py", 0o755)
    os.chmod(out / "client_main.py", 0o755)

    print(f"Created template game at {out}")


if __name__ == "__main__":
    main()
