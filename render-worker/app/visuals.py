import asyncio
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()
MEDIA = Path("/data/media")
PEXELS_API = "https://api.pexels.com"
MIN_VIDEO_WIDTH = 1280

class PexelsFatalError(RuntimeError):
    """Auth or rate-limit problems — abort the whole request, not just one segment."""

class VisualSegment(BaseModel):
    id: str
    type: str = "beat"
    durationSec: float
    visualQuery: str
    sfx: str = "none"
    onScreenText: str = ""
    audioPath: str = ""

class VisualsRequest(BaseModel):
    runId: str
    orientation: str = "landscape"
    fallbackQuery: str = "space nebula stars"
    segments: list[VisualSegment]

# ---------- Pexels client ----------
async def pexels_get(client: httpx.AsyncClient, url: str, params: dict) -> dict:
    resp = await client.get(url, params=params)
    if resp.status_code == 401:
        raise PexelsFatalError(
            "Pexels 401 Unauthorized — check PEXELS_API_KEY in docker-compose "
            "(raw key, no 'Bearer' prefix, no quotes)"
        )
    if resp.status_code == 429:
        raise PexelsFatalError("Pexels rate limit hit (200 requests/hour) — retry in a few minutes")
    resp.raise_for_status()
    return resp.json()

async def search_videos(client, query, orientation, per_page=8):
    data = await pexels_get(client, f"{PEXELS_API}/videos/search", {
        "query": query, "per_page": per_page, "orientation": orientation
    })
    if "videos" not in data:
        raise RuntimeError(f"unexpected video-search response, top-level keys: {list(data.keys())}")
    return data["videos"]

async def search_photos(client, query, orientation, per_page=8):
    data = await pexels_get(client, f"{PEXELS_API}/v1/search", {
        "query": query, "per_page": per_page, "orientation": orientation
    })
    if "photos" not in data:
        raise RuntimeError(f"unexpected photo-search response, top-level keys: {list(data.keys())}")
    return data["photos"]

# ---------- candidate extraction ----------
def video_candidate(item: dict):
    files = [
        f for f in item.get("video_files", [])
        if f.get("file_type") == "video/mp4" and (f.get("width") or 0) >= MIN_VIDEO_WIDTH
    ]
    if not files:
        return None
    files.sort(key=lambda f: f["width"])  # smallest HD variant = fastest download
    best = files[0]
    return {
        "kind": "video",
        "sourceId": item.get("id"),
        "url": best["link"],
        "clipDurationSec": item.get("duration"),
    }

def photo_candidate(item: dict, orientation: str):
    src = item.get("src") or {}
    keys = ("portrait", "large2x", "original") if orientation == "portrait" else ("landscape", "large2x", "original")
    for key in keys:
        if src.get(key):
            return {"kind": "photo", "sourceId": item.get("id"), "url": src[key]}
    return None

def rank_video_candidates(items, need_sec, used_ids):
    cands = []
    for v in items:
        if v.get("id") in used_ids:
            continue
        c = video_candidate(v)
        if c:
            cands.append(c)
    covering = [c for c in cands if (c["clipDurationSec"] or 0) >= need_sec]
    covering.sort(key=lambda c: c["clipDurationSec"] or 0)  # shortest clip that covers
    rest = sorted(cands, key=lambda c: -(c["clipDurationSec"] or 0))  # else longest
    return covering + rest

# ---------- download with validation + retries ----------
def ffprobe_duration_ok(path: Path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=30
        )
    except Exception as e:
        return False, f"ffprobe crashed: {e}"
    if r.returncode != 0 or not r.stdout.strip():
        return False, (r.stderr or "no output")[:200]
    try:
        float(r.stdout.strip())
    except ValueError:
        return False, f"unparseable duration: {r.stdout[:50]}"
    return True, ""

async def download(client, cand, out_path: Path, min_bytes: int, verify_video: bool = False, tries: int = 3):
    tmp = out_path.with_suffix(out_path.suffix + ".part")
    last_err = None
    for attempt in range(1, tries + 1):
        try:
            tmp.unlink(missing_ok=True)
            async with client.stream("GET", cand["url"]) as resp:
                resp.raise_for_status()
                with open(tmp, "wb") as f:
                    async for chunk in resp.aiter_bytes(65536):
                        f.write(chunk)
            size = tmp.stat().st_size
            if size < min_bytes:
                raise RuntimeError(f"file too small ({size} bytes)")
            if verify_video:
                ok, detail = ffprobe_duration_ok(tmp)
                if not ok:
                    raise RuntimeError(f"ffprobe rejected it: {detail}")
            tmp.replace(out_path)
            return size
        except Exception as e:
            last_err = e
            await asyncio.sleep(1.0 * attempt)
    tmp.unlink(missing_ok=True)
    raise RuntimeError(f"download failed after {tries} attempts: {last_err}")

# ---------- endpoint ----------
def base_record(seg: VisualSegment) -> dict:
    return {
        "id": seg.id, "type": seg.type, "durationSec": seg.durationSec,
        "visualQuery": seg.visualQuery, "sfx": seg.sfx,
        "onScreenText": seg.onScreenText, "audioPath": seg.audioPath,
    }

@router.post("/visuals")
async def build_visuals(req: VisualsRequest):
    api_key = os.environ.get("PEXELS_API_KEY", "").strip()
    if not api_key:
        return JSONResponse(status_code=500, content={
            "error": "PEXELS_API_KEY not set on render-worker — add it to docker-compose.yml and rebuild"
        })
    if not req.segments:
        return JSONResponse(status_code=422, content={"error": "segments list is empty"})

    run_dir = MEDIA / "runs" / req.runId
    vis_dir = run_dir / "visuals"
    vis_dir.mkdir(parents=True, exist_ok=True)
    used_ids = set()
    resolved = []
    failures = []

    try:
        async with httpx.AsyncClient(
            headers={"Authorization": api_key},
            timeout=httpx.Timeout(60.0, connect=15.0),
            follow_redirects=True,
        ) as client:
            for seg in req.segments:
                record = None
                reasons = []
                for query in (seg.visualQuery, req.fallbackQuery):
                    if record:
                        break
                    # --- video pass
                    try:
                        await asyncio.sleep(0.4)
                        found = await search_videos(client, query, req.orientation)
                        ranked = rank_video_candidates(found, seg.durationSec, used_ids)
                    except PexelsFatalError:
                        raise
                    except Exception as e:
                        ranked = []
                        reasons.append(f"video search '{query}': {e}")

                    for cand in ranked[:3]:
                        out = vis_dir / f"{seg.id}.mp4"
                        try:
                            size = await download(client, cand, out, min_bytes=100_000, verify_video=True)
                            record = {
                                **base_record(seg),
                                "downloadBytes": size,
                                "visual": {
                                    "kind": "video",
                                    "path": str(out),
                                    "sourceId": cand["sourceId"],
                                    "sourceUrl": cand["url"],
                                    "clipDurationSec": cand["clipDurationSec"],
                                },
                            }
                            used_ids.add(cand["sourceId"])
                            break
                        except Exception as e:
                            reasons.append(f"video #{cand['sourceId']}: {e}")

                    if record:
                        break

                    # --- photo pass
                    try:
                        await asyncio.sleep(0.4)
                        photos = await search_photos(client, query, req.orientation)
                    except PexelsFatalError:
                        raise
                    except Exception as e:
                        photos = []
                        reasons.append(f"photo search '{query}': {e}")

                    pcands = []
                    for p in photos:
                        if p.get("id") in used_ids:
                            continue
                        c = photo_candidate(p, req.orientation)
                        if c:
                            pcands.append(c)

                    for cand in pcands[:3]:
                        out = vis_dir / f"{seg.id}.jpg"
                        try:
                            size = await download(client, cand, out, min_bytes=30_000)
                            record = {
                                **base_record(seg),
                                "downloadBytes": size,
                                "visual": {
                                    "kind": "photo",
                                    "path": str(out),
                                    "sourceId": cand["sourceId"],
                                    "sourceUrl": cand["url"],
                                    "clipDurationSec": None,
                                },
                            }
                            used_ids.add(cand["sourceId"])
                            break
                        except Exception as e:
                            reasons.append(f"photo #{cand['sourceId']}: {e}")

                if record:
                    resolved.append(record)
                else:
                    failures.append({"segmentId": seg.id, "reasons": reasons[-6:]})

    except PexelsFatalError as e:
        return JSONResponse(status_code=502, content={"error": str(e)})

    if failures:
        return JSONResponse(status_code=502, content={
            "error": f"{len(failures)} segment(s) could not get a visual",
            "failures": failures,
            "resolvedSoFar": [r["id"] for r in resolved],
        })

    manifest = {
        "runId": req.runId,
        "orientation": req.orientation,
        "createdAt": datetime.now(timezone.utc).isoformat(),
        "segments": resolved,
    }
    manifest_path = run_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    n_video = sum(1 for r in resolved if r["visual"]["kind"] == "video")
    return {
        "runId": req.runId,
        "segmentCount": len(resolved),
        "videos": n_video,
        "photos": len(resolved) - n_video,
        "manifestPath": str(manifest_path),
        "segments": [
            {
                "id": r["id"],
                "kind": r["visual"]["kind"],
                "sourceId": r["visual"]["sourceId"],
                "query": r["visualQuery"],
                "file": r["visual"]["path"],
                "bytes": r["downloadBytes"],
            }
            for r in resolved
        ],
    }
