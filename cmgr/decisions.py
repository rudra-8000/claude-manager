"""DECISIONS.md reader. Entries are level-2 headings ("## D-248  [TAG] Title ... 2026-10-23 17:31 UTC+8"),
newest first; '###' and deeper headings stay inside the entry they belong to."""
from __future__ import annotations

import os
import re
import threading

NAMES = ("decisions.md",)  # matched case-insensitively
MAX_UP = 6                 # how many parent directories to search above a session's cwd

_ID = re.compile(r"^([A-Z]{1,4}-\d+)\b\s*")
_DATE = re.compile(r"(\d{4}-\d{2}-\d{2})(?:[ T](\d{1,2}:\d{2})(?::\d{2})?)?(?:\s*(UTC[+-]\d{1,2}(?::?\d{2})?|Z|[A-Z]{2,4}))?\s*$")
_BRACKETS = re.compile(r"\[([^\]]*)\]")
_FENCE = re.compile(r"^\s*(```|~~~)")


def find_file(start: str, max_up: int = MAX_UP) -> str | None:
    """Look for DECISIONS.md / decisions.md in `start` and its parents."""
    d = os.path.abspath(start)
    for _ in range(max_up + 1):
        hit = find_in_dir(d)
        if hit:
            return hit
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def find_in_dir(d: str) -> str | None:
    try:
        names = os.listdir(d)
    except OSError:
        return None
    for n in names:
        if n.lower() in NAMES and os.path.isfile(os.path.join(d, n)):
            return os.path.join(d, n)
    return None


def parse_heading(heading: str) -> dict:
    h = heading.strip()
    m = _ID.match(h)
    eid = m.group(1) if m else ""
    rest = h[m.end():] if m else h
    date = time = tz = ""
    dm = _DATE.search(rest)
    if dm:
        date, time, tz = dm.group(1), dm.group(2) or "", dm.group(3) or ""
        rest = rest[:dm.start()]
    tags = [t.strip() for t in _BRACKETS.findall(rest) if t.strip()]
    title = re.sub(r"\s{2,}", " ", _BRACKETS.sub(" ", rest)).strip(" -–—:|")
    return {"id": eid, "title": title or h, "tags": tags, "date": date, "time": time, "tz": tz}


def parse(text: str) -> list[dict]:
    entries: list[dict] = []
    cur: list[str] | None = None
    in_fence = False
    for line in text.splitlines(keepends=True):
        if _FENCE.match(line):
            in_fence = not in_fence
        if not in_fence and line.startswith("## "):
            if cur is not None:
                entries.append(_finish(cur))
            cur = [line]
        elif cur is not None:
            cur.append(line)
    if cur is not None:
        entries.append(_finish(cur))
    for i, e in enumerate(entries):
        e["index"] = i
    return entries


def _finish(lines: list[str]) -> dict:
    body = "".join(lines).rstrip() + "\n"
    meta = parse_heading(lines[0][3:])
    meta["heading"] = lines[0][3:].strip()
    meta["body"] = body
    meta["chars"] = len(body)
    meta["tokens_est"] = (len(body) + 3) // 4
    return meta


class DecisionsCache:
    """Parses each file once per mtime/size."""

    def __init__(self):
        self._lock = threading.Lock()
        self._files: dict[str, tuple[tuple, list[dict]]] = {}

    def entries(self, path: str) -> list[dict]:
        st = os.stat(path)
        sig = (st.st_mtime_ns, st.st_size)
        with self._lock:
            hit = self._files.get(path)
            if hit and hit[0] == sig:
                return hit[1]
        with open(path, encoding="utf-8", errors="replace") as f:
            parsed = parse(f.read())
        with self._lock:
            self._files[path] = (sig, parsed)
        return parsed

    def summary(self, path: str, bodies: int = 25) -> dict:
        es = self.entries(path)
        out = []
        for e in es:
            d = {k: v for k, v in e.items() if k != "body"}
            if e["index"] < bodies:
                d["body"] = e["body"]
            out.append(d)
        st = os.stat(path)
        return {"path": path, "project_dir": os.path.dirname(path),
                "project": os.path.basename(os.path.dirname(path)),
                "mtime": st.st_mtime, "count": len(es), "entries": out}

    def bodies(self, path: str, indexes: list[int]) -> dict[int, str]:
        es = self.entries(path)
        return {i: es[i]["body"] for i in indexes if 0 <= i < len(es)}
