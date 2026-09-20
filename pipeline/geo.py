"""Which province is a lat/lng in? Uses the same province outlines the map draws.

The outlines live in frontend/finland-map.json as SVG paths. An affine fit maps
lat/lng onto that canvas (about 10 px error on 460×820), then point-in-polygon.
Good enough to put a road sensor or a railway station in its province; not a
surveying tool.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
MAP = HERE / "frontend" / "finland-map.json"
PX = (35.2237, -1.6682, -560.3687)
PY = (1.1539, -76.0199, 5320.8471)
FINLAND = {"lat": (59.5, 70.2), "lng": (19.0, 31.7)}


def project(lat: float, lng: float) -> tuple[float, float]:
    return PX[0] * lng + PX[1] * lat + PX[2], PY[0] * lng + PY[1] * lat + PY[2]


def _polys(d: str) -> list[list[tuple[float, float]]]:
    out, cur = [], []
    for cmd, x, y in re.findall(r"([MLZ])(?:([\d.\-]+),([\d.\-]+))?", d):
        if cmd == "M":
            if cur:
                out.append(cur)
            cur = [(float(x), float(y))]
        elif cmd == "L":
            cur.append((float(x), float(y)))
        elif cmd == "Z":
            if cur:
                out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return [p for p in out if len(p) >= 3]


@lru_cache(maxsize=1)
def provinces() -> list[tuple[str, list[list[tuple[float, float]]], tuple[float, float, float, float]]]:
    geo = json.loads(MAP.read_text())
    res = []
    for p in geo["provinces"]:
        polys = [poly for d in p.get("paths", []) for poly in _polys(d)]
        xs = [x for poly in polys for x, _ in poly]
        ys = [y for poly in polys for _, y in poly]
        res.append((p["name"], polys, (min(xs), min(ys), max(xs), max(ys))))
    return res


def _inside(x: float, y: float, poly: list[tuple[float, float]]) -> bool:
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi:
            inside = not inside
        j = i
    return inside


def province_of(lat: float, lng: float) -> str | None:
    if not (FINLAND["lat"][0] <= lat <= FINLAND["lat"][1] and FINLAND["lng"][0] <= lng <= FINLAND["lng"][1]):
        return None
    x, y = project(lat, lng)
    best, best_d = None, 1e9
    for name, polys, (x0, y0, x1, y1) in provinces():
        if x0 - 12 <= x <= x1 + 12 and y0 - 12 <= y <= y1 + 12:
            for poly in polys:
                if _inside(x, y, poly):
                    return name
            # remember the nearest bbox centre as a fallback for coastal points that fall just outside an outline
            d = abs((x0 + x1) / 2 - x) + abs((y0 + y1) / 2 - y)
            if d < best_d:
                best, best_d = name, d
    return best
