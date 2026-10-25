"""Agentless access to another machine: the hub opens `ssh <machine> python3 -u -c <bootstrap>`, streams
this package's source over stdin (it is imported from memory, nothing is written to the remote disk),
then speaks JSON lines with the agent running there. Only ssh access and Python 3.9+ are needed on the
other machine."""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time

PKG_DIR = os.path.dirname(os.path.abspath(__file__))

# Line 1 sent over stdin; executed by the one-liner below. Installs an in-memory importer for `cmgr`.
_LOADER = """
import sys, json, importlib.abc, importlib.util
S = json.loads(sys.stdin.readline())
class F(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    def find_spec(self, name, path, target=None):
        if name in S:
            return importlib.util.spec_from_loader(name, self, is_package=(name == 'cmgr'))
    def create_module(self, spec):
        return None
    def exec_module(self, m):
        if m.__name__ == 'cmgr':
            m.__path__ = []
        m.__file__ = '/cmgr-in-memory/' + ('__init__' if m.__name__ == 'cmgr' else m.__name__[5:]) + '.py'
        exec(compile(S[m.__name__], '<cmgr:' + m.__name__ + '>', 'exec'), m.__dict__)
sys.meta_path.insert(0, F())
import cmgr.remote
cmgr.remote.serve_stdio()
"""
BOOTSTRAP = "exec(input())"  # no quotes or spaces: survives bash, cmd.exe and PowerShell


def package_sources() -> dict[str, str]:
    out = {}
    for f in sorted(os.listdir(PKG_DIR)):
        if f.endswith(".py") and f != "__main__.py":
            mod = "cmgr" if f == "__init__.py" else "cmgr." + f[:-3]
            with open(os.path.join(PKG_DIR, f), encoding="utf-8") as fh:
                out[mod] = fh.read()
    return out


def serve_stdio() -> None:
    """Remote side: answer JSON-line requests with the local agent until stdin closes."""
    from . import config
    from .agent import Agent, AgentError
    from .server import dispatch_agent
    out = sys.stdout
    sys.stdout = sys.stderr  # nothing else may write to the protocol stream
    agent = Agent(config.load())
    out.write(json.dumps({"id": 0, "ok": True, "result": {"ready": True, "name": agent.cfg["name"]}}) + "\n")
    out.flush()
    for line in sys.stdin:
        try:
            req = json.loads(line)
        except ValueError:
            continue
        try:
            res = dispatch_agent(agent, req["method"], req["path"], req.get("params") or {}, req.get("body") or {})
            msg = {"id": req.get("id"), "ok": True, "result": res}
        except AgentError as e:
            msg = {"id": req.get("id"), "ok": False, "error": str(e), "status": e.status}
        except KeyError as e:
            msg = {"id": req.get("id"), "ok": False, "error": f"missing parameter: {e.args[0]}", "status": 400}
        except Exception as e:
            msg = {"id": req.get("id"), "ok": False, "error": f"{e.__class__.__name__}: {e}", "status": 502}
        out.write(json.dumps(msg) + "\n")
        out.flush()


class _Channel:
    """One ssh process running the remote agent."""

    def __init__(self, argv: list[str]):
        self.argv = argv
        self.proc: subprocess.Popen | None = None
        self.lines: queue.Queue = queue.Queue()
        self.stderr: list[str] = []
        self.lock = threading.Lock()
        self.next_id = 1

    def _pump(self, stream, sink):
        for line in iter(stream.readline, ""):
            sink(line)
        sink(None)

    def start(self, timeout: float) -> None:
        self.stderr, self.lines = [], queue.Queue()
        self.proc = subprocess.Popen(self.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", errors="replace", bufsize=1,
                                     creationflags=0x08000000 if sys.platform == "win32" else 0)
        threading.Thread(target=self._pump, args=(self.proc.stdout, self.lines.put), daemon=True).start()
        threading.Thread(target=self._pump, args=(self.proc.stderr, lambda l: l and self.stderr.append(l.rstrip())),
                         daemon=True).start()
        self._write("exec(" + repr(_LOADER) + ")\n" + json.dumps(package_sources()) + "\n")
        self._read(0, timeout)  # the "ready" line

    def _write(self, data: str) -> None:
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except OSError:
            # ssh already exited (refused, bad key, no python...): report what it printed, not "broken pipe"
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                pass
            time.sleep(0.2)  # let the stderr reader catch up
            raise ConnectionError(self._why())

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(2)
            except Exception:
                self.proc.kill()

    def _read(self, want_id: int, timeout: float) -> dict:
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise TimeoutError("no answer")
            try:
                line = self.lines.get(timeout=left)
            except queue.Empty:
                raise TimeoutError("no answer")
            if line is None:
                raise ConnectionError(self._why())
            try:
                msg = json.loads(line)
            except ValueError:
                continue  # stray output (e.g. a login banner)
            if msg.get("id") == want_id:
                return msg

    def _why(self) -> str:
        err = [l for l in self.stderr if l.strip()]
        return err[-1] if err else "connection closed"

    def call(self, method, path, params, body, timeout) -> dict:
        rid = self.next_id
        self.next_id += 1
        self._write(json.dumps({"id": rid, "method": method, "path": path, "params": params, "body": body}) + "\n")
        return self._read(rid, timeout)


class SSHClient:
    """Same interface as the HTTP RemoteClient, over ssh. Two channels, so a slow snapshot doesn't block
    the open session's screen / chat refresh."""

    def __init__(self, target: str, python: str = "python3", argv: list[str] | None = None, channels: int = 2):
        self.target, self.python = target, python
        self.url = f"ssh://{target}"
        base = argv or ["ssh", "-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
                        "-o", "ServerAliveInterval=20", "-o", "ServerAliveCountMax=3",
                        "-o", "StrictHostKeyChecking=accept-new", target]
        self.argv = base + [python, "-u", "-c", f'"{BOOTSTRAP}"'] if not argv else argv
        self.channels = [_Channel(self.argv) for _ in range(channels)]
        self.last_error = ""

    def close(self) -> None:
        for ch in self.channels:
            ch.close()

    def call(self, method: str, path: str, params: dict | None = None, body: dict | None = None, timeout: float = 20.0):
        from .server import RemoteError
        ch = next((c for c in self.channels if c.lock.acquire(blocking=False)), None)
        if ch is None:
            ch = self.channels[0 if path == "/api/snapshot" else -1]
            ch.lock.acquire()
        try:
            for attempt in (1, 2):
                try:
                    if not ch.alive():
                        ch.close()
                        ch.start(timeout=25)
                    msg = ch.call(method, path, params or {}, body or {}, timeout)
                    break
                except (OSError, ConnectionError, TimeoutError, ValueError) as e:
                    ch.close()
                    self.last_error = (str(e) or e.__class__.__name__)
                    if attempt == 2 or isinstance(e, TimeoutError):
                        raise RemoteError(f"ssh {self.target}: {self.last_error}") from e
            if not msg.get("ok"):
                raise RemoteError(msg.get("error", "remote error"), msg.get("status", 502))
            self.last_error = ""
            return msg["result"]
        finally:
            ch.lock.release()
