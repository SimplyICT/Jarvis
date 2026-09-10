#!/usr/bin/env python3
"""
Isolate what the shell itself does to prosody.

Two things the voice shell does could make speech stilted, independent of which
voice is chosen:

1. Chunking. `speak()` splits text at ~180 characters and speaks each piece as
   a separate utterance. That is a hangover from browser synthesis, where Chrome
   mangles anything over ~14 seconds. Server-side rendering has no such limit,
   and splitting removes the model's cross-sentence context — the thing that
   makes a paragraph sound like a paragraph rather than a list of sentences.

2. Rate reduction. The shell defaulted to -6%, which stretches the read.

This renders one realistic passage three ways: whole, shell-chunked, and slowed,
so the two causes can be told apart and heard.

    python scripts/diagnose_chunking.py
"""

from __future__ import annotations

import argparse
import asyncio
import html
import subprocess
import sys
import webbrowser
from pathlib import Path

PASSAGE = (
    "Good evening, sir. After-hours support is two hundred and forty dollars an "
    "hour, rising to two hundred and sixty from the first of October. Standard "
    "managed services are ninety-five dollars per seat per month, with EDR at "
    "twelve dollars extra. Payment terms are fourteen days, and the minimum "
    "engagement is four hundred and ninety-five dollars."
)

VOICE = "en-GB-RyanNeural"
MOVIE_CHAIN = (
    "highpass=f=85,"
    "acompressor=threshold=-19dB:ratio=3.5:attack=6:release=240:makeup=1.8,"
    "bass=g=3:f=180,"
    "equalizer=f=3200:t=q:w=0.9:g=1.8,"
    "aecho=0.85:0.5:9:0.14,"
    "alimiter=limit=0.94"
)

# name, note, list of (text, pitch, rate) segments
CASES: list[tuple[str, str, list[tuple[str, str, str]]]] = [
    ("1-whole-neutral",
     "ONE utterance, no shift — the natural baseline",
     [(PASSAGE, "+0Hz", "+0%")]),

    ("2-whole-slow",
     "ONE utterance, rate -6% — isolates the slowdown alone",
     [(PASSAGE, "+0Hz", "-6%")]),

    ("3-chunked-neutral",
     "Split at 180 chars like the shell does, no shift",
     []),  # filled in below

    ("4-chunked-slow",
     "Shell chunking AND -6% rate — what you were actually hearing",
     []),  # filled in below

    ("5-whole-slight",
     "ONE utterance, rate -2% — a gentle, natural pacing",
     [(PASSAGE, "+0Hz", "-2%")]),
]


def chunk(text: str, limit: int = 180) -> list[str]:
    """Mirror speak()'s chunking so the comparison is faithful."""
    import re
    sentences = re.findall(r"[^.!?]+[.!?]*", text) or [text]
    groups: list[str] = []
    current = ""
    for sentence in sentences:
        if len(current + sentence) > limit and current:
            groups.append(current)
            current = sentence
        else:
            current += sentence
    if current:
        groups.append(current)
    return [g.strip() for g in groups if g.strip()]


async def render(text: str, pitch: str, rate: str, dest: Path) -> bool:
    import edge_tts
    try:
        await edge_tts.Communicate(text, VOICE, pitch=pitch, rate=rate).save(str(dest))
    except Exception as exc:
        print(f"  ! render failed: {exc}", file=sys.stderr)
        return False
    return dest.exists() and dest.stat().st_size > 0


def concat(parts: list[Path], dest: Path) -> bool:
    """Join segments losslessly, the way separate utterances would arrive."""
    listing = dest.with_suffix(".txt")
    listing.write_text(
        "\n".join(f"file '{p.as_posix()}'" for p in parts), encoding="utf-8"
    )
    result = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
         "-i", str(listing), "-c", "copy", str(dest)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    listing.unlink(missing_ok=True)
    return result.returncode == 0 and dest.exists()


def treat(source: Path, dest: Path) -> bool:
    result = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source),
         "-af", MOVIE_CHAIN, "-ac", "1", "-ar", "24000", "-b:a", "96k", str(dest)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    return result.returncode == 0 and dest.exists()


async def run(out_dir: Path, temp: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    temp.mkdir(parents=True, exist_ok=True)

    pieces = chunk(PASSAGE)
    print(f"  shell would split this into {len(pieces)} utterances:")
    for piece in pieces:
        print(f"    · {piece[:70]}...")

    CASES[2] = ("3-chunked-neutral", CASES[2][1], [(p, "+0Hz", "+0%") for p in pieces])
    CASES[3] = ("4-chunked-slow", CASES[3][1], [(p, "+0Hz", "-6%") for p in pieces])

    made: list[dict] = []
    for index, (name, note, segments) in enumerate(CASES):
        print(f"\n  [{name}] {note}")
        rendered: list[Path] = []
        for i, (text, pitch, rate) in enumerate(segments):
            part = temp / f"{name}-{i}.mp3"
            if not await render(text, pitch, rate, part):
                break
            rendered.append(part)

        if len(rendered) != len(segments):
            continue

        joined = temp / f"{name}-joined.mp3"
        if len(rendered) == 1:
            joined = rendered[0]
        elif not concat(rendered, joined):
            print("    ! could not join segments", file=sys.stderr)
            continue

        treated = out_dir / f"{name}.mp3"
        if treat(joined, treated):
            made.append({"file": treated.name, "name": name, "note": note})
        else:
            print("    ! treatment failed", file=sys.stderr)

    if not made:
        print("Nothing was produced.", file=sys.stderr)
        return 1

    rows = "\n".join(
        f"""    <div class="item">
      <div class="meta"><b>{html.escape(m['name'])}</b>
        <span>{html.escape(m['note'])}</span></div>
      <audio controls preload="none" src="{html.escape(m['file'])}"></audio>
    </div>"""
        for m in made
    )

    sheet = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>JARVIS chunking diagnosis</title>
<style>
  body {{ background:#05070d; color:#e6ecf7; font:15px/1.5 system-ui,sans-serif;
         max-width:740px; margin:0 auto; padding:28px 20px 60px; }}
  h1 {{ font-size:17px; letter-spacing:.12em; text-transform:uppercase; }}
  p.sub {{ color:#7d8bab; font-size:13px; }}
  .item {{ background:#0d1220; border:1px solid #1e2942; border-radius:11px;
           padding:12px 14px; margin-bottom:9px; }}
  .meta {{ display:flex; flex-direction:column; gap:2px; margin-bottom:8px; }}
  .meta b {{ color:#35d1ff; }}
  .meta span {{ color:#7d8bab; font-size:12px; }}
  audio {{ width:100%; height:34px; }}
</style></head><body>
<h1>Chunking &amp; pacing diagnosis</h1>
<p class="sub">All five use the same voice and the same movie treatment. The only
differences are whether the text was split, and how much it was slowed.</p>
<p class="sub"><b>1</b> is the natural baseline. Compare <b>1 vs 3</b> for the
chunking effect, and <b>1 vs 2</b> for the slowdown alone.
<b>4</b> is what you have been hearing.</p>
{rows}
</body></html>
"""

    (out_dir / "index.html").write_text(sheet, encoding="utf-8")
    print()
    print(f"  {len(made)} samples written to {out_dir}")
    return 0


def main() -> int:
    import tempfile
    parser = argparse.ArgumentParser(description="Diagnose chunking and pacing.")
    parser.add_argument("--out-dir",
                        default=str(Path(__file__).resolve().parent.parent / "voice" / "chunking"))
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        print()
        print("  Diagnosing chunking and pacing")
        print("  " + "-" * 60)
        code = asyncio.run(run(Path(args.out_dir), Path(tmp)))

    if code == 0 and args.open:
        webbrowser.open((Path(args.out_dir) / "index.html").as_uri())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
