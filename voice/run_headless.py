#!/usr/bin/env python3
"""
JARVIS headless runner — ask the DSH agent one question, get the answer back.

This is the bridge between JARVIS's voice shell (or any script) and the
DeepSeek Harness agent runtime. It wraps the harness's one-shot profile:

    dsh --profile headless "<task>"

Why a wrapper instead of calling `dsh` directly: the harness streams its
reasoning to stderr and prints only the final assistant message to stdout, it
needs the second-brain briefing prepended to the task, and it needs a timeout
and a readable error when something goes wrong. A voice shell cannot cope with
raw streamed output.

Usage
-----
    python run_headless.py "what do we charge for EDR?"
    python run_headless.py --raw "summarise the rate card"
    echo "what is our late fee policy?" | python run_headless.py -
    python run_headless.py --json "invoice Mike Johnson for the reputation job"

Environment
-----------
    DSH_BIN          path to the dsh bin.js  (default: auto-detected)
    DSH_PROFILE      profile to boot          (default: headless)
    JARVIS_VAULT     second-brain vault path
    JARVIS_BRAIN     path to brain.py
    JARVIS_TIMEOUT   seconds before giving up (default: 300)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_TIMEOUT = 300
REASONING_PREFIX = "dsh: reasoning:"


def _force_utf8_stdio() -> None:
    """Notes contain curly quotes and em dashes; Windows consoles default to cp1252.

    Without this, printing a briefing raises UnicodeEncodeError on a default
    Windows terminal. Reconfiguring is cheap and makes behaviour identical on
    every platform.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

# Candidate locations for the harness launcher, in priority order. A desktop
# install ships the whole tree under resources/app; a source checkout keeps it
# in node_modules.
DSH_BIN_CANDIDATES = (
    r"C:\Program Files\DSH Desktop\resources\app\node_modules\@deepseek-ai\dsh\lib\bin.js",
    "/Applications/DSH Desktop.app/Contents/Resources/app/node_modules/@deepseek-ai/dsh/lib/bin.js",
    "/opt/DSH Desktop/resources/app/node_modules/@deepseek-ai/dsh/lib/bin.js",
)


def find_dsh_bin() -> str | None:
    """Locate the harness launcher: env override, known install paths, then PATH."""
    override = os.environ.get("DSH_BIN")
    if override:
        return override if Path(override).exists() else None

    for candidate in DSH_BIN_CANDIDATES:
        if Path(candidate).exists():
            return candidate

    # A source checkout: look beside this repo, then walk up.
    here = Path(__file__).resolve().parent
    for parent in [here, *here.parents]:
        candidate = parent / "node_modules/@deepseek-ai/dsh/lib/bin.js"
        if candidate.exists():
            return str(candidate)

    cli = shutil.which("dsh")
    return cli


def locate_brain() -> Path | None:
    """Find brain.py: env override, then ../brain/, then the repo root."""
    override = os.environ.get("JARVIS_BRAIN")
    if override:
        path = Path(override)
        if path.is_file():
            return path
        if (path / "brain.py").is_file():
            return path / "brain.py"

    here = Path(__file__).resolve().parent
    for candidate in (here / "brain.py", here.parent / "brain" / "brain.py"):
        if candidate.is_file():
            return candidate
    return None


def brain_briefing(task: str, limit: int = 8, timeout: int = 60) -> str:
    """Ask brain.py for grounding context. Returns '' when unavailable.

    A missing or unbuilt brain must never block an answer, so every failure here
    degrades to an empty briefing rather than an exception.
    """
    vault = os.environ.get("JARVIS_VAULT")
    brain = locate_brain()
    if not vault or brain is None or not Path(vault).is_dir():
        return ""

    try:
        result = subprocess.run(
            [sys.executable, str(brain), "context", task, "--vault", vault,
             "--limit", str(limit)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return ""

    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def compose_task(task: str, briefing: str) -> str:
    """Prepend the second-brain briefing to the user's task."""
    if not briefing:
        return task

    return (
        "The following is retrieved from my second brain.\n"
        "Treat it as the authoritative source for any business fact, rate, "
        "client detail, or figure. Use its exact numbers. If something I ask "
        "for is not covered by it, say so rather than guessing.\n\n"
        "<second-brain>\n"
        f"{briefing}\n"
        "</second-brain>\n\n"
        "Answer as JARVIS using the notes above where relevant.\n\n"
        f"My request: {task}"
    )


def split_output(stdout: str) -> tuple[str, str]:
    """Separate the final answer from any reasoning the harness leaked to stdout."""
    reasoning_lines: list[str] = []
    answer_lines: list[str] = []
    in_reasoning = False

    for line in stdout.splitlines():
        if line.startswith(REASONING_PREFIX):
            in_reasoning = True
            remainder = line[len(REASONING_PREFIX):].strip()
            if remainder:
                reasoning_lines.append(remainder)
            continue
        if in_reasoning:
            # Reasoning runs until the blank line that precedes the answer.
            if line.strip() == "":
                in_reasoning = False
            else:
                reasoning_lines.append(line)
            continue
        answer_lines.append(line)

    answer = "\n".join(answer_lines).strip()
    # If the split left nothing, the whole output was the answer.
    if not answer:
        answer = stdout.strip()
    return answer, "\n".join(reasoning_lines).strip()


def ask(task: str, ground: bool = True, timeout: int = DEFAULT_TIMEOUT,
        profile: str = "headless") -> dict:
    """Run one task through the harness and return a result dict."""
    started = time.time()
    dsh_bin = find_dsh_bin()

    if dsh_bin is None:
        return {
            "ok": False,
            "answer": "",
            "error": "Could not find the DeepSeek Harness launcher. "
                     "Set DSH_BIN to the path of @deepseek-ai/dsh/lib/bin.js.",
            "elapsed": 0.0,
        }

    briefing = brain_briefing(task) if ground else ""
    prompt = compose_task(task, briefing)

    try:
        result = subprocess.run(
            ["node", dsh_bin, "--profile", profile, prompt],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "answer": "",
            "error": f"The agent did not answer within {timeout}s.",
            "elapsed": round(time.time() - started, 2),
        }
    except FileNotFoundError:
        return {
            "ok": False,
            "answer": "",
            "error": "Node.js was not found on PATH. JARVIS needs Node 20+.",
            "elapsed": round(time.time() - started, 2),
        }
    except OSError as exc:
        return {
            "ok": False,
            "answer": "",
            "error": f"Failed to launch the agent: {exc}",
            "elapsed": round(time.time() - started, 2),
        }

    answer, reasoning = split_output(result.stdout)

    if result.returncode != 0 and not answer:
        detail = (result.stderr or "").strip().splitlines()
        tail = detail[-1] if detail else f"exit code {result.returncode}"
        return {
            "ok": False,
            "answer": "",
            "error": f"The agent failed: {tail}",
            "reasoning": reasoning,
            "elapsed": round(time.time() - started, 2),
        }

    return {
        "ok": True,
        "answer": answer,
        "reasoning": reasoning,
        "grounded": bool(briefing),
        "briefing_chars": len(briefing),
        "elapsed": round(time.time() - started, 2),
    }


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(
        prog="run_headless.py",
        description="Ask the DSH agent one question and print its answer.",
    )
    parser.add_argument("task", nargs="*", help="the task; '-' reads stdin")
    parser.add_argument("--timeout", type=int,
                        default=int(os.environ.get("JARVIS_TIMEOUT", DEFAULT_TIMEOUT)))
    parser.add_argument("--profile", default=os.environ.get("DSH_PROFILE", "headless"))
    parser.add_argument("--no-ground", action="store_true",
                        help="skip the second-brain briefing")
    parser.add_argument("--raw", action="store_true",
                        help="print everything including reasoning")
    parser.add_argument("--json", action="store_true",
                        help="emit a machine-readable result object")
    parser.add_argument("--briefing-only", action="store_true",
                        help="print just the retrieved context and exit")
    args = parser.parse_args(argv)

    task = " ".join(args.task).strip()
    if task == "-" or task == "":
        task = sys.stdin.read().strip()
    if not task:
        print("error: a task is required", file=sys.stderr)
        return 2

    if args.briefing_only:
        print(brain_briefing(task) or "(no second-brain context available)")
        return 0

    result = ask(task, ground=not args.no_ground,
                 timeout=args.timeout, profile=args.profile)

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if result["ok"] else 1

    if args.raw:
        if result.get("reasoning"):
            print(f"{REASONING_PREFIX}\n{result['reasoning']}\n")
        print(result.get("answer", ""))
    elif result["ok"]:
        print(result["answer"])
    else:
        print(f"error: {result['error']}", file=sys.stderr)
        return 1

    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
