"""Elisa's public surfaces. All found behind elisa.fi/kuuluvuus; none needs a key.

  rating(lat, lng)          layers advertised at a point: networkType, frequencies (MHz), speed (Mbps)
  network_improvements()    planned/ongoing site work: coordinates, connectionType[], introductionDate
  tile(network, z, x, y)    coverage raster tile (Web Mercator, native zoom ≤ 9), PNG bytes

Not machine-readable (checked 19 Sep 2026): disturbance notices at elisa.fi/asiakastiedotteet
(HTML only), 5G locality list at elisa.fi/5g-paikkakunnat (HTML). developer.elisa.fi is
B2B and needs a contract.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

BASE = "https://content-api.external-resource.elisa.fi"
RATING = f"{BASE}/api/coverage-map/rating"
IMPROVEMENTS = f"{BASE}/api/coverage-map/network-improvements"
TILES = f"{BASE}/coverage_assets"
UA = "Pilot-optimize/1.0"
NETWORKS = ("5G", "4G", "2G", "LTEM", "NBIOT")


def _get(url: str, timeout: int = 15, retries: int = 3) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for i in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError, OSError):
            if i == retries - 1:
                return None
            time.sleep(1.2)
    return None


def rating(lat: float, lng: float) -> list[dict] | None:
    raw = _get(f"{RATING}?lat={lat:.5f}&lng={lng:.5f}")
    return json.loads(raw.decode()) if raw else None


def summarise(layers: list[dict] | None) -> dict:
    """Rated = the layer lists a band. At sea every layer comes back with empty frequencies."""
    by = {str(l.get("networkType")): l for l in (layers or []) if l.get("frequencies")}
    f5 = [int(f) for f in (by.get("5G") or {}).get("frequencies") or []]
    f4 = [int(f) for f in (by.get("4G") or {}).get("frequencies") or []]
    return {
        "rated": bool(by),
        "has_2g": "2G" in by, "has_4g": "4G" in by, "has_5g": "5G" in by,
        "has_nbiot": "NBIOT" in by, "has_ltem": "LTEM" in by,
        "five_g_freqs": f5, "four_g_freqs": f4,
        "five_g_midband": any(f >= 3000 for f in f5),
        "five_g_lowband_only": bool(f5) and all(f <= 800 for f in f5),
        "four_g_lowband_only": bool(f4) and all(f <= 900 for f in f4),
        "five_g_speed": (by.get("5G") or {}).get("speed"),
        "four_g_speed": (by.get("4G") or {}).get("speed"),
    }


def network_improvements() -> list[dict]:
    raw = _get(IMPROVEMENTS, timeout=25)
    if not raw:
        return []
    out = []
    for x in json.loads(raw.decode()):
        try:
            lng, lat = float(x["coordinates"][0]), float(x["coordinates"][1])
        except (KeyError, IndexError, ValueError, TypeError):
            continue
        out.append({"lat": lat, "lng": lng, "types": list(x.get("connectionType") or []), "date": str(x.get("introductionDate") or "")})
    return out


def tile(network: str, z: int, x: int, y: int) -> bytes | None:
    if network not in NETWORKS or not (0 <= z <= 9):
        return None
    return _get(f"{TILES}/{network}/png/{z}/{x}/{y}.png", timeout=12, retries=1)
