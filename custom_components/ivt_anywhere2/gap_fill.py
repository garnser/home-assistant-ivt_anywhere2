"""Short gaps in operating history; reported electricity is never inferred."""

from copy import deepcopy
from datetime import datetime, timedelta
from statistics import median

from .estimate_math import number

MAX_GAPS = 3


def fill_short_gaps(baseline, history, learned_factor=1.0, manual_factor=1.0):
    """Borrow a nearby temperature-model COP only when input energy is known.

    A valid zero can reflect coarse reporting; keep it. Missing energy, missing
    auxiliary readings and DST ambiguity cannot be filled from positive donors.
    Estimates remain presentation values, never stored or calibration targets.
    """
    result = deepcopy(baseline)
    rows = result.get("hours", [])
    gaps = [r for r in rows if r.get("status") != "estimated"]
    reported = len(rows) == 24 and all(
        number(r.get("compressor_kwh")) is not None
        and number(r.get("auxiliary_kwh")) is not None
        for r in rows
    )
    comp = sum(r["compressor_kwh"] for r in rows) if reported else None
    aux = sum(r["auxiliary_kwh"] for r in rows) if reported else None
    result.update(
        electricity_kwh=comp + aux if reported else None,
        reported_electricity_kwh=comp + aux if reported else None,
        compressor_kwh=comp,
        auxiliary_kwh=aux,
        inferred_electricity_kwh=0.0,
        gap_filled_hours=0,
        imputation_details=[],
    )
    if len(rows) == 24 and len(gaps) <= MAX_GAPS:
        for row in gaps:
            if row.get("status") != "missing_operating_history":
                continue
            if any(
                number(row.get(k)) is None for k in ("compressor_kwh", "auxiliary_kwh")
            ):
                continue
            target = datetime.fromisoformat(row["hour"])
            donors = {}
            for source in history:
                if source.get("status") != "estimated" or source.get("imputed"):
                    continue
                stamp = datetime.fromisoformat(source["hour"])
                cop = number(source.get("model_cop"))
                if (
                    target - timedelta(hours=3) <= stamp < target
                    and cop is not None
                    and 1 <= cop <= 8
                    and source.get("temperature_coverage", 0) >= 0.95
                ):
                    donors[stamp] = cop
            if len(donors) < 2:
                continue
            stamps = sorted(donors, reverse=True)[:3]
            cop = median(donors[t] for t in stamps)
            row.update(
                status="gap_filled",
                imputed=True,
                model_cop=cop,
                estimated_heat_kwh=row["compressor_kwh"]
                * cop
                * learned_factor
                * manual_factor
                + row["auxiliary_kwh"],
            )
            result["gap_filled_hours"] += 1
            result["imputation_details"].append(
                {
                    "hour": row["hour"],
                    "reason": "missing_operating_history",
                    "donor_hours": [t.isoformat() for t in stamps],
                    "method": "Median of earlier temperature-model COPs, at most three hours old",
                }
            )
    usable = [r for r in rows if r.get("status") in ("estimated", "gap_filled")]
    result.update(
        usable_hours=len(usable),
        unfilled_hours=24 - len(usable),
        imputation_note="Only short temperature-history gaps are approximated. Electricity is retained as reported. Gap estimates never become calibration labels.",
    )
    if len(usable) == 24:
        electricity = comp + aux
        heat = sum(r["estimated_heat_kwh"] for r in usable)
        result.update(
            heat_kwh=heat,
            model_heat_kwh=heat,
            cop=heat / electricity if electricity else None,
            model_electricity_kwh=electricity,
            model_compressor_kwh=comp,
            model_auxiliary_kwh=aux,
            status="estimated_with_gap_fill"
            if result["gap_filled_hours"]
            else "estimated",
        )
    return result
