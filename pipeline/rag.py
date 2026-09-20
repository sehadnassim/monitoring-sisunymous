"""Retrieve de-identified insights as context for the explainer LLM.

The store is rebuilt whenever insights.json changes (after a parquet is processed).
Chunks are population-level only: provinces, lenses, public API joins. No raw rows.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
INSIGHTS = HERE / "data" / "insights.json"

_TOKEN = re.compile(r"[a-z0-9äöå\-]+", re.I)
_STOP = {
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "is", "it", "this",
    "that", "with", "what", "why", "how", "where", "who", "can", "you", "me", "please",
}

LENS_ALIASES = {
    "service": ("service", "service fit", "video", "streaming", "4g", "throughput"),
    "energy": ("energy", "2g", "idle", "sunset", "power"),
    "coverage_check": ("map vs release", "coverage", "mid-band", "3500", "700", "reconcile", "label"),
    "mobility_lens": ("mobility", "road", "rail", "corridor", "fintraffic", "dig once", "station"),
}

_cache: dict = {"mtime": None, "chunks": []}


def tokens(text: str) -> list[str]:
    return [t for t in _TOKEN.findall((text or "").lower()) if t not in _STOP and len(t) > 1]


def _load() -> dict | None:
    if not INSIGHTS.exists():
        return None
    return json.loads(INSIGHTS.read_text())


def _chunk(title: str, text: str, tags: list[str]) -> dict:
    body = " ".join(str(text).split())
    return {"title": title, "text": body, "tags": tags, "tok": set(tokens(title + " " + body + " " + " ".join(tags)))}


def _build(ins: dict) -> list[dict]:
    chunks = []
    n = ins.get("national") or {}
    man = (ins.get("pipeline") or {}).get("manifest") or {}
    src = (ins.get("pipeline") or {}).get("source") or {}
    chunks.append(_chunk(
        "National release",
        (
            f"{ins.get('product')}. Window {ins.get('window')}. k={ins.get('k')}. "
            f"Source file {src.get('name')} · {src.get('rows')} rows · {src.get('subscribers')} subscribers (source count, not published). "
            f"Released: {n.get('cohorts')} cohorts, {n.get('gb')} GB, 5G byte share {n.get('gb_5g_share')}, "
            f"video sessions on 4G {n.get('video_rows_4g_share')}, 2G GB pooled {n.get('gb_2g')}. "
            f"Coverage samples without 3500 MHz: {n.get('points_without_midband')} / {n.get('coverage_points')}. "
            f"Headlines: {ins.get('headlines')}. "
            f"Policy: {man.get('process')}. Guarantees: {man.get('guarantees')}. "
            f"Withheld: {man.get('withheld_by_design')}. Audit: {man.get('audit')}."
        ),
        ["national", "release", "k", "audit"],
    ))
    for key, L in (ins.get("lenses") or {}).items():
        chunks.append(_chunk(
            f"Lens {L.get('title')}",
            f"id={key}. Question: {L.get('question')} How scored: {L.get('how')}",
            ["lens", key, *(LENS_ALIASES.get(key, ()))],
        ))
    for i, c in enumerate(ins.get("caveats") or []):
        chunks.append(_chunk(f"Caveat {i+1}", c, ["caveat", "limit"]))
    for cls in (n.get("classes") or [])[:16]:
        chunks.append(_chunk(
            f"Traffic class {cls.get('app')}",
            f"{cls.get('gb')} GB, {cls.get('rows')} rows, 5G bytes {cls.get('share_5g_gb')}, 5G rows {cls.get('share_5g_rows')}, RTT {cls.get('rtt_ms')} ms, pooled={cls.get('pooled')}",
            ["class", cls.get("app") or "", "traffic"],
        ))
    heat = ins.get("heatmap") or {}
    if heat.get("bins"):
        chunks.append(_chunk(
            "Heatmap clock",
            f"Grain {heat.get('grain')}. Bins: {[b.get('label') for b in heat.get('bins', [])]}. {heat.get('note')}",
            ["heatmap", "time", "play", "bin"],
        ))
    for p in ins.get("provinces") or []:
        prof = p.get("profile") or {}
        cov = p.get("coverage") or {}
        parts = [
            f"{p.get('place')} ({p.get('province')}). Released rows {prof.get('rows')}, GB {prof.get('gb')}, "
            f"5G bytes {prof.get('gb_5g_share')}, 5G sessions {prof.get('rows_5g_share')}, "
            f"video GB {prof.get('video_gb')} share {prof.get('video_gb_share')}, "
            f"video sessions on 4G {prof.get('video_rows_4g_share')}, "
            f"video tp 4G {prof.get('video_4g_tp')} → 5G {prof.get('video_5g_tp')}, "
            f"map mid-band share {cov.get('share_5g_midband')}, 700-only {cov.get('share_5g_lowband_only')}, "
            f"2G on map {cov.get('share_2g')}."
        ]
        for key in ("service", "energy", "coverage_check", "mobility_lens"):
            L = p.get(key) or {}
            parts.append(
                f"{key}: rank {L.get('rank')} score {L.get('score')} action «{L.get('action')}» why {L.get('why')}"
            )
        issues = p.get("issues") or []
        if issues:
            parts.append("Corridor issues: " + " | ".join(
                f"{it.get('name')} ({it.get('kind')}, score {it.get('score')}, {it.get('layer')}, {it.get('why')}"
                f"{', dig-once' if it.get('dig_once') else ''}{', planned' if it.get('already_planned') else ''})"
                for it in issues[:8]
            ))
        samples = p.get("coverage_samples") or []
        if samples:
            parts.append("Elisa samples: " + " | ".join(
                f"{s.get('place')} 5G={s.get('has_5g')} midband={s.get('midband')} bands={s.get('five_g_freqs')} {s.get('five_g_speed')}Mbps"
                for s in samples
            ))
        frames = []
        for fr in (heat.get("frames") or []):
            cell = (fr.get("provinces") or {}).get(p.get("province"))
            if cell:
                frames.append(
                    f"{fr.get('label')}: service {cell.get('service')} energy {cell.get('energy')} "
                    f"map-vs-release {cell.get('coverage_check')} mobility {cell.get('mobility_lens')} "
                    f"GB {cell.get('gb')} video4g {cell.get('video_rows_4g_share')}"
                )
        if frames:
            parts.append("By 10-minute bin: " + " · ".join(frames))
        tags = [p.get("place") or "", p.get("province") or "", "province"]
        chunks.append(_chunk(f"Province {p.get('place')}", " ".join(parts), tags))

    for it in (heat.get("issues") or [])[:40]:
        chunks.append(_chunk(
            f"Issue {it.get('name')}",
            f"{it.get('kind')} in {it.get('province')} score {it.get('score')} layer {it.get('layer')} "
            f"veh/h {it.get('vehicles_per_hour')} why {it.get('why')} planned={it.get('already_planned')} dig_once={it.get('dig_once')}",
            ["issue", it.get("name") or "", it.get("province") or "", it.get("kind") or ""],
        ))
    return chunks


def index() -> list[dict]:
    if not INSIGHTS.exists():
        _cache["chunks"] = []
        _cache["mtime"] = None
        return []
    mtime = INSIGHTS.stat().st_mtime
    if _cache["chunks"] and _cache["mtime"] == mtime:
        return _cache["chunks"]
    ins = _load()
    _cache["chunks"] = _build(ins) if ins else []
    _cache["mtime"] = mtime
    return _cache["chunks"]


def retrieve(question: str, lens: str | None = None, province: str | None = None, clock: str | None = None, k: int = 10) -> list[dict]:
    chunks = index()
    if not chunks:
        return []
    q = tokens(question or "")
    qset = set(q)
    # If the question names a place, prefer that over the map selection.
    named = None
    for ch in chunks:
        if not ch["title"].startswith("Province "):
            continue
        place = ch["title"].split(" ", 1)[-1].lower()
        if place and place in (question or "").lower():
            named = ch["title"]
            break
        for tag in ch["tags"]:
            if tag and len(tag) > 3 and tag.lower() in (question or "").lower() and tag.lower() not in _STOP:
                named = ch["title"]
                break
    pin = named or (f"Province {province}" if province else None)
    scored = []
    for ch in chunks:
        title_hits = len(qset & set(tokens(ch["title"])))
        overlap = len(qset & ch["tok"])
        boost = title_hits * 6 + overlap * 2
        if pin and pin.lower() in ch["title"].lower():
            boost += 10
        if lens:
            for alias in LENS_ALIASES.get(lens, (lens,)):
                if alias in ch["title"].lower() or alias in " ".join(ch["tags"]).lower():
                    boost += 4
                    break
        if clock and clock.replace(" UTC", "") in ch["text"]:
            boost += 2
        if ch["title"] == "National release" or ch["title"].startswith("Lens"):
            boost += 2
        if boost > 0:
            scored.append((boost, ch))
    scored.sort(key=lambda x: -x[0])
    picked = [c for _, c in scored[:k]]
    # Always pin national + active lens + selected province if not already in.
    must = []
    for ch in chunks:
        if ch["title"] == "National release":
            must.append(ch)
        if lens and ch["title"].lower().startswith("lens") and lens.replace("_", " ") in (ch["text"] + ch["title"]).lower():
            must.append(ch)
        if pin and pin.lower() in ch["title"].lower() and ch["title"].startswith("Province"):
            must.append(ch)
    out, seen = [], set()
    for ch in must + picked:
        if ch["title"] in seen:
            continue
        seen.add(ch["title"])
        out.append(ch)
        if len(out) >= k + 3:
            break
    return out


def context(question: str, lens: str | None = None, province: str | None = None, clock: str | None = None) -> str:
    hits = retrieve(question, lens, province, clock)
    if not hits:
        return (
            "NO RELEASE INDEX. The user has not submitted a parquet that finished the pipeline. "
            "Do not invent ranks, scores, or provincial facts. Tell them to submit a .parquet file."
        )
    parts = [
        "Retrieved passages from the de-identified insights (RAG). Use only these numbers. "
        f"UI lens={lens or '—'} selected_province={province or '—'} clock={clock or '—'}."
    ]
    for i, ch in enumerate(hits, 1):
        parts.append(f"[{i} {ch['title']}] {ch['text']}")
    return "\n\n".join(parts)
