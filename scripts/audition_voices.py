#!/usr/bin/env python3
"""
Audition voices for JARVIS.

Generates the same line in a set of candidate voices and prosody variants, so
the choice can be made by ear instead of by description. Writes .mp3 files plus
an index.html contact sheet you can open and play through.

The target register is the Marvel JARVIS archetype: crisp British, precise,
unflappable, a touch dry. That is a *style*, not a person — which is why this
auditions voices rather than cloning anyone.

    python scripts/audition_voices.py
    python scripts/audition_voices.py --out-dir C:\\temp\\voices --open
"""

from __future__ import annotations

import argparse
import asyncio
import html
import subprocess
import sys
import webbrowser
from pathlib import Path

LINE = (
    "Good evening, sir. The after-hours support rate is two hundred and forty "
    "dollars an hour, rising to two hundred and sixty on the first of October. "
    "Shall I prepare the invoice for PaintCo?"
)

# voice, pitch, rate, note. Pitch in Hz offset; edge-tts accepts "+0Hz".
CANDIDATES = [
    ("en-GB-RyanNeural", "+0Hz", "+0%", "British male, default"),
    ("en-GB-RyanNeural", "-6Hz", "-6%", "British male, lower and slower"),
    ("en-GB-RyanNeural", "-12Hz", "-8%", "British male, deepest and most deliberate"),
    ("en-GB-ThomasNeural", "+0Hz", "+0%", "British male, second option"),
    ("en-GB-ThomasNeural", "-8Hz", "-5%", "British male, warmed down"),
    ("en-US-ChristopherNeural", "-10Hz", "-6%", "US male, Authority - transatlantic feel"),
    ("en-US-SteffanNeural", "-8Hz", "-5%", "US male, Rational - drier") ,
    ("en-AU-WilliamNeural", "-4Hz", "-4%", "Australian male, for local business calls"),
]


async def synth(voice: str, pitch: str, rate: str, text: str, dest: Path) -> bool:
    """Render one sample. Returns False if this voice is unavailable."""
    try:
        import edge_tts
    except ImportError:
        print("  ! edge-tts is not installed.  pip install edge-tts", file=sys.stderr)
        return False

    try:
        communicate = edge_tts.Communicate(text, voice, pitch=pitch, rate=rate)
        await communicate.save(str(dest))
    except Exception as exc:  # network, unknown voice, service change
        print(f"  ! {voice} {pitch}/{rate} failed: {exc}", file=sys.stderr)
        return False
    return dest.exists() and dest.stat().st_size > 0


async def run(out_dir: Path, line: str) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)

    made: list[dict] = []
    for index, (voice, pitch, rate, note) in enumerate(CANDIDATES, start=1):
        name = f"{index:02d}-{voice}-{pitch}-{rate}".replace("+", "p").replace("%", "")
        dest = out_dir / f"{name}.mp3"
        print(f"  [{index}/{len(CANDIDATES)}] {voice}  pitch {pitch}  rate {rate} ...")
        if await synth(voice, pitch, rate, line, dest):
            made.append({"file": dest.name, "voice": voice, "pitch": pitch,
                         "rate": rate, "note": note})

    if not made:
        print("No samples were produced.", file=sys.stderr)
        return 1

    # A contact sheet beats opening twenty files by hand.
    rows = "\n".join(
        f"""    <div class="item">
      <div class="meta"><b>{html.escape(m['voice'])}</b>
        <span>pitch {html.escape(m['pitch'])} · rate {html.escape(m['rate'])}</span>
        <span class="note">{html.escape(m['note'])}</span></div>
      <audio controls preload="none" src="{html.escape(m['file'])}"></audio>
    </div>"""
        for m in made
    )

    sheet = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>JARVIS voice audition</title>
<style>
  body {{ background:#05070d; color:#e6ecf7; font:15px/1.5 system-ui,sans-serif;
         max-width:760px; margin:0 auto; padding:28px 20px 60px; }}
  h1 {{ font-size:17px; letter-spacing:.12em; text-transform:uppercase; }}
  p.sub {{ color:#7d8bab; font-size:13px; margin-top:-6px; }}
  .item {{ background:#0d1220; border:1px solid #1e2942; border-radius:11px;
           padding:12px 14px; margin-bottom:10px; }}
  .meta {{ display:flex; flex-direction:column; gap:2px; margin-bottom:8px; }}
  .meta b {{ color:#35d1ff; }}
  .meta span {{ color:#7d8bab; font-size:12px; }}
  .note {{ color:#3ddc97 !important; }}
  audio {{ width:100%; height:34px; }}
  code {{ background:#131a2c; border:1px solid #1e2942; border-radius:5px;
          padding:1px 6px; font-size:12px; }}
</style></head><body>
<h1>JARVIS voice audition</h1>
<p class="sub">{len(made)} samples. Play each, then name the winner and I'll set it as the default.</p>
<p class="sub">Line: <em>{html.escape(line[:150])}…</em></p>
{rows}
<p class="sub">Set the winner with <code>JARVIS_TTS_VOICE</code>, or just tell me which number.</p>
</body></html>
"""

    sheet_path = out_dir / "index.html"
    sheet_path.write_text(sheet, encoding="utf-8")

    print()
    print(f"  {len(made)} samples written to {out_dir}")
    print(f"  open {sheet_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Audition JARVIS voices.")
    parser.add_argument("--out-dir", default=str(Path(__file__).resolve().parent.parent / "voice" / "samples"))
    parser.add_argument("--line", default=LINE)
    parser.add_argument("--open", action="store_true", help="open the contact sheet")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    print()
    print("  Auditioning voices for JARVIS")
    print("  " + "-" * 46)
    code = asyncio.run(run(out_dir, args.line))
    if code == 0 and args.open:
        webbrowser.open((out_dir / "index.html").as_uri())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
