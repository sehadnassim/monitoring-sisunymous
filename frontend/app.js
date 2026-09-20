/* Where the network is not optimised — pipeline (submit → de-identify → insights), four lenses, one map. */

const $ = (id) => document.getElementById(id);
const LENSES = ["service", "energy", "coverage_check", "mobility_lens"];
// lat/lng → SVG (affine fit of province centroids to the map file, ~10 px error on a 460×820 canvas)
const PX = [35.2237, -1.6682, -560.3687];
const PY = [1.1539, -76.0199, 5320.8471];
const proj = (lat, lng) => ({ x: PX[0] * lng + PX[1] * lat + PX[2], y: PY[0] * lng + PY[1] * lat + PY[2] });
const unproj = (x, y) => {
  const a = PX[0], b = PX[1], c = PY[0], d = PY[1];
  const det = a * d - b * c;
  const X = x - PX[2], Y = y - PY[2];
  return { lng: (d * X - b * Y) / det, lat: (-c * X + a * Y) / det };
};

let data = null;
let geo = null;
let transport = null;
let lens = "service";
let selected = null;
let detailTab = "issues";
let rankAll = false;
let livePoint = null;
let clock = 0;
let playing = false;
let playTimer = null;
let osm = null;
let glowHeat = null;
let trafficLayer = null;
let trainLayer = null;
let liveTrains = [];
let liveRoads = [];
const trainMarks = new Map();
let trainTickOn = false;
let lastHeatAt = 0;
let lastTrainTick = 0;
const FINLAND_BOUNDS = [[59.5, 19.1], [70.1, 31.6]];
const layers = { samples: false, roads: true, trains: true, planned: false, heat: true };

const fmt = (n, d = 0) => (n == null ? "–" : Number(n).toLocaleString("en-GB", { maximumFractionDigits: d, minimumFractionDigits: d }));

const LENS_UI = {
  service: {
    title: "Video",
    line: "Video still on 4G, or without 3.5 GHz 5G.",
    parts: [["Coverage", "no 3.5 GHz"], ["Video", "still on 4G"], ["Roads", "busy, coverage-only 5G"]],
  },
  energy: {
    title: "Energy",
    line: "Radios left on with little traffic.",
    parts: [["2G", "still on"], ["5G", "capacity unused"]],
  },
  coverage_check: {
    title: "Coverage",
    line: "RAN says 5G. The coverage map does not.",
    parts: [["RAN", "5G sessions"], ["Map", "no 3.5 GHz"]],
  },
  mobility_lens: {
    title: "Mobility",
    line: "Busy roads and rail on coverage-only 5G.",
    parts: [["Traffic", "cars / trains"], ["Gap", "no 3.5 GHz"]],
  },
};

function scoreKeyHtml(key, empty) {
  if (empty) return "";
  const ui = LENS_UI[key] || { parts: [] };
  return ui.parts.map(([k, v]) => `<span><b>${k}</b> ${v}</span>`).join("");
}
const pct = (x, d = 0) => (x == null ? "–" : fmt(100 * x, d) + "%");
const gb = (x) => (x == null ? "–" : x >= 100 ? fmt(x) + " GB" : x >= 1 ? fmt(x, 1) + " GB" : fmt(x * 1000, 0) + " MB");
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function tint(score, max) {
  if (score == null) return "#0f1a25";
  const t = max ? Math.min(1, Math.max(0, score / max)) : 0;
  const stops = [[20, 40, 58], [124, 45, 18], [251, 113, 133]];
  const s = t < 0.5 ? [stops[0], stops[1], t * 2] : [stops[1], stops[2], (t - 0.5) * 2];
  const c = s[0].map((v, i) => Math.round(v + (s[1][i] - v) * s[2]));
  return `rgb(${c.join(",")})`;
}
const rows = () => data.provinces.filter((p) => p[lens].rank != null).sort((a, b) => a[lens].rank - b[lens].rank);
const byName = (n) => data?.provinces?.find((p) => p.province === n);

function heatScore(prov) {
  const frames = data?.heatmap?.frames || [];
  if (!frames.length) return byName(prov)?.[lens]?.score ?? null;
  const i = Math.max(0, Math.min(frames.length - 1, Math.floor(clock)));
  const f = Math.max(0, Math.min(1, clock - i));
  const a = frames[i].provinces[prov]?.[lens];
  const b = frames[Math.min(i + 1, frames.length - 1)].provinces[prov]?.[lens];
  if (a == null && b == null) return byName(prov)?.[lens]?.score ?? null;
  if (a == null) return b;
  if (b == null) return a;
  return a + (b - a) * f;
}
const maxScore = () => {
  if (!data) return 1;
  const fromHeat = (data.heatmap?.frames || []).flatMap((fr) => Object.values(fr.provinces).map((p) => p[lens] || 0));
  return Math.max(...fromHeat, ...data.provinces.map((p) => p[lens].score || 0), 1);
};
function clockLabel() {
  const bins = data?.heatmap?.bins || [];
  if (!bins.length) return "—";
  const i = Math.max(0, Math.min(bins.length - 1, Math.round(clock)));
  return bins[i].label;
}

/* ---------- pipeline ---------- */
async function pollPipeline(once = false) {
  const st = await (await fetch("/api/pipeline")).json();
  const order = ["idle", "submitted", "deidentify", "coverage", "transport", "insights", "done", "error"];
  const idx = order.indexOf(st.stage);
  const mark = (stage, from, to) => {
    const el = document.querySelector(`.stage[data-stage="${stage}"]`);
    el.classList.remove("done", "active", "error");
    if (st.stage === "error") el.classList.add(idx >= from ? "error" : "");
    else if (idx > to) el.classList.add("done");
    else if (idx >= from) el.classList.add("active");
  };
  mark("submit", 1, 1);
  mark("deidentify", 2, 2);
  mark("insights", 3, 5);
  if (st.stage === "done") for (const s of document.querySelectorAll(".stage")) s.classList.add("done");
  const status = $("status");
  status.classList.toggle("err", st.stage === "error");
  const pipePct = { submitted: 72, deidentify: 82, coverage: 88, transport: 92, insights: 96, done: 100, error: 100 }[st.stage];
  status.textContent =
    st.stage === "error" ? "pipeline failed:\n" + st.error
    : st.stage === "done" ? `done: ${st.file || "—"} · k=${st.k || "—"}`
    : st.stage === "idle" ? "Submit a .parquet file in step 1. The map stays empty until that file is de-identified."
    : `running: ${st.stage} · ${st.detail}`;
  if (st.stage !== "idle" && pipePct != null && !window._uploading) setLoad(pipePct, st.stage === "done" ? "100%" : `${pipePct}%`, st.stage === "error");
  if (st.stage === "idle" && !window._uploading) setLoad(0, "", false);
  $("load").classList.toggle("busy", !["idle", "done", "error"].includes(st.stage));
  $("load").classList.toggle("err", st.stage === "error");
  $("submitBtn").disabled = !["idle", "done", "error"].includes(st.stage) || window._uploading;
  if (st.stage === "done" && window._wasRunning) {
    window._wasRunning = false;
    await loadAll();
  }
  if (!["idle", "done", "error"].includes(st.stage)) {
    window._wasRunning = true;
    if (!once) setTimeout(() => pollPipeline(), 1200);
  }
  return st;
}

function setLoad(pct, label, err) {
  $("loadFill").style.width = Math.max(0, Math.min(100, pct)) + "%";
  $("loadPct").textContent = label || "";
  $("load").classList.toggle("err", !!err);
}

function mb(n) {
  return (n / 1e6).toFixed(1) + " MB";
}

function startLoad(text) {
  window._uploading = true;
  $("submitBtn").disabled = true;
  $("status").classList.remove("err");
  $("load").classList.add("busy");
  $("load").classList.remove("err");
  setLoad(4, "…", false);
  $("status").textContent = text;
  if (window._loadTick) clearInterval(window._loadTick);
  let shown = 4;
  window._loadTick = setInterval(() => {
    if (!window._uploading) return;
    shown = Math.min(68, shown + 2);
    setLoad(shown, Math.round(shown) + "%");
  }, 200);
}

function finishLoadOk(msg) {
  window._uploading = false;
  if (window._loadTick) clearInterval(window._loadTick);
  setLoad(70, "70%");
  $("status").textContent = msg;
  window._wasRunning = true;
  pollPipeline();
}

function finishLoadErr(msg) {
  window._uploading = false;
  if (window._loadTick) clearInterval(window._loadTick);
  $("status").classList.add("err");
  $("status").textContent = msg;
  setLoad(100, "failed", true);
  $("submitBtn").disabled = false;
  $("load").classList.remove("busy");
}

async function submit(ev) {
  ev.preventDefault();
  const f = $("file").files[0];
  if (!f) return alert("Submit a .parquet (or .csv) RAN file first.");
  startLoad(`reading ${f.name} (${mb(f.size)})…`);

  // Same file already sits next to the app (the hackathon mock). Skip the 89 MB POST.
  const local = await fetch(`/api/local?k=100&name=${encodeURIComponent(f.name)}`, { method: "POST" });
  if (local.ok) {
    finishLoadOk(`${f.name} found on disk · de-identifying…`);
    return;
  }

  startLoad(`uploading ${f.name} · 0% (0 / ${mb(f.size)})`);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", `/api/submit?k=100&name=${encodeURIComponent(f.name)}`);
  xhr.upload.onprogress = (e) => {
    if (!e.lengthComputable) return;
    const pct = (100 * e.loaded) / e.total;
    setLoad(4 + pct * 0.66, `${Math.round(pct)}%`);
    $("status").textContent = `uploading ${f.name} · ${Math.round(pct)}% (${mb(e.loaded)} / ${mb(e.total)})`;
  };
  xhr.onload = () => {
    let j = {};
    try { j = JSON.parse(xhr.responseText); } catch { j = { error: xhr.responseText }; }
    if (xhr.status >= 300) return finishLoadErr(j.error || "submit failed");
    finishLoadOk(`uploaded ${f.name} · de-identifying…`);
  };
  xhr.onerror = () => finishLoadErr("upload failed — check the connection and try again");
  xhr.send(f);
}


/* ---------- audit + sources ---------- */
function drawAudit() {
  const m = data.pipeline.manifest;
  const a = m.audit;
  const src = data.pipeline.source;
  const met = Object.entries(a.metrics).map(([k, v]) => `<tr><td>${k}</td><td class="num">${pct(v.observation_coverage_pct / 100, 2)}</td><td class="num">${fmt(v.global_mean_error_pct, 3)}%</td><td class="num">${v.withheld_cohorts}</td></tr>`).join("");
  const base = m.baseline_risk_before_policy.map((b) => `<tr><td>${b.grain}</td><td class="num">${fmt(b.groups)}</td><td class="num">${fmt(b.single_subject_row_pct, 1)}%</td><td class="num">${fmt(b.under_k_row_pct, 1)}%</td></tr>`).join("");
  const folded = m.folded_into_other_provinces.map((f) => `<li><b>${f.radio_access_type}</b>: ${f.provinces.length} source provinces folded into “Other provinces”</li>`).join("") || "<li>none</li>";
  $("audit").innerHTML = `
    <div class="panel-head"><h2>De-identification audit</h2><p class="hint">${esc(m.process)} · released ${m.released_utc}</p></div>
    <div class="kv">
      <div class="stat"><b>${fmt(src.rows)}</b><span>source rows · ${fmt(src.subscribers)} subscribers · ${fmt(src.cells)} cells · ${src.timestamps} bins</span></div>
      <div class="stat ok"><b>k = ${m.k}</b><span>on ${m.keys.join(" × ")}</span></div>
      <div class="stat ok"><b>${fmt(a.cohorts)}</b><span>cohorts released · min group ${a.min_people}</span></div>
      <div class="stat"><b>${fmt(a.retention_pct, 1)}%</b><span>rows retained</span></div>
      <div class="stat warn"><b>${fmt(a.pooled_app_rows)}</b><span>rows under a pooled application label</span></div>
      <div class="stat warn"><b>${fmt(a.pooled_province_rows)}</b><span>rows under “Other provinces”</span></div>
    </div>
    <h4>Guarantees</h4>
    <p class="hint small">k-anonymity on keys: <b class="${m.guarantees.k_anonymity_on_keys ? "ok" : "bad"}">${m.guarantees.k_anonymity_on_keys}</b> · l-diversity: <b>${m.guarantees.l_diversity}</b> · differential privacy: <b>${m.guarantees.differential_privacy}</b>. ${esc(m.guarantees.note)}</p>
    <h4>Withheld by design</h4>
    <p class="hint small">${m.withheld_by_design.map(esc).join(" · ")}</p>
    <h4>Pooled by the policy</h4>
    <ul class="caveats">${folded}</ul>
    <div class="grid2" style="grid-template-columns:1fr 1fr;align-items:start">
      <div><h4>Metric utility after the gate</h4><table><thead><tr><th>metric</th><th class="num">obs. coverage</th><th class="num">global mean error</th><th class="num">withheld cohorts</th></tr></thead><tbody>${met}</tbody></table></div>
      <div><h4>Risk before the policy</h4><table><thead><tr><th>grain</th><th class="num">groups</th><th class="num">rows alone</th><th class="num">rows under k</th></tr></thead><tbody>${base}</tbody></table></div>
    </div>`;
}

function drawSources() {
  const s = data.sources;
  const cards = Object.entries(s).filter(([k]) => k !== "not_usable").map(([k, v]) => `<div class="cls"><div class="row"><span>${k.replace(/_/g, " ")}</span><b>${v.items != null ? fmt(v.items) + " items" : v.calls != null ? fmt(v.calls) + " calls" : ""}</b></div><div class="url">${esc(v.url)}</div>${v.used_for ? `<div class="sub hint">${esc(v.used_for)}</div>` : ""}</div>`).join("");
  $("sources").innerHTML = `
    <div class="panel-head"><h2>Public sources used</h2><p class="hint">everything below is public and keyless; the submitted file never leaves inbox/</p></div>
    <div class="src">${cards}</div>
    <h4>Checked, not usable</h4><p class="hint small">${s.not_usable.map(esc).join(" · ")}</p>`;
}

/* ---------- header / KPIs ---------- */
function drawKpis() {
  if (!data) {
    document.body.classList.remove("has-release");
    $("kpis").hidden = true;
    $("kpis").innerHTML = "";
    $("window").textContent = "Submit a RAN file to start.";
    $("stageSubmit").textContent = "Upload the RAN export. Nothing is ranked until you do.";
    $("stageDeid").textContent = "Every published group has at least 100 people.";
    $("stageIns").textContent = "RAN file × coverage map × traffic.";
    return;
  }
  const n = data.national;
  const m = n.mobility || {};
  const items = [
    [fmt(n.released_provinces), "regions ranked"],
    [pct(n.video_rows_4g_share), "video still on 4G", "warn"],
    [pct(n.gb_5g_share), "bytes on 5G"],
    [`${n.points_without_midband} / ${n.coverage_points}`, "towns without 3.5 GHz", "warn"],
    [`${m.gap_points ?? "–"} / ${m.rated_points ?? "–"}`, "roads/rail with a gap", "warn"],
  ];
  document.body.classList.add("has-release");
  $("kpis").hidden = false;
  $("kpis").innerHTML = items.map(([v, l, c]) => `<div class="kpi ${c || ""}"><b>${v}</b><span>${l}</span></div>`).join("");
  $("window").textContent = `k ≥ ${data.k} · ${n.released_provinces} regions`;
  $("stageSubmit").textContent = (data.pipeline?.source?.name || "RAN file") + " loaded";
  $("stageDeid").textContent = `k = ${data.k} · ${fmt(n.cohorts)} groups`;
  $("stageIns").textContent = `${n.released_provinces} regions ranked`;
}

/* ---------- OpenStreetMap ---------- */
const LAYER_COLOR = { mid: "#86efac", low: "#fdba74", none: "#fb7185", planned: "#a78bfa" };

function markIcon(kind, cls) {
  const color = kind === "road" ? "#38bdf8" : (LAYER_COLOR[cls] || "#5eead4");
  const shape = kind === "rail" ? "dia" : kind === "road" ? "sq" : "dot";
  return L.divIcon({ className: `mk ${shape}`, html: `<i style="background:${color}"></i>`, iconSize: [12, 12], iconAnchor: [6, 6] });
}

function provinceCenter(name) {
  const pts = [];
  const cov = data?._coverage?.provinces?.[name];
  if (cov) for (const s of cov.samples || []) if (s.lat) pts.push([s.lat, s.lng]);
  for (const p of window._issues || []) if (p.province === name && p.lat) pts.push([p.lat, p.lng]);
  const row = byName(name);
  if (row) for (const s of row.coverage_samples || []) if (s.lat) pts.push([s.lat, s.lng]);
  if (!pts.length) return null;
  return [pts.reduce((a, p) => a + p[0], 0) / pts.length, pts.reduce((a, p) => a + p[1], 0) / pts.length];
}

function focusPlace(name) {
  selected = name;
  const c = provinceCenter(name);
  if (c && osm) osm.flyTo(c, Math.max(osm.getZoom(), 8), { duration: 0.45 });
  redraw();
}

function fitFinland() {
  if (!osm) return;
  osm.setView([64.7, 26.2], 5);
}

function ensureMap() {
  if (osm) return osm;
  osm = L.map("map", {
    zoomControl: true,
    minZoom: 5,
    maxBounds: [[58.8, 18.0], [70.8, 33.0]],
    maxBoundsViscosity: 0.8,
  });
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 18,
    attribution: "&copy; OpenStreetMap",
  }).addTo(osm);
  osm.createPane("glow");
  osm.getPane("glow").style.zIndex = 3;
  trafficLayer = L.layerGroup().addTo(osm);
  trainLayer = L.layerGroup().addTo(osm);
  fitFinland();
  osm.on("click", (e) => {
    if (e.originalEvent.target.closest(".leaflet-marker-icon")) return;
    liveQuery(e);
  });
  const refit = () => { osm.invalidateSize(); fitFinland(); };
  setTimeout(refit, 120);
  setTimeout(refit, 600);
  window.addEventListener("resize", () => osm && osm.invalidateSize());
  return osm;
}

function trafficPoints() {
  const roads = (liveRoads.length ? liveRoads : (transport?.points || []).filter((p) => p.kind === "road"));
  const rail = (transport?.points || []).filter((p) => p.kind === "rail");
  return [...roads, ...rail].filter((p) => p.lat && p.lng);
}

function drawMap() {
  ensureMap();
  drawHeat(true);
  drawTraffic();
}

function drawHeat(force) {
  if (!osm) return;
  const now = Date.now();
  if (!force && now - lastHeatAt < 280) return;
  lastHeatAt = now;
  const max = data ? maxScore() : 1;
  const issuePts = (data?.heatmap?.issues || trafficPoints().map((p) => ({
    ...p, layer: p.coverage?.five_g_midband ? "mid" : p.coverage?.has_5g ? "low" : "none",
  }))).filter((p) => p.lat && (p.score || 0) > 0);
  window._issues = issuePts;
  const legend = $("mapLegend");
  if (legend) {
    legend.innerHTML = data
      ? `<span>efficient</span><i></i><span>inefficient</span>`
      : `<span>Finland · road and rail</span>`;
  }

  if (glowHeat) { osm.removeLayer(glowHeat); glowHeat = null; }
  if (!layers.heat || !data || !window.L?.heatLayer) return;
  const pts = [];
  for (const r of data.provinces || []) {
    const c = provinceCenter(r.province);
    const sc = heatScore(r.province);
    if (!c || sc == null) continue;
    pts.push([c[0], c[1], Math.min(1, 0.55 + 0.45 * (sc / max))]);
  }
  for (const p of issuePts) {
    const regional = (heatScore(p.province) || 0) / max;
    pts.push([p.lat, p.lng, Math.min(1, 0.4 + 0.6 * ((p.score || 0) / 25) * (0.45 + 0.55 * regional))]);
  }
  if (!pts.length) return;
  glowHeat = L.heatLayer(pts, {
    pane: "glow",
    radius: 55,
    blur: 20,
    maxZoom: 5,
    max: 1,
    minOpacity: 0.5,
    gradient: { 0.2: "#155e75", 0.4: "#22d3ee", 0.55: "#facc15", 0.75: "#f97316", 1: "#ef4444" },
  }).addTo(osm);
}

function drawTraffic() {
  if (!trafficLayer) return;
  trafficLayer.clearLayers();
  if (layers.roads) {
    for (const p of trafficPoints().filter((p) => p.kind === "road")) {
      const veh = p.vehicles_per_hour || 0;
      L.marker([p.lat, p.lng], { icon: markIcon("road", "mid") })
        .on("click", (e) => { L.DomEvent.stop(e); if (byName(p.province)) { detailTab = "issues"; focusPlace(p.province); } })
        .bindTooltip(`${p.name || "road"} · Fintraffic · ${fmt(veh)} vehicles/h`)
        .addTo(trafficLayer);
    }
  }

  if (layers.trains) {
    for (const p of trafficPoints().filter((p) => p.kind === "rail")) {
      L.marker([p.lat, p.lng], { icon: markIcon("rail", "low") })
        .on("click", (e) => { L.DomEvent.stop(e); if (byName(p.province)) { detailTab = "issues"; focusPlace(p.province); } })
        .bindTooltip(`${p.name} · station`)
        .addTo(trafficLayer);
    }
  }
  if (!layers.trains) {
    for (const rec of trainMarks.values()) trainLayer.removeLayer(rec.marker);
    trainMarks.clear();
  }

  if (layers.samples && data) {
    for (const [prov, v] of Object.entries(data._coverage?.provinces || {})) {
      for (const s of v.samples || []) {
        if (!s.lat) continue;
        const cls = !s.has_5g ? "none" : s.five_g_midband ? "mid" : "low";
        L.marker([s.lat, s.lng], { icon: markIcon("town", cls) })
          .on("click", (e) => { L.DomEvent.stop(e); if (byName(prov)) { detailTab = "coverage"; focusPlace(prov); } })
          .bindTooltip(s.place)
          .addTo(trafficLayer);
      }
    }
  }
  if (layers.planned && transport) {
    for (const it of transport.improvements || []) {
      if (!it.lat) continue;
      L.marker([it.lat, it.lng], { icon: markIcon("town", "planned") })
        .bindTooltip(`Planned · ${(it.types || []).join("/")}`)
        .addTo(trafficLayer);
    }
  }
  if (livePoint?.lat) {
    L.circleMarker([livePoint.lat, livePoint.lng], {
      radius: 8, color: "#5eead4", weight: 2, fillColor: "#fff", fillOpacity: 1,
    }).addTo(trafficLayer);
  }
}

function trainIcon(deg) {
  return L.divIcon({
    className: "mk train",
    html: `<b style="transform:rotate(${deg}deg)">▶</b>`,
    iconSize: [16, 16],
    iconAnchor: [8, 8],
  });
}

function heading(from, to) {
  return (Math.atan2(to[0] - from[0], to[1] - from[1]) * 180) / Math.PI;
}

function syncTrains(list) {
  if (!trainLayer) return;
  const now = performance.now();
  const seen = new Set();
  for (const t of list) {
    if (t.lat == null) continue;
    const id = String(t.train);
    seen.add(id);
    let rec = trainMarks.get(id);
    if (!rec) {
      rec = {
        lat: t.lat, lng: t.lng, fromLat: t.lat, fromLng: t.lng, toLat: t.lat, toLng: t.lng,
        t0: now, dur: 8000, deg: 0, vLat: 0, vLng: 0, speed: t.speed,
        marker: L.marker([t.lat, t.lng], { icon: trainIcon(0), zIndexOffset: 400 })
          .bindTooltip(`Train ${t.train}`)
          .addTo(trainLayer),
      };
      trainMarks.set(id, rec);
    } else {
      const moved = Math.hypot(t.lat - rec.toLat, t.lng - rec.toLng) > 0.0004;
      if (moved) {
        rec.fromLat = rec.lat;
        rec.fromLng = rec.lng;
        rec.toLat = t.lat;
        rec.toLng = t.lng;
        rec.t0 = now;
        rec.dur = 8000;
        rec.vLat = (rec.toLat - rec.fromLat) / rec.dur;
        rec.vLng = (rec.toLng - rec.fromLng) / rec.dur;
        rec.deg = heading([rec.fromLat, rec.fromLng], [rec.toLat, rec.toLng]);
        rec.marker.setIcon(trainIcon(rec.deg));
      }
    }
    rec.speed = t.speed;
    rec.marker.setTooltipContent(`Train ${t.train}${t.speed != null ? ` · ${fmt(t.speed)} km/h · live` : " · live"}`);
  }
  for (const [id, rec] of trainMarks) {
    if (seen.has(id)) continue;
    trainLayer.removeLayer(rec.marker);
    trainMarks.delete(id);
  }
  if (!trainTickOn) {
    trainTickOn = true;
    requestAnimationFrame(tickTrains);
  }
}

function tickTrains(ts) {
  const dt = lastTrainTick ? ts - lastTrainTick : 16;
  lastTrainTick = ts;
  for (const rec of trainMarks.values()) {
    const u = Math.min(1, (ts - rec.t0) / rec.dur);
    if (u < 1 && (rec.toLat !== rec.fromLat || rec.toLng !== rec.fromLng)) {
      rec.lat = rec.fromLat + (rec.toLat - rec.fromLat) * u;
      rec.lng = rec.fromLng + (rec.toLng - rec.fromLng) * u;
    } else if (rec.vLat || rec.vLng) {
      rec.lat += (rec.vLat || 0) * dt;
      rec.lng += (rec.vLng || 0) * dt;
    }
    rec.marker.setLatLng([rec.lat, rec.lng]);
  }
  requestAnimationFrame(tickTrains);
}

async function loadTrains() {
  try {
    const j = await (await fetch("/api/live/trains")).json();
    liveTrains = j.trains || [];
    if (layers.trains) syncTrains(liveTrains);
  } catch { /* keep last */ }
}

async function loadRoads() {
  try {
    const j = await (await fetch("/api/live/roads")).json();
    liveRoads = j.roads || [];
    if (osm && layers.roads) drawTraffic();
  } catch { /* keep last */ }
}

function thinTowns(r) {
  return (r.coverage_samples || []).filter((s) => !s.midband).map((s) => s.place);
}

function issueLayer(x) {
  if (x.layer === "none" || x.layer === "low" || x.layer === "mid") return x.layer;
  const w = String(x.why || "");
  if (/no 5g|nothing rated/i.test(w)) return "none";
  if (/coverage|700|3500|mid-band/i.test(w)) return "low";
  return "low";
}

function thinRoads(r) {
  return (r.issues || []).filter((x) => issueLayer(x) !== "mid").map((x) => x.name);
}

function plainWhy(r) {
  const p = r.profile || {};
  const c = r.coverage || {};
  const m = r.mobility || {};
  const towns = thinTowns(r);
  const n = (r.coverage_samples || []).length;
  if (lens === "service") {
    if (n && towns.length) return `No 3.5 GHz 5G in ${towns.length} of ${n} towns`;
    if ((p.video_rows_4g_share || 0) > 0.35) return `${pct(p.video_rows_4g_share)} of video still on 4G`;
    if (m.gap_points) return `${m.gap_points} busy roads on coverage-only 5G`;
    return "Video still on 4G";
  }
  if (lens === "energy") {
    if ((c.share_2g || 0) > 0) return "2G still on, little traffic";
    return "Idle radios";
  }
  if (lens === "coverage_check") return "RAN 5G does not match the coverage map.";
  if (m.gap_points) return `${m.gap_points} busy roads on coverage-only 5G`;
  return "Roads and rail on coverage-only 5G";
}

function actionsFor(r) {
  const p = r.profile || {};
  const c = r.coverage || {};
  const m = r.mobility || {};
  const towns = thinTowns(r);
  const roads = thinRoads(r).slice(0, 3);
  const place = r.place;
  const acts = [];
  if (towns.length) {
    acts.push({
      title: "Add 3.5 GHz 5G",
      why: towns.slice(0, 3).join(", "),
      q: `Suggest a concrete RAN action to add 3.5 GHz 5G in ${place}. Towns without it: ${towns.join(", ")}.`,
    });
  }
  if ((p.video_rows_4g_share || 0) > 0.35 && (c.share_5g_midband || 0) >= 0.4) {
    acts.push({
      title: "Move video onto 5G",
      why: `${pct(p.video_rows_4g_share)} of video is still on 4G`,
      q: `Suggest a concrete action to move video onto 5G in ${place}. ${pct(p.video_rows_4g_share)} of video sessions are on 4G even though 3.5 GHz 5G is already on the map.`,
    });
  } else if ((p.video_rows_4g_share || 0) > 0.35 && !towns.length) {
    acts.push({
      title: "Move video onto 5G",
      why: `${pct(p.video_rows_4g_share)} of video is still on 4G`,
      q: `Suggest a concrete action to move video onto 5G in ${place}.`,
    });
  }
  if (roads.length) {
    acts.push({
      title: "Fix busy roads",
      why: roads.join(", "),
      q: `Suggest a concrete action for busy roads and stations in ${place}: ${roads.join(", ")}. They only have coverage 5G or none.`,
    });
  }
  if (lens === "energy") {
    acts.push({
      title: "Turn down empty 2G",
      why: "2G is still on",
      q: `Suggest a concrete action to turn down empty 2G in ${place}.`,
    });
  }
  if (lens === "coverage_check") {
    acts.push({
      title: "Fix the 5G label",
      why: "File and map disagree",
      q: `The file claims 5G in ${place} but the map does not show 3.5 GHz 5G. Suggest a concrete action.`,
    });
  }
  if (!acts.length) {
    acts.push({
      title: "What should we do?",
      why: plainWhy(r),
      q: `What should we do in ${place}? Give one concrete action.`,
    });
  }
  return acts.slice(0, 3);
}

function openAction(q) {
  showDock("chat");
  askChat(q);
}

function fillActionChips(r) {
  const box = $("chatSuggest");
  if (!box) return;
  const acts = r ? actionsFor(r) : [
    { title: "Why submit?", q: "Why do I need to submit a RAN file first?" },
    { title: "Video", q: "Which region is most inefficient for video, and what should we do?" },
    { title: "This region", q: "Explain the selected region and give one RAN action." },
  ];
  box.innerHTML = acts.map((a) => `<button type="button" data-q="${esc(a.q)}">${esc(a.title)}</button>`).join("");
}

function layerTag(layer) {
  if (layer === "none") return tag("bad", "No 5G");
  if (layer === "low") return tag("warn", "700 MHz only");
  return tag("ok", "3.5 GHz");
}

/* ---------- ranked ---------- */
function drawRanked() {
  if (!data) {
    $("ranked").innerHTML = `<li><span class="n">–</span><span class="name">Submit a RAN file</span></li>`;
    $("lensTitle").textContent = "Submit a RAN file";
    $("lensQuestion").textContent = "Nothing to rank yet.";
    $("lensHow").innerHTML = scoreKeyHtml(lens, true);
    return;
  }
  const max = maxScore();
  const all = rows();
  const shown = rankAll ? all : all.filter((r, i) => i < 8 || r.province === selected);
  $("ranked").innerHTML = shown.map((r) => {
    const act = actionsFor(r)[0];
    return `<li class="${r.province === selected ? "on" : ""}">
      <button type="button" class="place" data-name="${r.province}">
        <span class="n">${r[lens].rank}</span>
        <span class="body"><b>${r.place}</b><span class="why">${esc(plainWhy(r))}</span>
          <span class="bar"><i style="width:${(100 * (r[lens].score || 0)) / max}%"></i></span></span>
        <span class="score">${fmt(r[lens].score, 0)}</span>
      </button>
      <button type="button" class="do" data-name="${r.province}" data-q="${esc(act.q)}">${esc(act.title)}</button>
    </li>`;
  }).join("")
    + (all.length > 8 ? `<li class="more" data-more="1">${rankAll ? "Show top 8" : `All ${all.length}`}</li>` : "");
  if (selected) fillActionChips(byName(selected));
  const L = data.lenses[lens];
  $("lensTitle").textContent = (LENS_UI[lens] || {}).title || L.title;
  $("lensQuestion").textContent = (LENS_UI[lens] || {}).line || L.question;
  $("lensHow").innerHTML = scoreKeyHtml(lens);
}

/* ---------- detail ---------- */
const tag = (cls, txt) => `<span class="tag ${cls}">${txt}</span>`;

function drawDetail() {
  const el = $("detail");
  if (!data) return (el.innerHTML = `<p class="hint">Submit a parquet to see a place.</p>`);
  const r = byName(selected);
  if (!r) return (el.innerHTML = `<p class="hint">Pick a province on the map or in the list.</p>`);
  const p = r.profile;
  const L = r[lens];
  const mob = r.mobility || {};
  const stats = {
    service: [
      [p.video_pooled ? "—" : pct(p.video_rows_4g_share), "video on 4G"],
      [pct(r.coverage.share_5g_midband), "3.5 GHz 5G"],
      [mob.points ? `${mob.gap_points}/${mob.points}` : "–", "roads with a gap"],
    ],
    energy: [
      [pct(r.coverage.share_2g), "2G still on"],
      [pct(p.gb_5g_share), "bytes on 5G"],
      [fmt(L.planned_2g_sites), "planned 2G sites"],
    ],
    coverage_check: [
      [pct(p.rows_5g_share), "5G in the file"],
      [pct(r.coverage.share_5g_midband), "3.5 GHz 5G"],
      [pct(r.coverage.share_5g_lowband_only), "coverage 5G only"],
    ],
    mobility_lens: [
      [mob.points ? `${mob.gap_points}/${mob.points}` : "–", "roads with a gap"],
      [fmt(mob.vehicles_per_hour), "vehicles / hour"],
      [fmt(mob.planned_5g_sites), "planned 5G sites"],
    ],
  }[lens];

  const cohorts = `<table><thead><tr><th>network</th><th>service</th><th class="num">bytes</th></tr></thead><tbody>${r.cohorts.slice(0, 8).map((c) => `<tr><td>${c.radio_access_type}</td><td>${esc(c.application_category)}</td><td class="num">${gb(c.volume_sum)}</td></tr>`).join("")}</tbody></table>`;

  const cov = `<p class="hint">Elisa coverage map — not the RAN file. 3.5 GHz is capacity 5G. 700 MHz is coverage 5G only.</p>
    <table><thead><tr><th>town</th><th>5G on the map</th><th class="num">rated Mbps</th></tr></thead><tbody>${r.coverage_samples.map((s) => `<tr><td>${esc(s.place)}</td><td>${s.has_5g ? (s.midband ? tag("ok", "3.5 GHz") : tag("warn", "700 MHz only")) : tag("bad", "No 5G")}</td><td class="num">${fmt(s.five_g_speed)}</td></tr>`).join("")}</tbody></table>`;

  const tps = (r.issues || []).slice().sort((a, b) => (b.score || 0) - (a.score || 0));
  const roads = tps.length
    ? tps.map((x) => {
        const lay = issueLayer(x);
        return `<button type="button" class="issue" data-issue="${esc(x.name)}" data-q="${esc(`What should we do at ${x.name} in ${r.place}? It has ${lay === "none" ? "no 5G" : "coverage 5G only"}. Give one action.`)}"><div class="ih"><span><b>${esc(x.name)}</b>${x.kind === "road" ? ` · ${fmt(x.vehicles_per_hour)} /h` : ""}</span>${layerTag(lay)}</div></button>`;
      }).join("")
    : `<p class="hint">No weak roads or stations here.</p>`;

  const planned = (transport?.improvements || []).filter((x) => x.province === r.province);
  const byType = {};
  for (const x of planned) for (const t of x.types) byType[t] = (byType[t] || 0) + 1;
  const plannedHtml = Object.keys(byType).length
    ? `<div class="grid2">${Object.entries(byType).map(([t, n]) => `<div class="stat"><b>${n}</b><span>${esc(t)}</span></div>`).join("")}</div>`
    : `<p class="hint">Nothing planned here.</p>`;

  const apps = `<table><thead><tr><th>service</th><th class="num">bytes</th><th class="num">on 5G</th></tr></thead><tbody>${p.app_mix.slice(0, 8).map((a) => `<tr><td>${esc(a.app)}</td><td class="num">${gb(a.gb)}</td><td class="num">${pct(a.share_5g_rows)}</td></tr>`).join("")}</tbody></table>`;

  const tabs = [["issues", "Roads & rail"], ["cohorts", "In the file"], ["coverage", "Coverage"], ["planned", "Elisa's plan"], ["apps", "By service"]];
  const rank = L.rank != null ? `#${L.rank}` : "";
  const acts = actionsFor(r);
  el.innerHTML = `
    <h3>${r.place} <span class="muted">${rank}</span></h3>
    <p class="act-why">${esc(plainWhy(r))}</p>
    <div class="acts">${acts.map((a) => `<button type="button" data-q="${esc(a.q)}"><b>${esc(a.title)}</b><span>${esc(a.why)}</span></button>`).join("")}</div>
    <div class="grid2">${stats.map(([v, l]) => `<div class="stat"><b>${v}</b><span>${l}</span></div>`).join("")}</div>
    <div class="tabs">${tabs.map(([id, l]) => `<button data-tab="${id}" class="${detailTab === id ? "on" : ""}">${l}</button>`).join("")}</div>
    <div id="tabBody">${{ issues: roads, cohorts, coverage: cov, planned: plannedHtml, apps }[detailTab]}</div>`;
}

/* ---------- classes ---------- */
function drawClasses() {}

/* ---------- tooltips / live query ---------- */
function showTip(html, ev) {
  const tip = $("tip");
  tip.innerHTML = html;
  tip.hidden = false;
  tip.style.left = Math.min(ev.clientX + 14, window.innerWidth - 320) + "px";
  tip.style.top = ev.clientY + 14 + "px";
}
const hideTip = () => ($("tip").hidden = true);
function layersHtml(ls) {
  return ["5G", "4G", "2G", "LTEM", "NBIOT"].map((t) => {
    const l = ls.find((x) => x.networkType === t && (x.frequencies || []).length);
    return l ? `<div class="lay"><span>${t}</span><span>${(l.frequencies || []).join("/")} MHz${l.speed ? " · " + l.speed + " Mbps" : ""}</span></div>`
             : `<div class="lay"><span>${t}</span><span>not on map</span></div>`;
  }).join("");
}

async function liveQuery(ev) {
  const lat = ev.latlng.lat, lng = ev.latlng.lng;
  livePoint = { lat, lng };
  drawMap();
  const orig = ev.originalEvent || ev;
  showTip(`<b>Asking Elisa…</b><br>${lat.toFixed(3)}, ${lng.toFixed(3)}`, orig);
  try {
    const res = await (await fetch(`/api/coverage/point?lat=${lat.toFixed(5)}&lng=${lng.toFixed(5)}`)).json();
    if (res.error) throw new Error(res.error);
    showTip(`<b>Elisa, live</b> · ${lat.toFixed(3)}, ${lng.toFixed(3)}${layersHtml(res.layers)}<div class="muted">${res.rated ? "" : "Nothing rated here."}</div>`, orig);
  } catch (e) {
    showTip(`<b>Live query failed</b><br>${esc(e.message)}`, orig);
  }
  setTimeout(hideTip, 6000);
}

/* ---------- wiring ---------- */
function setupClock() {
  const bar = $("clockbar");
  const bins = data?.heatmap?.bins || [];
  if (!bins.length) {
    bar.hidden = true;
    return;
  }
  bar.hidden = false;
  $("timeTicks").innerHTML = bins.map((b) => `<span>${esc(b.label.replace(" UTC", ""))}</span>`).join("");
  const scrub = $("timeScrub");
  scrub.max = String(Math.max(0, bins.length - 1));
  scrub.value = String(clock);
  $("timeNow").textContent = clockLabel();
}

function setClock(v, fromScrub) {
  const n = (data?.heatmap?.bins || []).length;
  if (!n) return;
  clock = Math.max(0, Math.min(n - 1, v));
  if (!fromScrub) $("timeScrub").value = String(clock);
  $("timeNow").textContent = clockLabel();
  drawHeat();
}

function togglePlay() {
  playing = !playing;
  $("playBtn").textContent = playing ? "Pause" : "Play";
  if (playTimer) { clearInterval(playTimer); playTimer = null; }
  if (!playing) return;
  playTimer = setInterval(() => {
    const n = (data?.heatmap?.bins || []).length;
    if (!n) return;
    let next = clock + 0.015;
    if (next >= n - 1) next = 0;
    setClock(next);
  }, 200);
}

function redraw() { drawMap(); drawRanked(); drawDetail(); setupClock(); }

const chatHistory = [];

function addChat(role, text, extra) {
  const log = $("chatLog");
  const div = document.createElement("div");
  div.className = `msg ${role}${extra ? " " + extra : ""}`;
  div.textContent = text;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

async function askChat(text, opts = {}) {
  const q = (text || "").trim();
  if (!q) return "";
  setChatMin(false);
  addChat("user", q);
  chatHistory.push({ role: "user", content: q });
  $("chatInput").value = "";
  addChat("bot", "SisuNymous is thinking…");
  const pending = $("chatLog").lastElementChild;
  try {
    const res = await fetch("/api/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: chatHistory, lens, province: selected, clock: clockLabel(), voice: !!opts.voice }),
    });
    const j = await res.json();
    pending.remove();
    if (j.reply) {
      addChat("bot", j.reply);
      chatHistory.push({ role: "assistant", content: j.reply });
      return j.reply;
    }
    addChat("bot", j.error || "No reply", "err");
    return "";
  } catch (e) {
    pending.remove();
    addChat("bot", e.message, "err");
    return "";
  }
}

/* ---------- SisuNymous voice call ---------- */
let inCall = false;
let rec = null;
let voicesReady = [];

function pickVoice() {
  const all = speechSynthesis.getVoices() || voicesReady;
  voicesReady = all;
  const want = [/samantha/i, /karen/i, /moira/i, /fiona/i, /google us/i, /natural/i, /neural/i, /aria/i, /jenny/i, /susan/i];
  for (const re of want) {
    const hit = all.find((v) => re.test(v.name) && /en/i.test(v.lang));
    if (hit) return hit;
  }
  return all.find((v) => /en-US|en-GB|en-AU|en-IE/i.test(v.lang)) || all[0] || null;
}
if (window.speechSynthesis) speechSynthesis.onvoiceschanged = () => { voicesReady = speechSynthesis.getVoices(); };

function speakHuman(text) {
  return new Promise((resolve) => {
    if (!window.speechSynthesis) return resolve();
    speechSynthesis.cancel();
    const parts = String(text).replace(/[*#`_]/g, "").split(/(?<=[.!?])\s+/).filter(Boolean);
    const voice = pickVoice();
    let i = 0;
    const next = () => {
      if (i >= parts.length) return resolve();
      const u = new SpeechSynthesisUtterance(parts[i++]);
      u.voice = voice;
      u.lang = (voice && voice.lang) || "en-GB";
      u.rate = 0.94;
      u.pitch = 1.02;
      u.volume = 1;
      u.onend = next;
      u.onerror = next;
      speechSynthesis.speak(u);
    };
    next();
  });
}

function setChatMin(min) {
  $("dock").classList.toggle("chat-min", min);
  const btn = $("chatMin");
  btn.textContent = min ? "Open chat" : "Minimize";
  btn.setAttribute("aria-expanded", min ? "false" : "true");
  if (!min) $("chatInput")?.focus();
}

function showDock() {
  setChatMin(false);
}

function setChatOpen() {
  showDock();
}

function setCallUI(mode, line) {
  const btn = $("callBtn");
  btn.classList.toggle("live", mode === "talk" || mode === "live");
  btn.classList.toggle("listen", mode === "listen");
  btn.textContent = mode === "off" ? "Call" : "End";
  $("callStatus").textContent = line || (mode === "off" ? "RAN analyst" : "");
}

function makeRecognizer() {
  const Ctor = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!Ctor) return null;
  const r = new Ctor();
  r.lang = "en-GB";
  r.interimResults = true;
  r.continuous = false;
  r.maxAlternatives = 1;
  return r;
}

async function onHeard(said) {
  if (!inCall || !said) return;
  setCallUI("talk", said);
  const reply = await askChat(said, { voice: true });
  if (!inCall) return;
  if (reply) {
    setCallUI("talk", "Speaking…");
    await speakHuman(reply);
  }
  if (inCall) listenOnce();
}

function listenOnce() {
  if (!inCall) return;
  if (!rec) rec = makeRecognizer();
  if (!rec) {
    setCallUI("live", "Type instead — this browser has no mic.");
    return;
  }
  rec.onresult = (e) => {
    const last = e.results[e.results.length - 1];
    const said = last[0].transcript.trim();
    if (last.isFinal) onHeard(said);
    else setCallUI("listen", said);
  };
  rec.onerror = (e) => {
    if (!inCall) return;
    if (e.error === "no-speech") listenOnce();
    else setCallUI("listen", "Mic: " + e.error + " — try again");
  };
  rec.onend = () => { /* wait for result or next listen from onHeard */ };
  setCallUI("listen", "Listening…");
  try { rec.start(); } catch { /* already started */ }
}

function hangup() {
  inCall = false;
  try { rec && rec.stop(); } catch {}
  if (window.speechSynthesis) speechSynthesis.cancel();
  setCallUI("off", "RAN analyst");
}

function toggleCall() {
  if (inCall) return hangup();
  inCall = true;
  setChatOpen(true);
  setCallUI("live", "Connecting…");
  speakHuman("Hi, this is SisuNymous. What should we look at?").then(() => {
    if (inCall) listenOnce();
  });
}

function wire() {
  $("submitForm").addEventListener("submit", submit);
  $("callBtn").addEventListener("click", toggleCall);
  $("chatMin").addEventListener("click", () => setChatMin(!$("dock").classList.contains("chat-min")));
  $("chatForm").addEventListener("submit", (e) => { e.preventDefault(); askChat($("chatInput").value); });
  $("chatSuggest").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-q]");
    if (b) askChat(b.dataset.q);
  });
  $("auditBtn").addEventListener("click", (e) => {
    e.preventDefault();
    location.href = "/anonymise.html";
  });
  document.querySelector('[data-stage="deidentify"]')?.addEventListener("click", (e) => {
    if (e.target.closest("a, button, input, select, label")) return;
    location.href = "/anonymise.html";
  });
  $("sourcesBtn").addEventListener("click", () => {
    if (!data) return alert("Source list appears after a release is built. Public layers are already on the map.");
    $("sources").hidden = !$("sources").hidden;
    if (!$("sources").hidden) drawSources();
  });
  $("layers").addEventListener("change", (e) => { const l = e.target.dataset.layer; if (l) { layers[l] = e.target.checked; drawMap(); } });
  $("playBtn").addEventListener("click", togglePlay);
  $("timeScrub").addEventListener("input", (e) => { playing && togglePlay(); setClock(parseFloat(e.target.value), true); });
  $("lenses").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-lens]");
    if (!b) return;
    lens = b.dataset.lens;
    for (const x of $("lenses").querySelectorAll("button[data-lens]")) {
      x.classList.toggle("on", x === b);
      x.setAttribute("aria-pressed", x === b ? "true" : "false");
    }
    if (lens === "mobility_lens") {
      detailTab = "issues";
      layers.roads = layers.trains = layers.heat = true;
      for (const inp of $("layers").querySelectorAll("input[data-layer]")) inp.checked = !!layers[inp.dataset.layer];
    }
    if (byName(selected)?.[lens].rank == null && rows()[0]) selected = rows()[0].province;
    redraw();
  });
  $("ranked").addEventListener("click", (e) => {
    if (e.target.closest("[data-more]")) { rankAll = !rankAll; drawRanked(); return; }
    const act = e.target.closest("[data-q]");
    const btn = e.target.closest("[data-name]");
    if (btn) { focusPlace(btn.dataset.name); }
    if (act) { openAction(act.dataset.q); return; }
  });
  $("detail").addEventListener("click", (e) => {
    const tab = e.target.closest("button[data-tab]");
    if (tab) { detailTab = tab.dataset.tab; drawDetail(); return; }
    const card = e.target.closest(".issue[data-issue]");
    if (card) {
      const hit = (window._issues || []).find((p) => p.name === card.dataset.issue && p.province === selected);
      if (hit?.lat) {
        livePoint = { lat: hit.lat, lng: hit.lng };
        if (osm) osm.flyTo([hit.lat, hit.lng], 11, { duration: 0.4 });
        drawMap();
      }
    }
    const act = e.target.closest("[data-q]");
    if (act) openAction(act.dataset.q);
  });
}

async function loadAll() {
  const [ins, cov, tr] = await Promise.all([
    fetch("/api/insights").then((r) => (r.ok ? r.json() : null)),
    fetch("/api/coverage").then((r) => (r.ok ? r.json() : null)),
    fetch("/api/transport").then((r) => (r.ok ? r.json() : null)),
  ]);
  window._coverage = cov;
  transport = tr;
  if (!ins) {
    data = null;
    selected = null;
    if (playing) togglePlay();
    drawKpis();
    redraw();
    drawClasses();
    $("audit").hidden = true;
    return false;
  }
  data = ins;
  data._coverage = cov;
  if (!byName(selected)) selected = rows()[0]?.province || null;
  drawKpis();
  redraw();
  drawClasses();
  if (!$("audit").hidden) drawAudit();
  if (!$("sources").hidden) drawSources();
  if ((data.heatmap?.bins || []).length && !playing) togglePlay();
  if (osm) setTimeout(() => { osm.invalidateSize(); fitFinland(); }, 80);
  setTimeout(() => osm && osm.invalidateSize(), 300);
  return true;
}

async function boot() {
  wire();
  ensureMap();
  addChat("bot", "SisuNymous — RAN analyst. Pick a region or ask what to do.");
  fillActionChips();
  await loadAll();
  loadTrains();
  loadRoads();
  setInterval(loadTrains, 5000);
  setInterval(loadRoads, 25000);
  pollPipeline();
}
boot().catch((e) => { $("window").textContent = "Failed to load: " + e.message; });
