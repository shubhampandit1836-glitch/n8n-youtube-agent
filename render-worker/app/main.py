import asyncio
import subprocess
from pathlib import Path
import edge_tts
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from app.visuals import router as visuals_router
from app.render import router as render_router
from app.assemble import router as assemble_router

app = FastAPI(title="Media Worker")
app.include_router(visuals_router)
app.include_router(render_router)
app.include_router(assemble_router)

MEDIA = Path("/data/media")

class Segment(BaseModel):
    id: str
    text: str

class TTSRequest(BaseModel):
    runId: str
    voice: str = "hi-IN-MadhurNeural"
    rate: str = "+8%"
    segments: list[Segment]

def ffprobe_duration(path: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {result.stderr[:300]}")
    return round(float(result.stdout.strip()), 3)

async def synth_segment(text: str, voice: str, rate: str, out_path: Path, tries: int = 3):
    last_error = None
    for attempt in range(1, tries + 1):
        try:
            communicate = edge_tts.Communicate(text, voice, rate=rate)
            await communicate.save(str(out_path))
            size = out_path.stat().st_size if out_path.exists() else 0
            if size > 1000:
                return
            last_error = RuntimeError(f"suspiciously small output ({size} bytes)")
            out_path.unlink(missing_ok=True)
        except Exception as e:
            last_error = e
        await asyncio.sleep(1.0 * attempt)
    raise RuntimeError(f"TTS failed after {tries} attempts: {last_error}")

@app.get("/health")
def health():
    try:
        result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            raise RuntimeError("ffmpeg not available")
        return {"status": "ok", "ffmpeg": result.stdout.splitlines()[0], "tts": "edge-tts loaded"}
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "error", "detail": str(e)})

@app.post("/tts")
async def tts(req: TTSRequest):
    if not req.segments:
        return JSONResponse(status_code=422, content={"error": "segments list is empty"})

    run_dir = MEDIA / "runs" / req.runId / "audio"
    run_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for seg in req.segments:
        text = (seg.text or "").strip()
        if not text:
            return JSONResponse(status_code=422, content={"error": f"segment {seg.id}: empty text"})

        out_path = run_dir / f"{seg.id}.mp3"
        try:
            await synth_segment(text, req.voice, req.rate, out_path)
        except Exception as e:
            return JSONResponse(status_code=502, content={
                "error": f"edge-tts failed on segment {seg.id} (voice: {req.voice})",
                "detail": str(e)[:800],
            })

        results.append({
            "id": seg.id,
            "audioPath": str(out_path),
            "durationSec": ffprobe_duration(out_path),
        })
        await asyncio.sleep(0.2)

    return {
        "runId": req.runId,
        "voice": req.voice,
        "segmentCount": len(results),
        "totalDurationSec": round(sum(r["durationSec"] for r in results), 2),
        "segments": results,
    }
