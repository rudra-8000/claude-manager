"""Claude Code's own transcript (<config>/projects/<cwd with non-alphanumerics as '-'>/<sessionId>.jsonl)
turned into a readable chat: your prompts, Claude's text, and each tool call with its result.

Claude Code draws its UI in tmux's alternate screen, so tmux keeps no scrollback; this file is the only
place the full conversation exists. Files grow to hundreds of MB, so only byte ranges are read."""
from __future__ import annotations

import glob
import json
import os
import re

CHUNK = 2_000_000          # bytes read per "load older" step
MAX_CHUNK = 12_000_000     # keep reading back until enough messages were found, up to this
MIN_ITEMS = 40
TOOL_RESULT_MAX = 4000     # characters of a tool result kept for the UI


def encode_cwd(cwd: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def find(account_dir: str, cwd: str, session_id: str, started_at: float = 0.0) -> str | None:
    d = os.path.join(account_dir, "projects", encode_cwd(cwd))
    exact = os.path.join(d, f"{session_id}.jsonl") if session_id else ""
    files = glob.glob(os.path.join(d, "*.jsonl"))
    if not files:
        return None
    newest = max(files, key=os.path.getmtime)
    # after /clear or a resume the session writes to a new file; the newest one started after the process wins
    if exact and os.path.exists(exact):
        if newest != exact and os.path.getmtime(newest) > os.path.getmtime(exact) and os.path.getmtime(newest) >= started_at:
            return newest
        return exact
    return newest if os.path.getmtime(newest) >= started_at else None


_CMD = re.compile(r"<command-name>(.*?)</command-name>.*?(?:<command-args>(.*?)</command-args>)?", re.S)
_NOISE = re.compile(r"^\s*<(local-command-caveat|local-command-stdout|system-reminder|bash-stdout|bash-stderr)\b", re.S)


def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


def _tool_summary(name: str, inp: dict) -> str:
    if not isinstance(inp, dict):
        return ""
    for key in ("command", "file_path", "path", "pattern", "url", "query", "description", "prompt", "skill"):
        v = inp.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()[:300]
    s = json.dumps(inp, ensure_ascii=False)
    return s[:300]


def parse_lines(lines: list[str]) -> list[dict]:
    items: list[dict] = []
    for ln in lines:
        try:
            j = json.loads(ln)
        except ValueError:
            continue
        t = j.get("type")
        if t not in ("user", "assistant") or j.get("isSidechain") or j.get("isMeta") or j.get("isCompactSummary"):
            continue
        msg = j.get("message") or {}
        content = msg.get("content")
        ts = j.get("timestamp", "")
        if t == "user":
            if isinstance(content, list) and content and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content):
                for b in content:
                    res = b.get("content")
                    text = res if isinstance(res, str) else _text_of(res)
                    items.append({"kind": "result", "tool_id": b.get("tool_use_id", ""), "is_error": bool(b.get("is_error")),
                                  "text": text[:TOOL_RESULT_MAX], "truncated": len(text) > TOOL_RESULT_MAX, "ts": ts})
                continue
            text = _text_of(content)
            m = _CMD.search(text)
            if m:
                items.append({"kind": "command", "text": (m.group(1) + " " + (m.group(2) or "")).strip(), "ts": ts})
                continue
            if not text.strip() or _NOISE.match(text):
                continue
            items.append({"kind": "user", "text": text, "ts": ts})
        else:
            for b in content if isinstance(content, list) else []:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and b.get("text", "").strip():
                    items.append({"kind": "assistant", "text": b["text"], "ts": ts})
                elif b.get("type") == "tool_use":
                    name = b.get("name", "")
                    items.append({"kind": "tool", "tool_id": b.get("id", ""), "name": name,
                                  "summary": _tool_summary(name, b.get("input") or {}), "ts": ts})
    return items


def read(path: str, before: int | None = None, since: int | None = None) -> dict:
    """since=N: everything appended after byte N (polling). before=N: the chunk that ends at byte N
    (initial load / older history). Returns items plus the byte range they came from."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        if since is not None:
            if since > size:  # file was replaced; start over
                return {"reset": True, "path": path, "size": size, "start": size, "end": size, "items": []}
            f.seek(since)
            data = f.read(size - since)
            end = since + data.rfind(b"\n") + 1 if b"\n" in data else since
            lines = data[: end - since].decode("utf-8", "replace").splitlines()
            return {"path": path, "size": size, "start": since, "end": end, "items": parse_lines(lines)}
        end = size if before is None else min(before, size)
        start, items, want, data = end, [], CHUNK, b""
        while start > 0 and len(items) < MIN_ITEMS and end - start < MAX_CHUNK:
            start = max(0, end - want)
            f.seek(start)
            data = f.read(end - start)
            if start > 0:  # drop the partial first line
                cut = data.find(b"\n")
                data = data[cut + 1:] if cut >= 0 else b""
                real_start = end - len(data)
            else:
                real_start = 0
            if not data.endswith(b"\n"):
                data = data[: data.rfind(b"\n") + 1]
            items = parse_lines(data.decode("utf-8", "replace").splitlines())
            start = real_start
            want *= 2
        if before is None:
            end = start + len(data)
        return {"path": path, "size": size, "start": start, "end": end, "items": items}
