# IVT Anywhere II – Home Assistant Integration

A custom Home Assistant integration for **IVT / Bosch heat pumps** that use the **IVT Anywhere II** mobile app (Bosch **Pointt** cloud API).

This integration focuses on **recorded energy & utilization data** (hourly/daily/monthly totals) that Bosch exposes via the cloud recordings endpoints. It is **not** a “live telemetry” integration.

---

## Features

* Connects to the **Bosch Pointt cloud API** used by the IVT Anywhere II app
* Exposes recorded energy data as **Home Assistant sensors**
* Imports **hourly electricity statistics at their measurement time** for the Energy dashboard
* Uses **Config Flow** (UI setup)
* Handles **OAuth token refresh** during runtime
* Polls every **30 minutes**, with **one-minute retries** for incomplete or failed reports

---

## What you’ll get in Home Assistant

Sensors are built from the data Bosch records and publishes afterwards. Typical sensors include:

* Electricity (last complete hour)
* Heat output (last complete hour)
* Compressor energy (last complete hour)
* Electric heater energy (last complete hour)
* COP (last complete hour)

…and monthly totals equivalents where available.

> Exact availability depends on your heat pump model and what Bosch records for your gateway.

### How recorded energy is calculated

For the supported `energyMonitoring` recordings, `y` is a sampled sum and `c`
is the sample count. They are not raw Wh values. The integration calculates
each completed hourly (`P1H`) bucket as `y / c` kWh and each completed daily
(`P1D`) bucket as `24 * y / c` kWh. For example, `y=60, c=60` represents 1 kWh for an hourly
bucket; `y=600, c=1440` represents 10 kWh for a daily bucket. This matches the
[FHEM KM200 recording implementation](https://github.com/fhem/fhem-mirror/blob/master/fhem/FHEM/73_km200.pm).

Zero-count buckets are padding or missing data. They are not measured zero
consumption. Missing or invalid readings are unavailable, and electricity/COP
require both compressor and auxiliary-heater data. Month-to-date totals also
require data for every completed day and every completed hour of today. A genuine measured zero remains valid.

COP is reported heat output divided by compressor plus auxiliary-heater
electricity. Zero electricity makes COP undefined, so it is unavailable; reported
zero heat with positive electricity produces COP 0. These are Bosch-reported
values: the integration does not independently validate physical heat production.

Month-to-date is the sum of completed daily buckets before today plus today's
completed hourly buckets. Today's daily bucket is never included: its average
extrapolated to 24 hours is a projection, not consumption so far. For example,
`y=600, c=480` can project 30 kWh for the day even though the eight completed
hourly buckets sum to only 10 kWh. Future and incomplete hourly buckets are
excluded. Before the first hour of a new month is complete, the total is zero.

Small downward revisions of a monthly total retain the last accepted value,
so the monthly electricity total and COP stay available without making Home
Assistant treat a tiny correction as a meter reset. The tolerance is 0.1 kWh
or 0.1% of the accepted total, whichever is larger. Values can therefore lag a
minor cloud correction until the reported total catches up. Larger drops and
genuinely missing data remain unavailable. The last accepted values are retained
across restarts and reset at the next calendar month. Separate compressor and
auxiliary-heater monthly sensors make the electricity total easier to compare
with the corresponding app views.

The per-hour energy sensors use `total` with an explicit `last_reset` for the
recorded hour. This retains long-term statistics while treating each hour as a
new energy cycle. Repeated polls and restarts within the same recorded hour
keep the same reset marker. Monthly energy sensors use `total` because recorded
totals can be corrected, with an explicit reset at real month boundaries.
On initial installation or migration the reset marker stays unset for the
current month, so the transition does not count the entire month again.
COP uses `measurement`. See Home Assistant's
[periodic totals guidance](https://contributors.home-assistant.io/core/entities/sensor/#choosing-between-total-classes).

### Energy dashboard: use recorded-hour statistics

In the Energy dashboard configuration, add an **individual device** with the
source **IVT \<gateway\> electricity (recorded hours)**. If the heater is already
listed using **Electricity last complete hour** or a monthly electricity sensor,
edit that entry and replace its source instead of adding another heater entry.

The statistic ID is `ivt_anywhere2:<gateway>_electricity_recorded`. Gateway IDs
are lowercased and punctuation is normalized to underscores. This is a Recorder
statistics source, so it will not appear as a new entity in Developer Tools.
Recorder is a required dependency. The source appears after the first valid
hourly recording is imported; restart HA after updating the integration.

Each reading is imported with the start of the hour it covers. A 09:00–10:00
reading received at 10:03 is therefore placed in 09:00–10:00. The native
**Electricity last complete hour** sensor remains useful for current values
and automations, but its ordinary history is timestamped when HA receives it.
Use only the recorded-hour source for that heater's consumption in Energy.
Home Assistant's configured timezone must match the timezone used by the gateway's
recordings so local hours are assigned correctly.

The new source initially imports the day containing the last completed hour
and the preceding day, where data is available. During the first hour after
midnight, the last completed hour belongs to yesterday. Subsequent polls refresh
those days, replace corrected readings, and catch up missed days. At most three
historical days are fetched per poll, in addition to the already-requested
report day. Missing hours stay absent until valid data arrives. Measured zeros
remain valid. A transient empty response does not erase previously imported
readings. A persisted hour journal makes repeated polling and restarts idempotent and keeps cumulative
totals continuous across midnight, month and year boundaries. The journal and
outage catch-up are bounded to 62 days; older measured values become a persisted
counter baseline, while their long-term statistics stay in Recorder. Existing native
sensor statistics are not rewritten or deleted by this feature.

Timestamps use UTC internally. Actual 23/25-slot daylight-saving day recordings
are supported; an ambiguous 24-slot response on such a day is skipped instead
of assigning readings to guessed hours. Recorder import failures do not make
the live sensor entities unavailable.

Missing resources in a bulk response are retried once without replacing healthy
responses. If a report is still incomplete, or the cloud request fails, the
coordinator retries after one minute. Complete reports restore the normal
30-minute polling interval. Readings remain unavailable while valid data is
missing; the shorter interval reduces the delay before checking for recovery.

Upgrading from a build that included today's daily projection discards the old
cached peak. Corrected monthly readings may be lower. Historical Home Assistant
statistics are not rewritten by the integration; review any previously inflated
Energy readings separately before relying on them.

---

## Optional local readings and heat/COP estimation

**Configure** the IVT integration to enable read-only local gateway polling and
an optional background heat model. Select your temperature/modulation sensors,
gateway address and manufacturer reference rating; no personal sensor IDs or
LAN addresses are built into the feature. A matching Bosch integration entry
supplies local credentials. Cloud-only installations keep their existing setup.

Model outputs include rolling 24-hour estimates, covered-month subtotals and
explicitly labelled month projections. Valid zero energy readings are preserved;
possible whole-kWh reporting is flagged rather than replaced with guessed usage.
Calibration is disabled by default and can be enabled with strict whole-day and
chronological validation gates. These are estimates, not heat-meter readings.

See [setup, assumptions, data-quality rules and sensors](docs/heat-model.md).

---

## Installation

### Option A — HACS (recommended)

1. Open **HACS** in Home Assistant.
2. Open the **⋮ menu** (top right) → **Custom repositories**.
3. Add `https://github.com/garnser/home-assistant-ivt_anywhere2`, select type **Integration**, and add it.
4. Find **IVT Anywhere II** in HACS and click **Download**.
5. **Restart Home Assistant**.
6. Go to **Settings → Devices & services → Add integration** and search for **IVT Anywhere II**.

See the [HACS custom repository instructions](https://www.hacs.xyz/docs/faq/custom_repositories/).

### Option B — Manual install

1. Copy `custom_components/ivt_anywhere2/` into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.
3. Add the integration via **Settings → Devices & services → Add integration**.

### Updating an existing installation

1. Back up your configuration and any locally modified integration files.
2. Update through HACS, or replace the entire `custom_components/ivt_anywhere2/`
   folder from the new release for a manual installation. New modules such as
   `statistics.py` must be included.
3. Restart Home Assistant. Keep your existing integration entry and configuration.
4. In Energy, replace the heater's old hourly or monthly source with
   **IVT \<gateway\> electricity (recorded hours)** as described above.

Upgrading does not automatically change your Energy dashboard settings. The new
source initially supplies the two report days described above; older statistics
from the previous source remain stored separately and are not automatically
copied or repaired. Upgrades from v0.2.3 or earlier can lower corrected monthly
readings because unfinished-day projections are excluded.

---

## Configuration (UI)

The integration is configured through the Home Assistant UI.

During setup you will be asked for:

* **Refresh token** (from the IVT Anywhere II / Bosch Pointt OAuth flow)
* Then you’ll select a **Gateway ID** detected for your account

After you complete the flow, sensors will be added under a device named like:

* `IVT Anywhere II <gateway_id>`

---

## Getting a refresh token

This integration requires a **Bosch / IVT Anywhere II OAuth refresh token**.

A helper script is included at **`scripts/ivt_anywhere2_auth.py`**.
Run the commands below from the repository root.

### Prerequisites

* **Python 3.10+** for the standalone helper and offline tests
* Python dependency: **httpx**

For example, create a virtual environment and install httpx on macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install httpx
```

On Windows, activate the environment with `.venv\Scripts\activate` instead.
These prerequisites are for the helper on your computer; Home Assistant manages
its own Python environment.

### Step-by-step

1. Run the helper script:

```bash
python scripts/ivt_anywhere2_auth.py
```

2. The script will print an **authorization URL**. Open it in your browser and log in with the same account you use in the IVT Anywhere II app.

3. After login, Bosch SingleKey redirects to an **app redirect URI** (it looks like `com.bosch.tt.dashtt.pointt://app/login?...`). Many desktop browsers won’t “open” that redirect.

   You still need to capture the OAuth **`code`** from the final redirect. You can do this in one of these ways:

   * Copy the final redirect URL that contains `?code=...` (some browsers will show it briefly), **or**
   * In browser devtools → Network, inspect the final request and copy the **`Location`** header from the 302 redirect (it contains `code=`), **or**
   * Perform the login on a phone where the app is installed and copy/paste the `code` from the redirect URL.

4. Paste either:

   * the **full redirect URL** containing `?code=...`, or
   * **just the code value**

   …when the script prompts you.

5. On success, the script will:

   * print your **refresh token** (this is what you paste into the Home Assistant config flow)
   * write a `tokens.json` file (default) containing `access_token`, `refresh_token`, and `expires_at`

### Optional flags

* Verify the tokens by calling `/gateways/` after auth:

```bash
python scripts/ivt_anywhere2_auth.py --verify
```

* Write tokens to a specific file:

```bash
python scripts/ivt_anywhere2_auth.py --out /path/to/tokens.json
```

* Refresh access token later using an existing `tokens.json` (uses the stored refresh token and updates the file):

```bash
python scripts/ivt_anywhere2_auth.py --out tokens.json --refresh-only
```

Home Assistant refreshes and persists its own rotated token after setup. The
helper's token file is not kept in sync with HA and can become stale. Do not use
`--refresh-only` to manage a token already being used by a running HA entry.

### Notes & warnings

* The refresh token is **sensitive**. Treat it like a password.
* Do **not** commit `tokens.json` or your refresh token to GitHub.
* Tokens can be revoked by Bosch at any time, requiring you to generate a new one.
* The helper script relies on Bosch’s current OAuth implementation and may break if the API changes.

---

## Troubleshooting

### No gateway(s) found

* Verify the refresh token is correct and still valid.
* Confirm you can log in to the IVT Anywhere II app and see your system.

### Sensors show `unknown` / `unavailable`

* Recorded data can lag behind real time, especially just after an hour ends.
* Historical recordings are fetched during setup; you do not need to wait a new
  full hour if Bosch already has valid completed-hour data.
* Incomplete reports are retried after one minute. Persistent missing data stays
  unavailable and is not replaced with an estimated zero.
* Check whether the IVT Anywhere II app shows energy history for the same gateway.
  A successful update request does not guarantee that recording payloads or
  completed buckets were returned.

### Energy chart shows the wrong hour or the new source is absent

* Use **IVT \<gateway\> electricity (recorded hours)**, not the native last-hour
  sensor or a monthly total, and remove any duplicate source for the same heater.
* Restart HA after installing the update. The source requires at least one valid
  hourly recording; it is a statistic, not a sensor entity.
* Confirm HA's timezone matches the gateway recordings. Missing hours and
  ambiguous daylight-saving responses are left absent until supported data is available.

### Rate limits / API errors

* This is a cloud API. If Bosch throttles requests, the integration may temporarily fail updates.
* The integration polls every 30 minutes when reports are complete, and retries
  incomplete or failed requests after one minute. There is no scan-interval UI option.

---

## Data & privacy

This integration talks to Bosch’s cloud endpoints over HTTPS. Your Home Assistant instance will send requests that include OAuth tokens. No data is intentionally sent anywhere else.

---

## Support / Issues

### Running the regression tests

```sh
python -m unittest discover -s tests -v
```

Use Python 3.10 or newer. The tests use synthetic recordings and minimal Home
Assistant interface stubs to exercise the actual utility, coordinator, sensor,
and recorded-statistics modules offline. They
do not require credentials, network access, or a running Home Assistant server.

* Bugs and feature requests: use the repository issue tracker.
* Please include:

  * Home Assistant version
  * Integration version
  * Logs (with tokens redacted)
  * Your gateway model (if known)
  * Whether the IVT Anywhere II app shows energy history for that gateway
  * Which source is selected for the heater in the Energy dashboard

Do not attach `tokens.json`, refresh tokens, or unredacted authentication-helper output.

---

## Credits

Reverse engineering and testing made possible by:

* Community Home Assistant efforts
* Bosch thermostat/heat pump reverse-engineering projects
* Real-world testing against IVT Anywhere II
