"""Level II Radar pipeline — NEXRAD volume scan processing."""

from .config import (
    NEXRAD_SITES,
    LEVEL2_PRODUCTS,
    RADAR_CONFIG,
    RADAR_CACHE_DIR,
    RADAR_RENDERED_DIR,
    RADAR_REGIONS,
)

def __getattr__(name):
    """Load radar-only dependencies when a radar component is requested."""
    if name == "Level2Downloader":
        from .downloader import Level2Downloader
        return Level2Downloader
    if name == "Level2Scheduler":
        from .scheduler import Level2Scheduler
        return Level2Scheduler
    if name in {"Level2Renderer", "RenderedRadar"}:
        from .renderer import Level2Renderer, RenderedRadar
        return {"Level2Renderer": Level2Renderer, "RenderedRadar": RenderedRadar}[name]
    raise AttributeError(name)

__all__ = [
    'NEXRAD_SITES',
    'LEVEL2_PRODUCTS',
    'RADAR_CONFIG',
    'RADAR_CACHE_DIR',
    'RADAR_RENDERED_DIR',
    'RADAR_REGIONS',
    'Level2Downloader',
    'Level2Scheduler',
    'Level2Renderer',
    'RenderedRadar',
]
