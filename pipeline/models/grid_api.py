"""Model grid API for client-side value lookup."""

import struct
import gzip
import numpy as np
from pathlib import Path
from fastapi import HTTPException
from fastapi.responses import Response


def _find_grib_file(model: str, run_id: str, fxx: int) -> Path:
    """Find GRIB2 file for a specific forecast hour."""
    # Try multiple path patterns
    base_paths = [
        Path(f"/home/main/weather-viewer/data/models/{model}/{run_id}"),
        Path(f"/home/main/weather-viewer/data/forecast/{model}/{run_id}"),
    ]
    
    patterns = [
        f"{model}_{run_id}_{fxx:02d}.grib2",
        f"{model}_{run_id}_{fxx:03d}.grib2", 
        f"*{fxx:02d}.grib2",
        f"*{fxx:03d}.grib2",
        f"*f{fxx:02d}*.grib2",
    ]
    
    for base_path in base_paths:
        if not base_path.exists():
            continue
        for pattern in patterns:
            matches = list(base_path.glob(pattern))
            if matches:
                return matches[0]
    return None


def get_model_grid_binary(model: str, run_id: str, fxx: int, parameter: str) -> Response:
    """Return model grid as compressed binary Float32Array."""
    
    grib_file = _find_grib_file(model, run_id, fxx)
    if not grib_file:
        raise HTTPException(status_code=404, detail=f"GRIB not found for {model}/{run_id}/F{fxx:02d}")
    
    # Use cfgrib/xarray to load the field
    try:
        import xarray as xr
        ds = xr.open_dataset(grib_file, engine='cfgrib', 
                            filter_by_keys={'stepType': 'instant'},
                            backend_kwargs={'indexpath': ''})
        
        # Find parameter (try multiple naming conventions)
        var_name = None
        if parameter in ds.data_vars:
            var_name = parameter
        else:
            # Try common mappings
            param_map = {
                'temp_2m': ['t2m', 'TMP_2maboveground', 'TMP_2M'],
                'dewpoint_2m': ['d2m', 'DPT_2maboveground'],
                'wind_u_10m': ['u10', 'UGRD_10maboveground'],
                'wind_v_10m': ['v10', 'VGRD_10maboveground'],
                'precip': ['tp', 'APCP_surface'],
                'reflectivity': ['refc', 'REFC_entireatmosphere'],
                'cape': ['cape', 'CAPE_surface'],
                'cin': ['cin', 'CIN_surface'],
            }
            for candidate in param_map.get(parameter, [parameter]):
                if candidate in ds.data_vars:
                    var_name = candidate
                    break
        
        if var_name is None:
            raise HTTPException(status_code=404, detail=f"Parameter {parameter} not in GRIB")
        
        da = ds[var_name]
        
        # Handle time/step dimensions
        if 'step' in da.dims and fxx > 0:
            step_idx = min(fxx, len(da.step) - 1)
            da = da.isel(step=step_idx)
        
        # Get lat/lon coords
        lat = da.latitude.values if hasattr(da, 'latitude') else da.lat.values
        lon = da.longitude.values if hasattr(da, 'longitude') else da.lon.values
        values = da.values
        
        # Handle 2D vs 1D coords
        if lat.ndim == 1:
            lat_min, lat_max = float(lat.min()), float(lat.max())
            lon_min, lon_max = float(lon.min()), float(lon.max())
        else:
            lat_min, lat_max = float(lat.min()), float(lat.max())
            lon_min, lon_max = float(lon.min()), float(lon.max())
        
        nj, ni = values.shape[-2:]
        val_min, val_max = float(np.nanmin(values)), float(np.nanmax(values))
        
        # Pack header: 32 bytes
        header = struct.pack(
            '<IIffffff',
            ni, nj,
            lat_min, lat_max, lon_min, lon_max,
            val_min, val_max
        )
        
        # Convert to float32 and compress
        flat_data = values.astype(np.float32).tobytes()
        full_payload = header + flat_data
        compressed = gzip.compress(full_payload, compresslevel=4)
        
        ds.close()
        
        return Response(
            content=compressed,
            media_type="application/octet-stream",
            headers={
                "Content-Encoding": "gzip",
                "X-Model": model,
                "X-Run": run_id,
                "X-Fxx": str(fxx),
                "X-Param": parameter,
                "X-Grid-Shape": f"{ni},{nj}",
                "Cache-Control": "public, max-age=300"
            }
        )
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to load grid: {e}")
