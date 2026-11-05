#!/usr/bin/env python3
"""Check, apply or restore the bundled Bosch 0.28.2 recovery repair."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import uuid

BUNDLE = Path(__file__).resolve().parents[1] / "maintenance/bosch_0_28_2"


class RepairError(RuntimeError):
    """The installed files cannot safely be changed by this repair."""


def digest(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def read_optional(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def installation(config: Path) -> Path:
    root = config.resolve() / "custom_components/bosch"
    try:
        manifest = json.loads((root / "manifest.json").read_text())
    except (OSError, ValueError) as err:
        raise RepairError("Cannot read the installed Bosch manifest") from err
    if manifest.get("domain") != "bosch" or manifest.get("version") != "0.28.2":
        raise RepairError("This repair supports Bosch 0.28.2 only; no files changed")
    requirements = manifest.get("requirements", [])
    if not any(item in requirements for item in (
        "bosch-thermostat-client==0.28.2", "bosch-thermostat-client==v0.28.2"
    )):
        raise RepairError("Expected bosch-thermostat-client 0.28.2; no files changed")
    return root


def plan(config: Path):
    root = installation(config)
    manifest = json.loads((BUNDLE / "manifest.json").read_text())
    files = {}
    for name, hashes in manifest["files"].items():
        target = root / name
        if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
            raise RepairError(f"Refusing a symlink outside the component: {name}")
        original = read_optional(target)
        patched = (BUNDLE / "files" / name).read_bytes()
        if digest(patched) != hashes["patched_sha256"]:
            raise RepairError(f"Bundled repair file failed its checksum: {name}")
        current_hash = digest(original)
        if current_hash == hashes["patched_sha256"]:
            state = "patched"
        elif current_hash == hashes["original_sha256"]:
            state = "original"
        else:
            raise RepairError(f"Unrecognized local changes in {name}; no files changed")
        mode = stat.S_IMODE(target.stat().st_mode) if target.exists() else 0o644
        files[name] = {"before": original, "after": patched, "state": state, "mode": mode}
    return root, files


def write_file(path: Path, data: bytes | None, mode: int):
    if data is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def apply(config: Path) -> Path | None:
    root, files = plan(config)
    changes = {name: item for name, item in files.items() if item["state"] != "patched"}
    if not changes:
        return None
    backup = config.resolve() / ".ivt-bosch-recovery-backups" / uuid.uuid4().hex
    backup.mkdir(parents=True, mode=0o700)
    metadata = {"bosch_version": "0.28.2", "files": {}}
    for name, item in changes.items():
        if item["before"] is not None:
            write_file(backup / name, item["before"], 0o600)
        metadata["files"][name] = {
            "before_sha256": digest(item["before"]),
            "after_sha256": digest(item["after"]), "mode": item["mode"],
        }
    write_file(backup / "backup.json", (json.dumps(metadata, indent=2) + "\n").encode(), 0o600)
    written = []
    try:
        for name, item in changes.items():
            # Detect changes made after validation rather than overwriting them.
            if read_optional(root / name) != item["before"]:
                raise RepairError(f"File changed during repair: {name}")
            write_file(root / name, item["after"], item["mode"])
            written.append(name)
    except Exception:
        for name in reversed(written):
            item = changes[name]
            write_file(root / name, item["before"], item["mode"])
        raise
    return backup


def restore(config: Path, backup: Path):
    root = installation(config)
    manifest = json.loads((BUNDLE / "manifest.json").read_text())
    metadata = json.loads((backup / "backup.json").read_text())
    files = {}
    for name, item in metadata["files"].items():
        if name not in manifest["files"] or item["after_sha256"] != manifest["files"][name]["patched_sha256"]:
            raise RepairError(f"Unrecognized backup entry: {name}")
        target = root / name
        if target.is_symlink() or not target.resolve().is_relative_to(root.resolve()):
            raise RepairError(f"Refusing a symlink outside the component: {name}")
        before = read_optional(backup / name)
        if digest(before) != item["before_sha256"]:
            raise RepairError(f"Backup checksum mismatch: {name}")
        current = read_optional(target)
        if digest(current) not in (item["before_sha256"], item["after_sha256"]):
            raise RepairError(f"File changed since the repair: {name}; no files restored")
        files[name] = (before, current, item["mode"])
    written = []
    try:
        for name, (before, current, mode) in files.items():
            if read_optional(root / name) != current:
                raise RepairError(f"File changed during restore: {name}")
            write_file(root / name, before, mode)
            written.append(name)
    except Exception:
        for name in reversed(written):
            _, current, mode = files[name]
            write_file(root / name, current, mode)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="HA configuration directory")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--check", action="store_true")
    action.add_argument("--apply", action="store_true")
    action.add_argument("--restore", type=Path, metavar="BACKUP_DIRECTORY")
    args = parser.parse_args()
    try:
        if args.check:
            _, files = plan(args.config)
            print(json.dumps({name: item["state"] for name, item in files.items()}, indent=2))
            return 0 if all(item["state"] == "patched" for item in files.values()) else 1
        if args.apply:
            backup = apply(args.config)
            if backup is None:
                print("Bosch recovery repair is already applied.")
            else:
                print(f"Repair applied. Backup: {backup}\nRestart Home Assistant to load the repair.")
        else:
            restore(args.config, args.restore)
            print("Backup restored. Restart Home Assistant to load the original code.")
        return 0
    except (RepairError, OSError, ValueError) as err:
        print(f"Repair refused: {err}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
