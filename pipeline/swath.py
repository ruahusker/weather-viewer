"""
Running-max UH swath computation.

After each fxx renders, update the swath images so that swath/fNN.png
always shows the pixel-wise maximum UH seen from fxx=1 through fxx=NN.
This creates the classic storm-track streak that builds up as the run
progresses — viewable immediately, not just after the run completes.

The alpha channel is used as the intensity proxy: it scales monotonically
with UH value (0 at floor, max_alpha at vmax), so argmax(alpha) gives
the frame with the highest UH at each pixel.

Call order:
  update_running_max(run_dir, source_param, swath_param, new_fxx)
    — after each fxx source PNG is written (called from handle_result)

  finalize_swath(run_dir, source_param, swath_param)
    — after run completes, fills any gaps from out-of-order rendering
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import numpy as np
from PIL import Image

logger = logging.getLogger(__name__)

_FXX_RE = re.compile(r"^f(\d+)\.png$")


def _source_map(source_dir: Path) -> dict[int, Path]:
    """Return {fxx: path} for all rendered source PNGs, sorted."""
    return dict(sorted(
        (int(m.group(1)), p)
        for p in source_dir.glob("f*.png")
        if (m := _FXX_RE.match(p.name))
    ))


def _load_rgba(path: Path) -> np.ndarray:
    return np.array(Image.open(path).convert("RGBA"), dtype=np.uint8)


def _max_merge(acc: np.ndarray, frame: np.ndarray) -> np.ndarray:
    """Return pixel-wise max of two RGBA arrays using alpha as intensity proxy."""
    replace = frame[:, :, 3].astype(np.uint16) > acc[:, :, 3].astype(np.uint16)
    result = acc.copy()
    result[replace] = frame[replace]
    return result


def update_running_max(
    run_dir: Path,
    source_param: str,
    swath_param: str,
    new_fxx: int,
) -> None:
    """
    Called after source_param/fNN.png is written.

    Writes/updates swath_param/fNN.png = running max from fxx=1..NN,
    then cascades forward through any already-rendered subsequent frames
    so later swath frames also benefit from the newly added fxx.
    """
    source_dir = run_dir / source_param
    swath_dir = run_dir / swath_param

    src_map = _source_map(source_dir)
    if new_fxx not in src_map:
        return

    swath_dir.mkdir(parents=True, exist_ok=True)

    # All source fxx >= new_fxx that exist — cascade through these
    fxx_to_update = [f for f in src_map if f >= new_fxx]
    if not fxx_to_update:
        return

    # Seed the accumulator from the swath frame just before new_fxx
    acc: np.ndarray | None = None
    for prev_fxx in sorted((f for f in src_map if f < new_fxx), reverse=True):
        prev_swath = swath_dir / f"f{prev_fxx:02d}.png"
        if prev_swath.exists():
            try:
                acc = _load_rgba(prev_swath)
            except Exception:
                pass
            break

    try:
        for fxx in sorted(fxx_to_update):
            frame = _load_rgba(src_map[fxx])
            swath = frame if acc is None else _max_merge(acc, frame)
            out_path = swath_dir / f"f{fxx:02d}.png"
            Image.fromarray(swath, "RGBA").save(out_path, format="PNG", compress_level=1)
            acc = swath
    except Exception:
        logger.exception("Swath: failed updating %s -> %s at fxx=%02d",
                         source_param, swath_param, new_fxx)


def finalize_swath(
    run_dir: Path,
    source_param: str,
    swath_param: str,
) -> None:
    """
    Called after run completes. Fills in any swath frames that were missed
    due to out-of-order rendering or pipeline restarts.
    """
    source_dir = run_dir / source_param
    swath_dir = run_dir / swath_param

    src_map = _source_map(source_dir)
    if not src_map:
        return

    # Find first source fxx with no swath image
    first_missing = next(
        (fxx for fxx in src_map if not (swath_dir / f"f{fxx:02d}.png").exists()),
        None,
    )
    if first_missing is not None:
        update_running_max(run_dir, source_param, swath_param, first_missing)
