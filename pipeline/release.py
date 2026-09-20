"""Step 2 of the pipeline: submitted file → de-identified release.

Runs the Cohort policy from https://github.com/yushinliou/elisa unchanged
(pipeline/cohort_policy.py): k-anonymous cohorts on
(province, radio_access_type, application_category), three-level pooling
(application → 'Pooled categories' → 'All applications' → 'Other provinces'),
and the nonzero-support gate (a metric is published only when ≥ k distinct
subscribers have a nonzero value).

Everything downstream (build_insights.py, the UI) reads only what this writes
into release/. Raw rows stay in inbox/ and never leave this module. No
identifier is logged or written.

  python -m pipeline.release --input inbox/file.parquet --k 100
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from .cohort_policy import KEYS, SHIPPED_METRICS, build_leaf, build_policy

HERE = Path(__file__).resolve().parent.parent
RELEASE = HERE / "release"

# Same three metrics upstream ships, plus radio RTT and 5G/4G-relevant fields the
# insights need. Every extra metric passes the same nonzero-support gate.
METRICS = SHIPPED_METRICS + [
    ("cont_rtt_radio_avg", "radio_latency", 1),
    ("tcp_retrans_byte_ratio_downlink_avg", "retrans_down", 5),
]
REQUIRED = {"time_start", "msisdn", "imsi", "imei", "enb_id", *KEYS, *[m[0] for m in METRICS]}


def records(con, sql):
    cur = con.execute(sql)
    cols = [x[0] for x in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def run(source: Path, k: int = 100, out: Path = RELEASE) -> dict:
    if k not in (5, 20, 100):
        raise ValueError("k must be 5, 20 or 100 (the thresholds upstream evaluated)")
    out.mkdir(parents=True, exist_ok=True)
    t0 = time.perf_counter()
    c = duckdb.connect()
    c.execute("SET threads=4")
    c.execute("SET memory_limit='2GB'")
    c.execute("SET temp_directory=''")  # fail closed rather than spill raw rows to disk
    if source.suffix.lower() == ".csv":
        c.read_csv(str(source)).create_view("raw")
    else:
        c.read_parquet(str(source)).create_view("raw")
    cols = {r["column_name"] for r in records(c, "DESCRIBE raw")}
    missing = REQUIRED - cols
    if missing:
        raise ValueError(f"submitted file lacks required fields: {sorted(missing)}")

    profile = records(c, """SELECT count(*) AS rows, count(distinct msisdn) AS subscribers,
        count(distinct enb_id) AS cells, count(distinct province) AS province_labels,
        count(distinct application_category) AS app_categories,
        count(distinct time_start) AS timestamps, min(time_start) AS first_epoch, max(time_start) AS last_epoch FROM raw""")[0]
    if c.execute("""SELECT count(*) FROM (SELECT msisdn FROM raw GROUP BY 1
            HAVING count(distinct imsi)<>1 OR count(distinct imei)<>1)""").fetchone()[0]:
        raise ValueError("subscriber mapping is inconsistent (msisdn ↔ imsi/imei)")

    # Baseline risk before the policy: how many rows sit alone in their group.
    baselines = []
    for label, keys in [
        ("time + cell + app + network", "time_start,enb_id,application_category,radio_access_type"),
        ("time + province + app + network", "time_start,province,application_category,radio_access_type"),
        ("province + app + network", ",".join(KEYS)),
    ]:
        b = records(c, f"""WITH g AS (SELECT count(*) n,count(distinct msisdn) u FROM raw GROUP BY {keys})
            SELECT count(*) AS groups, sum(CASE WHEN u=1 THEN n ELSE 0 END)*100.0/sum(n) AS single_subject_row_pct,
            sum(CASE WHEN u<{k} THEN n ELSE 0 END)*100.0/sum(n) AS under_k_row_pct FROM g""")[0]
        baselines.append({"grain": label, **{kk: float(v) if isinstance(v, float) else v for kk, v in b.items()}})

    build_leaf(c, METRICS)
    totals = records(c, "SELECT " + ",".join(f"sum({l}_n) AS {l}_n,sum({l}_sum) AS {l}_sum" for _, l, _ in METRICS) + " FROM cohorts")[0]
    build_policy(c, k, METRICS)

    audit = records(c, """SELECT count(*) AS cohorts, sum(n) AS rows_released, min(people) AS min_people,
        sum(CASE WHEN application_category IN ('Pooled categories','All applications') THEN n ELSE 0 END) AS pooled_app_rows,
        sum(CASE WHEN province='Other provinces' THEN n ELSE 0 END) AS pooled_province_rows FROM current_release""")[0]
    audit = {kk: (float(v) if isinstance(v, float) else v) for kk, v in audit.items()}
    audit["retention_pct"] = round(100 * audit["rows_released"] / profile["rows"], 3)
    audit["metrics"] = {}
    for _, l, _ in METRICS:
        m = records(c, f"""SELECT sum({l}_support) AS released_obs, sum({l}_released*{l}_support) AS rec_sum,
            count(*) FILTER (WHERE {l}_released IS NULL AND {l}_people>0) AS withheld_cohorts FROM current_release""")[0]
        on, os_ = totals[f"{l}_n"], totals[f"{l}_sum"]
        rel_mean = (m["rec_sum"] / m["released_obs"]) if m["released_obs"] else None
        audit["metrics"][l] = {
            "observation_coverage_pct": round(100 * (m["released_obs"] or 0) / on, 2) if on else None,
            "global_mean_error_pct": round(100 * abs(rel_mean - os_ / on) / abs(os_ / on), 3) if rel_mean and on and os_ else None,
            "withheld_cohorts": int(m["withheld_cohorts"] or 0),
        }
    # Which source provinces got folded into 'Other provinces', per network.
    folded = records(c, "SELECT radio_access_type, list(province ORDER BY province) AS provinces FROM province_map WHERE released_province='Other provinces' GROUP BY 1 ORDER BY 1")

    # The release itself: one row per cohort. Means only where the gate allows.
    sel = ",".join(f"{l}_released AS {l}_mean, {l}_support AS {l}_n" for _, l, _ in METRICS)
    sums = ",".join(f"CASE WHEN {l}_released IS NOT NULL THEN {l}_sum END AS {l}_sum" for _, l, _ in METRICS if l == "volume")
    cohorts = records(c, f"""SELECT province, radio_access_type, application_category, n AS row_count, people,
        {sel}, {sums} FROM current_release ORDER BY province, radio_access_type, application_category""")
    for row in cohorts:
        for kk, v in list(row.items()):
            if isinstance(v, float):
                row[kk] = round(v, 6)

    sha = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest = {
        "process": "Cohort de-identification (yushinliou/elisa, cohort_policy.py, unchanged)",
        "k": k,
        "keys": KEYS,
        "metrics": [{"source_field": f, "label": l} for f, l, _ in METRICS],
        "guarantees": {
            "k_anonymity_on_keys": True,
            "l_diversity": False,
            "differential_privacy": False,
            "note": "One disjoint partition at one threshold. Nonzero-support gate mitigates sparse fields; it is not a diversity guarantee.",
        },
        "withheld_by_design": [
            "cell (enb_id) level anything",
            "time (10-minute bins)",
            "subscriber, device and IMSI identifiers",
            "any joint distribution beyond province × network × application",
        ],
        "source": {"name": source.name, "sha256": sha, "bytes": source.stat().st_size,
                   "rows": profile["rows"], "subscribers": profile["subscribers"], "cells": profile["cells"],
                   "timestamps": profile["timestamps"], "first_epoch": profile["first_epoch"], "last_epoch": profile["last_epoch"]},
        "baseline_risk_before_policy": baselines,
        "audit": audit,
        "folded_into_other_provinces": folded,
        "released_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "elapsed_s": round(time.perf_counter() - t0, 2),
    }
    (out / "cohort_summary.json").write_text(json.dumps(cohorts, ensure_ascii=False))
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))

    # Separate k-anonymous view: province × 10-minute bin only. No cells, no people.
    # Used to animate the issue heatmap. Same k gate; not a second cut of the cohort table.
    bins = records(c, f"""
        SELECT province, time_start, count(*) AS rows, count(distinct msisdn) AS people,
            sum(data_GB_sum) AS gb,
            sum(CASE WHEN radio_access_type='5G' THEN data_GB_sum ELSE 0 END) AS gb_5g,
            sum(CASE WHEN radio_access_type='5G' THEN 1 ELSE 0 END) AS rows_5g,
            sum(CASE WHEN radio_access_type='2G' THEN 1 ELSE 0 END) AS rows_2g,
            sum(CASE WHEN application_category='Streaming' THEN data_GB_sum ELSE 0 END) AS video_gb,
            sum(CASE WHEN application_category='Streaming' THEN 1 ELSE 0 END) AS video_rows,
            sum(CASE WHEN application_category='Streaming' AND radio_access_type='4G' THEN 1 ELSE 0 END) AS video_rows_4g
        FROM final_mapped
        GROUP BY 1, 2
        HAVING people >= {k}
        ORDER BY 1, 2
    """)
    for row in bins:
        for kk, v in list(row.items()):
            if isinstance(v, float):
                row[kk] = round(v, 6)
    (out / "province_bins.json").write_text(json.dumps(bins, ensure_ascii=False))
    manifest["time_view"] = {
        "grain": "released province × 10-minute bin",
        "k": k,
        "rows": len(bins),
        "note": "k-anonymous population totals only. Not published at cell or subscriber grain.",
    }
    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1))
    c.close()
    return manifest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True)
    ap.add_argument("--k", type=int, default=100)
    a = ap.parse_args()
    m = run(Path(a.input).resolve(), a.k)
    au = m["audit"]
    print(f"release: {au['cohorts']} cohorts, min group {au['min_people']}, retention {au['retention_pct']}%, "
          f"pooled app rows {au['pooled_app_rows']}, pooled province rows {au['pooled_province_rows']}, {m['elapsed_s']} s")
    for l, v in au["metrics"].items():
        print(f"  {l:14s} coverage {v['observation_coverage_pct']}%  mean err {v['global_mean_error_pct']}%  withheld cohorts {v['withheld_cohorts']}")
    if m["folded_into_other_provinces"]:
        for f in m["folded_into_other_provinces"]:
            print(f"  folded into 'Other provinces' on {f['radio_access_type']}: {len(f['provinces'])} provinces")


if __name__ == "__main__":
    main()
