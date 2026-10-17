"""Read-only projection arrays shared through the OS file cache across workers."""
import hashlib
import logging
import os
from pathlib import Path
import tempfile

import numpy as np

LOGGER = logging.getLogger(__name__)


def shared_arrays(directory, key, shape, names, build, dtype=np.float32):
    """Load complete arrays or atomically publish exact computed replacements."""
    digest = hashlib.sha256(repr(("geometry-v1", np.__version__, key)).encode()).hexdigest()[:20]
    prefix = Path(directory) / ("geometry-" + digest)
    result = {}
    for name in names:
        path = prefix.with_name(prefix.name + "." + name + ".npy")
        try:
            value = np.load(path, mmap_mode="r", allow_pickle=False)
            if value.shape != shape or value.dtype != dtype:
                raise ValueError("invalid shared geometry shape or dtype")
            result[name] = value
        except (OSError, ValueError, EOFError):
            pass
    if len(result) == len(names):
        return result, True
    values = build()
    for name in names:
        if name in result:
            continue
        value = values[name]
        # Keep calculations exact if a caller supplies another floating type.
        if value.shape != shape or value.dtype != dtype:
            result[name] = value
            continue
        path = prefix.with_name(prefix.name + "." + name + ".npy")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=prefix.parent, prefix=".geometry-",
                                             suffix=".npy", delete=False) as handle:
                temporary = Path(handle.name)
                np.save(handle, value, allow_pickle=False)
            temporary.chmod(0o644)
            os.replace(temporary, path)
            result[name] = np.load(path, mmap_mode="r", allow_pickle=False)
        except (OSError, ValueError) as exc:
            LOGGER.warning("Shared geometry cache unavailable: %s", exc)
            result[name] = value
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return result, False
