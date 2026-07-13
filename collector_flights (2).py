#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
EU air-traffic density collector
====================================
Takes ONE instantaneous sample per run of aircraft over Europe and stores
ONLY AGGREGATE COUNTS per 0.5 degree cell.

LEGAL (this is the whole design):
  adsb.fi open data is personal / non-commercial and may not be
  redistributed, licensed, sold, rented or leased.
  -> We therefore NEVER store or publish hex codes, callsigns, registrations
     or individual aircraft positions. We publish a COUNT per cell, which is
     our own derived measurement, not their dataset.
  -> Attribution to adsb.fi is mandatory and is embedded in every output file.
  -> Every daily archive file records its own source + licence, so a future
     commercially-licensed source (airplanes.live) can be cleanly separated.

METRIC HONESTY:
  One run = one instantaneous snapshot. Summing 24 hourly snapshots does NOT
  give "number of flights that passed". It gives a DENSITY PROXY (aircraft
  present at the sampled instants). The schema says so explicitly.

Outputs (repo root):
  flight-density.json          rolling window, read by the website
  archive/YYYY-MM-DD.json      append-only hourly samples  <- THE ASSET
"""

import json, math, os, sys, time, datetime, urllib.request, urllib.error

# ── CONFIG ────────────────────────────────────────────────────────────
SOURCE      = "adsb.fi"          # switch to "airplanes.live" when licensed
LICENSE     = "non-commercial"   # -> "commercial" with airplanes.live
API         = "https://opendata.adsb.fi/api/v3/lat/{lat:.3f}/lon/{lon:.3f}/dist/{d}"
UA          = "air-density-research/1.0 (aggregate statistics)"

DIST_NM     = 250       # adsb.fi hard maximum
REQ_SLEEP   = 1.25      # public limit is 1 req/s. Stay under it.
TIMEOUT     = 25
RETRIES     = 2

CELL        = 0.5       # degrees
MIN_ALT_M   = 7000      # contrail-capable altitude band
KEEP_HOURS  = 24        # rolling window in flight-density.json

# European window (traffic core). Widen later if needed.
LAT_MIN, LAT_MAX = 34.0, 66.0
LON_MIN, LON_MAX = -12.0, 34.0

ARCHIVE_DIR = "archive"
LIVE_FILE   = "flight-density.json"
DRY_RUN     = os.environ.get("DRY_RUN") == "1"   # test without writing archive


# ── GEOMETRY ──────────────────────────────────────────────────────────
def circle_centres():
    """Cover the window with 250 nm (463 km) radius circles.

    Centres are spread EVENLY so both edges are included. A 463 km radius
    tolerates a grid pitch up to 463*sqrt(2) = 654 km; we aim for <= 600 km,
    leaving a safety margin at the diagonal corners.
    """
    MAX_KM = 600.0
    out = []
    n_lat = max(1, int(math.ceil((LAT_MAX - LAT_MIN) * 111.0 / MAX_KM)))
    for i in range(n_lat + 1):
        lat = LAT_MIN + (LAT_MAX - LAT_MIN) * i / n_lat
        c = max(0.20, math.cos(math.radians(lat)))
        span_km = (LON_MAX - LON_MIN) * 111.0 * c
        n_lon = max(1, int(math.ceil(span_km / MAX_KM)))
        for j in range(n_lon + 1):
            lon = LON_MIN + (LON_MAX - LON_MIN) * j / n_lon
            out.append((round(lat, 3), round(lon, 3)))
    return out


def cell_key(lat, lon):
    la = math.floor(lat / CELL) * CELL
    lo = math.floor(lon / CELL) * CELL
    return "%.1f,%.1f" % (la, lo)


# ── FETCH ─────────────────────────────────────────────────────────────
def fetch(lat, lon):
    url = API.format(lat=lat, lon=lon, d=DIST_NM)
    req = urllib.request.Request(
        url, headers={"User-Agent": UA, "Accept": "application/json"})
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            # v3 returns {"ac":[...]}; v2 returned {"aircraft":[...]}
            return data.get("ac") or data.get("aircraft") or []
        except Exception as e:
            if attempt == RETRIES - 1:
                print("  ! FAIL %.1f,%.1f -> %s" % (lat, lon, e), file=sys.stderr)
                return None
            time.sleep(3)
    return None


def altitude_m(a):
    """adsb.fi altitudes are feet, or the string 'ground'."""
    for k in ("alt_geom", "alt_baro"):
        v = a.get(k)
        if isinstance(v, bool):
            continue
        if isinstance(v, (int, float)):
            return v * 0.3048
    return None


# ── SAMPLE ────────────────────────────────────────────────────────────
def take_sample():
    centres = circle_centres()
    print("circles: %d  (~%ds)" % (len(centres), int(len(centres) * REQ_SLEEP)))

    seen = {}            # hex -> (lat, lon, alt_m)   dedup across overlaps
    ok = 0
    for i, (lat, lon) in enumerate(centres):
        ac = fetch(lat, lon)
        if ac is None:
            time.sleep(REQ_SLEEP)
            continue
        ok += 1
        for a in ac:
            hx = a.get("hex")
            la, lo = a.get("lat"), a.get("lon")
            if not hx or not isinstance(la, (int, float)) or not isinstance(lo, (int, float)):
                continue
            if not (LAT_MIN <= la <= LAT_MAX and LON_MIN <= lo <= LON_MAX):
                continue
            seen[hx] = (la, lo, altitude_m(a))
        time.sleep(REQ_SLEEP)

    if ok == 0:
        raise SystemExit("ABORT: every request failed. Nothing written.")

    # ---- AGGREGATE. Raw aircraft data dies here, in memory. ----
    cells = {}
    hi_tot = all_tot = 0
    for la, lo, alt in seen.values():
        k = cell_key(la, lo)
        c = cells.setdefault(k, [0, 0])          # [hi (>=7km), all]
        c[1] += 1
        all_tot += 1
        if alt is not None and alt >= MIN_ALT_M:
            c[0] += 1
            hi_tot += 1

    now = datetime.datetime.now(datetime.timezone.utc)
    return {
        "t": now.strftime("%Y-%m-%dT%H:00:00Z"),
        "taken_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "cells": cells,
        "total_hi": hi_tot,
        "total_all": all_tot,
        "circles_ok": ok,
        "circles_total": len(centres),
    }


# ── ARCHIVE + ROLLING WINDOW ──────────────────────────────────────────
def load(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save(path, obj):
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)


def append_archive(sample):
    day = sample["t"][:10]
    path = os.path.join(ARCHIVE_DIR, day + ".json")
    doc = load(path, None)
    if doc is None:
        doc = {
            "date": day,
            "source": SOURCE,
            "license": LICENSE,
            "attribution": "Flight data by adsb.fi (https://adsb.fi)",
            "cell_deg": CELL,
            "min_alt_m": MIN_ALT_M,
            "bbox": [LAT_MIN, LON_MIN, LAT_MAX, LON_MAX],
            "metric": ("count of DISTINCT aircraft present per cell at the sampled "
                       "instant. NOT a count of flights that passed."),
            "note": ("Aggregate counts only. No aircraft identifiers, callsigns or "
                     "positions are stored or published."),
            "samples": [],
        }
    doc["samples"] = [s for s in doc["samples"] if s.get("t") != sample["t"]]
    doc["samples"].append(sample)
    doc["samples"].sort(key=lambda s: s["t"])
    save(path, doc)
    return path


def rolling(sample):
    """Sum the last KEEP_HOURS samples from today's + yesterday's archive."""
    day = datetime.datetime.strptime(sample["t"][:10], "%Y-%m-%d").date()
    got = []
    for off in (1, 0):
        d = (day - datetime.timedelta(days=off)).isoformat()
        doc = load(os.path.join(ARCHIVE_DIR, d + ".json"), None)
        if doc:
            got.extend(doc.get("samples", []))
    got.sort(key=lambda s: s["t"])
    got = got[-KEEP_HOURS:]
    if not got:
        got = [sample]

    agg = {}
    for s in got:
        for k, v in s.get("cells", {}).items():
            a = agg.setdefault(k, [0, 0])
            a[0] += v[0]
            a[1] += v[1]

    out = []
    for k, v in agg.items():
        la, lo = k.split(",")
        out.append([float(la), float(lo), v[0], v[1]])
    out.sort(key=lambda r: (-r[2], -r[3]))

    return {
        "schema_version": 1,
        "generated": sample["taken_at"],
        "source": SOURCE,
        "license": LICENSE,
        "attribution": "Flight data by adsb.fi (https://adsb.fi)",
        "window_hours": KEEP_HOURS,
        "samples_used": len(got),
        "cell_deg": CELL,
        "min_alt_m": MIN_ALT_M,
        "bbox": [LAT_MIN, LON_MIN, LAT_MAX, LON_MAX],
        "metric": ("sum of hourly instantaneous aircraft counts per cell. "
                   "A DENSITY PROXY, not a flight count."),
        "fields": ["lat", "lon", "count_ge_7km", "count_all"],
        "cells": out,
        "latest": {"hi": sample["total_hi"], "all": sample["total_all"],
                   "coverage": "%d/%d" % (sample["circles_ok"], sample["circles_total"])},
    }


def main():
    s = take_sample()
    print("sample %s  hi=%d all=%d  cells=%d  coverage=%d/%d"
          % (s["t"], s["total_hi"], s["total_all"], len(s["cells"]),
             s["circles_ok"], s["circles_total"]))
    if DRY_RUN:
        print("DRY_RUN=1 -> nothing written.")
        return
    print("archive ->", append_archive(s))
    save(LIVE_FILE, rolling(s))
    print("live    ->", LIVE_FILE)


if __name__ == "__main__":
    main()
