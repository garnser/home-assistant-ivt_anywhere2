# IVT Anywhere II – Home Assistant Integration

A custom Home Assistant integration for **IVT / Bosch heat pumps** that use the **IVT Anywhere II** mobile app (Bosch **Pointt** cloud API).

This integration focuses on **recorded energy & utilization data** (hourly/daily/monthly totals) that Bosch exposes via the cloud recordings endpoints. It is **not** a “live telemetry” integration.

---

## Features

* Connects to the **Bosch Pointt cloud API** used by the IVT Anywhere II app
* Exposes recorded energy data as **Home Assistant sensors**
* Uses **Config Flow** (UI setup)
* Handles **OAuth token refresh** during runtime
* Polls at a sensible interval (data resolution is typically **hourly**)

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
each hourly (`P1H`) bucket as `y / c` kWh and each daily (`P1D`) bucket as
`24 * y / c` kWh. For example, `y=60, c=60` represents 1 kWh for an hourly
bucket; `y=600, c=1440` represents 10 kWh for a daily bucket. This matches the
[FHEM KM200 recording implementation](https://github.com/fhem/fhem-mirror/blob/master/fhem/FHEM/73_km200.pm).

Zero-count buckets are padding or missing data. They are not measured zero
consumption. Missing or invalid readings are unavailable, and electricity/COP
require both compressor and auxiliary-heater data. Month-to-date totals also
require data for every completed day. A genuine measured zero remains valid.

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
keep the same reset marker. Monthly energy sensors use `total_increasing`.
COP uses `measurement`. See Home Assistant's
[periodic totals guidance](https://contributors.home-assistant.io/core/entities/sensor/#choosing-between-total-classes).

---

## Installation

### Option A — HACS (recommended)

1. Open **HACS** in Home Assistant.
2. Go to **Integrations**.
3. Open the **⋮ menu** (top right) → **Custom repositories**.
4. Add this repository URL and select category **Integration**.
5. Find **IVT Anywhere II** in HACS and click **Download**.
6. **Restart Home Assistant**.
7. Go to **Settings → Devices & services → Add integration** and search for **IVT Anywhere II**.

### Option B — Manual install

1. Copy `custom_components/ivt_anywhere2/` into your Home Assistant `config/custom_components/` directory.
2. Restart Home Assistant.
3. Add the integration via **Settings → Devices & services → Add integration**.

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

A helper script is included in the repository: **`ivt_anywhere2_auth.py`**.

### Prerequisites

* **Python 3.9+**
* Python dependency: **httpx**

Install httpx:

```bash
python -m pip install httpx
```

### Step-by-step

1. Run the helper script:

```bash
python ivt_anywhere2_auth.py
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
python ivt_anywhere2_auth.py --verify
```

* Write tokens to a specific file:

```bash
python ivt_anywhere2_auth.py --out /path/to/tokens.json
```

* Refresh access token later using an existing `tokens.json` (uses the stored refresh token and updates the file):

```bash
python ivt_anywhere2_auth.py --out tokens.json --refresh-only
```

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

* Recorded data can lag behind real time (especially for “last complete hour”).
* Wait until at least one full hour has passed after the integration is added.

### Rate limits / API errors

* This is a cloud API. If Bosch throttles requests, the integration may temporarily fail updates.
* Keep the scan interval reasonable (the integration defaults to polling in line with the data resolution).

---

## Data & privacy

This integration talks to Bosch’s cloud endpoints over HTTPS. Your Home Assistant instance will send requests that include OAuth tokens. No data is intentionally sent anywhere else.

---

## Support / Issues

### Running the regression tests

```sh
python -m unittest discover -s tests -v
```

The tests use synthetic recordings and minimal Home Assistant interface stubs
to exercise the actual utility, coordinator, and sensor modules offline. They
do not require credentials, network access, or a running Home Assistant server.

* Bugs and feature requests: use the repository issue tracker.
* Please include:

  * Home Assistant version
  * Integration version
  * Logs (with tokens redacted)
  * Your gateway model (if known)

---

## Credits

Reverse engineering and testing made possible by:

* Community Home Assistant efforts
* Bosch thermostat/heat pump reverse-engineering projects
* Real-world testing against IVT Anywhere II
