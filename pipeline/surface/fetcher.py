"""Fetch IEM Mesonet current surface observations and write slim JSON."""
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from pipeline.config import DATA_DIR

log = logging.getLogger(__name__)

IEM_BASE = "https://mesonet.agron.iastate.edu/api/1/currents.geojson"

# All US state/territory ASOS networks
US_ASOS_NETWORKS = [
    "AL_ASOS","AK_ASOS","AZ_ASOS","AR_ASOS","CA_ASOS","CO_ASOS","CT_ASOS",
    "DE_ASOS","FL_ASOS","GA_ASOS","HI_ASOS","ID_ASOS","IL_ASOS","IN_ASOS",
    "IA_ASOS","KS_ASOS","KY_ASOS","LA_ASOS","ME_ASOS","MD_ASOS","MA_ASOS",
    "MI_ASOS","MN_ASOS","MS_ASOS","MO_ASOS","MT_ASOS","NE_ASOS","NV_ASOS",
    "NH_ASOS","NJ_ASOS","NM_ASOS","NY_ASOS","NC_ASOS","ND_ASOS","OH_ASOS",
    "OK_ASOS","OR_ASOS","PA_ASOS","RI_ASOS","SC_ASOS","SD_ASOS","TN_ASOS",
    "TX_ASOS","UT_ASOS","VT_ASOS","VA_ASOS","WA_ASOS","WV_ASOS","WI_ASOS",
    "WY_ASOS","PR_ASOS","DC_ASOS",
]

OUTPUT_FILE = DATA_DIR / "surface_obs.json"
STATE_FILE  = DATA_DIR / "surface_state.json"
HISTORY_DIR = DATA_DIR / "surface_history"
HISTORY_RETENTION_SECONDS = 4 * 60 * 60

# ---------------------------------------------------------------------------
# Station tier sets — controls zoom-level density
# Tier 1: major metro / hub stations (shown on zoomed-out national view)
# Tier 2: primary 4-char ASOS stations (shown as you zoom in)
# Tier 3: everything else (shown on closer regional/local zooms)
# ---------------------------------------------------------------------------

TIER1_STATIONS: frozenset[str] = frozenset({
    # Largest CONUS metro / hub stations
    "ATL","BOS","BWI","CLT","DCA","DEN","DFW","DTW","EWR","FLL",
    "HOU","IAH","IAD","JFK","LAS","LAX","LGA","MCI","MCO","MEM",
    "MIA","MKE","MSP","MSY","ORD","PDX","PHL","PHX","PIT","RDU",
    "SAN","SEA","SFO","SLC","STL","TPA",
    # Major inland / plains cities
    "ABQ","AUS","BOI","CLE","CMH","DSM","IND","OKC","OMA","RIC",
    "SAT","TUL",
    # Alaska
    "PANC","PAFA","PAJN","PASI",
    # Hawaii
    "PHNL","PHOG","PHKO","PHLI",
    # Puerto Rico
    "TJSJ",
})


def _normalize_sid(sid: str) -> str:
    sid = (sid or "").strip().upper()
    if len(sid) == 4 and sid[0] == "K" and sid[1:].isalpha():
        return sid[1:]
    return sid


def _assign_tier(sid: str) -> int:
    sid_norm = _normalize_sid(sid)
    if sid_norm in TIER1_STATIONS:
        return 1
    if len(sid_norm) == 3 and sid_norm.isalpha():
        return 2
    if len(sid_norm) == 4 and sid_norm[0] in ("P", "T") and sid_norm.isalpha():
        return 2
    return 3


def _safe_float(val) -> float | None:
    if val is None or val == "":
        return None
    try:
        f = float(val)
        return None if f != f else f  # NaN check
    except (TypeError, ValueError):
        return None


def _safe_int(val) -> int | None:
    if val is None or val == "":
        return None
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def _kt_to_mph(kt) -> float | None:
    f = _safe_float(kt)
    return None if f is None else round(f * 1.15078, 1)


def _fetch_network(network: str) -> list[dict]:
    """Fetch one state network from IEM, return list of raw feature properties."""
    resp = requests.get(IEM_BASE, params={"network": network}, timeout=20)
    resp.raise_for_status()
    return resp.json().get("features", [])


def fetch_and_write() -> int:
    """Fetch all US ASOS networks in parallel, transform, write OUTPUT_FILE atomically.

    Returns count of stations written, or raises on total failure.
    """
    log.info("Fetching IEM Mesonet surface observations (%d networks)…", len(US_ASOS_NETWORKS))
    t0 = time.time()

    all_features: list = []
    errors: list[str] = []

    with ThreadPoolExecutor(max_workers=12) as pool:
        futs = {pool.submit(_fetch_network, net): net for net in US_ASOS_NETWORKS}
        for fut in as_completed(futs):
            net = futs[fut]
            try:
                feats = fut.result()
                all_features.extend(feats)
            except Exception as exc:
                errors.append(f"{net}: {exc}")
                log.warning("Failed to fetch %s: %s", net, exc)

    if errors:
        log.warning("%d networks failed: %s", len(errors), errors)

    if not all_features and errors:
        raise RuntimeError(f"All IEM network fetches failed: {errors[:3]}")

    stations = []
    seen: set[str] = set()

    for feat in all_features:
        props = feat.get("properties", {}) if isinstance(feat, dict) else {}
        if not props:
            continue

        sid = props.get("station", "")
        if not sid or sid in seen:
            continue
        seen.add(sid)

        lat = _safe_float(props.get("lat"))
        lon = _safe_float(props.get("lon"))
        if lat is None or lon is None:
            # Fall back to geometry
            geom = feat.get("geometry") or {}
            coords = geom.get("coordinates") or []
            if len(coords) >= 2:
                lon, lat = float(coords[0]), float(coords[1])
            else:
                continue

        # Wind — IEM ASOS returns knots
        wspd_mph = _kt_to_mph(props.get("sknt"))
        wgst_mph = _kt_to_mph(props.get("gust"))
        wdir = _safe_int(props.get("drct"))

        stations.append({
            "sid":   sid,
            "lat":   round(lat, 4),
            "lon":   round(lon, 4),
            "name":  props.get("name") or sid,
            "t":     _safe_float(props.get("tmpf")),
            "d":     _safe_float(props.get("dwpf")),
            "wdir":  wdir,
            "wspd":  wspd_mph,
            "wgst":  wgst_mph,
            "sky":   props.get("skyc1") or None,
            "wx":    props.get("wxcodes") or None,
            "vsby":  _safe_float(props.get("vsby")),
            "mslp":  _safe_float(props.get("mslp")),
            "valid": props.get("utc_valid") or props.get("valid") or None,
            "tier":  _assign_tier(sid),
        })

    elapsed = round(time.time() - t0, 2)
    log.info("Fetched %d stations in %.2fs (%d errors)", len(stations), elapsed, len(errors))

    payload = {
        "generated": int(time.time()),
        "count": len(stations),
        "stations": stations,
    }

    # Atomic write
    tmp = OUTPUT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, separators=(",", ":")))
    os.replace(tmp, OUTPUT_FILE)

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    hist_file = HISTORY_DIR / f"{payload['generated']}.json"
    hist_tmp = hist_file.with_suffix(".tmp")
    hist_tmp.write_text(json.dumps(payload, separators=(",", ":")))
    os.replace(hist_tmp, hist_file)

    cutoff = payload["generated"] - HISTORY_RETENTION_SECONDS
    for old_file in HISTORY_DIR.glob("*.json"):
        try:
            ts = int(old_file.stem)
        except ValueError:
            continue
        if ts < cutoff:
            old_file.unlink(missing_ok=True)

    state = {
        "last_fetch":     int(time.time()),
        "station_count":  len(stations),
        "elapsed_s":      elapsed,
        "network_errors": len(errors),
    }
    tmp2 = STATE_FILE.with_suffix(".tmp")
    tmp2.write_text(json.dumps(state))
    os.replace(tmp2, STATE_FILE)

    return len(stations)
