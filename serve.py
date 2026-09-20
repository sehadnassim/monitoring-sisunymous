#!/usr/bin/env python3
"""Serve the pipeline UI. Standard library only (DuckDB is imported lazily for the release step).

  ../.venv-data/bin/python serve.py            → http://127.0.0.1:8790

Routes
  GET  /                             UI
  POST /api/submit?k=100             body = a .parquet or .csv RAN file → inbox/, then release + insights run in a thread
  GET  /api/pipeline                 pipeline state: stage, progress, last error
  GET  /api/insights                 data/insights.json (built from the release only)
  GET  /api/release                  release/manifest.json (audit of the de-identification)
  GET  /api/release/cohorts          release/cohort_summary.json (the de-identified table itself)
  GET  /api/coverage                 data/coverage.json (Elisa map samples)
  GET  /api/transport                data/transport.json (Fintraffic + Elisa planned work)
  GET  /api/coverage/point?lat&lng   live pass-through to Elisa's coverage map
  GET  /api/tile/{5G|4G|2G|LTEM|NBIOT}/{z}/{x}/{y}.png   proxied Elisa coverage tile
  POST /api/chat                     insight explainer (Elisa OpenAI-compatible Mistral)
  GET  /api/anonymise/tests          measured linkage checks on the release
  GET  /api/anonymise/models         LLMs that can try to recover a person
  POST /api/anonymise/probe          body {model, method} — LLM attempt, scored, no ids returned
  POST /api/demo?k=100               run the bundled mock through the same pipeline
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from sources import digitraffic, elisa  # noqa: E402

FRONT = HERE / "frontend"
DATA = HERE / "data"
INBOX = HERE / "inbox"
RELEASE = HERE / "release"
SESSION = RELEASE / "user_session.json"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8790
PY = sys.executable

STATE = {"stage": "idle", "detail": "", "started": None, "finished": None, "error": None, "file": None, "k": None, "accepted": False}
LOCK = threading.Lock()


def load_env():
    path = HERE / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


load_env()
LLM_URL = os.environ.get("ELISA_LLM_URL", "https://containers.datacrunch.io/data-sovereignty-mistral-large-3/v1/chat/completions")
LLM_KEY = os.environ.get("ELISA_LLM_KEY", "")
LLM_MODEL = os.environ.get("ELISA_LLM_MODEL", "mistralai/Mistral-Large-3-675B-Instruct-2512-NVFP4")


def has_user_release() -> bool:
    with LOCK:
        accepted = bool(STATE.get("accepted"))
    return accepted and (DATA / "insights.json").exists()


from pipeline import rag, anonymise_probe  # noqa: E402

SYSTEM = """You are SisuNymous, the analyst voice of Monitoring, Elisa's network-optimisation desk.
After a parquet is uploaded it is de-identified; retrieved passages below are your only source (RAG).
Rules:
- Answer only from the retrieved passages. Copy ranks, scores, GB, percents and place names exactly.
- If a number is not in the passages, say it is not in the release. Do not invent it.
- Never invent cell ids, MSISDNs, IMSI/IMEI, or subscriber identities.
- Map vs release = 5G session share in the release × sampled places without 3500 MHz 5G on Elisa's map.
- Service fit = video demand on a thin layer. Energy = radios on for little traffic. Mobility = busy roads/rail vs advertised 5G.
- If the passages say NO RELEASE INDEX, tell the user to submit a .parquet file first.
- Finnish names unchanged. You may introduce yourself as SisuNymous once.
- Always end with one concrete action: "Do this: …". Name the place.
- Use RAN terms: 3.5 GHz 5G (n78 / capacity), 700 MHz 5G (n28 / coverage). Never say "fast 5G".
"""

VOICE_SYSTEM = """The user is on a voice call. Speak like a calm colleague, not a dashboard.
Use short spoken sentences. No markdown, no bullet stars, no tables, no URLs.
Read numbers naturally: 39.3 is thirty-nine point three; 93% is ninety-three percent.
Name the place first, then the finding, then what to do. Keep it under 80 words unless they ask for more.
"""


def llm_chat(user_messages: list[dict], lens: str | None, province: str | None, clock: str | None = None, voice: bool = False) -> dict:
    if not LLM_KEY:
        return {"error": "ELISA_LLM_KEY is not set (put it in optimize/.env)"}
    history = []
    for m in user_messages[-8:]:
        role = m.get("role")
        content = (m.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            history.append({"role": role, "content": content[:4000]})
    if not history or history[-1]["role"] != "user":
        return {"error": "send a user message"}
    payload = {
        "model": LLM_MODEL,
        "temperature": 0.45 if voice else 0.3,
        "max_tokens": 280 if voice else 700,
        "messages": [
            {"role": "system", "content": SYSTEM + ("\n" + VOICE_SYSTEM if voice else "")},
            {"role": "system", "content": rag.context(history[-1]["content"], lens, province, clock) if has_user_release() else rag.context("")},
            *history,
        ],
    }
    req = urllib.request.Request(
        LLM_URL,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {LLM_KEY}", "Content-Type": "application/json", "User-Agent": "Pilot-optimize/1.0"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as r:
            body = json.loads(r.read().decode())
    except urllib.error.HTTPError as exc:
        err = exc.read().decode()[:800]
        return {"error": f"LLM HTTP {exc.code}: {err}"}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"error": f"LLM unreachable: {exc}"}
    try:
        text = body["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return {"error": "unexpected LLM response", "raw": body}
    return {"reply": text, "model": body.get("model") or LLM_MODEL}


def set_state(**kw):
    with LOCK:
        STATE.update(kw)


def run_pipeline(path: Path, k: int):
    try:
        set_state(stage="deidentify", detail=f"Cohort policy, k={k}", started=time.time(), finished=None, error=None, file=path.name, k=k)
        subprocess.run([PY, "-m", "pipeline.release", "--input", str(path), "--k", str(k)], cwd=HERE, check=True, capture_output=True, text=True)
        if not (DATA / "coverage.json").exists():
            set_state(stage="coverage", detail="asking Elisa's coverage map (first run only)")
            subprocess.run([PY, "build_coverage.py"], cwd=HERE, check=True, capture_output=True, text=True)
        if not (DATA / "transport.json").exists():
            set_state(stage="transport", detail="Fintraffic + Elisa planned work (first run only)")
            subprocess.run([PY, "build_transport.py"], cwd=HERE, check=True, capture_output=True, text=True)
        set_state(stage="insights", detail="release × public APIs")
        subprocess.run([PY, "build_insights.py"], cwd=HERE, check=True, capture_output=True, text=True)
        SESSION.write_text(json.dumps({
            "accepted": True, "file": path.name, "k": k,
            "finished_utc": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        }))
        rag.index()
        set_state(stage="done", detail="", finished=time.time(), accepted=True)
    except subprocess.CalledProcessError as exc:
        set_state(stage="error", error=(exc.stderr or exc.stdout or str(exc))[-1200:], finished=time.time())
    except Exception:  # noqa: BLE001
        set_state(stage="error", error=traceback.format_exc()[-1200:], finished=time.time())


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(FRONT), **kw)

    def log_message(self, fmt, *args):
        if args and "/api/" in str(args[0]) and "/api/pipeline" not in str(args[0]):
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str, cache: str = "no-store"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def end_headers(self):
        path = (self.path or "").split("?", 1)[0]
        if path.endswith((".html", ".js", ".css")) or path in ("/", "/index.html"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def _json(self, code: int, payload):
        self._send(code, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _file(self, path: Path, hint: str):
        if not path.exists():
            return self._json(404, {"error": f"{path.name} missing; {hint}"})
        self._send(200, path.read_bytes(), "application/json; charset=utf-8")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        p = url.path
        if p == "/api/pipeline":
            with LOCK:
                st = dict(STATE)
            st["has_release"] = has_user_release()
            st["has_insights"] = has_user_release()
            st["accepted"] = has_user_release()
            return self._json(200, st)
        if p == "/api/anonymise/tests":
            if not has_user_release():
                return self._json(404, {"error": "upload data first"})
            out = anonymise_probe.structural()
            return self._json(200 if "error" not in out else 404, out)
        if p == "/api/anonymise/models":
            pack = anonymise_probe.catalog(LLM_URL, LLM_KEY, LLM_MODEL)
            pack["methods"] = [{"id": m[0], "title": m[1], "prompt": m[2]} for m in anonymise_probe.METHODS]
            return self._json(200, pack)
        if p == "/api/insights":
            if not has_user_release():
                return self._json(404, {"error": "no submitted file yet — public map layers are Elisa/Fintraffic, not a RAN release"})
            return self._file(DATA / "insights.json", "submit a file first")
        if p == "/api/release":
            if not has_user_release():
                return self._json(404, {"error": "upload data first"})
            return self._file(RELEASE / "manifest.json", "upload data first")
        if p == "/api/release/cohorts":
            if not has_user_release():
                return self._json(404, {"error": "submit a file first"})
            return self._file(RELEASE / "cohort_summary.json", "submit a file first")
        if p == "/api/coverage":
            return self._file(DATA / "coverage.json", "run build_coverage.py")
        if p == "/api/transport":
            return self._file(DATA / "transport.json", "run build_transport.py")
        if p == "/api/live/trains":
            trains = digitraffic.train_locations()
            return self._json(200, {"trains": trains, "source": f"{digitraffic.RAIL}/train-locations/latest"})
        if p == "/api/live/roads":
            now = time.time()
            cached = getattr(self.server, "_roads", None)
            if cached and now - cached[0] < 25:
                roads = cached[1]
            else:
                roads = digitraffic.live_roads()
                self.server._roads = (now, roads)
            return self._json(200, {
                "roads": roads,
                "source": f"{digitraffic.ROAD}/tms/v1/stations/data",
                "note": "Fintraffic TMS — real vehicles/hour, not simulated",
            })
        if p == "/api/coverage/point":
            q = urllib.parse.parse_qs(url.query)
            try:
                lat, lng = float(q["lat"][0]), float(q["lng"][0])
            except (KeyError, ValueError):
                return self._json(400, {"error": "lat and lng required"})
            layers = elisa.rating(lat, lng)
            if layers is None:
                return self._json(502, {"error": "Elisa coverage API unreachable"})
            return self._json(200, {"lat": lat, "lng": lng, "source": elisa.RATING, "layers": layers, **elisa.summarise(layers)})
        if p.startswith("/api/tile/"):
            parts = p[len("/api/tile/"):].removesuffix(".png").split("/")
            try:
                net, z, x, y = parts[0], int(parts[1]), int(parts[2]), int(parts[3])
            except (IndexError, ValueError):
                return self._json(400, {"error": "use /api/tile/{network}/{z}/{x}/{y}.png"})
            png = elisa.tile(net, z, x, y)
            if png is None:
                return self._json(502, {"error": "tile unavailable"})
            return self._send(200, png, "image/png", cache="max-age=3600")
        if p == "/":
            self.path = "/index.html"
        if p.endswith((".js", ".css", ".html")):
            self.close_connection = True
        return super().do_GET()

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if url.path == "/api/anonymise/probe":
            if not has_user_release():
                return self._json(404, {"error": "upload data first"})
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "invalid JSON"})
            result = anonymise_probe.probe(
                str(body.get("model") or "mistral"),
                str(body.get("method") or "join_back"),
                LLM_URL,
                LLM_KEY,
                LLM_MODEL,
                prompt=str(body.get("prompt") or "").strip() or None,
            )
            return self._json(200 if "error" not in result else 502, result)
        if url.path == "/api/chat":
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._json(400, {"error": "invalid JSON"})
            result = llm_chat(body.get("messages") or [], body.get("lens"), body.get("province"), body.get("clock"), bool(body.get("voice")))
            return self._json(200 if "reply" in result else 502, result)
        if url.path in ("/api/demo", "/api/local"):
            q = urllib.parse.parse_qs(url.query)
            k = 100
            name = Path(urllib.parse.unquote(q.get("name", ["elisa_aaltoai_hackathon_2026_mock.parquet"])[0])).name
            candidates = [HERE.parent / name, INBOX / name, HERE.parent / "elisa_aaltoai_hackathon_2026_mock.parquet"]
            src = next((p for p in candidates if p.exists()), None)
            if src is None:
                return self._json(404, {"error": f"{name} is not on the machine next to optimize/"})
            with LOCK:
                busy = STATE["stage"] not in ("idle", "done", "error")
            if busy:
                return self._json(409, {"error": "pipeline is running"})
            set_state(stage="submitted", detail=f"{src.name} (local) · {src.stat().st_size:,} bytes", error=None)
            threading.Thread(target=run_pipeline, args=(src, k), daemon=True).start()
            return self._json(202, {"accepted": src.name, "local": True, "bytes": src.stat().st_size, "k": k})
        if url.path != "/api/submit":
            return self._json(404, {"error": "unknown route"})
        with LOCK:
            busy = STATE["stage"] not in ("idle", "done", "error")
        if busy:
            return self._json(409, {"error": "pipeline is running"})
        q = urllib.parse.parse_qs(url.query)
        k = 100
        name = Path(urllib.parse.unquote(q.get("name", ["submission.parquet"])[0])).name
        if not name.lower().endswith((".parquet", ".csv")):
            return self._json(400, {"error": "submit a .parquet or .csv file"})
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return self._json(400, {"error": "empty body"})
        INBOX.mkdir(exist_ok=True)
        dest = INBOX / name
        with dest.open("wb") as fh:
            remaining = length
            while remaining > 0:
                chunk = self.rfile.read(min(1 << 20, remaining))
                if not chunk:
                    break
                fh.write(chunk)
                remaining -= len(chunk)
        set_state(stage="submitted", detail=f"{name} · {length:,} bytes", error=None)
        threading.Thread(target=run_pipeline, args=(dest, k), daemon=True).start()
        return self._json(202, {"accepted": name, "bytes": length, "k": k})


if __name__ == "__main__":
    import ssl
    bind = os.environ.get("ELISA_BIND", "127.0.0.1")
    cert = os.environ.get("ELISA_SSL_CERT", "")
    key = os.environ.get("ELISA_SSL_KEY", "")
    httpd = ThreadingHTTPServer((bind, PORT), Handler)
    scheme = "http"
    if cert and key:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"
    print(f"optimize UI on {scheme}://{bind}:{PORT}  (python {PY})", flush=True)
    httpd.serve_forever()
