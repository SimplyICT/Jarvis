#!/usr/bin/env python3
"""
JARVIS voice shell — a local web app for talking to your agent.

One process, three jobs:

1. Serves the UI (index.html) on localhost.
2. Runs jobs: it hands a spoken request to the agent and tracks the answer while
   the browser polls. A request takes many seconds, so the browser cannot simply
   block on it.
3. Exposes the second brain over HTTP, so the UI can search and rebuild memory
   without shelling out itself.

Speech itself is the browser's job (Web Speech API): recognition and synthesis
both happen in the page, so there are no API keys and no audio ever leaves the
machine for speech. The only outbound traffic is the agent's own model call.

Standard library only. Start it with:

    python server.py

Environment
-----------
    JARVIS_VAULT     the notes folder (required for memory features)
    JARVIS_BRAIN     path to brain.py
    JARVIS_PORT      port to bind (default 4731)
    JARVIS_HOST      interface to bind (default 127.0.0.1)
"""

from __future__ import annotations

import json
import os
import secrets
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_headless  # noqa: E402  (needs the sys.path line above)

DEFAULT_PORT = 4731
DEFAULT_HOST = "127.0.0.1"
JOB_TTL_SECONDS = 3600
MAX_BODY_BYTES = 1_000_000
UI_FILE = Path(__file__).resolve().parent / "index.html"

# Jobs live in memory: this is a single-user local tool, not a service.
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------

def _reap_jobs() -> None:
    """Drop jobs older than the TTL so a long-running shell does not leak."""
    cutoff = time.time() - JOB_TTL_SECONDS
    with _jobs_lock:
        stale = [jid for jid, job in _jobs.items() if job["created"] < cutoff]
        for jid in stale:
            _jobs.pop(jid, None)


def start_job(task: str, ground: bool = True) -> str:
    """Register a job and answer it on a worker thread. Returns the job id."""
    job_id = secrets.token_hex(8)
    with _jobs_lock:
        _jobs[job_id] = {
            "id": job_id,
            "task": task,
            "status": "working",
            "answer": "",
            "error": "",
            "detail": "",
            "briefing_chars": 0,
            "elapsed": 0.0,
            "created": time.time(),
        }

    def worker() -> None:
        try:
            result = run_headless.ask(task, ground=ground)
        except Exception as exc:  # a crash must not strand the browser polling
            result = {"ok": False, "answer": "", "error": f"internal error: {exc}",
                      "briefing_chars": 0, "elapsed": 0.0}
        with _jobs_lock:
            job = _jobs.get(job_id)
            if job is None:
                return
            job["status"] = "done" if result.get("ok") else "failed"
            job["answer"] = result.get("answer", "")
            job["error"] = result.get("error", "")
            job["detail"] = result.get("reasoning", "")
            job["briefing_chars"] = result.get("briefing_chars", 0)
            job["elapsed"] = result.get("elapsed", 0.0)

    threading.Thread(target=worker, name=f"jarvis-job-{job_id}", daemon=True).start()
    return job_id


def get_job(job_id: str) -> dict | None:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job else None


# --------------------------------------------------------------------------
# Brain helpers
# --------------------------------------------------------------------------

def brain_path() -> Path | None:
    return run_headless.locate_brain()


def brain_run(args: list[str], timeout: int = 120) -> dict:
    """Run brain.py with the configured vault. Returns a result dict."""
    brain = brain_path()
    vault = os.environ.get("JARVIS_VAULT", "")

    if brain is None:
        return {"ok": False, "error": "brain.py not found. Set JARVIS_BRAIN."}
    if not vault or not Path(vault).is_dir():
        return {"ok": False, "error": f"JARVIS_VAULT is not a folder: {vault or '(unset)'}"}

    import subprocess
    try:
        result = subprocess.run(
            [sys.executable, str(brain), *args, "--vault", vault],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.SubprocessError as exc:
        return {"ok": False, "error": str(exc)}

    return {
        "ok": result.returncode == 0,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def status_payload() -> dict:
    """Everything the UI needs to render its header."""
    vault = os.environ.get("JARVIS_VAULT", "")
    brain = brain_path()
    index_file = (Path(vault) / ".jarvis-brain" / "index.json") if vault else None

    brain_info: dict = {"configured": bool(vault), "vault": vault}
    if index_file and index_file.is_file():
        try:
            index = json.loads(index_file.read_text(encoding="utf-8"))
            brain_info.update({
                "indexed": True,
                "notes": index.get("note_count", 0),
                "terms": len(index.get("postings", {})),
                "built": index.get("built", ""),
                "folders": len(index.get("folders", [])),
            })
        except (OSError, ValueError):
            brain_info["indexed"] = False
    else:
        brain_info["indexed"] = False
        brain_info["hint"] = "Run a memory rebuild to create the index."

    return {
        "brain": brain_info,
        "runner": {
            "dsh": run_headless.find_dsh_bin() is not None,
            "brain": brain is not None,
            "dsh_path": run_headless.find_dsh_bin(),
            "brain_path": str(brain) if brain else None,
        },
        "profile": os.environ.get("DSH_PROFILE", "headless"),
    }


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "JarvisVoice/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:
        # The default logger prints every poll; keep the console readable.
        if self.path.startswith("/api/job/"):
            return
        sys.stderr.write(f"  {self.command} {self.path}\n")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def handle_one_request(self) -> None:
        """Treat a dropped client as routine.

        With HTTP/1.1 keep-alive the browser holds the socket open and closes it
        when it likes, so a reset while waiting for the next request line is
        normal. The base class prints a full traceback for it, which buries real
        errors in noise.
        """
        try:
            super().handle_one_request()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                TimeoutError, OSError):
            self.close_connection = True

    def handle(self) -> None:
        """Same reasoning as above, for the connect and teardown path."""
        try:
            super().handle()
        except (ConnectionResetError, ConnectionAbortedError, BrokenPipeError):
            pass

    def _json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0 or length > MAX_BODY_BYTES:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    # -- routes -----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        path = self.path.split("?", 1)[0]

        if path in ("/", "/index.html"):
            if not UI_FILE.is_file():
                self._send(500, b"index.html is missing from the voice folder.",
                           "text/plain; charset=utf-8")
                return
            self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
            return

        if path == "/api/status":
            self._json(status_payload())
            return

        if path.startswith("/api/job/"):
            job = get_job(path.rsplit("/", 1)[-1])
            if job is None:
                self._json({"ok": False, "error": "unknown job"}, 404)
            else:
                self._json({"ok": True, "job": job})
            return

        if path == "/api/memory/stats":
            result = brain_run(["stats"])
            self._json(result)
            return

        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        path = self.path.split("?", 1)[0]
        payload = self._read_json()

        if path == "/api/ask":
            task = str(payload.get("task", "")).strip()
            if not task:
                self._json({"ok": False, "error": "no task given"}, 400)
                return
            _reap_jobs()
            job_id = start_job(task, ground=bool(payload.get("ground", True)))
            self._json({"ok": True, "job_id": job_id})
            return

        if path == "/api/memory/search":
            query = str(payload.get("query", "")).strip()
            if not query:
                self._json({"ok": False, "error": "no query given"}, 400)
                return
            limit = str(int(payload.get("limit", 5)))
            self._json(brain_run(["search", query, "--limit", limit]))
            return

        if path == "/api/memory/rebuild":
            self._json(brain_run(["build"], timeout=300))
            return

        self._json({"ok": False, "error": "not found"}, 404)


def main() -> int:
    run_headless._force_utf8_stdio()

    host = os.environ.get("JARVIS_HOST", DEFAULT_HOST)
    port = int(os.environ.get("JARVIS_PORT", DEFAULT_PORT))

    vault = os.environ.get("JARVIS_VAULT", "")
    dsh = run_headless.find_dsh_bin()

    print()
    print("  JARVIS voice shell")
    print("  " + "-" * 46)
    print(f"  memory vault   {vault or '(not set — memory features off)'}")
    print(f"  brain.py       {brain_path() or '(not found)'}")
    print(f"  agent          {dsh or '(harness not found)'}")
    print(f"  profile        {os.environ.get('DSH_PROFILE', 'headless')}")
    print("  " + "-" * 46)
    print(f"  open           http://{host}:{port}")
    print("  stop           Ctrl+C")
    print()

    if not dsh:
        print("  ! The DeepSeek Harness launcher was not found.")
        print("    Set DSH_BIN to @deepseek-ai/dsh/lib/bin.js")
        print()

    try:
        server = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        print(f"  ! Could not bind {host}:{port} — {exc}", file=sys.stderr)
        print("    Set JARVIS_PORT to a free port.", file=sys.stderr)
        return 1

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  JARVIS offline.\n")
    except Exception as exc:
        # serve_forever only returns or raises when something is genuinely wrong:
        # the port was taken, or a handler blew up in a way that escaped. Say so
        # instead of vanishing with a bare exit code.
        print(f"\n  ! The server stopped: {type(exc).__name__}: {exc}\n",
              file=sys.stderr)
        return 1
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
