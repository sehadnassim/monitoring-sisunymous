#!/usr/bin/env python3
"""Sample Elisa's public coverage map per province and cache the answers.

The RAN file carries a province and a cell id, never GPS. So each province is
sampled at its main town plus a few secondary towns and sparse points, and
the layers Elisa advertises there are kept: network type, frequencies, rated
speed. 3500 MHz on the 5G layer is the capacity band; 700 MHz alone is
coverage-only 5G.

Run from optimize/:
  ../.venv/bin/python build_coverage.py

Endpoint (public, behind elisa.fi/kuuluvuus):
  GET https://content-api.external-resource.elisa.fi/api/coverage-map/rating?lat=&lng=
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "data" / "coverage.json"
API = "https://content-api.external-resource.elisa.fi/api/coverage-map/rating"

# Province → sampled places (name, lat, lng). Main town first.
POINTS: dict[str, list[tuple[str, float, float]]] = {
    "Uusimaa": [("Helsinki", 60.1699, 24.9384), ("Espoo", 60.2055, 24.6559), ("Porvoo", 60.3932, 25.6639), ("Lohja", 60.2486, 24.0653)],
    "Varsinais-Suomi": [("Turku", 60.4518, 22.2666), ("Salo", 60.3841, 23.1280), ("Uusikaupunki", 60.8004, 21.4085), ("Parainen", 60.3010, 22.3020)],
    "Satakunta": [("Pori", 61.4851, 21.7974), ("Rauma", 61.1286, 21.5111), ("Kankaanpää", 61.8036, 22.3936)],
    "Kanta-Häme": [("Hämeenlinna", 60.9959, 24.4643), ("Riihimäki", 60.7386, 24.7729), ("Forssa", 60.8148, 23.6216)],
    "Pirkanmaa": [("Tampere", 61.4978, 23.7610), ("Nokia", 61.4787, 23.5080), ("Mänttä-Vilppula", 62.0311, 24.6259), ("Virrat", 62.2405, 23.7702)],
    "Päijät-Häme": [("Lahti", 60.9827, 25.6612), ("Heinola", 61.2058, 26.0378), ("Sysmä", 61.5031, 25.6839)],
    "Kymenlaakso": [("Kouvola", 60.8679, 26.7042), ("Kotka", 60.4664, 26.9458), ("Hamina", 60.5697, 27.1979)],
    "Etelä-Karjala": [("Lappeenranta", 61.0587, 28.1887), ("Imatra", 61.1719, 28.7726), ("Parikkala", 61.5497, 29.5013)],
    "Etelä-Savo": [("Mikkeli", 61.6886, 27.2723), ("Savonlinna", 61.8699, 28.8790), ("Pieksämäki", 62.3007, 27.1584)],
    "Pohjois-Savo": [("Kuopio", 62.8924, 27.6770), ("Iisalmi", 63.5592, 27.1903), ("Varkaus", 62.3150, 27.8730)],
    "Pohjois-Karjala": [("Joensuu", 62.6010, 29.7636), ("Lieksa", 63.3167, 30.0167), ("Kitee", 62.0997, 30.1380), ("Koli", 63.0967, 29.8072)],
    "Keski-Suomi": [("Jyväskylä", 62.2426, 25.7473), ("Jämsä", 61.8642, 25.1903), ("Saarijärvi", 62.7053, 25.2555), ("Viitasaari", 63.0750, 25.8597)],
    "Etelä-Pohjanmaa": [("Seinäjoki", 62.7903, 22.8403), ("Alajärvi", 63.0009, 23.8158), ("Kauhajoki", 62.4321, 22.1795)],
    "Pohjanmaa": [("Vaasa", 63.0960, 21.6158), ("Pietarsaari", 63.6747, 22.7028), ("Kristiinankaupunki", 62.2745, 21.3766)],
    "Keski-Pohjanmaa": [("Kokkola", 63.8385, 23.1307), ("Kannus", 63.9000, 23.9167), ("Perho", 63.2167, 24.4167)],
    "Pohjois-Pohjanmaa": [("Oulu", 65.0121, 25.4651), ("Raahe", 64.6858, 24.4795), ("Kuusamo", 65.9646, 29.1887), ("Pudasjärvi", 65.3617, 26.9967)],
    "Kainuu": [("Kajaani", 64.2273, 27.7285), ("Kuhmo", 64.1258, 29.5167), ("Suomussalmi", 64.8867, 28.9006)],
    "Lappi": [("Rovaniemi", 66.5039, 25.7294), ("Kemi", 65.7364, 24.5637), ("Kittilä", 67.6580, 24.9106), ("Enontekiö", 68.3856, 23.6325), ("Salla", 66.8330, 28.6670), ("Utsjoki", 69.9075, 27.0273)],
    "Ahvenanmaa": [("Mariehamn", 60.0971, 19.9348), ("Godby", 60.2000, 20.0500)],
}


def fetch(lat: float, lng: float) -> list[dict] | None:
    req = urllib.request.Request(f"{API}?lat={lat}&lng={lng}", headers={"User-Agent": "Pilot-optimize/1.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == 2:
                print(f"    failed {lat},{lng}: {exc}", file=sys.stderr)
                return None
            time.sleep(1.5)
    return None


def summarise(layers: list[dict]) -> dict:
    # At sea / outside Finland the API still lists every layer, with empty frequencies. Rated = has a band.
    by = {str(l.get("networkType")): l for l in layers if l.get("frequencies")}
    five = by.get("5G") or {}
    four = by.get("4G") or {}
    freqs5 = [int(f) for f in five.get("frequencies") or []]
    return {
        "has_2g": "2G" in by,
        "has_4g": "4G" in by,
        "has_5g": "5G" in by,
        "has_nbiot": "NBIOT" in by,
        "has_ltem": "LTEM" in by,
        "five_g_freqs": freqs5,
        "five_g_midband": 3500 in freqs5 or any(f >= 3000 for f in freqs5),
        "five_g_lowband_only": bool(freqs5) and all(f <= 800 for f in freqs5),
        "five_g_speed": five.get("speed"),
        "four_g_speed": four.get("speed"),
        "four_g_freqs": [int(f) for f in four.get("frequencies") or []],
    }


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    result: dict[str, dict] = {}
    total = sum(len(v) for v in POINTS.values())
    done = 0
    for province, places in POINTS.items():
        samples = []
        for name, lat, lng in places:
            layers = fetch(lat, lng)
            done += 1
            if layers is None:
                continue
            samples.append({"place": name, "lat": lat, "lng": lng, "layers": layers, **summarise(layers)})
            print(f"  [{done:2d}/{total}] {province:18s} {name:18s} 5G={'yes' if samples[-1]['has_5g'] else 'no ':3s} "
                  f"midband={'yes' if samples[-1]['five_g_midband'] else 'no '} 2G={'yes' if samples[-1]['has_2g'] else 'no'}")
            time.sleep(0.25)
        n = max(len(samples), 1)
        result[province] = {
            "samples": samples,
            "points": len(samples),
            "share_5g": round(sum(s["has_5g"] for s in samples) / n, 3),
            "share_5g_midband": round(sum(s["five_g_midband"] for s in samples) / n, 3),
            "share_5g_lowband_only": round(sum(s["five_g_lowband_only"] for s in samples) / n, 3),
            "share_2g": round(sum(s["has_2g"] for s in samples) / n, 3),
            "max_5g_speed": max((s["five_g_speed"] or 0) for s in samples) if samples else None,
            "max_4g_speed": max((s["four_g_speed"] or 0) for s in samples) if samples else None,
        }
    payload = {
        "source": API,
        "queried_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "note": (
            "Public endpoint behind elisa.fi/kuuluvuus. The RAN file has no GPS, so each province is sampled at "
            "its main town plus secondary towns and sparse points. 3500 MHz on the 5G layer is the capacity band; "
            "700 MHz alone is coverage-only 5G."
        ),
        "provinces": result,
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    print(f"wrote {OUT} ({len(result)} provinces, {sum(v['points'] for v in result.values())} points)")


if __name__ == "__main__":
    main()
