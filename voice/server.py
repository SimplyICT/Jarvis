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
import shutil
import subprocess
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

# Neural text-to-speech, rendered on the server.
#
# Why server-side at all: Windows exposes its good neural voices only to Edge,
# so Chrome and Firefox fall back to the robotic SAPI set. Rendering here means
# every browser gets the same voice. The browser also gains real pitch control,
# which Edge's own neural voices ignore.
# Voices, and the prosody each is rendered with.
#
# Pitch and rate are deliberately left at neutral. Neural TTS is trained at
# natural pitch and rate, so shifting it pushes the model outside the
# distribution it learned and its timing and intonation degrade — which is heard
# as "stilted". Measurement bore this out: an -8Hz shift moved the spectral
# centroid by under 1% (1966Hz -> 1848Hz), i.e. less than a semitone on a voice
# with a ~120Hz fundamental, so it bought nothing while costing naturalness.
# A -6% rate stretched the same passage from 21.8s to 23.2s.
#
# A user who wants slower speech has the rate slider; the defaults should be the
# most natural read the model can give.
NEURAL_VOICES = [
    # short_name, label, pitch, rate, note
    ("en-GB-RyanNeural", "Ryan (British)", "+0Hz", "+0%", "British male — neutral, most natural"),
    ("en-GB-ThomasNeural", "Thomas (British)", "+0Hz", "+0%", "British male, warmer"),
    ("en-US-AndrewMultilingualNeural", "Andrew (warm, transatlantic)", "+0Hz", "+0%",
     "US male, Warm/Confident — close to the film register"),
    ("en-US-BrianNeural", "Brian (US, natural)", "+0Hz", "+0%", "US male, natural delivery"),
    ("en-US-ChristopherNeural", "Christopher (Authority)", "+0Hz", "+0%", "US male, authoritative"),
    ("en-US-SteffanNeural", "Steffan (Rational)", "+0Hz", "+0%", "US male, drier"),
    ("en-AU-WilliamNeural", "William (Australian)", "+0Hz", "+0%", "Australian male, local calls"),
]
DEFAULT_NEURAL_VOICE = os.environ.get("JARVIS_TTS_VOICE", "en-GB-RyanNeural")

# Post-processing chains.
#
# A clean TTS read is flat next to the film's JARVIS, which is a studio
# recording: close-mic'd, compressed, with a faint metallic ring and a touch of
# room. These chains add that production. "movie" is the chosen default and was
# picked by ear from an audition (`scripts/audition_effects.py`).
TREATMENTS: list[tuple[str, str, str]] = [
    ("none", "None (dry TTS read)", ""),
    ("warm",
     "Warm",
     "highpass=f=85,"
     "acompressor=threshold=-18dB:ratio=3:attack=6:release=220:makeup=1.6,"
     "bass=g=3.5:f=180,"
     "equalizer=f=3200:t=q:w=0.9:g=1.6,"
     "alimiter=limit=0.94"),
    ("movie",
     "Movie (chosen)",
     "highpass=f=85,"
     "acompressor=threshold=-19dB:ratio=3.5:attack=6:release=240:makeup=1.8,"
     "bass=g=3:f=180,"
     "equalizer=f=3200:t=q:w=0.9:g=1.8,"
     "aecho=0.85:0.5:9:0.14,"
     "alimiter=limit=0.94"),
    ("metallic",
     "Metallic (HUD)",
     "asetrate=24000*1.05,aresample=24000,"
     "flanger=delay=0:depth=2:regen=50:width=71:speed=0.5,"
     "aecho=0.8:0.88:15:0.5,"
     "highpass=f=200,"
     "treble=g=6"),
    ("hud",
     "Crisp HUD (dry, no echo)",
     "highpass=f=150,"
     "equalizer=f=2600:t=q:w=1.0:g=2.5,"
     "equalizer=f=7000:t=q:w=1.2:g=2,"
     "acompressor=threshold=-20dB:ratio=3:attack=5:release=180:makeup=1.7,"
     "alimiter=limit=0.95"),
    ("deep",
     "Deep authority (lower, slower)",
     "asetrate=24000*0.94,aresample=24000,"
     "highpass=f=80,"
     "acompressor=threshold=-20dB:ratio=4:attack=8:release=260:makeup=2.0,"
     "bass=g=4:f=170,"
     "alimiter=limit=0.94"),
]
TREATMENT_CHAINS = {name: chain for name, _label, chain in TREATMENTS}
DEFAULT_TREATMENT = os.environ.get("JARVIS_TTS_TREATMENT", "movie")

# Rendered audio is content-addressed and cached, because the same phrases
# recur constantly ("At your service, sir") and a cache hit is instant.
TTS_CACHE_DIR = Path(tempfile.gettempdir()) / "jarvis-tts-cache"
TTS_MAX_CHARS = 3000
TTS_TIMEOUT_SECONDS = 45
FFMPEG_TIMEOUT_SECONDS = 60


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
        "tts": {
            "available": neural_tts_available(),
            "ffmpeg": ffmpeg_available(),
            "default_voice": os.environ.get("JARVIS_TTS_VOICE", DEFAULT_NEURAL_VOICE),
            "default_treatment": DEFAULT_TREATMENT,
            "voices": [
                {"id": voice, "label": label, "pitch": pitch, "rate": rate, "note": note}
                for voice, label, pitch, rate, note in NEURAL_VOICES
            ],
            "treatments": [
                {"id": name, "label": label,
                 "active": bool(chain) and ffmpeg_available()}
                for name, label, chain in TREATMENTS
            ],
        },
    }


# --------------------------------------------------------------------------
# Neural text-to-speech
# --------------------------------------------------------------------------

_tts_available: bool | None = None


def neural_tts_available() -> bool:
    """Is edge-tts importable? Probed once and cached, since it costs a spawn."""
    global _tts_available
    if _tts_available is None:
        try:
            probe = subprocess.run(
                [sys.executable, "-c", "import edge_tts"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=30,
            )
            _tts_available = probe.returncode == 0
        except (OSError, subprocess.SubprocessError):
            _tts_available = False
    return _tts_available


def _cache_key(text: str, voice: str, pitch: str, rate: str, treatment: str) -> str:
    import hashlib
    payload = f"{voice}|{pitch}|{rate}|{treatment}|{text}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def apply_treatment(source: Path, dest: Path, treatment: str) -> tuple[bool, str]:
    """Run the treatment's ffmpeg chain. Returns (ok, error)."""
    chain = TREATMENT_CHAINS.get(treatment, "")
    if not chain:
        if source != dest:
            shutil.copy(source, dest)
        return True, ""

    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source),
             "-af", chain, "-ac", "1", "-ar", "24000", "-b:a", "96k", str(dest)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            timeout=FFMPEG_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, f"audio treatment timed out after {FFMPEG_TIMEOUT_SECONDS}s"
    except OSError as exc:
        return False, f"could not run ffmpeg: {exc}"

    if result.returncode != 0 or not dest.exists() or dest.stat().st_size == 0:
        detail = (result.stderr or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit code {result.returncode}"
        return False, f"audio treatment failed: {tail}"

    return True, ""


def synthesize(text: str, voice: str, pitch: str, rate: str,
               treatment: str = DEFAULT_TREATMENT) -> tuple[Path | None, str]:
    """Render text to a treated MP3. Returns (path, error).

    edge-tts is driven as a subprocess rather than through its asyncio API: this
    server is threaded and synchronous, so spawning the CLI avoids running an
    event loop inside a request thread.

    A failing treatment is not fatal: a plain voice still beats no voice, so the
    untreated render is returned rather than an error.
    """
    if not neural_tts_available():
        return None, "edge-tts is not installed (pip install edge-tts)"

    text = text.strip()
    if not text:
        return None, "no text to speak"
    if len(text) > TTS_MAX_CHARS:
        text = text[:TTS_MAX_CHARS].rsplit(" ", 1)[0] + "."

    if treatment not in TREATMENT_CHAINS:
        treatment = DEFAULT_TREATMENT

    TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cached = TTS_CACHE_DIR / f"{_cache_key(text, voice, pitch, rate, treatment)}.mp3"
    if cached.exists() and cached.stat().st_size > 0:
        return cached, ""

    # Stage to a unique name then move into place, so two concurrent requests
    # for the same line cannot read a half-written file.
    with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as handle:
        raw = Path(handle.name)

    try:
        result = subprocess.run(
            [sys.executable, "-m", "edge_tts",
             "--voice", voice, "--pitch", pitch, "--rate", rate,
             "--text", text, "--write-media", str(raw)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            timeout=TTS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raw.unlink(missing_ok=True)
        return None, f"speech synthesis timed out after {TTS_TIMEOUT_SECONDS}s"
    except OSError as exc:
        raw.unlink(missing_ok=True)
        return None, f"could not run edge-tts: {exc}"

    if result.returncode != 0 or not raw.exists() or raw.stat().st_size == 0:
        detail = (result.stderr or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit code {result.returncode}"
        raw.unlink(missing_ok=True)
        return None, f"speech synthesis failed: {tail}"

    # Treat the raw take, falling back to it untreated if ffmpeg is missing or
    # the chain fails.
    treated = raw
    if TREATMENT_CHAINS.get(treatment) and ffmpeg_available():
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as handle:
            staged = Path(handle.name)
        ok, _error = apply_treatment(raw, staged, treatment)
        if ok:
            treated = staged
        else:
            staged.unlink(missing_ok=True)

    try:
        shutil.move(str(treated), str(cached))
    except OSError:
        # Another thread won the race; its copy is just as good.
        if not cached.exists():
            if treated != raw:
                treated.unlink(missing_ok=True)
            raw.unlink(missing_ok=True)
            return None, "could not store the rendered audio"
    finally:
        if treated != raw:
            raw.unlink(missing_ok=True)

    return cached, ""


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

        if path == "/api/tts":
            self._handle_tts(payload)
            return

        self._json({"ok": False, "error": "not found"}, 404)

    def _handle_tts(self, payload: dict) -> None:
        """Render text to MP3 and return it.

        Returning audio rather than a URL keeps the cache opaque and avoids
        exposing arbitrary paths: the client never names a file, only text.
        """
        text = str(payload.get("text", "")).strip()
        if not text:
            self._json({"ok": False, "error": "no text given"}, 400)
            return

        requested = str(payload.get("voice", "") or DEFAULT_NEURAL_VOICE)
        pitch, rate = "+0Hz", "+0%"
        for voice, _label, candidate_pitch, candidate_rate, _note in NEURAL_VOICES:
            if voice == requested:
                pitch, rate = candidate_pitch, candidate_rate
                break

        # Allow the caller to override prosody for the rate slider.
        if payload.get("pitch"):
            pitch = str(payload["pitch"])
        if payload.get("rate"):
            rate = str(payload["rate"])

        treatment = str(payload.get("treatment", "") or DEFAULT_TREATMENT)
        if treatment not in TREATMENT_CHAINS:
            treatment = DEFAULT_TREATMENT

        audio, error = synthesize(text, requested, pitch, rate, treatment)
        if audio is None:
            self._json({"ok": False, "error": error}, 503)
            return

        try:
            body = audio.read_bytes()
        except OSError as exc:
            self._json({"ok": False, "error": f"could not read rendered audio: {exc}"}, 500)
            return

        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


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
    print(f"  speech         {'neural (edge-tts)' if neural_tts_available() else 'browser voices only'}")
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
