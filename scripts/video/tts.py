"""Generate the narration clips for the demo video and report their durations.

    python scripts/video/tts.py            # writes media/narration/<id>.mp3 and media/narration/durations.json

Voice: Microsoft's en-IN-NeerjaExpressiveNeural, through the open-source edge-tts client (no key needed).
"""

import asyncio
import json
import subprocess
from pathlib import Path

import edge_tts

ROOT = Path(__file__).resolve().parents[2]
SPEC = json.loads((ROOT / "scripts/video/narration.json").read_text())
OUT = ROOT / "media/narration"


LIMIT = asyncio.Semaphore(4)   # the service drops requests when too many arrive at once


async def speak(seg: dict) -> tuple[str, float]:
    path = OUT / f"{seg['id']}.mp3"
    async with LIMIT:
        for attempt in range(4):
            try:
                await edge_tts.Communicate(seg["text"], SPEC["voice"], rate=SPEC.get("rate", "+0%")).save(str(path))
                break
            except edge_tts.exceptions.NoAudioReceived:
                if attempt == 3:
                    raise
                await asyncio.sleep(2 * (attempt + 1))
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, check=True)
    return seg["id"], round(float(probe.stdout.strip()), 2)


async def main():
    OUT.mkdir(parents=True, exist_ok=True)
    durations = dict(await asyncio.gather(*(speak(s) for s in SPEC["segments"])))
    (OUT / "durations.json").write_text(json.dumps(durations, indent=2))
    for k, v in durations.items():
        print(f"{k:<15} {v:>6.1f}s")
    print(f"{'total':<15} {sum(durations.values()):>6.1f}s")


if __name__ == "__main__":
    asyncio.run(main())
