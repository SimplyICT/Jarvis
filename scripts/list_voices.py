"""List edge-tts voices worth auditioning for the JARVIS register."""
import asyncio
import edge_tts


async def main() -> None:
    voices = await edge_tts.list_voices()

    def personalities(v):
        return " ".join(v.get("VoiceTag", {}).get("VoicePersonalities") or [])

    print("=== en-GB (the JARVIS accent) ===")
    for v in sorted(voices, key=lambda x: x["ShortName"]):
        if v["Locale"].startswith("en-GB"):
            print(f"  {v['ShortName']:<30} {v['Gender']:<7} {personalities(v)}")

    print()
    print("=== male English voices tagged authoritative/calm/mature ===")
    wanted = ("Authority", "Calm", "Confident", "Mature", "Rational", "Deep", "Narration")
    for v in sorted(voices, key=lambda x: x["ShortName"]):
        if v["Locale"] in ("en-US", "en-AU", "en-IE", "en-NZ", "en-CA") and v["Gender"] == "Male":
            p = personalities(v)
            if any(w in p for w in wanted):
                print(f"  {v['ShortName']:<30} {v['Locale']:<7} {p}")

    print()
    print(f"total voices available: {len(voices)}")


if __name__ == "__main__":
    asyncio.run(main())
