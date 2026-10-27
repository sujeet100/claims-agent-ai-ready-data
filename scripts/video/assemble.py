"""Cut the screen recording to the narration and lay the narration over it.

    python scripts/video/assemble.py <screencast.mp4> <marks.json> <out.mp4>

marks.json: {"trim": seconds to drop at the start, "marks": {segment id: seconds after the trim},
             "extend": {segment id: extra seconds of picture to keep after its narration},
             "crop": optional ffmpeg crop "w:h:x:y" applied before scaling to 1920x1080}.
Each segment keeps its narration plus a short tail; the dead air between segments (page loads,
waits) is cut down to a beat, and the narration clips are placed at their new positions.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NARRATION = ROOT / "media/narration"
LEAD, TAIL = 0.5, 1.1      # seconds kept before each segment's narration, and after it ends


def intervals(marks: dict[str, float], durations: dict[str, float], extend: dict[str, float] | None = None) -> list[tuple[str, float, float]]:
    order = sorted(marks, key=marks.get)
    out = []
    for i, seg in enumerate(order):
        start = max(0.0, marks[seg] - LEAD)
        end = marks[seg] + durations[seg] + TAIL + (extend or {}).get(seg, 0.0)   # extend: hold the picture a beat longer
        if i + 1 < len(order):   # never run into the next segment's lead-in
            end = min(end, marks[order[i + 1]] - LEAD)
        else:
            end += 1.5
        out.append((seg, start, end))
    return out


def main(video: str, marks_path: str, out: str):
    spec = json.loads(Path(marks_path).read_text())
    durations = json.loads((NARRATION / "durations.json").read_text())
    trim = spec.get("trim", 0.0)
    cuts = intervals(spec["marks"], durations, spec.get("extend"))

    filters, vlabels, alabels, inputs = [], [], [], ["-i", video]
    position = 0.0
    for i, (seg, start, end) in enumerate(cuts):
        s, e = start + trim, end + trim
        filters.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]")
        vlabels.append(f"[v{i}]")
        at = position + (spec["marks"][seg] - start)
        inputs += ["-i", str(NARRATION / f"{seg}.mp3")]
        ms = int(at * 1000)
        filters.append(f"[{i + 1}:a]adelay={ms}|{ms},apad[a{i}]")
        alabels.append(f"[a{i}]")
        position += end - start
    crop = f"crop={spec['crop']}," if spec.get("crop") else ""   # e.g. "2208:1242:0:0" when the capture has a blank margin
    filters.append(f"{''.join(vlabels)}concat=n={len(vlabels)}:v=1:a=0,"
                   f"{crop}scale=1920:1080:flags=lanczos,fps=30,format=yuv420p[vout]")
    filters.append(f"{''.join(alabels)}amix=inputs={len(alabels)}:normalize=0:duration=longest,"
                   f"atrim=end={position:.3f},loudnorm=I=-16:TP=-1.5:LRA=11[aout]")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", *inputs, "-filter_complex", ";".join(filters),
                    "-map", "[vout]", "-map", "[aout]", "-c:v", "libx264", "-preset", "medium", "-crf", "20",
                    "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out], check=True)
    print(f"wrote {out} · {position:.1f}s · {len(cuts)} segments")


if __name__ == "__main__":
    main(*sys.argv[1:4])
