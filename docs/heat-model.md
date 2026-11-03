# Optional local readings and heat/COP model

The integration's default remains cloud energy monitoring. In **Settings →
Devices & services → IVT Anywhere II → Configure**, you can enable independent
local readings and a background heat model. Both are disabled by default.
These are read-only features: they do not control the heat pump.

## Requirements and setup

1. Configure the **Bosch thermostat** custom integration for the same gateway
   serial/ID as this IVT integration. It must have working local credentials.
   This feature reuses that entry's credentials in memory; it does not copy
   passwords into IVT options, expose credentials in diagnostics, or sign in
   to the cloud a second time. Bosch is an optional companion integration,
   not an automatically installed integration.
2. Enable **local readings** and enter your gateway's LAN IP address or hostname,
   without `http://` or a URL path. Default polling is every 300 seconds;
   the permitted interval is 300–3600 seconds. The configured address is used
   only for read-only local HTTP requests through `bosch-thermostat-client`.
   A cloud failure does not stop local refreshes or model startup.
3. Enable the **heat/COP model** and select three distinct sensors:
   supply temperature, return temperature, and actual compressor modulation.
   Temperature sensors must declare °C or °F; Fahrenheit history is converted
   to Celsius. Modulation must declare `%` and mean 0–100% compressor operation,
   not pump speed or a binary heating request. An optional tank temperature
   sensor adds probable domestic-hot-water detection and a storage diagnostic.
4. Enter your appliance's manufacturer reference COP and its source/brine and
   supply temperatures. **COP 4.8 at B0/W35 and tank 180 L are GEO 512C defaults,
   not universal specifications.** Set the assumed source temperature and
   performance factor explicitly. Condenser/evaporator offsets are assumptions,
   not measured refrigerant temperatures.
5. Leave calibration disabled initially. Enable it only after checking the
   gateway heat-output definition, including whether auxiliary heat is already
   included. Options changes reload this entry; no HA restart is required.

The local client has been exercised with an IVT GEO 512C/iCom gateway. Other
models need compatible recording endpoints and suitable operating sensors;
this does not establish support for every Bosch gateway. It is a temperature-
model approximation, particularly limited for appliances with defrost cycles.

Recorder must retain the selected operating sensors. Installation reads the
preceding 48 hours of available history; subsequent updates recompute a rolling
24-hour window every five minutes. It cannot recover operating history that
Recorder never collected. HA's configured timezone is used for recording
buckets. The two ambiguous repeated local hours at autumn DST are excluded,
not duplicated or evenly split. Monthly coverage counts elapsed UTC hours.

## Outputs

| Output | Meaning |
|---|---|
| Local energy sensors | Direct gateway compressor, auxiliary and output recordings, plus cloud comparison |
| Estimated heat / COP, last 24 hours | Temperature-model heat weighted by reported compressor input, plus auxiliary heat at COP 1 |
| Model electricity, last 24 hours | Reported electrical input; **no inferred electricity is added** |
| Input coverage | Valid electrical samples and temperature/modulation history across the window; **not confidence or accuracy** |
| Zero input hours with activity | Valid zero compressor buckets with thermal activity; possible coarse reporting, not proven missing energy |
| Gap-filled model hours | Short temperature-history gaps approximated from earlier model COPs |
| Modelled heat / electricity / COP this month | Subtotals over identical covered hours only, with coverage attributes |
| Projected heat / COP this month | Rough projection using full reported month electricity and covered-period compressor efficiency |
| Model status | Calibration disabled, collecting, calibrated, rejected candidate, or stale calibration |

Reported cloud sensor unique IDs and the existing recorded-electricity feed are
preserved. Rolling, partial and projected energy estimates have **no cumulative
state class** and cannot be mistaken for an additional Energy-dashboard meter.
Use the existing recorded-electricity feed for consumption accounting.
Month projections assume uncovered periods resemble the model's covered
conditions. A recent model basis (within two hours) is required. They prefer at least 24 covered month hours and 5 kWh compressor
input; an early-month fallback requires a complete rolling 24-hour model basis.
They remain estimates and can be revised; they are not backfilled heat readings.

## What the model calculates

A Carnot-shaped temperature-lift curve is anchored to the configured manufacturer
rating. Compressor modulation weights temperatures during probable heat transfer;
it is **not** used as a direct wattmeter. Supply/return temperature difference
and compressor activity identify likely heating, not absolute thermal power:
there is no measured water flow in this calculation.

A rising tank temperature with compressor activity marks probable hot-water
charging. The model then uses at least the tank temperature as its hot-side
proxy. Tank storage change is `litres × 4.186 / 3600 × ΔT` kWh. A single probe
cannot measure the tank's uniform temperature, stratification, draws, or losses,
so that diagnostic is never added to compressor heat or used as a training label.

Valid recorded zeros stay zero. Gateway resolution and rounding rules are not
assumed; an active compressor during a zero bucket does not justify adding a
positive donor estimate. `c=0`, invalid counts, missing resources and ambiguous
DST readings remain missing. A zero-derived heat point describes the estimate
from the reported input, not proof that no physical heating occurred.

Up to three hours with **missing temperature history but known electrical input**
can use the median COP from at least two valid, earlier hours within three hours.
The original observations remain untouched. No future data, guessed electricity,
missing auxiliary input, or previous gap estimates are used as donors.
Large gaps and insufficient evidence leave the full-window estimate unavailable;
known electrical totals and covered-hour subtotals are retained separately.

## Optional statistical calibration

Calibration uses robust medians from Python's standard statistics library,
not a neural network or a fixed COP. It starts from the manufacturer curve and
accepts adjustments only after a chronological holdout check:

- Entire completed 24-hour days, including zeros, must have sufficient operating
  history (95%) and recording samples (98%). Hourly and daily aggregates must
  agree, without substantial zero heat while compressor energy is consumed.
- A completed day's raw aggregate must repeat unchanged at least 30 minutes
  later. Model predictions, gap fills and tank-storage estimates are never labels.
- At least 14 qualifying days are required. A median scale factor fits the earlier
  days; the four latest days are held out. The factor must be 0.5–1.2, validation
  weighted absolute error at most 20%, and improvement at least 10% over baseline.
- An accepted factor scales compressor heat only. Auxiliary heat remains at COP 1.
  Calibration expires when qualifying observations stop for more than 30 days.

Quality gates may reject every day on a gateway with a broken heat channel.
Waiting 14 calendar days does not guarantee calibration. Even agreeing local and
cloud readings are copies of gateway data, not independent heat-meter validation.

Cumulative panel photographs or lifetime ratios are useful plausibility checks.
They are not automatically imported as hourly labels or used to force a seasonal
model to a lifetime average. Whole-MWh display resolution and unknown counter
reset histories limit comparisons between snapshots.

## Persistence, privacy and maintenance

Raw model observations and rejected-day reasons are kept in a per-gateway HA
Store for up to 180 days; calibration considers up to 90 days. Local comparison
history is bounded to 576 samples (about two days at the default poll interval).
Changing the model inputs or physics resets its cached training history; it does
not delete Recorder history or the reported energy feed. Disabling the feature
stops its polling after options reload and preserves stored history for reuse.
No model inputs or learned parameters are sent to an external inference service.

Download integration diagnostics for model status, coverage and rejection
counts. Refresh tokens, gateway host/ID and sensor mappings are redacted.
If a sensor changes units or is unavailable, that history is rejected or converted
as specified above; unsupported units do not silently become Celsius or percent.
