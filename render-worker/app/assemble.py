import json
import threading
import time
import uuid
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from app.render import FPS, MEDIA, ZOOM_AMOUNT, build_ass, load_manifest, probe, run_ffmpeg

router = APIRouter()
ASSETS = Path("/data/assets")
TRANSITION_SEC = 0.35        # crossfade length between segments
SEG_TAIL_PAD_SEC = 0.8       # frozen/silent tail per segment (transitions eat into this)
NARRATION_OFFSET_SEC = 0.15  # narration starts just after the cut
OUTRO_TAIL_SEC = 1.2         # frozen last frame + music fade-out
SFX_VOLUME = {"impact": 0.9, "whoosh": 0.5, "pop": 0.5, "ding": 0.45}
AFORMAT = "aformat=sample_rates=48000:channel_layouts=stereo"
JOBS: dict = {}
JOBS_LOCK = threading.Lock()

class FinalRenderRequest(BaseModel):
    runId: str
    musicFile: str = ""  # empty = auto-pick first available track

def _update(job_id: str, **fields):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job is None:
            return
        job.update(fields)
        snapshot = dict(job)
    try:
        run_dir = MEDIA / "runs" / snapshot["runId"]
        if run_dir.exists():
            (run_dir / "render_job.json").write_text(
                json.dumps(snapshot, indent=2), encoding="utf-8")
    except Exception:
        pass

def pick_music(requested: str) -> Path:
    music_dir = ASSETS / "music"
    if requested:
        p = music_dir / requested
        if p.exists():
            return p
    tracks = sorted(music_dir.glob("music_*.mp3"))
    if not tracks:
        raise FileNotFoundError(
            f"No music_*.mp3 found in {music_dir} — add your Step 8 tracks first")
    return tracks[0]

# ---------- PASS 1: silent per-segment intermediates ----------
def render_intermediate(seg: dict, run_dir: Path, inter_dir: Path,
                        log_dir: Path, width: int, height: int) -> float:
    visual = seg["visual"]
    out_path = inter_dir / f"{seg['id']}.mp4"
    dur = float(seg["durationSec"])
    total = round(dur + SEG_TAIL_PAD_SEC, 3)
    frames = max(2, round(total * FPS))
    filters = []

    if visual.get("kind") == "photo":
        big_w, big_h = width * 2, height * 2
        filters.append(
            f"scale={big_w}:{big_h}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={big_w}:{big_h},"
            f"zoompan=z='1+{ZOOM_AMOUNT}*on/{frames - 1}':"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d={frames}:s={width}x{height}:fps={FPS},"
            f"setsar=1")
    else:
        filters.append(
            f"scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
            f"crop={width}:{height},setsar=1,fps={FPS},"
            f"tpad=stop_mode=clone:stop_duration={round(SEG_TAIL_PAD_SEC + 2, 3)},"
            f"trim=duration={total},setpts=PTS-STARTPTS")

    text = (seg.get("onScreenText") or "").strip()
    if text:
        subs_dir = run_dir / "subs"
        subs_dir.mkdir(parents=True, exist_ok=True)
        ass_path = subs_dir / f"{seg['id']}.ass"
        ass_path.write_text(
            build_ass(text, 0.15, dur + 0.35, width, height), encoding="utf-8")
        filters.append(f"subtitles=filename={ass_path}")

    filters.append("format=yuv420p")
    args = [
        "-i", visual["path"],
        "-filter_complex", "[0:v]" + ",".join(filters) + "[v]",
        "-map", "[v]", "-an",
        "-c:v", "libx264", "-preset", "medium", "-crf", "18",
        "-t", str(total),
        str(out_path),
    ]
    run_ffmpeg(args, log_dir / f"pass1_{seg['id']}.log")
    return float(probe(out_path)["format"]["duration"])

# ---------- timeline math ----------
def build_timeline(durations: list) -> tuple:
    starts, t = [], 0.0
    for i, d in enumerate(durations):
        starts.append(round(t, 3))
        if i < len(durations) - 1:
            t += d - TRANSITION_SEC
    total = round(t + durations[-1] + OUTRO_TAIL_SEC, 3)
    return starts, total

# ---------- PASS 2: xfade chain + full audio scene ----------
def assemble_final(run_dir: Path, inter_dir: Path, out_dir: Path, log_dir: Path,
                   segs: list, durations: list, starts: list, total: float,
                   music_path: Path, width: int, height: int) -> Path:
    n = len(segs)
    inputs, fc = [], []

    for seg in segs:
        inputs += ["-i", str(inter_dir / f"{seg['id']}.mp4")]

    music_idx = n
    inputs += ["-stream_loop", "-1", "-i", str(music_path)]

    narr_base = n + 1
    for seg in segs:
        inputs += ["-i", seg["audioPath"]]

    sfx_events = []
    for i, seg in enumerate(segs):
        sfx = (seg.get("sfx") or "none").lower()
        if sfx in SFX_VOLUME and (ASSETS / "sfx" / f"{sfx}.wav").exists():
            sfx_events.append((i, sfx))

    sfx_base = narr_base + n
    for _, sfx in sfx_events:
        inputs += ["-i", str(ASSETS / "sfx" / f"{sfx}.wav")]

    # --- video: normalize streams to constant 30 fps CFR, xfade chain + outro freeze
    for i in range(n):
        fc.append(
            f"[{i}:v]setpts=PTS-STARTPTS,scale={width}:{height},setsar=1,format=yuv420p,fps={FPS}[v{i}]"
        )

    offset, prev = 0.0, "v0"
    for i in range(1, n):
        offset += durations[i - 1] - TRANSITION_SEC
        out = f"x{i}" if i < n - 1 else "xchain"
        fc.append(f"[{prev}][v{i}]xfade=transition=fade:"
                  f"duration={TRANSITION_SEC}:offset={round(offset, 3)}[{out}]")
        prev = out

    fc.append(f"[{prev}]tpad=stop_mode=clone:stop_duration={OUTRO_TAIL_SEC},"
              f"format=yuv420p[vout]")

    # --- music: trim to total, fades
    fade_out_start = round(max(0.0, total - 2.5), 3)
    fc.append(f"[{music_idx}:a]{AFORMAT},atrim=duration={total},asetpts=PTS-STARTPTS,"
              f"afade=t=in:st=0:d=1,afade=t=out:st={fade_out_start}:d=2.5,"
              f"volume=0.35[music_bed]")

    # --- narration: delay to cut time, pad to full length
    narr_labels = []
    for i, seg in enumerate(segs):
        delay_ms = int(round((starts[i] + NARRATION_OFFSET_SEC) * 1000))
        adelay = f"adelay={delay_ms}|{delay_ms}," if delay_ms > 0 else ""
        fc.append(f"[{narr_base + i}:a]{AFORMAT},{adelay}"
                  f"apad=whole_dur={total}[n{i}]")
        narr_labels.append(f"[n{i}]")

    fc.append("".join(narr_labels) + f"amix=inputs={n}:normalize=0[narrbus]")
    fc.append("[narrbus]asplit=2[narr_sc][narr_mix]")

    # --- sfx at cut points
    if sfx_events:
        sfx_labels = []
        for k, (i, sfx) in enumerate(sfx_events):
            delay_ms = int(round(starts[i] * 1000))
            adelay = f"adelay={delay_ms}|{delay_ms}," if delay_ms > 0 else ""
            fc.append(f"[{sfx_base + k}:a]{AFORMAT},{adelay}"
                      f"volume={SFX_VOLUME[sfx]},apad=whole_dur={total}[s{k}]")
            sfx_labels.append(f"[s{k}]")

        if len(sfx_labels) == 1:
            fc.append(f"{sfx_labels[0]}[sfxbus]")
        else:
            fc.append("".join(sfx_labels)
                      + f"amix=inputs={len(sfx_labels)}:normalize=0[sfxbus]")
        fc.append("[narr_mix][sfxbus]amix=inputs=2:normalize=0,"
                  "alimiter=limit=0.93[voicefx]")
    else:
        fc.append("[narr_mix]anull[voicefx]")

    # --- music ducks under narration, final mix, YouTube loudness
    fc.append("[music_bed][narr_sc]sidechaincompress=threshold=0.02:ratio=12:"
              "attack=25:release=400[mduck]")
    fc.append("[voicefx][mduck]amix=inputs=2:normalize=0,"
              "loudnorm=I=-14:TP=-1.5:LRA=11,aresample=48000,"
              f"{AFORMAT}[aout]")

    out_path = out_dir / "final.mp4"
    args = [
        *inputs,
        "-filter_complex", ";".join(fc),
        "-map", "[vout]", "-map", "[aout]",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-t", str(total),
        "-movflags", "+faststart",
        str(out_path),
    ]
    run_ffmpeg(args, log_dir / "pass2_assemble.log")
    return out_path

# ---------- job runner ----------
def run_final_render_job(job_id: str, run_id: str, music_file: str):
    started = time.time()
    try:
        _update(job_id, stage="preparing")
        manifest = load_manifest(run_id)
        segs = manifest.get("segments") or []
        if len(segs) < 2:
            raise RuntimeError(f"manifest has {len(segs)} segments; need at least 2")

        missing = []
        for s in segs:
            v = (s.get("visual") or {}).get("path")
            a = s.get("audioPath")
            if not v or not Path(v).exists() or not a or not Path(a).exists():
                missing.append(s["id"])
        if missing:
            raise RuntimeError(f"missing visual/audio files for segments {missing} "
                               f"— re-run the pipeline for this runId")

        music_path = pick_music(music_file)
        orientation = manifest.get("orientation", "landscape")
        width, height = (1920, 1080) if orientation == "landscape" else (1080, 1920)

        run_dir = MEDIA / "runs" / run_id
        inter_dir = run_dir / "intermediate"
        inter_dir.mkdir(parents=True, exist_ok=True)
        out_dir = run_dir / "output"
        out_dir.mkdir(parents=True, exist_ok=True)
        log_dir = run_dir / "logs"

        durations = []
        for i, seg in enumerate(segs):
            _update(job_id, stage="pass1", segmentIndex=i + 1,
                    totalSegments=len(segs))
            cached = inter_dir / f"{seg['id']}.mp4"
            if cached.exists():
                durations.append(float(probe(cached)["format"]["duration"]))
                continue
            durations.append(render_intermediate(seg, run_dir, inter_dir,
                                                 log_dir, width, height))

        starts, total = build_timeline(durations)
        _update(job_id, stage="pass2", totalDurationSec=total)
        out_path = assemble_final(run_dir, inter_dir, out_dir, log_dir, segs,
                                  durations, starts, total, music_path,
                                  width, height)

        info = probe(out_path)
        actual = float(info["format"]["duration"])
        has_v = any(s.get("codec_type") == "video" for s in info["streams"])
        has_a = any(s.get("codec_type") == "audio" for s in info["streams"])
        size = out_path.stat().st_size
        problems = []

        if not (has_v and has_a):
            problems.append("output missing video or audio stream")
        if abs(actual - total) > 0.5:
            problems.append(f"duration {actual:.2f}s != expected {total:.2f}s")
        if size < 500_000:
            problems.append(f"suspiciously small file ({size} bytes)")

        if problems:
            raise RuntimeError("render finished but failed validation: "
                               + "; ".join(problems)
                               + f" — see {log_dir / 'pass2_assemble.log'}")

        _update(job_id, status="done", stage="done", result={
            "outputPath": str(out_path),
            "hostPathHint": f"D:\\youtube\\media\\runs\\{run_id}\\output\\final.mp4",
            "durationSec": round(actual, 2),
            "expectedDurationSec": total,
            "resolution": f"{width}x{height}",
            "fileSizeBytes": size,
            "segmentCount": len(segs),
            "musicFile": music_path.name,
            "renderSeconds": round(time.time() - started, 1),
        })
    except Exception as e:
        _update(job_id, status="failed", stage="failed",
                error=str(e)[:1500])

# ---------- endpoints ----------
@router.post("/render/final")
def start_final_render(req: FinalRenderRequest):
    try:
        load_manifest(req.runId)
    except FileNotFoundError as e:
        return JSONResponse(status_code=404, content={"error": str(e)})

    with JOBS_LOCK:
        for j in JOBS.values():
            if j.get("runId") == req.runId and j.get("status") == "running":
                return {"jobId": j["jobId"], "runId": req.runId,
                        "alreadyRunning": True,
                        "statusUrl": f"/render/status/{j['jobId']}"}
        job_id = uuid.uuid4().hex[:12]
        JOBS[job_id] = {"jobId": job_id, "runId": req.runId, "status": "running",
                        "stage": "queued", "startedAt": time.time()}

    threading.Thread(target=run_final_render_job,
                     args=(job_id, req.runId, req.musicFile), daemon=True).start()
    return {"jobId": job_id, "runId": req.runId,
            "statusUrl": f"/render/status/{job_id}"}

@router.get("/render/status/{job_id}")
def render_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if job is None:
        return JSONResponse(status_code=404, content={
            "error": f"unknown jobId '{job_id}' — if the worker restarted mid-job, "
                     f"just resubmit /render/final (finished segments are cached)"})
    return job
