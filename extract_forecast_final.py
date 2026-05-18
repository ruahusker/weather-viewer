#!/usr/bin/env python3
"""
Extract forecast data for Lincoln, Nebraska from GRIB files.
Lincoln: lat=40.8136, lon=-96.7026
"""
import eccodes
import math
import os
from datetime import datetime, timedelta, timezone

TARGET_LAT = 40.8136
TARGET_LON = -96.7026

def safe_get_str(msg, key):
    try:
        return eccodes.codes_get_string(msg, key)
    except Exception:
        return '?'

def safe_get_int(msg, key):
    try:
        return eccodes.codes_get(msg, key)
    except Exception:
        return -9999

def find_nearest_idx(lats, lons_adj):
    best_dist = float('inf')
    best_idx = 0
    for i, (la, lo) in enumerate(zip(lats, lons_adj)):
        d = abs(la - TARGET_LAT) + abs(lo - TARGET_LON)
        if d < best_dist:
            best_dist = d
            best_idx = i
    return best_idx, best_dist

def extract_fields(grib_path, wanted):
    """
    Extract fields from GRIB file at nearest grid point.
    wanted: list of dicts with keys: shortName, typeOfLevel, level, prefer_step_from_zero
    Returns dict keyed by shortName -> (value, stepRange, grid_lat, grid_lon)
    """
    results = {}
    nearest_idx_cache = {}

    with open(grib_path, 'rb') as f:
        while True:
            msg = eccodes.codes_grib_new_from_file(f)
            if msg is None:
                break
            try:
                sn = safe_get_str(msg, 'shortName')
                tol = safe_get_str(msg, 'typeOfLevel')
                lev = safe_get_int(msg, 'level')
                step = safe_get_str(msg, 'stepRange')

                # Find matching wanted entry
                matched = None
                for w in wanted:
                    if w['shortName'] != sn:
                        continue
                    if w.get('typeOfLevel') and tol != w['typeOfLevel']:
                        continue
                    if w.get('level') is not None and lev != w['level']:
                        continue
                    matched = w
                    break

                if matched is None:
                    continue

                # Get grid data
                lats = eccodes.codes_get_array(msg, 'latitudes')
                lons = eccodes.codes_get_array(msg, 'longitudes')
                vals = eccodes.codes_get_array(msg, 'values')
                grid_size = len(lats)

                if grid_size not in nearest_idx_cache:
                    lons_adj = [l if l <= 180 else l - 360 for l in lons]
                    idx, dist = find_nearest_idx(lats, lons_adj)
                    nearest_idx_cache[grid_size] = (idx, lats[idx], lons_adj[idx])

                idx, grid_lat, grid_lon = nearest_idx_cache[grid_size]
                val = vals[idx]

                key = sn
                if key not in results:
                    results[key] = (val, step, grid_lat, grid_lon)
                else:
                    # For tp: prefer cumulative from step 0
                    if sn == 'tp' and matched.get('prefer_step_from_zero'):
                        old_step = results[key][1]
                        new_starts_at_0 = step.startswith('0-') or step == '0'
                        old_starts_at_0 = old_step.startswith('0-') or old_step == '0'
                        if new_starts_at_0 and not old_starts_at_0:
                            results[key] = (val, step, grid_lat, grid_lon)
                        # if both start at 0, keep the longer one
                        elif new_starts_at_0 and old_starts_at_0:
                            try:
                                new_end = int(step.split('-')[-1]) if '-' in step else int(step)
                                old_end = int(old_step.split('-')[-1]) if '-' in old_step else int(old_step)
                                if new_end > old_end:
                                    results[key] = (val, step, grid_lat, grid_lon)
                            except:
                                pass
            except Exception:
                pass
            finally:
                try:
                    eccodes.codes_release(msg)
                except:
                    pass

    return results


def k_to_f(k):
    return (k - 273.15) * 9/5 + 32

def ms_to_mph(ms):
    return ms * 2.23694

def pa_to_mb(pa):
    return pa / 100.0

def kgm2_to_in(kg):
    """1 kg/m² = 1 mm = 0.0393701 inches"""
    return kg * 0.0393701

def m_to_in(m):
    """1 m = 39.3701 inches (for ECMWF precip in meters)"""
    return m * 39.3701

def wind_dir_str(u, v):
    spd = math.sqrt(u*u + v*v)
    if spd < 0.1:
        return "Calm", 0.0
    deg = math.degrees(math.atan2(-u, -v)) % 360
    dirs = ['N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSW','SW','WSW','W','WNW','NW','NNW']
    idx = round(deg / 22.5) % 16
    return dirs[idx], deg

def print_table(rows, model_name, run_info, precip_note="cumulative from run start"):
    print(f"\n{'='*95}")
    print(f"  MODEL : {model_name}")
    print(f"  RUN   : {run_info}")
    print(f"  POINT : Lincoln, NE — nearest grid: {rows[0]['grid_lat']:.4f}N, {rows[0]['grid_lon']:.4f}E  (target: {TARGET_LAT}N, {TARGET_LON}E)")
    print(f"  PRECIP: {precip_note}")
    print(f"{'='*95}")
    hdr = f"{'Valid Time (UTC)':<18}  {'F+':>4}  {'T2m':>6}  {'Td2m':>6}  {'MSLP':>7}  {'Wind':>9}  {'Dir':>4}  {'Precip':>8}"
    sub = f"{'':18}  {'hr':>4}  {'(°F)':>6}  {'(°F)':>6}  {'(mb)':>7}  {'(mph)':>9}  {'':>4}  {'(in)':>8}"
    print(hdr)
    print(sub)
    print(f"{'-'*95}")
    for r in rows:
        td_str = f"{r['td2m']:6.1f}" if not math.isnan(r['td2m']) else "  n/a "
        print(f"{r['valid_str']:<18}  {r['fxx']:>4}  {r['t2m']:>6.1f}  {td_str}  {r['mslp']:>7.1f}  {r['wspd']:>9.1f}  {r['wdir']:>4}  {r['precip']:>8.3f}")
    print(f"{'='*95}")


# ============================================================
# HRRR: t00z 2026-03-16, fxx 00, 03, 06, 09, 12, 18, 24
# Fields: 2t, 2d (heightAboveGround/2m), mslma (meanSea),
#         10u/10v (heightAboveGround/10m), tp (surface, cumul 0-N)
# ============================================================
hrrr_base = "/home/main/weather-viewer/data/grib_cache/hrrr/20260316"
hrrr_run_utc = datetime(2026, 3, 16, 0, 0, tzinfo=timezone.utc)
hrrr_fxx_list = [0, 3, 6, 9, 12, 18, 24]
hrrr_wanted = [
    {'shortName': '2t',    'typeOfLevel': 'heightAboveGround', 'level': 2},
    {'shortName': '2d',    'typeOfLevel': 'heightAboveGround', 'level': 2},
    {'shortName': 'mslma', 'typeOfLevel': 'meanSea',           'level': 0},
    {'shortName': '10u',   'typeOfLevel': 'heightAboveGround', 'level': 10},
    {'shortName': '10v',   'typeOfLevel': 'heightAboveGround', 'level': 10},
    {'shortName': 'tp',    'typeOfLevel': 'surface',           'level': 0, 'prefer_step_from_zero': True},
]

hrrr_rows = []
for fxx in hrrr_fxx_list:
    fname = f"subset_bd7f07849911__hrrr.t00z.wrfsfcf{fxx:02d}.grib2"
    fpath = os.path.join(hrrr_base, fname)
    if not os.path.exists(fpath):
        continue
    res = extract_fields(fpath, hrrr_wanted)
    valid_time = hrrr_run_utc + timedelta(hours=fxx)
    t2m  = k_to_f(res['2t'][0]) if '2t' in res else float('nan')
    td2m = k_to_f(res['2d'][0]) if '2d' in res else float('nan')
    mslp = pa_to_mb(res['mslma'][0]) if 'mslma' in res else float('nan')
    u10  = res['10u'][0] if '10u' in res else 0.0
    v10  = res['10v'][0] if '10v' in res else 0.0
    wspd = ms_to_mph(math.sqrt(u10**2 + v10**2))
    wdir_str, _ = wind_dir_str(u10, v10)
    tp = kgm2_to_in(res['tp'][0]) if 'tp' in res else 0.0
    glat = res['2t'][2] if '2t' in res else TARGET_LAT
    glon = res['2t'][3] if '2t' in res else TARGET_LON
    hrrr_rows.append({'fxx': fxx, 'valid_str': valid_time.strftime("%Y-%m-%d %H:%MZ"),
                      't2m': t2m, 'td2m': td2m, 'mslp': mslp,
                      'wspd': wspd, 'wdir': wdir_str, 'precip': tp,
                      'grid_lat': glat, 'grid_lon': glon,
                      'tp_step': res['tp'][1] if 'tp' in res else 'N/A'})

print_table(hrrr_rows, "HRRR CONUS 3km", "2026-03-16 00Z",
            precip_note="cumul total precip from run start (kg/m²→in); F00 is instantaneous=0")
print(f"  TP step ranges: " + ", ".join(f"F{r['fxx']:02d}={r['tp_step']}" for r in hrrr_rows))


# ============================================================
# NAM 3km (CONUS Nest): t00z 2026-03-16, fxx 00, 03, 06, 12, 24, 36, 48
# NOTE: NAM subset only has 10u, not 10v → wind speed from u-component only,
#       direction shown as eastward/westward component sign only (use gust for speed check)
# ============================================================
nam_base = "/home/main/weather-viewer/data/grib_cache/nam/20260316"
nam_run_utc = datetime(2026, 3, 16, 0, 0, tzinfo=timezone.utc)
nam_fxx_list = [0, 3, 6, 12, 24, 36, 48]
nam_wanted = [
    {'shortName': '2t',    'typeOfLevel': 'heightAboveGround', 'level': 2},
    {'shortName': '2d',    'typeOfLevel': 'heightAboveGround', 'level': 2},
    {'shortName': 'prmsl', 'typeOfLevel': 'meanSea',           'level': 0},
    {'shortName': '10u',   'typeOfLevel': 'heightAboveGround', 'level': 10},
    {'shortName': 'gust',  'typeOfLevel': 'surface',           'level': 0},
    {'shortName': 'tp',    'typeOfLevel': 'surface',           'level': 0, 'prefer_step_from_zero': True},
]

nam_rows = []
for fxx in nam_fxx_list:
    fname = f"subset_320bf9247943__nam.t00z.awphys{fxx:02d}.tm00.grib2"
    fpath = os.path.join(nam_base, fname)
    if not os.path.exists(fpath):
        continue
    res = extract_fields(fpath, nam_wanted)
    valid_time = nam_run_utc + timedelta(hours=fxx)
    t2m  = k_to_f(res['2t'][0]) if '2t' in res else float('nan')
    td2m = k_to_f(res['2d'][0]) if '2d' in res else float('nan')
    mslp = pa_to_mb(res['prmsl'][0]) if 'prmsl' in res else float('nan')
    # NAM subset: only 10u available; use gust for speed, u for direction sign
    u10  = res['10u'][0] if '10u' in res else 0.0
    gust = ms_to_mph(res['gust'][0]) if 'gust' in res else float('nan')
    # Wind direction from 10u sign only (no v component)
    # u > 0 means wind from west; u < 0 means wind from east
    if abs(u10) < 0.1:
        nam_wdir = "Calm"
        nam_wspd = 0.0
    elif u10 > 0:
        nam_wdir = "W"   # eastward flow = wind from West
        nam_wspd = ms_to_mph(abs(u10))
    else:
        nam_wdir = "E"   # westward flow = wind from East
        nam_wspd = ms_to_mph(abs(u10))
    tp = kgm2_to_in(res['tp'][0]) if 'tp' in res else 0.0
    glat = res['2t'][2] if '2t' in res else TARGET_LAT
    glon = res['2t'][3] if '2t' in res else TARGET_LON
    nam_rows.append({'fxx': fxx, 'valid_str': valid_time.strftime("%Y-%m-%d %H:%MZ"),
                     't2m': t2m, 'td2m': td2m, 'mslp': mslp,
                     'wspd': nam_wspd, 'wdir': nam_wdir, 'precip': tp,
                     'grid_lat': glat, 'grid_lon': glon,
                     'gust': gust,
                     'tp_step': res['tp'][1] if 'tp' in res else 'N/A'})

print_table(nam_rows, "NAM 3km CONUS Nest", "2026-03-16 00Z",
            precip_note="cumul total precip from run start (kg/m²→in) [NOTE: step varies — see below]")
print(f"  TP step ranges: " + ", ".join(f"F{r['fxx']:02d}={r['tp_step']}" for r in nam_rows))
print(f"  Wind NOTE: NAM subset contains only 10u (no 10v) — speed=|u|, dir=E/W only; use gust for approx speed")
print(f"  Gust (mph): " + ", ".join(f"F{r['fxx']:02d}={r['gust']:.1f}" for r in nam_rows if not math.isnan(r['gust'])))


# ============================================================
# GFS 0.25-deg: t18z 2026-03-15, fxx 006, 012, 024, 048, 072
# ============================================================
gfs_base = "/home/main/weather-viewer/data/grib_cache/gfs/20260315"
gfs_run_utc = datetime(2026, 3, 15, 18, 0, tzinfo=timezone.utc)
gfs_fxx_list = [6, 12, 24, 48, 72]
gfs_wanted = [
    {'shortName': '2t',    'typeOfLevel': 'heightAboveGround', 'level': 2},
    {'shortName': '2d',    'typeOfLevel': 'heightAboveGround', 'level': 2},
    {'shortName': 'prmsl', 'typeOfLevel': 'meanSea',           'level': 0},
    {'shortName': '10u',   'typeOfLevel': 'heightAboveGround', 'level': 10},
    {'shortName': '10v',   'typeOfLevel': 'heightAboveGround', 'level': 10},
    {'shortName': 'tp',    'typeOfLevel': 'surface',           'level': 0, 'prefer_step_from_zero': True},
]

gfs_rows = []
for fxx in gfs_fxx_list:
    fname = f"subset_fd35925906e4__gfs.t18z.pgrb2.0p25.f{fxx:03d}"
    fpath = os.path.join(gfs_base, fname)
    if not os.path.exists(fpath):
        continue
    res = extract_fields(fpath, gfs_wanted)
    valid_time = gfs_run_utc + timedelta(hours=fxx)
    t2m  = k_to_f(res['2t'][0]) if '2t' in res else float('nan')
    td2m = k_to_f(res['2d'][0]) if '2d' in res else float('nan')
    mslp = pa_to_mb(res['prmsl'][0]) if 'prmsl' in res else float('nan')
    u10  = res['10u'][0] if '10u' in res else 0.0
    v10  = res['10v'][0] if '10v' in res else 0.0
    wspd = ms_to_mph(math.sqrt(u10**2 + v10**2))
    wdir_str, _ = wind_dir_str(u10, v10)
    tp = kgm2_to_in(res['tp'][0]) if 'tp' in res else 0.0
    glat = res['2t'][2] if '2t' in res else TARGET_LAT
    glon = res['2t'][3] if '2t' in res else TARGET_LON
    gfs_rows.append({'fxx': fxx, 'valid_str': valid_time.strftime("%Y-%m-%d %H:%MZ"),
                     't2m': t2m, 'td2m': td2m, 'mslp': mslp,
                     'wspd': wspd, 'wdir': wdir_str, 'precip': tp,
                     'grid_lat': glat, 'grid_lon': glon,
                     'tp_step': res['tp'][1] if 'tp' in res else 'N/A'})

print_table(gfs_rows, "GFS 0.25-deg", "2026-03-15 18Z",
            precip_note="cumul total precip from run start (kg/m²→in)")
print(f"  TP step ranges: " + ", ".join(f"F{r['fxx']:03d}={r['tp_step']}" for r in gfs_rows))


# ============================================================
# ECMWF IFS: 2026031500 (00Z), fxx 0, 24, 48, 72, 108
# NOTE: ECMWF tp is in METERS (not kg/m²), convert with 39.3701 in/m
# ============================================================
ifs_base = "/home/main/weather-viewer/data/grib_cache/ifs/20260315"
ifs_run_utc = datetime(2026, 3, 15, 0, 0, tzinfo=timezone.utc)
ifs_fxx_list = [0, 24, 48, 72, 108]
ifs_wanted = [
    {'shortName': '2t',  'typeOfLevel': 'heightAboveGround', 'level': 2},
    {'shortName': '2d',  'typeOfLevel': 'heightAboveGround', 'level': 2},
    {'shortName': 'msl', 'typeOfLevel': 'meanSea',           'level': 0},
    {'shortName': '10u', 'typeOfLevel': 'heightAboveGround', 'level': 10},
    {'shortName': '10v', 'typeOfLevel': 'heightAboveGround', 'level': 10},
    {'shortName': 'tp',  'typeOfLevel': 'surface',           'level': 0, 'prefer_step_from_zero': True},
]

ifs_rows = []
for fxx in ifs_fxx_list:
    fname = f"subset_1139961e16d4__20260315000000-{fxx}h-oper-fc.grib2"
    fpath = os.path.join(ifs_base, fname)
    if not os.path.exists(fpath):
        continue
    res = extract_fields(fpath, ifs_wanted)
    valid_time = ifs_run_utc + timedelta(hours=fxx)
    t2m  = k_to_f(res['2t'][0]) if '2t' in res else float('nan')
    td2m = k_to_f(res['2d'][0]) if '2d' in res else float('nan')
    mslp = pa_to_mb(res['msl'][0]) if 'msl' in res else float('nan')
    u10  = res['10u'][0] if '10u' in res else 0.0
    v10  = res['10v'][0] if '10v' in res else 0.0
    wspd = ms_to_mph(math.sqrt(u10**2 + v10**2))
    wdir_str, _ = wind_dir_str(u10, v10)
    # ECMWF tp is in meters, not kg/m²
    tp = m_to_in(res['tp'][0]) if 'tp' in res else 0.0
    glat = res['2t'][2] if '2t' in res else (res['msl'][2] if 'msl' in res else TARGET_LAT)
    glon = res['2t'][3] if '2t' in res else (res['msl'][3] if 'msl' in res else TARGET_LON)
    # f0: 2d may be missing (it was in the analysis file)
    ifs_rows.append({'fxx': fxx, 'valid_str': valid_time.strftime("%Y-%m-%d %H:%MZ"),
                     't2m': t2m, 'td2m': td2m, 'mslp': mslp,
                     'wspd': wspd, 'wdir': wdir_str, 'precip': tp,
                     'grid_lat': glat, 'grid_lon': glon,
                     'tp_step': res['tp'][1] if 'tp' in res else 'N/A'})

print_table(ifs_rows, "ECMWF IFS (0.1-deg grid)", "2026-03-15 00Z",
            precip_note="cumul total precip from run start (meters→in, ×39.37)")
print(f"  TP step ranges: " + ", ".join(f"F{r['fxx']:03d}={r['tp_step']}" for r in ifs_rows))
print(f"  NOTE: ECMWF tp uses meters as unit; raw values at Lincoln: " +
      ", ".join(f"F{r['fxx']:03d}={r['precip']/39.3701*1000:.2f}mm" for r in ifs_rows))
