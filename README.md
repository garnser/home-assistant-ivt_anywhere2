# IVT Anywhere II – Home Assistant Integration

This is a **custom Home Assistant integration** for **IVT / Bosch heat pumps using IVT Anywhere II** (Bosch Pointt cloud API).

It focuses on **energy and utilization data** that is available *after the fact* via Bosch’s cloud recordings API.

---

## ✨ What this integration does

* Connects to the **Bosch Pointt cloud API** used by the IVT Anywhere II mobile app
* Collects **energy usage and heat output data** from your heat pump
* Exposes the data as **Home Assistant sensors**
* Handles **OAuth token refresh automatically**
* Works reliably with **hourly and daily recorded data** (no live scraping)

---

## 📊 Available sensors

### Last complete hour (hourly resolution)

These sensors represent the **last fully completed hour** (not the current ongoing hour):

* Electricity – last complete hour (kWh)
* Compressor electricity – last complete hour (kWh)
* Electric heater electricity – last complete hour (kWh)
* Heat output – last complete hour (kWh)
* COP – last complete hour

Each “last hour” sensor includes an attribute:

```
bucket: "YYYY-MM-DD HH:00"
```

which indicates which hour the value represents.

---

### Month-to-date totals (daily aggregation)

* Electricity – month to date (kWh)
* Heat output – month to date (kWh)
* COP – month to date

These values are calculated by summing **daily buckets** returned by the Bosch API.

---

## ⚠️ Important limitations (by design)

This integration is limited by what Bosch exposes in the Anywhere II cloud:

* ❌ No real-time power (W)
* ❌ No per-minute resolution
* ❌ No guaranteed lifetime (monotonic) counters

### Resolution

* **Hourly** is the finest supported resolution
* Data may be delayed (hourly buckets often appear after the hour is finished)

This is normal and matches the behavior of the official IVT Anywhere II app.

---

## 🔌 Home Assistant Energy Dashboard

The sensors provided are **period-based kWh values**, not lifetime counters.

To use them in the Energy dashboard, you should create `utility_meter` sensors on top of them, for example:

```yaml
utility_meter:
  ivt_electricity_daily:
    source: sensor.ivt_anywhere2_electricity_last_complete_hour
    cycle: daily

  ivt_electricity_monthly:
    source: sensor.ivt_anywhere2_electricity_last_complete_hour
    cycle: monthly
```

---

## 🔐 Authentication model

### Why authentication is manual

IVT Anywhere II uses:

* Bosch SingleKey ID
* OAuth2 Authorization Code flow
* PKCE
* A **mobile-app-only redirect URI**

Because of this, Home Assistant **cannot perform the initial login flow directly**.

---

## ✅ How authentication works

### Step 1: Generate tokens (one-time)

Use the provided helper script (or your existing working script) to authenticate once:

```bash
python scripts/ivt_anywhere2_auth.py --out tokens.json --verify
```

* Open a browser
* Let you log in with your Bosch / IVT account
* Create a `tokens.json` file

Example:

```json
{
  "access_token": "...",
  "refresh_token": "...",
  "expires_at": 1768093925
}
```

You only need the **refresh_token**.

---

### Step 2: Set up the integration in Home Assistant

1. Copy the integration folder to:

   ```
   custom_components/ivt_anywhere2/
   ```

2. Restart Home Assistant

3. Go to:
   **Settings → Devices & Services → Add integration**

4. Select **IVT Anywhere II**

5. Paste the **refresh token** when prompted

6. Select your gateway (usually only one)

That’s it 🎉

The integration will:

* Refresh access tokens automatically
* Retry when Bosch’s API is flaky
* Keep working until Bosch revokes the refresh token

---

## 🔄 Update interval

The integration polls the cloud every **30 minutes** by default.

This is intentional:

* Hourly data does not change more frequently
* Avoids unnecessary load and throttling

---

## 🛠️ Reliability & retries

Bosch’s `/bulk` endpoint is occasionally flaky and may return empty payloads.

To handle this, the integration:

* Retries bulk requests automatically
* Re-requests data if all payloads come back empty
* Fails gracefully without crashing Home Assistant

---

## 🧠 Data interpretation

### Electricity usage

```
Total electricity = compressor + electric heater
```

### COP (Coefficient of Performance)

```
COP = heat output / total electricity
```

All values are calculated from **Wh recordings**, converted to **kWh**.

---

## 🚧 Known gaps / future work

Possible future improvements:

* Optional auth helper bundled with the integration
* Lifetime monotonic energy counters (if Bosch exposes them)
* Separate CH / DHW energy if Bosch enables it
* Configurable polling interval

---

## ⚖️ Disclaimer

This integration:

* Is **not affiliated with Bosch or IVT**
* Uses **undocumented APIs**
* May break if Bosch changes their backend

Use at your own risk.

---

## ❤️ Credits

Reverse engineering and testing made possible by:

* Community Home Assistant efforts
* Bosch thermostat reverse-engineering projects
* Real-world testing against IVT Anywhere II

---

If you want help polishing this into a public HACS-ready integration (naming, icons, translations, options flow), just say the
