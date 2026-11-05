# Bosch 0.28.2 connection recovery repair

The separate `bosch` custom integration registers its actions only after a
successful gateway connection. When XMPP startup fails, an automation calling
`bosch.send_custom_get` stops with an unknown-action repair before it can reload
the connection. `continue_on_error` does not suppress missing-action errors.

This repair keeps domain actions registered while entries reconnect, reports
offline calls as recoverable runtime errors, resets stale XMPP authentication
flags, closes failed clients and pending connection attempts, and cleans up
callbacks during reload. It also initializes sensor class attributes and returns
`None` for missing endpoint data, preventing repeated sensor exceptions.

It supports **Bosch 0.28.2 with bosch-thermostat-client 0.28.2 only**. The files
are derived from upstream commit `158549b2552bea7356f17a0b4f971713b719f1c7`
([v0.28.2](https://github.com/bosch-thermostat/home-assistant-bosch-custom-component/releases/tag/v0.28.2)).
The original Bosch source and modified copies retain its [Apache 2.0 license](LICENSE).
Review the exact changes in [recovery.patch](recovery.patch).

## Apply or check

Use a checkout of this repository on a machine that can access the HA
configuration directory. The script uses only Python's standard library.
Replace `/path/to/ha-config` with that directory; in HA's terminal add-on it is
usually `/config`.

```sh
python3 scripts/repair_bosch.py --config /path/to/ha-config --check
python3 scripts/repair_bosch.py --config /path/to/ha-config --apply
```

`--check` exits 0 if all four files are patched, 1 if the repair is needed,
and 2 for an unsupported version, unknown local changes or another error.
`--apply` validates every original and replacement checksum before writing,
creates a backup under `.ivt-bosch-recovery-backups`, and prints its directory.
Applying it again does nothing. A write failure restores files already changed.
Unknown local edits are preserved and cause the operation to stop.

Restart Home Assistant after a successful apply. Then verify that the Bosch
integration loads, `bosch.send_custom_get` is present in Developer Tools, and
temperature readings update. A separate watchdog may still need its own
availability guard and reload cooldown. This repair does not change heating
settings, credentials, entity IDs or automation configuration.

## Restore

Use the exact backup directory printed by the installer:

```sh
python3 scripts/repair_bosch.py --config /path/to/ha-config \
  --restore /path/to/ha-config/.ivt-bosch-recovery-backups/BACKUP_ID
```

Restart Home Assistant afterward. Restore refuses to overwrite later local
changes or a newer Bosch version.

## Updates and verification

The changes survive Home Assistant restarts. A HACS update or reinstallation
of **Bosch** may replace them. This repository preserves the repair and installer;
an IVT Anywhere II HACS update does not install or automatically reapply it.
After a Bosch update, run `--check`. For a newer Bosch version, review upstream
changes rather than forcing this version-specific repair over them.

CI tests installation, idempotency, rollback and preservation of local edits.
A separate job checks out the pinned upstream version, applies the installer
to a temporary configuration, and runs nine native Home Assistant regression
tests for registration, offline/live calls, connection recovery, setup cleanup,
sensor defaults and reload behavior. No gateway credentials or production
configuration are used. The source repair was also verified on HA 2026.10.4
with a full restart, a real Bosch reload and fresh live readings.

The repair handles software failure paths; the incident logs identify an XMPP
connection timeout but do not establish whether the original interruption
occurred in Bosch's service or the network.
