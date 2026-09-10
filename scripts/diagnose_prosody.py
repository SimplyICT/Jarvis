#!/usr/bin/env python3
"""
Diagnose stiltedness.

"Not very natural, a little stilted" is a prosody complaint, not an accent one,
and it has testable causes:

1. Pitch and rate shifting. Neural TTS models are trained at natural pitch. A
   -8Hz shift and a -6% rate are outside that distribution, so the model's
   timing and intonation degrade. This is the most likely culprit and the easiest
   to disprove.
2. Voice model quality. Some voices are simply more natural than others.
3. Long vs short passages. A voice can be fine on one sentence and stilted across
   a paragraph, because it has no cross-sentence context.

This renders the same passage across those axes so the cause can be isolated
rather than guessed at.

    python scripts/diagnose_prosody.py
"""

from __future__ import annotations

import argparse
import asyncio
import html
import shutil
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path

# A realistic multi-sentence answer: the length the shell actually speaks, with
# the clause structure and numbers the agent produces.
PASSAGE = (
    "After-hours support is two hundred and forty dollars an hour, rising to two "
    "hundred and sixty from the first of October. Payment terms are fourteen days, "
    "and the minimum engagement is four hundred and ninety-five dollars. Shall I "
    "prepare the invoice for PaintCo?"
)

# label, voice, pitch, rate
CASES: list[tuple[str, str, str, str]] = [
    # --- Axis 1: is the pitch/rate shift the problem? ---
    ("A1 Ryan NEUTRAL", "en-GB-RyanNeural", "+0Hz", "+0%"),
    ("A2 Ryan shifted (current)", "en-GB-RyanNeural", "-8Hz", "-6%"),
    ("A3 Ryan slight shift", "en-GB-RyanNeural", "-3Hz", "-3%"),
    ("A4 Ryan pitch only", "en-GB-RyanNeural", "-8Hz", "+0%"),
    ("A5 Ryan rate only", "en-GB-RyanNeural", "+0Hz", "-6%"),

    # --- Axis 2: are other voices more natural? ---
    ("B1 Thomas NEUTRAL", "en-GB-ThomasNeural", "+0Hz", "+0%"),
    ("B2 Andrew Multilingual (Warm/Authentic)", "en-US-AndrewMultilingualNeural", "+0Hz", "+0%"),
    ("B3 Brian (US, natural)", "en-US-BrianNeural", "+0Hz", "+0%"),
    ("B4 Guy (US, natural)", "en-US-GuyNeural", "+0Hz", "+0%"),
    ("B5 William (AU)", "en-AU-WilliamNeural", "+0Hz", "+0%"),

    # --- Axis 3: does a lightly slowed neutral read sound better than a
    #     heavily slowed shifted one? ---
    ("C1 Andrew slight slow", "en-US-AndrewMultilingualNeural", "+0Hz", "-4%"),
    ("C2 Brian slight slow", "en-US-BrianNeural", "+0Hz", "-4%"),
]


async def render(voice: str, pitch: str, rate: str, text: str, dest: Path) -> bool:
    import edge_tts
    try:
        await edge_tts.Communicate(text, voice, pitch=pitch, rate=rate).save(str(dest))
    except Exception as exc:
        print(f"  ! {voice} failed: {exc}", file=sys.stderr)
        return False
    return dest.exists() and dest.stat().st_size > 0


async def run(out_dir: Path, passage: str, movie_chain: bool) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)

    made: list[dict] = []
    for label, voice, pitch, rate in CASES:
        # Labels are human-readable and may contain slashes or punctuation;
        # nothing outside [A-Za-z0-9_] is safe in a filename.
        safe = "".join(c if c.isalnum() else "_" for c in label)
        raw = out_dir / f"{safe}.mp3"
        print(f"  {label:<44} {voice} {pitch} {rate}")

        if not await render(voice, pitch, rate, passage, raw):
            continue

        # Optionally apply the movie chain, so we can tell an untreated natural
        # read from the same read plus production.
        if movie_chain:
            treated = out_dir / f"{safe}-movie.mp3"
            chain = ("highpass=f=85,"
                     "acompressor=threshold=-19dB:ratio=3.5:attack=6:release=240:makeup=1.8,"
                     "bass=g=3:f=180,"
                     "equalizer=f=3200:t=q:w=0.9:g=1.8,"
                     "aecho=0.85:0.5:9:0.14,"
                     "alimiter=limit=0.94")
            result = subprocess.run(
                ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw),
                 "-af", chain, "-ac", "1", "-ar", "24000", "-b:a", "96k", str(treated)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            if result.returncode == 0:
                made.append({"file": treated.name, "label": label + "  [+ movie]",
                             "voice": voice, "pitch": pitch, "rate": rate})
                raw.unlink(missing_ok=True)

        made.append({"file": raw.name, "label": label + ("  [raw]" if movie_chain else ""),
                     "voice": voice, "pitch": pitch, "rate": rate})

    if not made:
        print("Nothing was produced.", file=sys.stderr)
        return 1

    rows = "\n".join(
        f"""    <div class="item">
      <div class="meta"><b>{html.escape(m['label'])}</b>
        <span>{html.escape(m['voice'])} · pitch {html.escape(m['pitch'])} · rate {html.escape(m['rate'])}</span></div>
      <audio controls preload="none" src="{html.escape(m['file'])}"></audio>
    </div>"""
        for m in made
    )

    sheet = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>JARVIS prosody diagnosis</title>
<style>
  body {{ background:#05070d; color:#e6ecf7; font:15px/1.5 system-ui,sans-serif;
         max-width:740px; margin:0 auto; padding:28px 20px 60px; }}
  h1 {{ font-size:17px; letter-spacing:.12em; text-transform:uppercase; }}
  h2 {{ font-size:12px; letter-spacing:.14em; text-transform:uppercase;
        color:#7d8bab; margin:26px 0 8px; }}
  p.sub {{ color:#7d8bab; font-size:13px; }}
  .item {{ background:#0d1220; border:1px solid #1e2942; border-radius:11px;
           padding:12px 14px; margin-bottom:9px; }}
  .meta {{ display:flex; flex-direction:column; gap:2px; margin-bottom:8px; }}
  .meta b {{ color:#35d1ff; }}
  .meta span {{ color:#7d8bab; font-size:12px; }}
  audio {{ width:100%; height:34px; }}
</style></head><body>
<h1>Prosody diagnosis</h1>
<p class="sub">Same passage, different causes. Play A1 first — that is the voice
untouched by pitch or rate changes.</p>
<p class="sub"><b>A1 vs A2</b> tells you whether my pitch/rate shift caused the
stiltedness. <b>B</b> tells you whether another voice is simply better.
<b>C</b> finds a gentler middle.</p>
<h2>Samples</h2>
{rows}
</body></html>
"""

    (out_dir / "index.html").write_text(sheet, encoding="utf-8")
    print()
    print(f"  {len(made)} samples written to {out_dir}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose TTS stiltedness.")
    parser.add_argument("--out-dir",
                        default=str(Path(__file__).resolve().parent.parent / "voice" / "prosody"))
    parser.add_argument("--passage", default=PASSAGE)
    parser.add_argument("--no-movie", action="store_true",
                        help="skip the treated variants; just compare raw reads")
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    print()
    print("  Diagnosing prosody")
    print("  " + "-" * 60)
    code = asyncio.run(run(out_dir, args.passage, not args.no_movie))
    if code == 0 and args.open:
        webbrowser.open((out_dir / "index.html").as_uri())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
