"""Loading and saving translation scripts.

Three formats, because translators and toolchains want different things:

  ``.json``    ``{"MSG_KEY": "text"}`` -- best for round-tripping with a
               decomp or a translation memory, since keys are preserved.
  ``.txt``     one entry per line, ``#`` comments -- fine for quick tests.
  ``.pks``     ``[MSG_KEY]`` headers followed by body lines -- readable for
               humans working through dialogue, keys preserved, and multi-line
               entries do not need escaping.

In every format, ``\\n`` ``\\l`` ``\\p`` and ``{PLAYER}`` keep their meaning as
markup; see ``charmap.Charmap.tokenise``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_HEADER = re.compile(r"^\[([^\]]+)\]\s*$")


def load(path: str | Path) -> dict[str, str]:
    p = Path(path)
    text = p.read_text(encoding="utf-8")
    suffix = p.suffix.lower()

    if suffix == ".json":
        data = json.loads(text)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
        return {f"line_{i:04d}": str(v) for i, v in enumerate(data)}

    if suffix == ".pks" or _HEADER.search(text):
        return _load_blocks(text)

    out: dict[str, str] = {}
    for i, raw in enumerate(text.splitlines()):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        out[f"line_{i:04d}"] = line
    return out


def _load_blocks(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    key: str | None = None
    body: list[str] = []

    def flush() -> None:
        if key is not None:
            out[key] = "\n".join(body).strip("\n")

    for raw in text.splitlines():
        m = _HEADER.match(raw)
        if m:
            flush()
            key, body = m.group(1).strip(), []
            continue
        if key is None:
            if raw.strip() and not raw.lstrip().startswith("#"):
                key, body = "line_0000", [raw]
            continue
        body.append(raw)
    flush()
    return out


def save_blocks(entries: dict[str, str], path: str | Path) -> None:
    lines = []
    for key, value in entries.items():
        lines.append(f"[{key}]")
        lines.append(value)
        lines.append("")
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def load_many(paths: list[str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for path in paths:
        for key, value in load(path).items():
            if key in merged and merged[key] != value:
                key = f"{Path(path).stem}:{key}"
            merged[key] = value
    return merged
