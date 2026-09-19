import json
import subprocess
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()
MEDIA = Path("/data/media")
FPS = 30
TAIL_PAD_SEC = 0.4        # small silent tail so text fade-out completes
ZOOM_AMOUNT = 0.10        # total Ken Burns zoom over a photo segment
PRESCALE = 2              # render photos at 2x target before zoompan (anti-jitter)
FONT_FAMILY = "Noto Sans Devanagari"
STYLE_NAME = "HindiMain"

class RenderSegmentRequest(BaseModel):
    runId: str
    segmentId: str

# ---------- helpers ----------
def load_manifest(run_id: str) -> dict:
    manifest_path = MEDIA / "runs" / run_id / "manifest.json"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"manifest.json not found at {manifest_path} — check the runId, "
            f"or re-run the 'Pipeline A – Script' workflow so /visuals creates it"
        )
    return json.loads(manifest_path.read_text(encoding="utf-8"))

def ass_time(sec: float) -> str:
    sec = max(0.0, sec)
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"

def sanitize_ass_text(text: str) -> str:
    return (text.replace("{", "(").replace("}", ")")
                .replace("\r", " ").replace("\n", "\\N").strip())

def build_ass(text: str, start: float, end: float, width: int, height: int) -> str:
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: {STYLE_NAME},{FONT_FAMILY},80,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,2,5,80,80,80,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,{ass_time(start)},{ass_time(end)},{STYLE_NAME},,0,0,0,,{{\\fad(150,150)}}{sanitize_ass_text(text)}"""

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

# ---------- endpoint ----------
@router.post("/render/segment")
def render_segment(req: RenderSegmentRequest):
    try:
        manifest = load_manifest(req.runId)
    except FileNotFoundError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})

    seg = next((s for s in manifest.get("segments", []) if s.get("id") == req.segmentId), None)
    if seg is None:
        return JSONResponse(status_code=404, content={
            "error": f"segment '{req.segmentId}' not found in manifest",
            "availableIds": [s.get("id") for s in manifest.get("segments", [])]
        })

    visual = seg.get("visual") or {}
    audio_path = seg.get("audioPath") or ""
    for label, p in (("visual", visual.get("path")), ("audio", audio_path)):
        if not p or not Path(p).exists():
            return JSONResponse(status_code=409, content={
                "error": f"{label} file missing on disk: '{p}' — re-run /visuals for this runId"
            })

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

    # --- video filter chain
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

    text_applied = bool(text)
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

    try:
        run_ffmpeg(args, log_path)
    except RuntimeError as e:
        return JSONResponse(status_code=500, content={
            "error": str(e), "logPath": str(log_path)
        })

    # --- validate output
    info = probe(out_path)
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    a = next((s for s in info["streams"] if s.get("codec_type") == "audio"), None)
    problems = []
    if not v or not a:
        problems.append("output missing video or audio stream")
    elif int(v["width"]) != width or int(v["height"]) != height:
        problems.append(f"resolution {v['width']}x{v['height']} != {width}x{height}")
    actual_dur = float(info["format"]["duration"])
    if abs(actual_dur - total) > 0.35:
        problems.append(f"duration {actual_dur:.2f}s != expected {total:.2f}s")
    size = out_path.stat().st_size
    if size < 50_000:
        problems.append(f"suspiciously small file ({size} bytes)")

    if problems:
        return JSONResponse(status_code=500, content={
            "error": "render completed but failed validation",
            "problems": problems,
            "logPath": str(log_path)
        })

    return {
        "runId": req.runId,
        "segmentId": req.segmentId,
        "outputPath": str(out_path),
        "hostPathHint": f"D:\\youtube\\media\\runs\\{req.runId}\\output\\{req.segmentId}.mp4",
        "visualKind": visual.get("kind"),
        "textOverlay": text_applied,
        "expectedDurationSec": total,
        "actualDurationSec": round(actual_dur, 2),
        "resolution": f"{width}x{height}",
        "fileSizeBytes": size,
        "logPath": str(log_path),
    }
