import json
import subprocess
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()
MEDIA = Path("/data/media")
FPS = 30
TAIL_PAD_SEC = 0.4
ZOOM_AMOUNT = 0.10
PRESCALE = 2
FONT_FAMILY = "Arial"
STYLE_NAME = "HinglishMain"

class RenderSegmentRequest(BaseModel):
    runId: str
    segmentId: str

def load_manifest(run_id: str) -> dict:
    manifest_path = MEDIA / "runs" / run_id / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(f"manifest.json not found at {manifest_path}")
    return json.loads(manifest_path.read_text(encoding="utf-8"))

def ass_time(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"

def sanitize_ass_text(text: str) -> str:
    return (text.replace("{", "(").replace("}", ")")
                .replace("\r", " ").replace("\n", "\\N").strip().upper())

def build_ass(text: str, start: float, end: float, width: int, height: int) -> str:
    # High-impact uppercase font size for portrait
    fontsize = 105 if height > width else 75
    # High-contrast bold style: Vibrant Yellow text (&H0000FFFF), heavy black outline (&H00000000)
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: {STYLE_NAME},{FONT_FAMILY},{fontsize},&H0000FFFF,&H0000FFFF,&H00000000,&H96000000,-1,0,0,0,100,100,1,0,1,5,2,2,60,60,320,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,{ass_time(start)},{ass_time(end)},{STYLE_NAME},,0,0,0,,{{\\fad(120,120)}}{sanitize_ass_text(text)}"""

def run_ffmpeg(args: list, log_path: Path):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-y", *args],
            capture_output=True, text=True, timeout=900
        )
    except subprocess.TimeoutExpired:
        log_path.write_text("TIMEOUT after 900s\nCMD: ffmpeg " + " ".join(args), encoding="utf-8")
        raise RuntimeError("ffmpeg timed out after 900s")
    log_path.write_text(
        "CMD: ffmpeg " + " ".join(args) + "\n\n--- stderr ---\n" + proc.stderr,
        encoding="utf-8"
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg exited with code {proc.returncode}. stderr tail: {proc.stderr[-1000:]}"
        )

def probe(path: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, timeout=60
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffprobe failed on output: {r.stderr[:300]}")
    return json.loads(r.stdout)

@router.post("/render/segment")
def render_segment(req: RenderSegmentRequest):
    manifest = load_manifest(req.runId)
    seg = next((s for s in manifest.get("segments", []) if s.get("id") == req.segmentId), None)
    if seg is None:
        return JSONResponse(status_code=404, content={"error": f"segment '{req.segmentId}' not found"})

    visual = seg.get("visual") or {}
    audio_path = seg.get("audioPath") or ""

    run_dir = MEDIA / "runs" / req.runId
    out_dir = run_dir / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    log_path = run_dir / "logs" / f"render_{req.segmentId}.log"
    out_path = out_dir / f"{req.segmentId}.mp4"

    orientation = manifest.get("orientation", "landscape")
    width, height = (1920, 1080) if orientation == "landscape" else (1080, 1920)
    dur = float(seg["durationSec"])
    total = round(dur + TAIL_PAD_SEC, 3)
    frames = max(2, round(total * FPS))
    text = (seg.get("onScreenText") or "").strip()

    filters = []
    if visual.get("kind") == "photo":
        big_w, big_h = width * PRESCALE, height * PRESCALE
        filters.append(
            f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={big_w}:{big_h},"
            f"zoompan=z='1+{ZOOM_AMOUNT}*on/{frames - 1}':"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d={frames}:s={width}x{height}:fps={FPS}"
        )
    else:
        filters.append(
            f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={width}:{height},fps={FPS},"
            f"tpad=stop_mode=clone:stop_duration={round(TAIL_PAD_SEC + 2, 3)},"
            f"trim=duration={total},setpts=PTS-STARTPTS"
        )

    if text:
        subs_dir = run_dir / "subs"
        subs_dir.mkdir(parents=True, exist_ok=True)
        ass_path = subs_dir / f"{req.segmentId}.ass"
        ass_path.write_text(build_ass(text, 0.15, dur + 0.25, width, height), encoding="utf-8")
        filters.append(f"subtitles=filename={ass_path}")

    filters.append("format=yuv420p")

    args = [
        "-i", visual["path"],
        "-i", audio_path,
        "-filter_complex", "[0:v]" + ",".join(filters) + "[v]",
        "-map", "[v]", "-map", "1:a",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-af", "aformat=sample_rates=48000:channel_layouts=stereo,apad",
        "-t", str(total),
        "-movflags", "+faststart",
        str(out_path),
    ]

    run_ffmpeg(args, log_path)
    info = probe(out_path)
    actual_dur = float(info["format"]["duration"])

    return {
        "runId": req.runId,
        "segmentId": req.segmentId,
        "outputPath": str(out_path),
        "actualDurationSec": round(actual_dur, 2),
    }
