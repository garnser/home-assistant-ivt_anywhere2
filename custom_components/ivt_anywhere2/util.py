from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Tuple

def wh_to_kwh(wh: float) -> float:
    return float(wh) / 1000.0

def recording_points(payload: dict) -> list[float]:
    rec = payload.get("recording") or []
    return [float((p.get("y") or 0.0)) for p in rec]

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

def last_complete_hour_kwh(payload: Optional[dict], day: str) -> Tuple[Optional[float], Optional[str]]:
    """
    For interval YYYY-MM-DD payload with sampleRate P1H.
    Returns (kWh, label) for the last bucket that likely represents a completed hour.
    """
    if not payload:
        return None, None

    rec = payload.get("recording") or []
    if not rec:
        return None, None

    # Prefer last non-zero bucket; if all are zero, take the last bucket anyway.
    idx = None
    for i in range(len(rec) - 1, -1, -1):
        y = float((rec[i].get("y") or 0.0))
        if y > 0:
            idx = i
            break
    if idx is None:
        idx = len(rec) - 1

    # Label hour index -> HH:00 (best-effort)
    label = f"{day} {idx:02d}:00"
    return wh_to_kwh(float((rec[idx].get("y") or 0.0))), label

def month_total_kwh(payload: Optional[dict]) -> Optional[float]:
    return sum_kwh(payload)

def compute_cop(heat_kwh: Optional[float], elec_kwh: Optional[float]) -> Optional[float]:
    if heat_kwh is None or elec_kwh is None or elec_kwh <= 0:
        return None
    return heat_kwh / elec_kwh

def today_str() -> str:
    return datetime.now().strftime("%Y-%m-%d")

def month_str() -> str:
    return datetime.now().strftime("%Y-%m")
