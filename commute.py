"""
Commute estimation for local jobs.

drive_minutes(dest_lat, dest_lng) -> float | None
    Driving time in minutes from the home base to a job's coordinates. Uses the
    free public OSRM routing service (no API key). On ANY failure (network, bad
    response, service down) it logs a warning and falls back to a haversine
    straight-line estimate scaled by road-factor and average speed. Returns None
    only when the destination coordinates are missing or invalid.

grade(minutes) -> str | None
    Letter grade for a commute length. None in -> None out.

Home base + tuning constants live in config (HOME_LAT / HOME_LNG,
COMMUTE_ROAD_FACTOR, COMMUTE_AVG_MPH). Stdlib only — no third-party deps.
"""

import json
import math
import logging
import urllib.request

import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# OSRM public routing. Coordinate order is lon,lat (NOT lat,lon). overview=false
# skips the route geometry — we only need the duration.
_OSRM_URL = (
    "http://router.project-osrm.org/route/v1/driving/"
    "{lng1},{lat1};{lng2},{lat2}?overview=false"
)

# Earth mean radius in miles (for the haversine fallback).
_EARTH_RADIUS_MI = 3958.7613


def _to_float(val):
    """Coerce a value to float, or None if it's missing/uncoercible."""
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _haversine_miles(lat1, lng1, lat2, lng2) -> float:
    """Great-circle distance in miles between two lat/lng points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi = math.radians(lat2 - lat1)
    d_lambda = math.radians(lng2 - lng1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    )
    return _EARTH_RADIUS_MI * 2 * math.asin(math.sqrt(a))


def _haversine_minutes(dest_lat, dest_lng) -> float:
    """Straight-line-based drive-time estimate: miles * road_factor / mph * 60."""
    miles = _haversine_miles(config.HOME_LAT, config.HOME_LNG, dest_lat, dest_lng)
    return miles * config.COMMUTE_ROAD_FACTOR / config.COMMUTE_AVG_MPH * 60


def drive_minutes(dest_lat, dest_lng):
    """
    Driving minutes from the home base to (dest_lat, dest_lng).

    Returns None if the destination coordinates are missing or invalid.
    Tries OSRM first; on any failure logs a warning and returns the haversine
    estimate instead (so a routing outage never blocks the pipeline).
    """
    lat = _to_float(dest_lat)
    lng = _to_float(dest_lng)
    if lat is None or lng is None:
        return None

    url = _OSRM_URL.format(
        lng1=config.HOME_LNG, lat1=config.HOME_LAT, lng2=lng, lat2=lat
    )
    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("code") == "Ok" and data.get("routes"):
            return data["routes"][0]["duration"] / 60
        raise ValueError(f"OSRM returned code={data.get('code')!r}")
    except Exception as e:
        logging.warning(f"OSRM commute lookup failed ({e}); using haversine estimate.")
        return _haversine_minutes(lat, lng)


def grade(minutes):
    """
    Letter grade for a commute length in minutes. None -> None.

    Scale: <=10 A+ | <=20 A | <=30 B | <=40 C | <=60 D | else F.
    (The 41-60 band — including 50-60 — is D; anything over 60 is F. Intentional.)
    """
    if minutes is None:
        return None
    if minutes <= 10:
        return "A+"
    if minutes <= 20:
        return "A"
    if minutes <= 30:
        return "B"
    if minutes <= 40:
        return "C"
    if minutes <= 60:
        return "D"
    return "F"
