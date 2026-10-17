"""Live 3D map endpoints, restricted to Omaha and Hastings."""

import asyncio
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import sys
import time

import psutil

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from pipeline.config import DATA_DIR
from pipeline.radar.config import RADAR_CACHE_DIR
from pipeline.radar.map3d import SITES, VERSION

router = APIRouter()
logger = logging.getLogger(__name__)
CACHE = Path(os.getenv('WX_RADAR_MAP3D_CACHE', str(DATA_DIR / 'radar_map3d')))
STATIC = Path(__file__).parent / 'static'
_pending = asyncio.Semaphore(2)
_jobs: dict[str, asyncio.Task] = {}


def has_headroom() -> bool:
    if psutil.virtual_memory().available < 768 * 1024 ** 2:
        return False
    try:
        line = next(line for line in Path('/proc/self/cgroup').read_text().splitlines() if line.startswith('0::'))
        cgroup = Path('/sys/fs/cgroup') / line[3:].lstrip('/')
        high = (cgroup / 'memory.high').read_text().strip()
        if high != 'max':
            current = int((cgroup / 'memory.current').read_text())
            stats = dict(line.split() for line in (cgroup / 'memory.stat').read_text().splitlines())
            return current - int(stats.get('inactive_file', 0)) + 768 * 1024 ** 2 < int(high)
    except (OSError, ValueError, StopIteration):
        pass
    return True


def checked_site(site: str) -> str:
    if site not in SITES:
        raise HTTPException(404, '3D map coverage is currently Omaha (KOAX) and Hastings (KUEX).')
    return site


def raw_frames(site: str) -> list[Path]:
    # Complete archives only: an in-progress low-tilt chunk is not a full volume.
    paths = list((RADAR_CACHE_DIR / site).glob('*.ar2v'))
    return sorted(paths, key=lambda p: p.name, reverse=True)[:12]


def frame_id(path: Path) -> str:
    stat = path.stat()
    return hashlib.sha256(f'{VERSION}:{path.name}:{stat.st_size}:{stat.st_mtime_ns}'.encode()).hexdigest()[:24]


def ensure_volume(site: str, raw: Path, key: str) -> dict:
    directory = CACHE / site
    directory.mkdir(parents=True, exist_ok=True)
    manifest = directory / f'{key}.json'
    if manifest.exists() and (directory / f'{key}.bin.gz').exists():
        return json.loads(manifest.read_text())
    # Cross-worker lock: at most one decode process for this feature on the host.
    # The process exits after each decode so its large arrays cannot accumulate.
    with (CACHE / 'build.lock').open('a+') as lock:
        deadline = time.monotonic() + 70
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() > deadline:
                    raise TimeoutError('3D volume preparation is busy. Please retry shortly.')
                time.sleep(.1)
        if manifest.exists() and (directory / f'{key}.bin.gz').exists():
            return json.loads(manifest.read_text())
        while not has_headroom():
            if time.monotonic() > deadline:
                raise TimeoutError('Waiting for memory to prepare the 3D radar volume')
            time.sleep(.25)
        env = os.environ.copy()
        env.update(OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1')
        result = subprocess.run([sys.executable, '-m', 'pipeline.radar.map3d', str(raw),
                                 str(directory), site, key], cwd=STATIC.parent.parent,
                                env=env, capture_output=True, text=True, timeout=60)
        if result.returncode:
            logger.error('3D map build failed for %s/%s: %s', site, raw.name, result.stderr[-1200:])
            raise ValueError('This radar volume could not be prepared. Please try the next scan.')
        # Keep at most 24 cached frames per site. Completed responses are immutable.
        old = sorted(directory.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)[24:]
        for path in old:
            path.unlink(missing_ok=True)
            path.with_suffix('.bin.gz').unlink(missing_ok=True)
        return json.loads(manifest.read_text())


async def prepare(site: str, raw: Path, key: str) -> dict:
    async with _pending:
        return await asyncio.to_thread(ensure_volume, site, raw, key)


async def shared_volume(site: str, raw: Path, key: str) -> dict:
    job_key = f'{site}:{key}'
    task = _jobs.get(job_key)
    if task is None:
        if len(_jobs) >= 8:
            raise HTTPException(503, '3D radar preparation is busy. Please retry shortly.')
        task = asyncio.create_task(prepare(site, raw, key))
        _jobs[job_key] = task
        def finished(done):
            _jobs.pop(job_key, None)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(finished)
    # Keep reservations and the task alive if the requesting browser disconnects.
    return await asyncio.shield(task)


@router.get('/radar-map-3d')
@router.get('/wx/radar-map-3d')
async def radar_map_page():
    return FileResponse(STATIC / 'radar-map3d.html', headers={'Cache-Control': 'no-cache'})


@router.get('/api/radar/map3d/{site}/frames')
async def frames(site: str):
    checked_site(site)
    output = []
    for path in raw_frames(site):
        try:
            match = re.search(r'(\d{8})_(\d{4,6})', path.name)
            if match is None:
                continue
            stamp = match.group(1) + match.group(2)
            dt = datetime.strptime(stamp, '%Y%m%d%H%M%S' if len(stamp) == 14 else '%Y%m%d%H%M')
            output.append({'scan': path.name, 'key': frame_id(path),
                           'scan_time': dt.replace(tzinfo=timezone.utc).isoformat()})
        except (OSError, ValueError):
            continue
    return JSONResponse({'site': site, 'frames': output}, headers={'Cache-Control': 'no-store'})


@router.get('/api/radar/map3d/{site}')
async def volume(site: str, scan: str | None = Query(None, max_length=100)):
    checked_site(site)
    candidates = raw_frames(site)
    raw = next((p for p in candidates if scan is None or p.name == scan), None)
    if raw is None:
        raise HTTPException(404, 'No complete scan is available for that time.')
    try:
        key = frame_id(raw)
        result = await shared_volume(site, raw, key)
    except (TimeoutError, subprocess.TimeoutExpired):
        raise HTTPException(503, '3D volume preparation is busy. Please retry shortly.')
    except (ValueError, OSError):
        raise HTTPException(503, 'Radar data is changing or unavailable. Please retry shortly.')
    return JSONResponse({**result, 'data_url': f'/api/radar/map3d/data/{site}/{key}'},
                        headers={'Cache-Control': 'no-store'})


@router.get('/api/radar/map3d/data/{site}/{key}')
async def volume_data(site: str, key: str):
    checked_site(site)
    if not re.fullmatch(r'[a-f0-9]{24}', key):
        raise HTTPException(404, 'Unknown radar frame')
    path = CACHE / site / f'{key}.bin.gz'
    if not path.is_file():
        raise HTTPException(404, 'Radar frame expired. Reload the latest scan.')
    return FileResponse(path, media_type='application/octet-stream',
                        headers={'Content-Encoding': 'gzip', 'Cache-Control': 'public, max-age=3600',
                                 'Vary': 'Accept-Encoding'})
