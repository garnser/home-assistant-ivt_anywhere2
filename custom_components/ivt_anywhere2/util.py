from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional, Tuple


def wh_to_kwh(wh: float) -> float:
    return float(wh * 10) / 1000.0

def recording_points(payload: dict) -> list[float]:
    rec = payload.get("recording") or []
    pts = []
    for p in rec:
        # Drop padding / incomplete buckets (often y=1, c=0)
        if (p.get("c") or 0) <= 0:
            continue
        pts.append(float(p.get("y") or 0.0))
    return pts

def sum_kwh(payload: Optional[dict]) -> Optional[float]:
    if not payload:
        return None
    return sum(wh_to_kwh(v) for v in recording_points(payload))


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
    target = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    day_str = target.strftime("%Y-%m-%d")
    idx = target.hour
    label = f"{day_str} {idx:02d}:00"
    return day_str, idx, label


def kwh_at_index(payload: Optional[dict], idx: int) -> Optional[float]:
    """
    Return kWh for recording[idx] where 'y' is Wh.
    For missing payload / out of range / missing y -> None.
    """
    if not payload:
        return None

    rec = payload.get("recording") or []
    if idx < 0 or idx >= len(rec):
        return None

    y = rec[idx].get("y")
    if y is None:
        return None

    try:
        return wh_to_kwh(float(y))
    except Exception:
        return None


def month_total_kwh(payload: Optional[dict]) -> Optional[float]:
    return sum_kwh(payload)


def compute_cop(heat_kwh: Optional[float], elec_kwh: Optional[float]) -> Optional[float]:
    if heat_kwh is None or elec_kwh is None or elec_kwh <= 0:
        return None
    return heat_kwh / elec_kwh


def month_str(now: Optional[datetime] = None) -> str:
    now = _ensure_aware(now)
    return now.strftime("%Y-%m")
