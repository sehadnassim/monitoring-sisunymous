#!/usr/bin/env python3
"""Step 3: insights from the de-identified release, joined with public APIs.

Reads ONLY release/cohort_summary.json (province × network × application cohorts
from pipeline/release.py) plus data/coverage.json and data/transport.json. Raw
rows are never opened here. Whatever the release withheld stays withheld and
is labelled as such in the output rather than estimated.

Lenses
  service    video demanded on a layer that cannot carry it well
  energy     radio kept on for traffic that is not there (2G, underused 5G)
  coverage   file vs map: 5G in the release where the map rates coverage-band only
  mobility   busy roads and stations vs advertised layers (public data only)

  ../.venv/bin/python build_insights.py
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
RELEASE = HERE / "release"
COVERAGE = HERE / "data" / "coverage.json"
TRANSPORT = HERE / "data" / "transport.json"
OUT = HERE / "data" / "insights.json"
VIDEO = {"Streaming"}
OTHER = "Other provinces"
PLACE = {
    "Uusimaa": "Helsinki region", "Varsinais-Suomi": "Southwest Finland", "Satakunta": "Satakunta",
    "Kanta-Häme": "Tavastia", "Pirkanmaa": "Tampere region", "Päijät-Häme": "Lahti region",
    "Kymenlaakso": "Kymenlaakso", "Etelä-Karjala": "South Karelia", "Etelä-Savo": "South Savo",
    "Pohjois-Savo": "North Savo", "Pohjois-Karjala": "North Karelia", "Keski-Suomi": "Central Finland",
    "Etelä-Pohjanmaa": "South Ostrobothnia", "Pohjanmaa": "Ostrobothnia", "Keski-Pohjanmaa": "Central Ostrobothnia",
    "Pohjois-Pohjanmaa": "North Ostrobothnia", "Kainuu": "Kainuu", "Lappi": "Lapland", "Ahvenanmaa": "Aland",
    OTHER: "Other provinces (pooled)",
}
WITHHELD = "withheld by de-identification"


def r(x, n=3):
    return None if x is None else round(float(x), n)


def clip(x):
    return float(min(1.0, max(0.0, x)))


def wmean(rows, mean_key, n_key):
    num = sum((c[mean_key] or 0) * (c[n_key] or 0) for c in rows if c.get(mean_key) is not None)
    den = sum((c[n_key] or 0) for c in rows if c.get(mean_key) is not None)
    return (num / den) if den else None


def profile(rows: list[dict]) -> dict:
    """Everything the release lets us say about one released province."""
    gb = sum(c["volume_sum"] or 0 for c in rows)
    n = sum(c["row_count"] for c in rows)
    by_rat_gb = defaultdict(float)
    by_rat_n = defaultdict(int)
    for c in rows:
        by_rat_gb[c["radio_access_type"]] += c["volume_sum"] or 0
        by_rat_n[c["radio_access_type"]] += c["row_count"]
    video = [c for c in rows if c["application_category"] in VIDEO]
    v_gb = sum(c["volume_sum"] or 0 for c in video)
    v_n = sum(c["row_count"] for c in video)
    v5 = [c for c in video if c["radio_access_type"] == "5G"]
    v4 = [c for c in video if c["radio_access_type"] == "4G"]
    apps = defaultdict(lambda: {"gb": 0.0, "rows": 0, "rows_5g": 0, "cohorts": 0, "pooled": False})
    for c in rows:
        a = apps[c["application_category"]]
        a["gb"] += c["volume_sum"] or 0
        a["rows"] += c["row_count"]
        a["rows_5g"] += c["row_count"] if c["radio_access_type"] == "5G" else 0
        a["cohorts"] += 1
        a["pooled"] = c["application_category"] in ("Pooled categories", "All applications")
    app_mix = sorted(
        [{"app": k, "gb": r(v["gb"]), "rows": v["rows"], "share_5g_rows": r(v["rows_5g"] / v["rows"] if v["rows"] else 0),
          "rtt_ms": r(wmean([c for c in rows if c["application_category"] == k], "radio_latency_mean", "radio_latency_n"), 1),
          "tp_dl": r(wmean([c for c in rows if c["application_category"] == k], "throughput_mean", "throughput_n"), 4),
          "pooled": v["pooled"]} for k, v in apps.items()],
        key=lambda x: -(x["gb"] or 0))
    video_present = bool(video)
    return {
        "cohorts": len(rows), "rows": n, "gb": r(gb),
        "min_people": min((c["people"] for c in rows), default=None),
        "gb_5g_share": r(by_rat_gb["5G"] / gb if gb else 0),
        "rows_5g_share": r(by_rat_n["5G"] / n if n else 0),
        "rows_2g": by_rat_n.get("2G", 0) or None,  # None → folded into Other provinces
        "gb_2g": r(by_rat_gb["2G"], 5) if "2G" in by_rat_gb else None,
        "video_gb": r(v_gb) if video_present else None,
        "video_gb_share": r(v_gb / gb if gb else 0) if video_present else None,
        "video_rows": v_n if video_present else None,
        "video_rows_4g_share": r(sum(c["row_count"] for c in v4) / v_n if v_n else 0) if video_present else None,
        "video_5g_gb_share": r(sum(c["volume_sum"] or 0 for c in v5) / v_gb if v_gb else 0) if video_present else None,
        "video_4g_tp": r(wmean(v4, "throughput_mean", "throughput_n"), 4),
        "video_5g_tp": r(wmean(v5, "throughput_mean", "throughput_n"), 4),
        "video_rtt_ms": r(wmean(video, "radio_latency_mean", "radio_latency_n"), 1),
        "video_pooled": not video_present,  # Streaming folded into 'Pooled categories' or 'All applications'
        "rtt_ms": r(wmean(rows, "radio_latency_mean", "radio_latency_n"), 1),
        "tp_dl": r(wmean(rows, "throughput_mean", "throughput_n"), 4),
        "retrans": r(wmean(rows, "retrans_down_mean", "retrans_down_n"), 4),
        "app_mix": app_mix[:14],
        "withheld": ["cells", "10-minute time bins", "idle-cell detection", "per-cell video split", "subscriber counts below k"],
    }


def score_service(p, cov, mob, vmax):
    if p["video_pooled"]:
        return {"score": None, "why": [f"Streaming {WITHHELD}: folded into a pooled application label here"], "action": "Cannot be read from the release", "withheld": True}
    if not cov:
        return {"score": None, "why": ["no Elisa map samples for this label"], "action": "—", "withheld": False}
    demand = clip(math.log1p(p["video_gb"] or 0) / math.log1p(vmax)) if vmax else 0.0
    midband_gap = 1.0 - (cov.get("share_5g_midband") or 0.0)
    session_gap = p["video_rows_4g_share"] or 0.0
    tp4, tp5 = p["video_4g_tp"] or 0.0, p["video_5g_tp"] or 0.0
    tp_gap = clip(1.0 - tp4 / tp5) if tp5 else 0.0
    road_gap = mob.get("gap_share") or 0.0
    gap = 0.40 * midband_gap + 0.35 * session_gap * tp_gap + 0.25 * road_gap
    why = []
    if midband_gap > 0:
        why.append(f"{int(round(midband_gap * cov.get('points', 0)))} of {cov.get('points')} sampled places show no 3500 MHz 5G on Elisa's map")
    why.append(f"{int(round(session_gap * 100))}% of video sessions in the release ran on 4G at {tp4:.3f} vs {tp5:.2f} on 5G")
    if road_gap > 0:
        why.append(f"{mob.get('gap_points')} of {mob.get('points')} busy road/rail points lack capacity 5G")
    if (p["video_gb_share"] or 0) >= 0.3:
        why.append(f"video is {int(round(p['video_gb_share'] * 100))}% of released bytes here")
    action = ("Add mid-band 5G where video demand sits" if midband_gap >= 0.25
              else "Steer video sessions onto the 5G layer already there" if session_gap >= 0.5
              else "Close the corridor gaps video rides through" if road_gap >= 0.3
              else "Video is served on the layer it needs")
    return {"score": r(100 * demand * gap, 1), "demand": r(demand), "gap": r(gap), "why": why[:3], "action": action, "withheld": False}


def score_energy(p, cov, mob, national_2g, planned_2g_max, underuse_max):
    """Provincial signals that survive the release: planned 2G work sites (Elisa) while released 2G volume is ~0
    nationally, and advertised mid-band 5G that released bytes do not use. Idle cells are withheld."""
    if not cov:
        return {"score": None, "why": ["no Elisa map samples for this label"], "action": "—", "withheld": False}
    planned_2g = mob.get("planned_2g_sites") or 0
    planned_norm = planned_2g / planned_2g_max if planned_2g_max else 0.0
    midband = cov.get("share_5g_midband") or 0.0
    underuse = midband * (1.0 - (p["gb_5g_share"] or 0.0))
    underuse_norm = underuse / underuse_max if underuse_max else 0.0
    score = 100 * (0.55 * planned_norm + 0.45 * underuse_norm)
    why = []
    if planned_2g:
        why.append(f"Elisa lists {planned_2g} planned work site{'s' if planned_2g != 1 else ''} touching 2G here, while the released 2G layer carries {national_2g['gb']} GB nationally in the whole window")
    else:
        why.append("no 2G work planned here; 2G carries "
                   f"{national_2g['gb']} GB nationally (pooled by the release)")
    why.append(f"mid-band 5G at {int(round(midband * 100))}% of sampled places, yet {int(round((1 - (p['gb_5g_share'] or 0)) * 100))}% of released bytes still ride 4G/2G")
    why.append(f"idle-cell detection {WITHHELD} (no cells, no time bins in the release)")
    action = ("Redirect planned 2G work to the sunset plan" if planned_norm >= 0.5
              else "Steer traffic onto the 5G layer so its energy buys something" if underuse_norm >= 0.6
              else "Radio use roughly matches the load")
    return {"score": r(score, 1), "planned_2g_sites": planned_2g, "five_g_underused": r(underuse), "why": why[:3], "action": action,
            "withheld": False, "note": "Energy is a proxy: no power meters in the data, and cell-level idleness is not in the release."}


def score_coverage(p, cov):
    if not cov:
        return {"score": None, "why": ["no Elisa map samples for this label"], "action": "—", "withheld": False}
    no_midband = 1.0 - (cov.get("share_5g_midband") or 0.0)
    lowband_only = cov.get("share_5g_lowband_only") or 0.0
    five = p["rows_5g_share"] or 0.0
    mismatch = five * no_midband
    why = []
    if mismatch > 0.05:
        why.append(f"{int(round(five * 100))}% of released sessions are 5G while {int(round(no_midband * cov.get('points', 0)))} of {cov.get('points')} sampled places show no mid-band 5G")
    if lowband_only > 0:
        why.append(f"{int(round(lowband_only * cov.get('points', 0)))} sampled places rate coverage-only 5G (700 MHz)")
    if not why:
        why.append("map and release agree on the 5G layer here")
    return {"score": r(100 * mismatch, 1), "why": why[:3], "withheld": False,
            "action": "Reconcile 5G labels with the advertised layer" if mismatch > 0.05 else "No reconciliation needed"}


def bin_label(ts) -> str:
    try:
        t = int(ts)
        if t > 10**12:
            t //= 1000
        return datetime.fromtimestamp(t, tz=timezone.utc).strftime("%H:%M UTC")
    except (TypeError, ValueError, OSError):
        return str(ts)


def slim_point(p: dict) -> dict:
    cov = p.get("coverage") or {}
    layer = "none" if not cov.get("has_5g") else ("mid" if cov.get("five_g_midband") else "low")
    return {
        "name": p.get("name"), "kind": p.get("kind"), "lat": p.get("lat"), "lng": p.get("lng"),
        "province": p.get("province"), "vehicles_per_hour": p.get("vehicles_per_hour"),
        "demand": r(p.get("demand"), 3), "gap": r(p.get("gap"), 3), "score": r(p.get("score"), 1),
        "why": p.get("why"), "layer": layer, "already_planned": bool(p.get("already_planned")),
        "dig_once": bool(p.get("dig_once")),
    }


def score_bin(b: dict, cov: dict, mob: dict, vmax: float) -> dict:
    mid_gap = 1.0 - (cov.get("share_5g_midband") or 0.0)
    video_n = b.get("video_rows") or 0
    v4 = (b.get("video_rows_4g") or 0) / video_n if video_n else 0.0
    demand = clip(math.log1p(b.get("video_gb") or 0) / math.log1p(vmax)) if vmax else 0.0
    gb = b.get("gb") or 0
    share5 = (b.get("gb_5g") or 0) / gb if gb else 0.0
    rows = b.get("rows") or 0
    rows5 = (b.get("rows_5g") or 0) / rows if rows else 0.0
    corridor = (mob.get("score") or 0) / 100.0
    service = r(100 * demand * (0.45 * mid_gap + 0.35 * v4 + 0.20 * corridor), 1)
    energy = r(100 * (0.6 * (1 - share5) * ((cov.get("share_5g_midband") or 0)) + 0.4 * (1 if (b.get("rows_2g") or 0) == 0 and (cov.get("share_2g") or 0) > 0.5 else 0.2)), 1)
    coverage = r(100 * rows5 * mid_gap, 1)
    mobility = r(100 * corridor * clip((gb / vmax) if vmax else 0) * 2 + 40 * corridor, 1)
    return {
        "t": b.get("time_start"), "people": b.get("people"), "rows": b.get("rows"),
        "gb": r(gb, 3), "gb_5g_share": r(share5), "video_gb": r(b.get("video_gb"), 3),
        "video_rows_4g_share": r(v4), "rows_5g_share": r(rows5),
        "service": service, "energy": energy, "coverage_check": coverage, "mobility_lens": mobility,
    }


def build_heatmap(profiles, coverage, transport, k: int) -> dict:
    path = RELEASE / "province_bins.json"
    if not path.exists():
        return {"bins": [], "note": "no province × time view in the release"}
    raw = json.loads(path.read_text())
    times = sorted({b["time_start"] for b in raw})
    vmax = max((b.get("video_gb") or 0) for b in raw) or 1
    by = defaultdict(dict)
    for b in raw:
        if b["province"] in ("Undefined", "Unavailable"):
            continue
        cov = coverage.get(b["province"]) or {}
        mob = transport.get("provinces", {}).get(b["province"]) or {}
        by[b["province"]][b["time_start"]] = score_bin(b, cov, mob, vmax)
    frames = []
    for t in times:
        provinces = {}
        for prov, series in by.items():
            if t in series:
                provinces[prov] = series[t]
        frames.append({"t": t, "label": bin_label(t), "provinces": provinces})
    # corridor issues joined to the release: keep points that sit in a released province
    released = set(profiles)
    issues = [slim_point(p) for p in transport.get("points", []) if p.get("province") in released and p.get("coverage") and (p.get("score") or 0) > 0]
    issues.sort(key=lambda x: -(x["score"] or 0))
    return {
        "k": k,
        "grain": "released province × 10-minute bin, people ≥ k",
        "bins": [{"t": f["t"], "label": f["label"]} for f in frames],
        "frames": frames,
        "issues": issues[:180],
        "note": "Heat is the de-identified load in that bin, multiplied by the Elisa/Fintraffic gap at the same place. Cells stay withheld.",
    }


def score_mobility(mob):
    if not mob or not mob.get("points"):
        return {"score": None, "why": ["no rated Fintraffic points in this province"], "action": "—", "withheld": False}
    why = [f"{mob['gap_points']} of {mob['points']} busy road/rail points lack capacity 5G ({mob['roads']} road sensors, {mob['rail']} stations)"]
    if mob["planned_5g_sites"]:
        why.append(f"Elisa already lists {mob['planned_5g_sites']} 5G work sites in the province")
    if mob["dig_once"]:
        why.append(f"{mob['dig_once']} gap points sit within 5 km of an open Fintraffic road work")
    action = ("Ride the open road works: add sites while the road is dug" if mob["dig_once"] >= 2
              else "Close corridor gaps not covered by planned work" if mob["gap_points"] > mob["planned_5g_sites"] / 3
              else "Corridors are covered or already in the plan")
    return {"score": mob["score"], "why": why[:3], "action": action, "withheld": False}


def main() -> None:
    cohorts = json.loads((RELEASE / "cohort_summary.json").read_text())
    manifest = json.loads((RELEASE / "manifest.json").read_text())
    cov_doc = json.loads(COVERAGE.read_text())
    coverage = cov_doc["provinces"]
    transport = json.loads(TRANSPORT.read_text()) if TRANSPORT.exists() else {"provinces": {}, "national": {}, "sources": {}}

    by_prov = defaultdict(list)
    label_only = defaultdict(int)  # 'Undefined', 'Unavailable': labels with no geography, reported but not ranked
    for c in cohorts:
        if c["province"] in ("Undefined", "Unavailable"):
            label_only[c["province"]] += c["row_count"]
            continue
        by_prov[c["province"]].append(c)
    profiles = {prov: profile(rows) for prov, rows in by_prov.items()}
    national_2g = {"rows": sum(c["row_count"] for c in cohorts if c["radio_access_type"] == "2G"),
                   "gb": r(sum(c["volume_sum"] or 0 for c in cohorts if c["radio_access_type"] == "2G"), 4)}
    vmax = max((p["video_gb"] or 0 for p in profiles.values()), default=1) or 1
    total_gb = sum(p["gb"] or 0 for p in profiles.values())
    planned_2g_max = max((v.get("planned_2g_sites", 0) for v in transport["provinces"].values()), default=0)
    underuse_max = max(((coverage.get(pr) or {}).get("share_5g_midband") or 0) * (1 - (p["gb_5g_share"] or 0)) for pr, p in profiles.items()) or 1

    rows = []
    for prov, p in profiles.items():
        cov = coverage.get(prov) or {}
        mob = transport["provinces"].get(prov) or {}
        rows.append({
            "province": prov, "place": PLACE.get(prov, prov), "pooled_province": prov == OTHER,
            "profile": p,
            "cohorts": sorted(by_prov[prov], key=lambda c: -(c["volume_sum"] or 0))[:40],
            "coverage": {k: v for k, v in cov.items() if k != "samples"},
            "coverage_samples": [{"place": s["place"], "has_5g": s["has_5g"], "midband": s["five_g_midband"], "lowband_only": s["five_g_lowband_only"],
                                  "has_2g": s["has_2g"], "five_g_speed": s["five_g_speed"], "four_g_speed": s["four_g_speed"], "five_g_freqs": s["five_g_freqs"]}
                                 for s in cov.get("samples", [])],
            "mobility": {k: v for k, v in mob.items()},
            "service": score_service(p, cov, mob, vmax),
            "energy": score_energy(p, cov, mob, national_2g, planned_2g_max, underuse_max),
            "coverage_check": score_coverage(p, cov),
            "mobility_lens": score_mobility(mob),
        })
    lenses = ("service", "energy", "coverage_check", "mobility_lens")
    for lens in lenses:
        ranked = sorted([x for x in rows if x[lens]["score"] is not None], key=lambda x: -x[lens]["score"])
        for i, row in enumerate(ranked, 1):
            row[lens]["rank"] = i
        for row in rows:
            row[lens].setdefault("rank", None)

    apps = defaultdict(lambda: {"gb": 0.0, "rows": 0, "rows_5g": 0, "gb_5g": 0.0, "rtt_num": 0.0, "rtt_den": 0})
    for c in cohorts:
        a = apps[c["application_category"]]
        a["gb"] += c["volume_sum"] or 0
        a["rows"] += c["row_count"]
        if c["radio_access_type"] == "5G":
            a["rows_5g"] += c["row_count"]
            a["gb_5g"] += c["volume_sum"] or 0
        if c.get("radio_latency_mean") is not None:
            a["rtt_num"] += c["radio_latency_mean"] * c["radio_latency_n"]
            a["rtt_den"] += c["radio_latency_n"]
    classes = sorted([{"app": k, "gb": r(v["gb"]), "rows": v["rows"], "share_5g_gb": r(v["gb_5g"] / v["gb"] if v["gb"] else 0),
                       "share_5g_rows": r(v["rows_5g"] / v["rows"] if v["rows"] else 0),
                       "rtt_ms": r(v["rtt_num"] / v["rtt_den"], 1) if v["rtt_den"] else None,
                       "pooled": k in ("Pooled categories", "All applications")} for k, v in apps.items()],
                     key=lambda x: -(x["gb"] or 0))

    def top(lens):
        c = [x for x in rows if x[lens]["rank"] == 1]
        return c[0] if c else None

    tn = transport.get("national", {})
    heat = build_heatmap(profiles, coverage, transport, manifest["k"])
    issues_by = defaultdict(list)
    for it in heat.get("issues", []):
        issues_by[it["province"]].append(it)
    for row in rows:
        row["issues"] = issues_by.get(row["province"], [])[:12]
    payload = {
        "product": "Where the network is not optimised",
        "pipeline": {
            "steps": [
                {"id": "submit", "title": "Submit", "detail": f"{manifest['source']['name']} · {manifest['source']['rows']:,} rows · sha256 {manifest['source']['sha256'][:12]}…"},
                {"id": "deidentify", "title": "De-identify", "detail": f"Cohort policy (yushinliou/elisa), k = {manifest['k']} · {manifest['audit']['cohorts']} cohorts · min group {manifest['audit']['min_people']} · retention {manifest['audit']['retention_pct']}%"},
                {"id": "insights", "title": "Insights", "detail": f"release × Elisa coverage map ({cov_doc['provinces'].__len__()} provinces, {sum(v['points'] for v in coverage.values())} points) × Fintraffic ({tn.get('rated_points', 0)} rated places)"},
            ],
            "manifest": {k: manifest[k] for k in ("process", "k", "keys", "guarantees", "withheld_by_design", "audit", "folded_into_other_provinces", "released_utc", "baseline_risk_before_policy")},
            "source": {k: manifest["source"][k] for k in ("name", "rows", "subscribers", "cells", "timestamps", "sha256")},
        },
        "window": f"release {manifest['released_utc']} · coverage map {cov_doc['queried_utc']} · Fintraffic {transport.get('queried_utc', '—')}",
        "k": manifest["k"],
        "national": {
            "gb": r(total_gb), "cohorts": len(cohorts), "released_provinces": len(rows),
            "label_only_rows": dict(label_only),
            "rows_2g": national_2g["rows"], "gb_2g": national_2g["gb"],
            "gb_5g_share": r(sum(c["volume_sum"] or 0 for c in cohorts if c["radio_access_type"] == "5G") / total_gb) if total_gb else None,
            "video_rows_4g_share": r(sum(c["row_count"] for c in cohorts if c["application_category"] in VIDEO and c["radio_access_type"] == "4G")
                                     / max(sum(c["row_count"] for c in cohorts if c["application_category"] in VIDEO), 1)),
            "coverage_points": sum(v["points"] for v in coverage.values()),
            "points_without_midband": sum(1 for v in coverage.values() for s in v["samples"] if not s["five_g_midband"]),
            "points_with_2g": sum(1 for v in coverage.values() for s in v["samples"] if s["has_2g"]),
            "mobility": tn,
            "classes": classes,
        },
        "headlines": {lens: (f"{top(lens)[lens]['action']} — {top(lens)['place']}" if top(lens) else "—") for lens in lenses},
        "lenses": {
            "service": {"title": "Service fit", "question": "Video sitting on a weak layer.",
                        "how": "Video demand × thin coverage. Three parts: no capacity 5G on the map, video still on 4G, busy roads without capacity 5G."},
            "energy": {"title": "Energy", "question": "Radios left on for little traffic.",
                       "how": "2G still on with almost no 2G traffic, plus capacity 5G that the release barely uses."},
            "coverage_check": {"title": "Map vs release", "question": "The file says 5G. The map does not.",
                               "how": "5G sessions in the file × places on Elisa's map with no capacity 5G."},
            "mobility_lens": {"title": "Mobility", "question": "Busy roads and stations on thin 5G.",
                              "how": "Fintraffic traffic × Elisa map gap (no 5G or coverage-only 5G)."},
        },
        "sources": {
            "elisa_rating": {"url": "https://content-api.external-resource.elisa.fi/api/coverage-map/rating", "used_for": "layers, bands, speed at a point", "calls": sum(v["points"] for v in coverage.values()) + tn.get("rated_points", 0)},
            "elisa_network_improvements": {"url": "https://content-api.external-resource.elisa.fi/api/coverage-map/network-improvements", "used_for": "planned 5G/4G/2G work sites", "items": tn.get("improvement_sites", 0)},
            "elisa_tiles": {"url": "https://content-api.external-resource.elisa.fi/coverage_assets/{5G|4G|2G|LTEM|NBIOT}/png/{z}/{x}/{y}.png", "used_for": "coverage raster preview in the detail panel (proxied)"},
            **{k: {"url": v["url"], "items": v["items"]} for k, v in transport.get("sources", {}).items()},
            "not_usable": ["elisa.fi/asiakastiedotteet disturbance notices (HTML only)", "elisa.fi/5g-paikkakunnat (HTML only)", "developer.elisa.fi (B2B, contract)", "api.digitransit.fi routing (needs subscription key)"],
        },
        "heatmap": heat,
        "provinces": rows,
        "caveats": [
            "The map heatmap is a second k-anonymous view (province × 10-minute bin). Cells and subscribers stay withheld.",
            "Insights are computed from the de-identified release only. Anything below k subscribers is not available and is marked as withheld, not estimated.",
            "2G is pooled into 'Other provinces' by the policy: the 2G energy signal is national, not provincial.",
            "The RAN file is fabricated; Elisa's coverage map, planned-work list and Fintraffic feeds are live and public.",
            "A province's map score is a sample of towns and busy transport points, not a survey.",
            "Energy is inferred from layers carrying nothing; there is no power measurement in the data.",
        ],
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    for lens in lenses:
        print(f"\n{lens}:")
        for row in sorted([x for x in rows if x[lens]["rank"]], key=lambda x: x[lens]["rank"])[:5]:
            print(f"  {row[lens]['rank']:2d}. {row['place']:22s} {row[lens]['score']:6.1f}  {row[lens]['action']}")
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
