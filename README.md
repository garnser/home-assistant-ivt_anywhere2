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
