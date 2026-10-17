"""Bounded, detached NetCDF arrays shared by satellite product jobs."""

from collections import OrderedDict
from contextlib import contextmanager
import os
from pathlib import Path
import threading

import xarray as xr

NETCDF_LOCK = threading.RLock()
_RENDER_GATE = threading.BoundedSemaphore(2)
_LOCAL = threading.local()
_ARRAYS: OrderedDict = OrderedDict()
_METADATA: OrderedDict = OrderedDict()
_CACHE_BYTES = max(16, int(os.getenv("SATELLITE_DATASET_CACHE_MB", "192"))) * 1024 ** 2
_COMMON = {"goes_imager_projection", "kappa0", "planck_fk1", "planck_fk2",
           "planck_bc1", "planck_bc2", "band_id", "x", "y"}


def clear_dataset_cache() -> None:
    with NETCDF_LOCK:
        _ARRAYS.clear()
        _METADATA.clear()


@contextmanager
def _render_slot():
    """A composite's two nested datasets share one render slot."""
    depth = getattr(_LOCAL, "depth", 0)
    if depth == 0:
        _RENDER_GATE.acquire()
    _LOCAL.depth = depth + 1
    try:
        yield
    finally:
        _LOCAL.depth -= 1
        if depth == 0:
            _RENDER_GATE.release()


@contextmanager
def load_dataset(path: Path, variables: tuple[str, ...] | None = None):
    """Load only required arrays under the HDF5 lock; render after releasing it.

    Cached arrays never escape directly: each job receives its own copies so
    existing renderer transformations cannot mutate another product's input.
    A render gate bounds active image buffers independently of download jobs.
    """
    path = Path(path)
    stat = path.stat()
    identity = (str(path), stat.st_size, stat.st_mtime_ns)
    with _render_slot():
        with NETCDF_LOCK:
            metadata = _METADATA.get(identity)
            requested = set(variables or ())
            if metadata is None or variables is None:
                with xr.open_dataset(path) as source:
                    names = set(source.variables)
                    common = _COMMON & names
                    base = source[list(common)].load().copy(deep=True)
                    metadata = (base, names, set(source.coords))
                    _METADATA[identity] = metadata
                    requested = names - common if variables is None else requested & names
                    for name in requested:
                        key = (identity, name)
                        if key not in _ARRAYS:
                            # Cache only this variable, not its attached auxiliary
                            # coordinates. GOES SST arrays carry band-ID coordinates
                            # that must not be reinserted as ordinary data variables.
                            _ARRAYS[key] = source[name].variable.load().copy(deep=True)
            else:
                requested &= metadata[1]
                missing = [name for name in requested if (identity, name) not in _ARRAYS]
                if missing:
                    with xr.open_dataset(path) as source:
                        for name in missing:
                            _ARRAYS[(identity, name)] = source[name].variable.load().copy(deep=True)
            detached = metadata[0].copy(deep=True)
            for name in requested:
                key = (identity, name)
                target = detached.coords if name in metadata[2] else detached
                target[name] = _ARRAYS[key].copy(deep=True)
                _ARRAYS.move_to_end(key)
            _METADATA.move_to_end(identity)
            while len(_METADATA) > 8:
                _METADATA.popitem(last=False)
            while sum(array.nbytes for array in _ARRAYS.values()) > _CACHE_BYTES:
                _ARRAYS.popitem(last=False)
        try:
            yield detached
        finally:
            detached.close()
