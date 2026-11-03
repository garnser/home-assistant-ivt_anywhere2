"""Configurable, unmetered temperature-lift heat model; reported heat is never an input."""

from __future__ import annotations

from bisect import bisect_right
from collections import Counter
from datetime import datetime, timedelta, timezone
from math import isfinite
from statistics import median

from .model_config import ModelSettings

REFERENCE_COP = 4.8
CONDENSING_OFFSET = 2.0
EVAPORATING_OFFSET = 3.0
ENTITIES = ("supply", "return", "modulation")
TANK_ENTITY = "tank"
HISTORY_ENTITIES = (*ENTITIES, TANK_ENTITY)
TANK_VOLUME_LITRES = 180
TANK_KWH_PER_K = TANK_VOLUME_LITRES * 4.186 / 3600


def number(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def compressor_cop(flow, brine=0.0, factor=1.0, settings=None):
    """Scale a Carnot-shaped curve to manufacturer B0/W35 COP4.8.

    Brine0C, condenser+2K and evaporator-3K are modelling assumptions.
    A performance factor of1 keeps the factory reference; it is not a
    calibrated real-installation efficiency. No outdoor-temperature proxy.
    """
    settings = settings or ModelSettings()
    flow, brine, factor = map(number, (flow, brine, factor))
    if flow is None or brine is None or factor is None:
        return None
    if not 20 <= flow <= 70 or not -10 <= brine <= 25 or not 0.5 <= factor <= 1.2:
        return None
    hot = flow + settings.condensing_offset_k + 273.15
    lift = flow + settings.condensing_offset_k - (brine - settings.evaporating_offset_k)
    if lift <= 10:
        return None
    reference_hot = settings.reference_flow_c + settings.condensing_offset_k + 273.15
    reference_lift = (
        settings.reference_flow_c
        + settings.condensing_offset_k
        - settings.reference_source_c
        + settings.evaporating_offset_k
    )
    cop = (
        settings.reference_cop
        * (hot / lift)
        / (reference_hot / reference_lift)
        * factor
    )
    return cop if 1 <= cop <= 8 else None


def points(states):
    result = []
    for state in states:
        stamp = state.get("last_updated") or state.get("last_changed")
        try:
            t = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        except (AttributeError, TypeError, ValueError):
            continue
        result.append((t, number(state.get("state"))))
    return sorted(result, key=lambda p: p[0])


def thermal_profile(history, start, minutes=60, settings=None):
    """One-minute activity evidence; a temperature gap alone is not power.

    A >=0.5K rise between adjacent five-minute tank medians, together with
    compressor operation, marks probable DHW charging. This is a heuristic,
    not a valve-state measurement. Tank stratification and draws remain unknown.
    """
    settings = settings or ModelSettings()
    streams = {key: points(history.get(key, [])) for key in HISTORY_ENTITIES}
    stamps = {key: [p[0] for p in rows] for key, rows in streams.items()}
    values = []
    for minute in range(-10, minutes):
        t = start.timestamp() + minute * 60 + 30
        row = {}
        for key in HISTORY_ENTITIES:
            i = bisect_right(stamps[key], t) - 1
            row[key] = streams[key][i][1] if i >= 0 else None
        tank = row[TANK_ENTITY]
        row["tank"] = tank if tank is not None and 5 <= tank <= 85 else None
        values.append(row)
    result = []
    for minute in range(minutes):
        i = minute + 10
        r = values[i]
        flow, ret, mod = (r[k] for k in ENTITIES)
        valid = (
            all(v is not None for v in (flow, ret, mod))
            and 20 <= flow <= 70
            and 5 <= ret <= 75
            and 0 <= mod <= 100
            and -1 <= flow - ret <= 25
        )
        recent = [v["tank"] for v in values[i - 4 : i + 1] if v["tank"] is not None]
        prior = [v["tank"] for v in values[i - 9 : i - 4] if v["tank"] is not None]
        rise = (
            median(recent) - median(prior)
            if len(recent) >= 4 and len(prior) >= 4
            else None
        )
        delta = flow - ret if valid else None
        if not valid:
            activity = "unknown"
        elif mod > 0.5 and rise is not None and rise >= 0.5:
            activity = "probable_dhw"
        elif mod > 0.5 and delta > 0.2:
            activity = "heating"
        elif mod > 0.5:
            activity = "unconfirmed"
        elif delta > 0.2:
            activity = "residual_or_circulation"
        else:
            activity = "idle"
        result.append(
            {
                "valid": valid,
                "flow": flow,
                "return": ret,
                "modulation": mod,
                "delta": delta,
                "tank": r["tank"],
                "tank_rise_5min_k": rise,
                "activity": activity,
            }
        )
    start_tank = [v["tank"] for v in values[5:10] if v["tank"] is not None]
    end_tank = [v["tank"] for v in values[-5:] if v["tank"] is not None]
    tank_coverage = (
        sum(v["tank"] is not None for v in result) / minutes if minutes else 0
    )
    net = (
        (median(end_tank) - median(start_tank))
        * (settings.tank_volume_litres * 4.186 / 3600)
        if (len(start_tank) >= 4 and len(end_tank) >= 4 and tank_coverage >= 0.9)
        else None
    )
    return result, {
        "tank_temperature_coverage": tank_coverage,
        "tank_net_stored_heat_change_kwh": net,
    }


def live_thermal_evidence(history, now, settings=None):
    end = now.replace(second=0, microsecond=0)
    rows, _ = thermal_profile(history, end - timedelta(minutes=5), 5, settings)
    last = rows[-1]
    labels = {
        "probable_dhw": "Hot water (likely)",
        "heating": "Heating",
        "residual_or_circulation": "Residual / circulating",
        "idle": "Idle",
        "unconfirmed": "Unconfirmed",
        "unknown": "Unknown",
    }
    return {
        "thermal_activity": labels[last["activity"]],
        "supply_return_delta_c": last["delta"],
        "tank_rise_5min_k": last["tank_rise_5min_k"],
    }


def hour_model(history, start, brine, factor, settings=None):
    """Weight the COP curve only while temperature/compressor evidence agrees."""
    rows, tank = thermal_profile(history, start, settings=settings)
    valid = active = dhw = residual = unconfirmed = 0
    weighted_cop = weight = weighted_flow = weighted_delta = 0.0
    for r in rows:
        if not r["valid"]:
            continue
        valid += 1
        dhw += r["activity"] == "probable_dhw"
        residual += r["activity"] == "residual_or_circulation"
        unconfirmed += r["activity"] == "unconfirmed"
        if r["activity"] not in ("heating", "probable_dhw"):
            continue
        # If the heating-loop probe is cooler than a rising tank, it must not
        # imply low-temperature space-heating COP for that probable DHW period.
        # This is a minimum hot-side temperature proxy, not a coil measurement.
        hot = (
            max(r["flow"], r["tank"]) if r["activity"] == "probable_dhw" else r["flow"]
        )
        cop = compressor_cop(hot, brine, factor, settings)
        if cop is None:
            continue
        mod = r["modulation"]
        active += 1
        weight += mod
        weighted_cop += mod * cop
        weighted_flow += mod * hot
        weighted_delta += mod * r["delta"]
    return {
        "temperature_coverage": valid / 60,
        "active_minutes": active,
        "model_cop": weighted_cop / weight if weight else None,
        "active_flow_c": weighted_flow / weight if weight else None,
        "active_supply_return_delta_c": weighted_delta / weight if weight else None,
        "probable_dhw_minutes": dhw,
        "residual_or_circulation_minutes": residual,
        "unconfirmed_compressor_minutes": unconfirmed,
        **tank,
    }


def energy_point(payload, day, hour):
    if (
        not isinstance(payload, dict)
        or payload.get("interval") != day
        or payload.get("sampleRate") != "P1H"
    ):
        return None
    rows = payload.get("recording", [])
    if len(rows) <= hour:
        return None
    y, c = number(rows[hour].get("y")), number(rows[hour].get("c"))
    if y is None or c is None or y < 0 or not 54 <= c <= 60:
        return None
    return {"kwh": y / c, "coverage": c / 60}


def estimate_window(
    history, recordings, end, zone, brine=0.0, factor=1.0, settings=None
):
    """24 elapsed UTC hours; refuse ambiguous gateway hours at DST fall-back."""
    settings = settings or ModelSettings()
    end = end.astimezone(timezone.utc)
    start = end - timedelta(hours=24)
    hours = [start + timedelta(hours=i) for i in range(24)]
    labels = [h.astimezone(zone).strftime("%Y-%m-%d %H") for h in hours]
    ambiguous = {label for label, count in Counter(labels).items() if count > 1}
    result = {
        "window_start": start.astimezone(zone).isoformat(),
        "window_end": end.astimezone(zone).isoformat(),
        "heat_kwh": None,
        "cop": None,
        "electricity_kwh": None,
        "covered_heat_kwh": 0.0,
        "covered_electricity_kwh": 0.0,
        "covered_hours": 0,
        "expected_hours": 24,
        "coverage_percent": 0.0,
        "energy_sample_coverage_percent": None,
        "assumed_brine_c": brine,
        "performance_factor": factor,
        "reference_cop": settings.reference_cop,
        "reference_source_c": settings.reference_source_c,
        "reference_flow_c": settings.reference_flow_c,
        "model": "Manufacturer-anchored temperature-lift estimate; not a heat measurement",
        "condensing_offset_k": settings.condensing_offset_k,
        "evaporating_offset_k": settings.evaporating_offset_k,
        "includes_auxiliary_heating": True,
        "auxiliary_cop": 1.0,
        "tank_energy_added": False,
        "status": "ambiguous_dst_hour" if ambiguous else "incomplete",
        "hours": [],
    }
    total_comp = total_aux = heat = 0.0
    energy_coverage = []
    input_coverage = []
    for h, label in zip(hours, labels):
        local = h.astimezone(zone)
        day = local.strftime("%Y-%m-%d")
        if label in ambiguous:
            energy_coverage.extend([0, 0])
            input_coverage.append(0)
            result["hours"].append(
                {"hour": local.isoformat(), "status": "ambiguous_dst_hour"}
            )
            continue
        cp = energy_point(recordings.get((day, "compressor")), day, local.hour)
        aux = energy_point(recordings.get((day, "eheater")), day, local.hour)
        model = hour_model(history, h, brine, factor, settings)
        item = {
            "hour": local.isoformat(),
            "status": "missing_electricity",
            "compressor_kwh": cp["kwh"] if cp else None,
            "auxiliary_kwh": aux["kwh"] if aux else None,
            **model,
        }
        energy_coverage.extend(
            [cp["coverage"] if cp else 0, aux["coverage"] if aux else 0]
        )
        input_coverage.append(
            min(
                cp["coverage"] if cp else 0,
                aux["coverage"] if aux else 0,
                model["temperature_coverage"],
            )
        )
        if cp is None or aux is None:
            result["hours"].append(item)
            continue
        # No compressor electricity needs no guessed compressor heat, and no
        # return-temperature calculation or tank-rise summation is added.
        if cp["kwh"] > 0 and (
            model["temperature_coverage"] < 0.8
            or model["active_minutes"] < 3
            or model["model_cop"] is None
        ):
            item["status"] = "missing_operating_history"
            result["hours"].append(item)
            continue
        if cp["kwh"] == 0 and model["active_minutes"] >= 15:
            # Coarse counters can legitimately report zero during operation.
            # Without a documented rounding rule this is not missing input.
            item["electricity_resolution_warning"] = True
        item_heat = cp["kwh"] * (model["model_cop"] or 0) + aux["kwh"]
        item.update({"estimated_heat_kwh": item_heat, "status": "estimated"})
        result["covered_hours"] += 1
        total_comp += cp["kwh"]
        total_aux += aux["kwh"]
        heat += item_heat
        result["hours"].append(item)
    result.update(
        {
            "covered_heat_kwh": heat,
            "covered_electricity_kwh": total_comp + total_aux,
            "coverage_percent": 100 * sum(input_coverage) / 24,
            "energy_sample_coverage_percent": 100
            * sum(energy_coverage)
            / len(energy_coverage)
            if energy_coverage
            else None,
        }
    )
    tank_changes = [h.get("tank_net_stored_heat_change_kwh") for h in result["hours"]]
    result.update(
        {
            "probable_dhw_heating_hours": sum(
                h.get("probable_dhw_minutes", 0) for h in result["hours"]
            )
            / 60,
            "tank_observed_hours": sum(v is not None for v in tank_changes),
            "tank_net_stored_heat_change_kwh": sum(tank_changes)
            if all(v is not None for v in tank_changes) and len(tank_changes) == 24
            else None,
            "tank_volume_litres": settings.tank_volume_litres,
            "tank_kwh_per_degree": settings.tank_volume_litres * 4.186 / 3600,
            "tank_energy_note": "Stored-energy change assumes the configured tank follows its single temperature probe uniformly. Stratification, hot-water draws and losses are unmeasured. This is not DHW production, is not a calibration label, and is not added to compressor heat.",
        }
    )
    result["zero_energy_active_hours"] = sum(
        bool(r.get("electricity_resolution_warning")) for r in result["hours"]
    )
    result["coverage_note"] = (
        "Input coverage counts valid electrical samples and temperature/modulation history; it is not a percentage accuracy or confidence."
    )
    result["electricity_resolution_note"] = (
        "Zero is retained as reported. The gateway rounding rule is unknown; activity during a zero bucket is not proof of missing energy."
    )
    if result["covered_hours"] == 24:
        total = total_comp + total_aux
        result.update(
            {
                "heat_kwh": heat,
                "electricity_kwh": total,
                "compressor_kwh": total_comp,
                "auxiliary_kwh": total_aux,
                "cop": heat / total if total > 0 else None,
                "status": "estimated",
            }
        )
    return result
