from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Tuple


def wh_to_kwh(wh: float) -> float:
    """Convert actual Wh. PointT yRecording values use sample normalization instead."""
    return float(wh) / 1000.0


def recording_points(payload: dict) -> Optional[list[float]]:
    """Normalize PointT yRecording sums by sample count and interval hours.

    These energy paths are not Wh values: y/c is the sampled hourly value.
    P1H buckets cover one hour; P1D buckets cover 24 hours. A zero count is
    padding or missing data, not measured zero consumption.
    """
    hours = {"P1H": 1.0, "P1D": 24.0}.get(payload.get("sampleRate"))
    rec = payload.get("recording")
    if hours is None or not isinstance(rec, list):
        return None
    points = []
    for point in rec:
        if not isinstance(point, dict):
            return None
        count = point.get("c")
        if (
            isinstance(count, bool)
            or not isinstance(count, (int, float))
            or not math.isfinite(count)
            or count < 0
        ):
            return None
        if count == 0:
            continue
        value = point.get("y")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
        ):
            return None
        normalized = float(value) / float(count) * hours
        if not math.isfinite(normalized):
            return None
        points.append(normalized)
    return points or None


def sum_kwh(payload: Optional[dict]) -> Optional[float]:
    if not isinstance(payload, dict):
        return None
    points = recording_points(payload)
    if points is None:
        return None
    total = sum(points)
    return total if math.isfinite(total) else None


def _extract_payload_from_bulk(bulk_resp: Any, needle: str) -> Optional[dict]:
    """
    needle example: "/energyMonitoring/compressor?interval="
    """
    try:
        root = bulk_resp[0]
        for entry in root.get("resourcePaths", []):
            rp = entry.get("resourcePath", "")
            gw = entry.get("gatewayResponse") or {}
            if needle in rp and gw.get("status") == 200 and gw.get("payload") is not None:
                return gw["payload"]
    except Exception:
        return None
    return None


def _ensure_aware(now: Optional[datetime] = None) -> datetime:
    """Ensure we always operate on a timezone-aware datetime."""
    if now is None:
        # Local timezone (system) with tzinfo attached
        return datetime.now().astimezone()
    if now.tzinfo is None:
        # Treat as local time if someone accidentally passes naive
        return now.astimezone()
    return now


def last_complete_hour_start(now: Optional[datetime] = None) -> datetime:
    """Return a stable UTC cycle marker for the last completed hour.

    Calculate in UTC so the two hours with the same local label during the
    autumn DST transition still have distinct reset markers.
    """
    current = _ensure_aware(now).astimezone(timezone.utc)
    return current.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)


def last_complete_hour_target(now: Optional[datetime] = None) -> Tuple[str, int, str]:
    """
    Determine the most recently completed hour using a timezone-aware 'now'.

    Returns:
      day_str:  'YYYY-MM-DD' (day interval to request)
      idx:      0..23 index into recording[] for P1H payload
      label:    'YYYY-MM-DD HH:00'

    Example:
      If local time is 19:34, target is 18:00 -> day=today, idx=18
      If local time is 00:15, target is 23:00 of previous day -> day=yesterday, idx=23
    """
    now = _ensure_aware(now)
    target = last_complete_hour_start(now).astimezone(now.tzinfo)
    day_str = target.strftime("%Y-%m-%d")
    idx = target.hour
    label = f"{day_str} {idx:02d}:00"
    return day_str, idx, label


def kwh_at_index(payload: Optional[dict], idx: int) -> Optional[float]:
    if not isinstance(payload, dict) or payload.get("sampleRate") != "P1H":
        return None
    rec = payload.get("recording")
    if not isinstance(rec, list) or idx < 0 or idx >= len(rec):
        return None
    points = recording_points(
        {"sampleRate": payload.get("sampleRate"), "recording": [rec[idx]]}
    )
    return None if points is None else points[0]


def month_total_kwh(payload: Optional[dict], required_days: int = 0) -> Optional[float]:
    if not isinstance(payload, dict) or payload.get("sampleRate") != "P1D":
        return None
    rec = payload.get("recording")
    if not isinstance(rec, list) or len(rec) < required_days:
        return None
    for point in rec[:required_days]:
        values = recording_points(
            {"sampleRate": payload.get("sampleRate"), "recording": [point]}
        )
        if values is None:
            return None
    return sum_kwh(payload)


def compute_cop(heat_kwh: Optional[float], elec_kwh: Optional[float]) -> Optional[float]:
    if heat_kwh is None or elec_kwh is None or elec_kwh <= 0:
        return None
    return heat_kwh / elec_kwh


def accept_monthly_total(
    value: Optional[float], previous: Optional[float]
) -> Optional[float]:
    """Hold small cloud revisions without resetting cumulative energy statistics.

    Reports can revise completed daily buckets slightly downwards. Retain the
    accepted high-water value for corrections up to 0.1 kWh or 0.1%, whichever
    is larger. Missing reports and larger drops remain unavailable; the existing
    calendar-month reset clears the high-water values at the month boundary.
    """
    if value is None or not math.isfinite(value) or value < 0:
        return None
    if previous is None or not math.isfinite(previous) or previous < 0:
        return value
    if value >= previous:
        return value
    if previous - value <= max(0.1, previous * 0.001):
        return previous
    return None


def month_str(now: Optional[datetime] = None) -> str:
    now = _ensure_aware(now)
    return now.strftime("%Y-%m")
