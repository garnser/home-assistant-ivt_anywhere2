"""Calendar-month subtotals for explicitly covered model hours only."""

from datetime import datetime, timedelta, timezone

from .learning import numeric


def aggregate_month(rows, end, zone, learned_factor=1.0, manual_factor=1.0):
    """Do not extrapolate unobserved days, fill gaps or average hourly COPs.

    Output is a covered-hours subtotal until is_complete is true. The elapsed
    UTC-hour denominator includes local DST changes in the calendar month.
    """
    end = end.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    local_end = end.astimezone(zone)
    start = local_end.replace(
        day=1, hour=0, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)
    expected = int((end - start).total_seconds() // 3600)
    factor = learned_factor * manual_factor
    unique = {}
    for row in rows:
        if row.get("status") != "estimated":
            continue
        try:
            stamp = datetime.fromisoformat(row["hour"])
            if stamp.tzinfo is None:
                continue
            stamp = stamp.astimezone(timezone.utc)
        except (KeyError, ValueError, TypeError):
            continue
        if (
            not start <= stamp < end
            or stamp.minute
            or stamp.second
            or stamp.microsecond
        ):
            continue
        comp = numeric(row.get("compressor_kwh"))
        aux = numeric(row.get("auxiliary_kwh"))
        cop = numeric(row.get("model_cop"))
        if (
            comp is None
            or aux is None
            or comp < 0
            or aux < 0
            or (comp > 0 and (cop is None or not 1 <= cop <= 8))
        ):
            continue
        unique[stamp] = (comp, aux, comp * (cop or 0) * factor + aux)
    covered = len(unique)
    comp = sum(r[0] for r in unique.values())
    aux = sum(r[1] for r in unique.values())
    heat = sum(r[2] for r in unique.values())
    electricity = comp + aux
    complete = covered == expected and expected > 0
    return {
        "window_start": start.astimezone(zone).isoformat(),
        "window_end": end.astimezone(zone).isoformat(),
        "covered_from": min(unique).astimezone(zone).isoformat() if unique else None,
        "covered_until": (max(unique) + timedelta(hours=1)).astimezone(zone).isoformat()
        if unique
        else None,
        "covered_hours": covered,
        "expected_hours": expected,
        "missing_hours": expected - covered,
        "is_complete": complete,
        "status": "complete" if complete else "partial" if covered else "no_data",
        "heat_kwh": heat if covered else None,
        "electricity_kwh": electricity if covered else None,
        "cop": heat / electricity if electricity > 0 else None,
        "compressor_kwh": comp if covered else None,
        "auxiliary_kwh": aux if covered else None,
        "full_month_heat_kwh": heat if complete else None,
        "full_month_electricity_kwh": electricity if complete else None,
        "learned_factor": learned_factor,
        "manual_factor": manual_factor,
        "method": "Sum modelled heat and electricity over identical covered hours; gaps are excluded, not zero-filled or extrapolated.",
    }


def project_month(month, rolling, local, now):
    """Rough full-month projection, keeping the full reported electrical input.

    Covered-hours compressor efficiency is extrapolated to reported month input.
    It is not reconstructed historical heat or a calibrated uncertainty interval.
    """
    result = {
        "heat_kwh": None,
        "cop": None,
        "status": "insufficient_basis",
        "method": "Rough projection: uncovered periods assumed similar to covered operating conditions",
    }
    local = local or {}
    try:
        sampled = datetime.fromisoformat(local["sampled_at"])
    except (KeyError, ValueError, TypeError):
        return result
    if sampled.tzinfo is None:
        return {**result, "status": "stale_month_input"}
    poll = numeric(local.get("poll_interval_seconds")) or 300
    fresh_seconds = max(900, min(3600, max(300, poll)) * 2)
    if local.get("month") != now.astimezone(sampled.tzinfo).strftime(
        "%Y-%m"
    ) or not timedelta(0) <= now - sampled <= timedelta(seconds=fresh_seconds):
        return {**result, "status": "stale_month_input"}
    values = local.get("values", {})
    comp, aux = (numeric(values.get(k)) for k in ("compressor_month", "eheater_month"))
    if comp is None or aux is None or comp < 0 or aux < 0:
        return result
    if comp == 0:
        return {
            **result,
            "status": "auxiliary_only",
            "heat_kwh": aux,
            "electricity_kwh": aux,
            "cop": 1.0 if aux else None,
            "compressor_kwh": 0,
            "auxiliary_kwh": aux,
            "basis_hours": 0,
        }
    basis = month
    try:
        covered_until = datetime.fromisoformat(month["covered_until"])
        recent = covered_until.tzinfo is not None and timedelta(
            0
        ) <= now - covered_until <= timedelta(hours=2)
    except (KeyError, ValueError, TypeError):
        recent = False
    period = "covered_month_hours"
    if (
        not recent
        or month.get("covered_hours", 0) < 24
        or (month.get("compressor_kwh") or 0) < 5
    ):
        if rolling.get("usable_hours") != 24:
            return result
        period = "last_24_hours"
        basis = {
            "covered_hours": 24,
            "heat_kwh": rolling.get("model_heat_kwh"),
            "compressor_kwh": rolling.get("model_compressor_kwh"),
            "auxiliary_kwh": rolling.get("model_auxiliary_kwh"),
        }
    bcomp, baux, bheat = (
        numeric(basis.get(k)) for k in ("compressor_kwh", "auxiliary_kwh", "heat_kwh")
    )
    if bcomp is None or baux is None or bheat is None or bcomp < 5:
        return result
    else:
        ratio = (bheat - baux) / bcomp
        if not 1 <= ratio <= 8:
            return result
        heat = comp * ratio + aux
    electricity = comp + aux
    return {
        **result,
        "status": "rough_projection",
        "heat_kwh": heat,
        "cop": heat / electricity if electricity else None,
        "electricity_kwh": electricity,
        "compressor_kwh": comp,
        "auxiliary_kwh": aux,
        "basis_hours": basis.get("covered_hours"),
        "basis_period": period,
        "compressor_cop_basis": ratio,
    }
