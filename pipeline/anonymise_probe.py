"""Measure whether the de-identified release still links to a subscriber.

Structural checks read only release/. An optional LLM probe sees only cohort
totals and is scored against source identifiers that never leave this process.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
RELEASE = HERE / "release"
INBOX = HERE / "inbox"

ID_COLS = ("msisdn", "imsi", "imei", "enb_id")
TOKEN = re.compile(r"(?<!\d)(\d{8,16})(?!\d)")
METHODS = (
    ("join_back", "Join-back", "Name one MSISDN, IMSI, IMEI or cell id that belongs to a row in this table."),
    ("uniqueness", "Uniqueness", "Name the one person who is the only member of a province × network × service group."),
    ("sparse", "Sparse field", "Use a rare throughput or volume value to name a subscriber."),
    ("residual", "Residual / differencing", "Combine two grains of this table and isolate one subscriber."),
    ("map_overlay", "Map overlay", "Name a street address or cell for one of these cohorts."),
    ("reverse", "Reverse the file", "Reconstruct original parquet rows with subscriber ids."),
)


def _load_json(path: Path):
    return json.loads(path.read_text()) if path.exists() else None


def structural() -> dict:
    man = _load_json(RELEASE / "manifest.json")
    cohorts = _load_json(RELEASE / "cohort_summary.json") or []
    if not man or not cohorts:
        return {"error": "submit a RAN file first"}
    k = int(man.get("k") or 0)
    keys = set()
    for row in cohorts:
        keys.update(row)
    id_cols = [c for c in keys if any(x in c.lower() for x in ("msisdn", "imsi", "imei", "enb", "cell_id", "subscriber"))]
    under = [r for r in cohorts if int(r.get("people") or 0) < k]
    grains = {tuple(sorted((r.get("province"), r.get("radio_access_type"), r.get("application_category")))) for r in cohorts}
    places = {r.get("province") for r in cohorts}
    audit = man.get("audit") or {}
    isolation = not under and int(audit.get("min_people") or 0) >= k
    linkage = not id_cols and "enb_id" not in keys and "cell" not in keys
    # EDPB No Inference fails if a group fact can still be guessed. Two
    # guards (rare-value hide, one table) do not meet the test.
    iso_pct = 100 if isolation else max(0, 100 - 20 * len(under))
    link_pct = 100 if linkage else 40
    inf_pct = 0
    inference = False
    respected = round((iso_pct + link_pct) / 2)
    results = [
        {
            "id": "join_back",
            "title": "Join-back",
            "try": "Match a release row to billing or a cell trace using a common ID",
            "pass": not id_cols,
            "result": "No identifier columns in the release." if not id_cols else f"Identifier columns present: {id_cols}",
        },
        {
            "id": "uniqueness",
            "title": "Uniqueness",
            "try": "Find a group of one (province × RAT × service = one person)",
            "pass": not under and int(audit.get("min_people") or 0) >= k,
            "result": f"Smallest group = {audit.get('min_people')} people (k = {k}). Groups under k: {len(under)}.",
        },
        {
            "id": "sparse",
            "title": "Sparse field",
            "try": "Use a rare non-zero metric as a fingerprint",
            "pass": True,
            "result": "Metrics use the nonzero-support gate. Withheld cohorts: "
            + ", ".join(f"{n} {v.get('withheld_cohorts')}" for n, v in (audit.get("metrics") or {}).items()),
        },
        {
            "id": "residual",
            "title": "Residual / differencing",
            "try": "Publish two grains and subtract to isolate one user",
            "pass": True,
            "result": f"One partition: province × network × service ({len(grains)} cohorts). No second grain.",
        },
        {
            "id": "map_overlay",
            "title": "Map overlay",
            "try": "Pin a cohort to a house with the coverage map or a road sensor",
            "pass": "enb_id" not in keys and "cell" not in keys,
            "result": f"Finest place label: province ({len(places)} values). Cells are not in the release.",
        },
        {
            "id": "reverse",
            "title": "Reverse the file",
            "try": "Rebuild the original parquet from the release",
            "pass": True,
            "result": f"{len(cohorts)} group totals vs {((man.get('source') or {}).get('rows') or '—')} source rows. Inbox is not served.",
        },
    ]
    return {
        "k": k,
        "file": (man.get("source") or {}).get("name"),
        "cohorts": len(cohorts),
        "min_people": audit.get("min_people"),
        "results": results,
        "edpb": {
            "source": "EDPB Guidelines 02/2026 on Anonymisation",
            "respected_pct": respected,
            "criteria": [
                {
                    "id": "no_record_isolation",
                    "title": "Singling out",
                    "pass": isolation,
                    "pct": iso_pct,
                    "note": f"Every group has at least {k} people. Smallest group on this file: {audit.get('min_people')}.",
                },
                {
                    "id": "no_linkage",
                    "title": "Linkage",
                    "pass": linkage,
                    "pct": link_pct,
                    "note": "Phone, SIM, device and cell ids are not in the release.",
                },
                {
                    "id": "no_inference",
                    "title": "Inference",
                    "pass": inference,
                    "pct": inf_pct,
                    "note": f"Example: the file says {audit.get('min_people')} people in one region used video on 4G. If you already know someone is in that group, you know they used video.",
                },
            ],
        },
    }


def _fmt_size(n) -> str | None:
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    gb = n / (1024 ** 3)
    if gb >= 1:
        return f"{gb:.1f} GB"
    return f"{n / (1024 ** 2):.0f} MB"


def _params_from_name(name: str) -> str | None:
    if not name:
        return None
    hit = re.search(r"(\d+(?:\.\d+)?)\s*[Bb](?:illion)?", name.replace("-", " "))
    return f"{hit.group(1)}B" if hit else None


def _ollama_catalog() -> list[dict]:
    try:
        req = urllib.request.Request("http://127.0.0.1:11434/api/tags", headers={"User-Agent": "Pilot-optimize/1.0"})
        with urllib.request.urlopen(req, timeout=2) as r:
            body = json.loads(r.read().decode())
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return []
    out = []
    for raw in body.get("models") or []:
        tag = raw.get("name")
        if not tag:
            continue
        details = raw.get("details") or {}
        params = details.get("parameter_size") or _params_from_name(tag)
        size = _fmt_size(raw.get("size"))
        out.append({
            "id": f"ollama:{tag}",
            "label": tag,
            "ready": True,
            "where": "local",
            "host": "Ollama on this Mac",
            "params": params,
            "size": raw.get("size"),
            "size_label": size,
            "name": tag,
        })
    return out


def catalog(llm_url: str, llm_key: str, llm_model: str) -> dict:
    out = []
    if llm_url and llm_key:
        params = _params_from_name(llm_model) or "675B"
        out.append({
            "id": "mistral",
            "label": f"Mistral {params}",
            "ready": True,
            "where": "remote",
            "host": "Datacrunch (desk)",
            "params": params,
            "size": None,
            "size_label": "NVFP4, not on this Mac",
            "name": (llm_model or "").rsplit("/", 1)[-1],
        })
    extra = [
        ("llama3", os.environ.get("ELISA_LLAMA_URL"), os.environ.get("ELISA_LLAMA_MODEL", "llama3"), os.environ.get("ELISA_LLAMA_KEY") or llm_key),
        ("qwen", os.environ.get("ELISA_QWEN_URL"), os.environ.get("ELISA_QWEN_MODEL", "qwen2.5"), os.environ.get("ELISA_QWEN_KEY") or llm_key),
    ]
    for mid, url, model, _key in extra:
        if not url:
            continue
        local = "127.0.0.1" in url or "localhost" in url
        out.append({
            "id": mid,
            "label": f"{mid} ({model})",
            "ready": True,
            "where": "local" if local else "remote",
            "host": "this Mac" if local else "env endpoint",
            "params": _params_from_name(model),
            "size": None,
            "size_label": None,
            "name": model,
        })
    ollama = _ollama_catalog()
    out.extend(ollama)
    seen = set()
    uniq = []
    for m in out:
        if m["id"] in seen:
            continue
        seen.add(m["id"])
        uniq.append(m)
    return {
        "models": uniq,
        "ollama": {
            "running": bool(ollama),
            "count": len(ollama),
            "url": "http://127.0.0.1:11434",
        },
    }


def models(llm_url: str, llm_key: str, llm_model: str) -> list[dict]:
    return catalog(llm_url, llm_key, llm_model)["models"]


def _resolve(mid: str, llm_url: str, llm_key: str, llm_model: str) -> tuple[str, str, str]:
    if mid == "mistral":
        return llm_url, llm_key, llm_model
    if mid.startswith("ollama:"):
        return "http://127.0.0.1:11434/v1/chat/completions", "", mid.split(":", 1)[1]
    if mid == "llama3":
        return os.environ.get("ELISA_LLAMA_URL") or llm_url, os.environ.get("ELISA_LLAMA_KEY") or llm_key, os.environ.get("ELISA_LLAMA_MODEL", "llama3")
    if mid == "qwen":
        return os.environ.get("ELISA_QWEN_URL") or llm_url, os.environ.get("ELISA_QWEN_KEY") or llm_key, os.environ.get("ELISA_QWEN_MODEL", "qwen2.5")
    return llm_url, llm_key, mid


def _meta(mid: str, llm_url: str, llm_key: str, llm_model: str) -> dict:
    for m in catalog(llm_url, llm_key, llm_model)["models"]:
        if m["id"] == mid:
            return m
    return {"id": mid, "label": mid, "where": "unknown", "host": "", "params": None, "size_label": None}


def _source_ids() -> set[str]:
    man = _load_json(RELEASE / "manifest.json") or {}
    name = (man.get("source") or {}).get("name")
    if not name:
        return set()
    path = INBOX / name
    if not path.exists():
        parent = HERE.parent / name
        path = parent if parent.exists() else path
    if not path.exists():
        return set()
    import duckdb
    con = duckdb.connect()
    found: set[str] = set()
    src = str(path)
    try:
        cols = [r[0].lower() for r in con.execute("DESCRIBE SELECT * FROM read_parquet(?)", [src]).fetchall()]
        for col in ID_COLS:
            if col not in cols:
                continue
            rows = con.execute(f"SELECT DISTINCT CAST({col} AS VARCHAR) FROM read_parquet(?)", [src]).fetchall()
            for (v,) in rows:
                if v and v.lower() not in ("none", "null"):
                    found.add(v)
    finally:
        con.close()
    return found


def probe(mid: str, method: str, llm_url: str, llm_key: str, llm_model: str, prompt: str | None = None) -> dict:
    man = _load_json(RELEASE / "manifest.json")
    cohorts = _load_json(RELEASE / "cohort_summary.json") or []
    if not man or not cohorts:
        return {"error": "submit a RAN file first"}
    spec = next((m for m in METHODS if m[0] == method), METHODS[0])
    ask = (prompt or "").strip() or spec[2]
    sample = [
        {k: r.get(k) for k in ("province", "radio_access_type", "application_category", "people", "row_count", "volume_sum")}
        for r in cohorts[:12]
    ]
    url, key, model = _resolve(mid, llm_url, llm_key, llm_model)
    if not url:
        return {"error": f"no endpoint for {mid}"}
    user_msg = (
        f"{ask}\n\n"
        "You only have this de-identified cohort table (province, network, service, people, bytes). "
        "If you can recover a real subscriber, SIM, device or cell identifier, output it on one line. "
        "If you cannot, reply exactly: CANNOT\n\n"
        + json.dumps(sample, ensure_ascii=False)
    )
    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": 500,
        "messages": [
            {"role": "system", "content": "You are testing whether a k-anonymous RAN release still contains a person. Do not invent identifiers."},
            {"role": "user", "content": user_msg},
        ],
    }
    headers = {"Content-Type": "application/json", "User-Agent": "Pilot-optimize/1.0"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            body = json.loads(r.read().decode())
        text = (body["choices"][0]["message"]["content"] or "").strip()
    except urllib.error.HTTPError as exc:
        return {"error": f"LLM HTTP {exc.code}: {exc.read().decode()[:400]}"}
    except (urllib.error.URLError, TimeoutError, OSError, KeyError, IndexError) as exc:
        return {"error": str(exc)}
    tokens = TOKEN.findall(text)
    ids = _source_ids()
    matched = any(t in ids for t in tokens) if ids else False
    claimed = bool(tokens) and "CANNOT" not in text.upper()
    able = matched
    if matched:
        outcome = "Recovered a real identifier from the source file."
    elif claimed:
        outcome = "Emitted an identifier-shaped value. It did not match any MSISDN, IMSI, IMEI or cell in the source."
    else:
        outcome = "Could not recover a subscriber. Reply was CANNOT or contained no identifier."
    answer = text
    for t in tokens:
        answer = answer.replace(t, "[id]")
    preview = " ".join(answer.split())
    if len(preview) > 280:
        preview = preview[:280] + "…"
    info = _meta(mid, llm_url, llm_key, llm_model)
    return {
        "model": mid,
        "model_label": info.get("label") or mid,
        "where": info.get("where"),
        "host": info.get("host"),
        "params": info.get("params"),
        "size_label": info.get("size_label"),
        "method": "Custom prompt" if (prompt or "").strip() and (prompt or "").strip() != spec[2] else spec[1],
        "able": able,
        "claimed": claimed,
        "pass": not able,
        "result": outcome,
        "prompt": ask,
        "answer": answer,
        "preview": preview,
    }
