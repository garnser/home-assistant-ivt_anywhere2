"""Conservative, daily, chronological calibration of the physical heat model.

Gateway labels are not an independent heat meter. Complete days, including all
zero/low readings, are assessed together; positive hours are never cherry-picked.
No predictions are fed back as labels. A rejected day remains in the audit data.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from math import isfinite
from statistics import median

MIN_DAYS = 14
VALIDATION_DAYS = 4
MAX_DAYS = 90
MIN_SAMPLE_FRACTION = 0.98


def numeric(value):
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if isfinite(result) else None


def parent_point(payload, day):
    """Daily recording in a monthly resource; never interpret c=0 as zero."""
    if (
        not isinstance(payload, dict)
        or payload.get("interval") != day[:7]
        or payload.get("sampleRate") != "P1D"
    ):
        return None
    rows = payload.get("recording", [])
    i = int(day[-2:]) - 1
    if i >= len(rows):
        return None
    row = rows[i]
    y, c = numeric(row.get("y")), numeric(row.get("c"))
    return (
        {"y": y, "c": c}
        if y is not None and y >= 0 and c is not None and c > 0
        else None
    )


def assess_day(day, rows, parents, heat_includes_auxiliary=True):
    """Return one whole-day training example or a concrete rejection reason."""
    result = {"day": day, "status": "incomplete_hours", "qualified": False}
    # The gateway cannot disambiguate the repeated hour at DST fall-back.
    if len(rows) != 24 or {datetime.fromisoformat(r["hour"]).hour for r in rows} != set(
        range(24)
    ):
        return result
    if any(r.get("status") != "estimated" for r in rows):
        return {**result, "status": "incomplete_operating_history"}
    if any(
        r.get("temperature_coverage", 0) < 0.95
        for r in rows
        if r.get("compressor_kwh", 0) > 0
    ):
        return {**result, "status": "operating_coverage_below_95_percent"}
    totals = {}
    signature = []
    for kind in ("compressor", "eheater", "outputProduced"):
        raw = [r.get("raw", {}).get(kind) for r in rows]
        p = parents.get(kind)
        if any(
            not isinstance(r, dict)
            or numeric(r.get("y")) is None
            or numeric(r.get("c")) is None
            or r["y"] < 0
            or not 60 * MIN_SAMPLE_FRACTION <= r["c"] <= 60
            for r in raw
        ):
            return {**result, "status": "recording_coverage_below_98_percent"}
        if p is None or not 1440 * MIN_SAMPLE_FRACTION <= p["c"] <= 1440:
            return {**result, "status": "missing_complete_daily_rollup"}
        y = sum(r["y"] for r in raw)
        c = sum(r["c"] for r in raw)
        total = sum(r["y"] / r["c"] for r in raw)
        daily_total = p["y"] / p["c"] * 24
        if (
            abs(y - p["y"]) > max(1, 0.02 * y)
            or abs(c - p["c"]) > 3
            or abs(total - daily_total) > max(0.5, 0.03 * total)
        ):
            return {**result, "status": "hourly_daily_disagreement"}
        totals[kind] = total
        signature.extend([p["y"], p["c"], y, c])
    comp, aux, observed = (
        totals[k] for k in ("compressor", "eheater", "outputProduced")
    )
    # Require an informative whole day, not an auxiliary-only or idle example.
    if comp < 5:
        return {**result, "status": "insufficient_compressor_energy"}
    # Long zero-heat gaps while the compressor consumes electricity indicate
    # the known broken output channel. Reject the entire day, not just gaps.
    zero_energy = sum(
        r["compressor_kwh"] for r in rows if r["raw"]["outputProduced"]["y"] == 0
    )
    if zero_energy / comp > 0.10:
        return {
            **result,
            "status": "heat_output_gaps",
            "zero_heat_electricity_fraction": zero_energy / comp,
        }
    compressor_heat = observed - aux if heat_includes_auxiliary else observed
    base = sum(r["compressor_kwh"] * (r.get("model_cop") or 0) for r in rows)
    if base <= 0 or not comp <= compressor_heat <= comp * 8:
        return {**result, "status": "implausible_daily_energy_balance"}
    return {
        **result,
        "status": "awaiting_stability",
        "signature": signature,
        "base_compressor_heat_kwh": base,
        "observed_compressor_heat_kwh": compressor_heat,
        "compressor_kwh": comp,
        "auxiliary_kwh": aux,
        "reported_heat_kwh": observed,
    }


def observe_day(candidate, previous, now):
    """Only a matching reread >=30 minutes later admits a completed day."""
    candidate = dict(candidate)
    if candidate["status"] != "awaiting_stability":
        return candidate
    if previous and previous.get("signature") == candidate["signature"]:
        first = previous.get("stable_since")
        if first and now - datetime.fromisoformat(first) >= timedelta(minutes=30):
            candidate.update(qualified=True, status="qualified")
        candidate["stable_since"] = first or now.isoformat()
    else:
        candidate["stable_since"] = now.isoformat()
    return candidate


def fit_model(days, now):
    """Robust scale fit, tested on four strictly later, unused calendar days.

    Refit/revalidate from raw examples every time; never compound corrections.
    This is empirical calibration against vetted gateway data, not measured COP.
    """
    cutoff = (now - timedelta(days=MAX_DAYS)).date().isoformat()
    qualified = sorted(
        (
            d
            for d in days
            if d.get("qualified") and cutoff <= d["day"] < now.date().isoformat()
        ),
        key=lambda d: d["day"],
    )
    result = {
        "status": "collecting",
        "factor": 1.0,
        "qualified_days": len(qualified),
        "required_days": MIN_DAYS,
        "validation_days": 0,
        "training_days": 0,
        "validation_wape_percent": None,
        "baseline_wape_percent": None,
    }
    if len(qualified) < MIN_DAYS:
        return result
    # Stop using calibration if no qualifying labels have arrived for a month.
    if (now.date() - datetime.fromisoformat(qualified[-1]["day"]).date()).days > 30:
        return {**result, "status": "calibration_stale"}
    train, validation = qualified[:-VALIDATION_DAYS], qualified[-VALIDATION_DAYS:]
    factor = median(
        d["observed_compressor_heat_kwh"] / d["base_compressor_heat_kwh"] for d in train
    )
    result.update(
        training_days=len(train),
        validation_days=len(validation),
        candidate_factor=factor,
    )
    if not 0.5 <= factor <= 1.2:
        return {**result, "status": "candidate_out_of_bounds"}
    denominator = sum(d["observed_compressor_heat_kwh"] for d in validation)
    if denominator <= 0:
        return {**result, "status": "validation_failed"}
    error = (
        sum(
            abs(
                factor * d["base_compressor_heat_kwh"]
                - d["observed_compressor_heat_kwh"]
            )
            for d in validation
        )
        / denominator
    )
    baseline = (
        sum(
            abs(d["base_compressor_heat_kwh"] - d["observed_compressor_heat_kwh"])
            for d in validation
        )
        / denominator
    )
    result.update(
        validation_wape_percent=100 * error, baseline_wape_percent=100 * baseline
    )
    if error > 0.20 or error > baseline * 0.90:
        return {**result, "status": "baseline_retained"}
    return {**result, "status": "calibrated", "factor": factor}


def apply_model(result, learned_factor, manual_factor):
    """Scale compressor heat only; preserve electrical totals and aux COP=1."""
    factor = learned_factor * manual_factor
    for row in result.get("hours", []):
        if row.get("status") == "estimated":
            row["estimated_heat_kwh"] = (
                row["compressor_kwh"] * (row.get("model_cop") or 0) * factor
                + row["auxiliary_kwh"]
            )
    result["covered_heat_kwh"] = sum(
        r.get("estimated_heat_kwh", 0) for r in result.get("hours", [])
    )
    if result.get("heat_kwh") is not None:
        result["heat_kwh"] = result["covered_heat_kwh"]
        electricity = result.get("electricity_kwh", 0)
        result["cop"] = result["heat_kwh"] / electricity if electricity else None
    result["performance_factor"] = manual_factor
    result["learned_factor"] = learned_factor
    result["effective_performance_factor"] = factor
    return result
