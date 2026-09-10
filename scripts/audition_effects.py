#!/usr/bin/env python3
"""
Audition JARVIS voice *treatments*.

The voice model is only half the sound. The Marvel JARVIS is a studio
recording: close, compressed, with a faint metallic ring and a touch of room.
A plain TTS read is flat by comparison, which is why a good British voice still
does not sound like the film.

This renders one line through a set of ffmpeg effect chains so the treatment can
be chosen by ear next to the untreated take.

The metallic chain is borrowed from the TinkerClaw `jarvis-voice` skill
(MIT-0, github.com/globalcaos/tinkerclaw), whose documented recipe is
"pitch up 5% for a tighter AI feel, flanger for metallic sheen, 15ms echo for a
robotic ring, highpass 200Hz plus treble +6dB for crisp HUD clarity".

    python scripts/audition_effects.py
    python scripts/audition_effects.py --voice en-GB-ThomasNeural --open
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

LINE = (
    "Good evening, sir. The after-hours support rate is two hundred and forty "
    "dollars an hour, rising to two hundred and sixty on the first of October. "
    "Shall I prepare the invoice for PaintCo?"
)

# name, description, ffmpeg -af chain ("" = leave the raw render alone)
TREATMENTS: list[tuple[str, str, str]] = [
    ("01-raw", "Untreated neural render — the baseline to beat", ""),

    ("02-warm",
     "Gentle: low-shelf lift and compression. Richer, still natural",
     "highpass=f=85,"
     "acompressor=threshold=-18dB:ratio=3:attack=6:release=220:makeup=1.6,"
     "bass=g=3.5:f=180,"
     "equalizer=f=3200:t=q:w=0.9:g=1.6,"
     "alimiter=limit=0.94"),

    ("03-movie",
     "Produced: warm plus a short plate reverb. The 'close studio mic' feel",
     "highpass=f=85,"
     "acompressor=threshold=-19dB:ratio=3.5:attack=6:release=240:makeup=1.8,"
     "bass=g=3:f=180,"
     "equalizer=f=3200:t=q:w=0.9:g=1.8,"
     "aecho=0.85:0.5:9:0.14,"
     "alimiter=limit=0.94"),

    ("04-metallic",
     "Full TinkerClaw JARVIS chain: 5% pitch-up, flanger, 15ms ring, crisp treble",
     "asetrate=24000*1.05,aresample=24000,"
     "flanger=delay=0:depth=2:regen=50:width=71:speed=0.5,"
     "aecho=0.8:0.88:15:0.5,"
     "highpass=f=200,"
     "treble=g=6"),

    ("05-metallic-tamed",
     "Same idea, dialled back: subtler flanger and a quieter echo tail",
     "asetrate=24000*1.03,aresample=24000,"
     "flanger=delay=2:depth=1:regen=25:width=60:speed=0.4,"
     "aecho=0.8:0.7:12:0.22,"
     "highpass=f=160,"
     "treble=g=4,"
     "acompressor=threshold=-18dB:ratio=2.5:attack=8:release=200:makeup=1.3"),

    ("06-hud",
     "Crisp and dry: highpass, presence lift, light compression, no echo",
     "highpass=f=150,"
     "equalizer=f=2600:t=q:w=1.0:g=2.5,"
     "equalizer=f=7000:t=q:w=1.2:g=2,"
     "acompressor=threshold=-20dB:ratio=3:attack=5:release=180:makeup=1.7,"
     "alimiter=limit=0.95"),

    ("07-deep-authority",
     "Lower and slower, compressed. For a heavier, more imposing read",
     "asetrate=24000*0.94,aresample=24000,"
     "highpass=f=80,"
     "acompressor=threshold=-20dB:ratio=4:attack=8:release=260:makeup=2.0,"
     "bass=g=4:f=170,"
     "alimiter=limit=0.94"),
]


def have_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


async def render_raw(voice: str, pitch: str, rate: str, text: str, dest: Path) -> bool:
    import edge_tts
    try:
        await edge_tts.Communicate(text, voice, pitch=pitch, rate=rate).save(str(dest))
    except Exception as exc:
        print(f"  ! render failed: {exc}", file=sys.stderr)
        return False
    return dest.exists() and dest.stat().st_size > 0


def apply_chain(source: Path, dest: Path, chain: str) -> bool:
    result = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source),
         "-af", chain, "-ac", "1", "-ar", "24000", "-b:a", "96k", str(dest)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if result.returncode != 0:
        print(f"  ! ffmpeg failed: {(result.stderr or '').strip()[:160]}", file=sys.stderr)
        return False
    return dest.exists() and dest.stat().st_size > 0


async def run(out_dir: Path, voice: str, pitch: str, rate: str, line: str) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)

    if not have_ffmpeg():
        print("ffmpeg was not found on PATH — install it to apply treatments.",
              file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        raw = Path(tmp) / "raw.mp3"
        print(f"  rendering base take with {voice} ...")
        if not await render_raw(voice, pitch, rate, line, raw):
            return 1

        made: list[dict] = []
        for name, note, chain in TREATMENTS:
            dest = out_dir / f"{name}.mp3"
            print(f"  [{name}] {note[:56]} ...")
            if chain == "":
                shutil.copy(raw, dest)
                ok = True
            else:
                ok = apply_chain(raw, dest, chain)
            if ok:
                made.append({"file": dest.name, "name": name, "note": note})

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
<title>JARVIS voice treatments</title>
<style>
  body {{ background:#05070d; color:#e6ecf7; font:15px/1.5 system-ui,sans-serif;
         max-width:720px; margin:0 auto; padding:28px 20px 60px; }}
  h1 {{ font-size:17px; letter-spacing:.12em; text-transform:uppercase; }}
  p.sub {{ color:#7d8bab; font-size:13px; }}
  .item {{ background:#0d1220; border:1px solid #1e2942; border-radius:11px;
           padding:12px 14px; margin-bottom:10px; }}
  .meta {{ display:flex; flex-direction:column; gap:2px; margin-bottom:8px; }}
  .meta b {{ color:#35d1ff; }}
  .meta span {{ color:#7d8bab; font-size:12px; }}
  audio {{ width:100%; height:34px; }}
</style></head><body>
<h1>JARVIS voice treatments</h1>
<p class="sub">Same line, same voice ({html.escape(voice)}), different production.
Play them in order and tell me which number is closest.</p>
{rows}
</body></html>
"""

    (out_dir / "treatments.html").write_text(sheet, encoding="utf-8")
    print()
    print(f"  {len(made)} treatments written to {out_dir}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Audition JARVIS voice treatments.")
    parser.add_argument("--out-dir", default=str(Path(__file__).resolve().parent.parent / "voice" / "treatments"))
    parser.add_argument("--voice", default="en-GB-RyanNeural")
    parser.add_argument("--pitch", default="-8Hz")
    parser.add_argument("--rate", default="-6%")
    parser.add_argument("--line", default=LINE)
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    print()
    print("  Auditioning voice treatments")
    print("  " + "-" * 46)
    code = asyncio.run(run(out_dir, args.voice, args.pitch, args.rate, args.line))
    if code == 0 and args.open:
        webbrowser.open((out_dir / "treatments.html").as_uri())
    return code


if __name__ == "__main__":
    raise SystemExit(main())
