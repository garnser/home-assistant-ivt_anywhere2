"""Repair installer tests; synthetic config directories, no live HA changes."""

import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("bosch_repair", ROOT / "scripts/repair_bosch.py")
repair = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repair)


class BoschRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name)
        self.component = self.config / "custom_components/bosch"
        shutil.copytree(ROOT / "tests/fixtures/bosch_0_28_2", self.component)
        self.manifest = self.component / "manifest.json"
        self.manifest.write_text(json.dumps({"domain": "bosch", "version": "0.28.2",
            "requirements": ["bosch-thermostat-client==v0.28.2"]}))

    def snapshot(self):
        return {str(path.relative_to(self.component)): path.read_bytes()
                for path in self.component.rglob("*") if path.is_file()}

    def test_apply_is_idempotent_and_restore_recovers_original_files(self):
        original = self.snapshot()
        backup = repair.apply(self.config)
        _, files = repair.plan(self.config)
        self.assertTrue(all(item["state"] == "patched" for item in files.values()))
        self.assertIsNone(repair.apply(self.config))
        self.assertEqual(len(list((self.config / ".ivt-bosch-recovery-backups").iterdir())), 1)
        repair.restore(self.config, backup)
        self.assertEqual(self.snapshot(), original)
        repair.restore(self.config, backup)
        self.assertEqual(self.snapshot(), original)

    def test_unknown_modification_refuses_before_changing_any_file(self):
        (self.component / "services.py").write_text("# other local fix\n")
        before = self.snapshot()
        with self.assertRaisesRegex(repair.RepairError, "local changes"):
            repair.apply(self.config)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.config / ".ivt-bosch-recovery-backups").exists())

    def test_existing_unrecognized_helper_is_preserved(self):
        helper = self.component / "xmpp_recovery.py"
        helper.write_text("# independently maintained helper\n")
        before = self.snapshot()
        with self.assertRaises(repair.RepairError):
            repair.apply(self.config)
        self.assertEqual(self.snapshot(), before)

    def test_new_bosch_version_is_not_downgraded(self):
        self.manifest.write_text(json.dumps({"domain": "bosch", "version": "0.29.0"}))
        before = self.snapshot()
        with self.assertRaisesRegex(repair.RepairError, "0.28.2 only"):
            repair.apply(self.config)
        self.assertEqual(self.snapshot(), before)

    def test_different_client_dependency_is_refused(self):
        self.manifest.write_text(json.dumps({"domain": "bosch", "version": "0.28.2",
            "requirements": ["bosch-thermostat-client==0.29.0"]}))
        with self.assertRaisesRegex(repair.RepairError, "client 0.28.2"):
            repair.apply(self.config)

    def test_restore_does_not_overwrite_later_local_changes(self):
        backup = repair.apply(self.config)
        (self.component / "services.py").write_text("# newer fix\n")
        before = self.snapshot()
        with self.assertRaisesRegex(repair.RepairError, "changed since"):
            repair.restore(self.config, backup)
        self.assertEqual(self.snapshot(), before)

    def test_tampered_backup_is_refused_before_any_restore(self):
        backup = repair.apply(self.config)
        (backup / "services.py").write_text("# corrupt backup\n")
        before = self.snapshot()
        with self.assertRaisesRegex(repair.RepairError, "checksum mismatch"):
            repair.restore(self.config, backup)
        self.assertEqual(self.snapshot(), before)

    def test_write_failure_rolls_back_already_written_files(self):
        original = self.snapshot()
        real_write = repair.write_file

        def fail_on_services(path, data, mode):
            if path == (self.component / "services.py").resolve():
                raise OSError("disk full")
            real_write(path, data, mode)

        with patch.object(repair, "write_file", side_effect=fail_on_services):
            with self.assertRaisesRegex(OSError, "disk full"):
                repair.apply(self.config)
        self.assertEqual(self.snapshot(), original)


if __name__ == "__main__":
    unittest.main()
