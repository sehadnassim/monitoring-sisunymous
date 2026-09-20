#!/usr/bin/env python3
"""Mobility lens, built entirely from public APIs. No submitted data touches this step.

  Fintraffic  →  where traffic actually is: road sensors with vehicles/hour, passenger railway stations
  Elisa       →  what is advertised there (rating) and where Elisa already plans work (network-improvements)
  Fintraffic  →  road works nearby: a road already opened is the cheap moment to pull fibre or add a site

Writes data/transport.json. ~350 rating calls, a few minutes on a slow link.

  ../.venv/bin/python build_transport.py [--roads 200]
"""

from __future__ import annotations

import argparse
import json
import math
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from pipeline.geo import province_of
from sources import digitraffic, elisa

HERE = Path(__file__).resolve().parent
OUT = HERE / "data" / "transport.json"
NEAR_KM = 5.0


def km(lat1, lng1, lat2, lng2) -> float:
    p = math.pi / 180
    a = 0.5 - math.cos((lat2 - lat1) * p) / 2 + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lng2 - lng1) * p)) / 2
    return 12742 * math.asin(math.sqrt(a))


def nearest(items: list[dict], lat: float, lng: float, limit_km: float) -> list[dict]:
    out = []
    for it in items:
        d = km(lat, lng, it["lat"], it["lng"])
        if d <= limit_km:
            out.append({**it, "km": round(d, 1)})
    return sorted(out, key=lambda x: x["km"])[:5]


def gap_score(cov: dict, weight: float) -> tuple[float, str]:
    """How badly a busy place is served, 0..1, and the one-line reason."""
    if not cov["rated"]:
        return 1.0, "nothing rated here on Elisa's map"
    if not cov["has_5g"]:
        return 0.9, "no 5G on the map"
    if not cov["five_g_midband"]:
        if cov["four_g_lowband_only"]:
            return 0.75, "5G is 700 MHz only and 4G is low-band only"
        return 0.6, "5G is coverage band (700 MHz) only, no 3500 MHz"
    if cov["four_g_lowband_only"]:
        return 0.25, "mid-band 5G present but 4G is low-band only"
    return 0.0, "mid-band 5G advertised"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--roads", type=int, default=200, help="how many of the busiest road sensors to rate")
    a = ap.parse_args()
    t0 = time.perf_counter()
    status: dict[str, dict] = {}

    print("Fintraffic: road sensors …")
    stations = digitraffic.tms_stations()
    latest = digitraffic.tms_latest()
    status["tms_stations"] = {"url": f"{digitraffic.ROAD}/tms/v1/stations", "items": len(stations)}
    status["tms_data"] = {"url": f"{digitraffic.ROAD}/tms/v1/stations/data", "items": len(latest)}
    roads = []
    for s in stations:
        d = latest.get(s["id"])
        if not d:
            continue
        roads.append({**s, **d, "province": province_of(s["lat"], s["lng"])})
    roads.sort(key=lambda r: -r["vehicles_per_hour"])
    rated_roads = roads[: a.roads]

    print("Fintraffic: railway stations …")
    rail = [{**s, "province": province_of(s["lat"], s["lng"])} for s in digitraffic.rail_stations()]
    status["rail_stations"] = {"url": f"{digitraffic.RAIL}/metadata/stations", "items": len(rail)}

    print("Fintraffic: road works …")
    works = digitraffic.road_works()
    status["road_works"] = {"url": f"{digitraffic.ROAD}/traffic-message/v1/messages", "items": len(works)}

    print("Elisa: planned network work …")
    improvements = elisa.network_improvements()
    for it in improvements:
        it["province"] = province_of(it["lat"], it["lng"])
    status["network_improvements"] = {"url": elisa.IMPROVEMENTS, "items": len(improvements)}

    points = [{"kind": "road", **r} for r in rated_roads] + [{"kind": "rail", **s} for s in rail]
    print(f"Elisa: rating {len(points)} places ({len(rated_roads)} busiest road sensors + {len(rail)} passenger stations) …")

    def rate(pt):
        layers = elisa.rating(pt["lat"], pt["lng"])
        return pt, elisa.summarise(layers), layers

    calls = 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        for pt, cov, layers in ex.map(rate, points):
            calls += 1
            if calls % 50 == 0:
                print(f"  {calls}/{len(points)}")
            if layers is None:
                pt["coverage"] = None
                continue
            pt["coverage"] = cov
            pt["planned"] = nearest(improvements, pt["lat"], pt["lng"], NEAR_KM)
            pt["works"] = nearest(works, pt["lat"], pt["lng"], NEAR_KM)
    status["rating"] = {"url": elisa.RATING, "items": sum(1 for p in points if p.get("coverage"))}

    # Demand weight: road → vehicles/hour normalised; rail → passenger station counts as a fixed mid weight.
    vmax = max((p["vehicles_per_hour"] for p in points if p["kind"] == "road"), default=1)
    for p in points:
        if not p.get("coverage"):
            p["score"] = None
            continue
        demand = min(1.0, p["vehicles_per_hour"] / vmax) if p["kind"] == "road" else 0.35
        gap, why = gap_score(p["coverage"], demand)
        p["demand"] = round(demand, 3)
        p["gap"] = gap
        p["why"] = why
        p["score"] = round(100 * demand * gap, 1)
        p["already_planned"] = any(("5G" in x["types"]) for x in p.get("planned", []))
        p["dig_once"] = bool(p.get("works")) and gap > 0

    by_prov: dict[str, dict] = {}
    for p in points:
        if not p.get("province") or p["score"] is None:
            continue
        b = by_prov.setdefault(p["province"], {"points": 0, "roads": 0, "rail": 0, "vehicles_per_hour": 0, "gap_points": 0,
                                               "planned_5g_sites": 0, "planned_2g_sites": 0, "dig_once": 0, "score_sum": 0.0, "top": []})
        b["points"] += 1
        b[p["kind"] + "s" if p["kind"] == "road" else "rail"] += 1
        b["vehicles_per_hour"] += p.get("vehicles_per_hour") or 0
        b["gap_points"] += 1 if p["gap"] > 0 else 0
        b["dig_once"] += 1 if p["dig_once"] else 0
        b["score_sum"] += p["score"]
    # Every province with any planned work gets an entry, even without rated transport points.
    for it in improvements:
        if not it["province"]:
            continue
        b = by_prov.setdefault(it["province"], {"points": 0, "roads": 0, "rail": 0, "vehicles_per_hour": 0, "gap_points": 0,
                                                 "planned_5g_sites": 0, "planned_2g_sites": 0, "dig_once": 0, "score_sum": 0.0, "top": []})
        if "5G" in it["types"]:
            b["planned_5g_sites"] += 1
        if "2G" in it["types"]:
            b["planned_2g_sites"] += 1
    for prov, b in by_prov.items():
        top = sorted((p for p in points if p.get("province") == prov and p["score"]), key=lambda x: -x["score"])[:8]
        b["top"] = [{k: p.get(k) for k in ("kind", "name", "lat", "lng", "vehicles_per_hour", "mean_speed_kmh", "score", "why", "already_planned", "dig_once")} for p in top]
        b["score"] = round(b["score_sum"] / b["points"], 1) if b["points"] else None
        b["gap_share"] = round(b["gap_points"] / max(b["points"], 1), 3)
        del b["score_sum"]

    payload = {
        "queried_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "elapsed_s": round(time.perf_counter() - t0, 1),
        "sources": status,
        "near_km": NEAR_KM,
        "how": ("Demand = vehicles/hour at Fintraffic road sensors (passenger rail stations get a fixed 0.35). "
                "Gap = 1.0 nothing rated, 0.9 no 5G, 0.6–0.75 coverage-band 5G only, 0.25 mid-band 5G but low-band-only 4G, 0 mid-band 5G. "
                "Score = 100 × demand × gap. 'already planned' = an Elisa 5G work site within 5 km. 'dig once' = a Fintraffic road work within 5 km of a gap."),
        "points": [{k: v for k, v in p.items() if k not in ("status", "measured")} for p in points],
        "provinces": by_prov,
        "improvements": improvements,
        "road_works_count": len(works),
        "national": {
            "rated_points": sum(1 for p in points if p.get("coverage")),
            "gap_points": sum(1 for p in points if p.get("gap", 0) > 0),
            "gap_points_already_planned": sum(1 for p in points if p.get("gap", 0) > 0 and p.get("already_planned")),
            "dig_once_points": sum(1 for p in points if p.get("dig_once")),
            "improvement_sites": len(improvements),
            "improvement_sites_5g": sum(1 for x in improvements if "5G" in x["types"]),
            "improvement_sites_2g": sum(1 for x in improvements if "2G" in x["types"]),
        },
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False))
    n = payload["national"]
    print(f"\nrated {n['rated_points']} places; {n['gap_points']} have a gap, {n['gap_points_already_planned']} of those already have Elisa 5G work within 5 km; "
          f"{n['dig_once_points']} sit next to an open road work.")
    for prov, b in sorted(by_prov.items(), key=lambda kv: -(kv[1]["score"] or 0))[:6]:
        print(f"  {prov:18s} score {b['score']:5.1f}  gap {b['gap_points']}/{b['points']}  planned 5G sites {b['planned_5g_sites']}  dig-once {b['dig_once']}")
    print(f"wrote {OUT} in {payload['elapsed_s']} s")


if __name__ == "__main__":
    main()
