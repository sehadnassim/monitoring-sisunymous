"""Fintraffic open data (digitraffic.fi, CC BY 4.0). No key; gzip and a Digitraffic-User header are required.

Used:
  tms_stations()      519 road traffic measurement points (LAM) with coordinates
  tms_latest()        latest sensor values per point: vehicles/hour per direction, mean speed
  rail_stations()     563 railway stations, 215 with passenger traffic
  road_works()        active road works and traffic announcements with a point/line geometry

Available but not used here: road weather stations (/api/weather/v1/stations), weathercams,
live train positions (rata.digitraffic.fi/api/v1/train-locations/latest), live-trains per station,
vessel positions (meri.digitraffic.fi/api/ais/v1/locations). Digitransit journey planning
(api.digitransit.fi) needs a subscription key and is not called.
"""

from __future__ import annotations

import gzip
import json
import urllib.error
import urllib.request

ROAD = "https://tie.digitraffic.fi/api"
RAIL = "https://rata.digitraffic.fi/api/v1"
HEADERS = {"Digitraffic-User": "Pilot-optimize/1.0", "Accept-Encoding": "gzip", "User-Agent": "Pilot-optimize/1.0"}

VOL = ("OHITUKSET_60MIN_KIINTEA_SUUNTA1", "OHITUKSET_60MIN_KIINTEA_SUUNTA2")
SPD = ("KESKINOPEUS_60MIN_KIINTEA_SUUNTA1", "KESKINOPEUS_60MIN_KIINTEA_SUUNTA2")


def _get(url: str, timeout: int = 40):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return json.loads(raw.decode())
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def tms_stations() -> list[dict]:
    d = _get(f"{ROAD}/tms/v1/stations")
    out = []
    for f in (d or {}).get("features", []):
        lng, lat = f["geometry"]["coordinates"][:2]
        p = f["properties"]
        out.append({"id": p["id"], "tms": p.get("tmsNumber"), "name": p.get("name"), "lat": lat, "lng": lng,
                    "status": p.get("collectionStatus")})
    return out


def tms_latest() -> dict[int, dict]:
    d = _get(f"{ROAD}/tms/v1/stations/data")
    out = {}
    for s in (d or {}).get("stations", []):
        vals = {v["name"]: v for v in s.get("sensorValues", [])}
        veh = [vals[n]["value"] for n in VOL if n in vals]
        spd = [vals[n]["value"] for n in SPD if n in vals]
        if not veh:
            continue
        out[s["id"]] = {
            "vehicles_per_hour": round(sum(veh)),
            "mean_speed_kmh": round(sum(spd) / len(spd)) if spd else None,
            "measured": max((vals[n].get("measuredTime") or "" for n in VOL if n in vals), default=None),
        }
    return out


def rail_stations(passenger_only: bool = True) -> list[dict]:
    d = _get(f"{RAIL}/metadata/stations") or []
    return [{"code": s["stationShortCode"], "name": s["stationName"], "lat": s["latitude"], "lng": s["longitude"],
             "passenger": bool(s.get("passengerTraffic"))}
            for s in d if s.get("countryCode") == "FI" and (s.get("passengerTraffic") or not passenger_only)]


_STATIONS = None


def live_roads() -> list[dict]:
    global _STATIONS
    if _STATIONS is None:
        _STATIONS = tms_stations()
    latest = tms_latest()
    out = []
    for s in _STATIONS:
        d = latest.get(s["id"])
        if not d:
            continue
        out.append({"kind": "road", "name": s.get("name"), "lat": s["lat"], "lng": s["lng"],
                    "id": s["id"], **d})
    return out


def train_locations() -> list[dict]:
    d = _get(f"{RAIL}/train-locations/latest") or []
    out = []
    for t in d:
        loc = (t.get("location") or {}).get("coordinates") or []
        if len(loc) < 2:
            continue
        out.append({
            "train": t.get("trainNumber"),
            "speed": t.get("speed"),
            "lng": loc[0],
            "lat": loc[1],
        })
    return out


def road_works() -> list[dict]:
    url = (f"{ROAD}/traffic-message/v1/messages?inactiveHours=0&includeAreaGeometry=false"
           "&situationType=ROAD_WORK&situationType=TRAFFIC_ANNOUNCEMENT")
    d = _get(url, timeout=60)
    out = []
    for f in (d or {}).get("features", []):
        g = f.get("geometry") or {}
        c = g.get("coordinates")
        if not c:
            continue
        # take a representative point for lines/multilines
        pt = c
        while isinstance(pt, list) and pt and isinstance(pt[0], list):
            pt = pt[len(pt) // 2]
        if not (isinstance(pt, list) and len(pt) >= 2):
            continue
        p = f["properties"]
        a = (p.get("announcements") or [{}])[0]
        td = a.get("timeAndDuration") or {}
        out.append({"lng": pt[0], "lat": pt[1], "type": p.get("situationType"), "title": (a.get("title") or "").strip(),
                    "start": td.get("startTime"), "end": td.get("endTime")})
    return out
