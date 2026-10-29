"""Safe configuration/preflight tests; real formatting uses regular image files only."""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from configparser import ConfigParser
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(
    os.environ.get("STORAGE_SETUP_SCRIPT")
    or Path(__file__).resolve().parent.parent / "storage-setup.py"
).resolve()
spec = importlib.util.spec_from_loader(
    "storage_setup_under_test",
    importlib.machinery.SourceFileLoader("storage_setup_under_test", str(SCRIPT)),
)
if spec is None or spec.loader is None:
    raise RuntimeError(f"cannot import {SCRIPT}")
storage = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = storage
spec.loader.exec_module(storage)

GROUP = "[group:data]\nname=data\ndevices=/dev/vdb\n"
VOLUME = "[volume:logs]\ngroup=data\nvolume=logs\nsize=1g\n"
FILESYSTEM = "[filesystem:logs]\ndev=/dev/data/logs\nfstype=xfs\n"


class ConfigFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        formatter = patch.object(storage.shutil, "which", return_value="/test/mkfs.xfs")
        formatter.start()
        self.addCleanup(formatter.stop)

    def load(self, fragments: dict[str, str]) -> ConfigParser:
        for filename, content in fragments.items():
            (self.directory / filename).write_text(content)
        return storage.load_config(self.directory)


class ConfigurationTests(ConfigFixture):
    def test_multiple_groups_defaults_and_cross_fragment_references(self) -> None:
        config = self.load(
            {
                "10-volume.conf": VOLUME,
                "20-group.conf": GROUP,
                "30-other.conf": "[group:other]\nname=other\ndevices=/dev/vdc /dev/vdd\n",
            }
        )
        self.assertEqual(config.get("group:data", "pesize"), "4")
        self.assertEqual(
            storage.sections(config, "group"), ["group:data", "group:other"]
        )
        self.assertEqual(
            storage.generated_devices(config),
            {"/dev/data/logs", "/dev/mapper/data-logs"},
        )

    def test_empty_directory_and_backup_files_are_noops(self) -> None:
        config = self.load({"10-invalid.conf.bak": "not INI", "notes": "not INI"})
        with (
            patch.object(storage, "check_call") as commands,
            patch.object(storage, "signatures") as probes,
        ):
            storage.execute(config, 0)
        commands.assert_not_called()
        probes.assert_not_called()

    def test_invalid_configuration_is_rejected(self) -> None:
        cases = [
            "not INI",
            "[DEFAULT]\nname=data\n" + GROUP,
            "[groups:data]\nname=data\ndevices=/dev/vdb\n",
            "[group:]\nname=data\ndevices=/dev/vdb\n",
            GROUP + "force=true\n",
            GROUP + "options=--force\n",
            "[group:data]\nname=data\n",
            GROUP.replace("name=data", "name=-data"),
            GROUP.replace("name=data", "name=" + "a" * 129),
            GROUP.replace("/dev/vdb", "/dev/../tmp/disk"),
            GROUP.replace("/dev/vdb", "/dev//vdb"),
            GROUP.replace("/dev/vdb", "/dev/vdb;touch"),
            GROUP.replace("/dev/vdb", "vdb"),
            GROUP.replace("/dev/vdb", ""),
            GROUP.replace("/dev/vdb", "/dev/vdb /dev/vdb"),
            GROUP + "pesize=3\n",
            GROUP + "pesize=0\n",
            GROUP + VOLUME.replace("group=data", "group=missing"),
            GROUP + VOLUME.replace("volume=logs", "volume=snapshot1"),
            GROUP + VOLUME.replace("size=1g", "size=0"),
            GROUP + VOLUME.replace("size=1g", "size=101%FREE"),
            GROUP.replace("/dev/vdb", "/dev/data/logs") + VOLUME,
            GROUP + FILESYSTEM.replace("/dev/data/logs", "/dev/vdb"),
            FILESYSTEM + "user=0\n",
            FILESYSTEM + "fstype=ext4\n",
            FILESYSTEM.replace("fstype=xfs", "fstype=xfs bad"),
            FILESYSTEM + "mount=/\n",
            FILESYSTEM + "mount=/data\nmode=999\n",
            FILESYSTEM + "mount=/data\nwanted_by=--bad.service\n",
            "[write:test]\nfile=relative\ncontent=test\n",
            "[write:test]\nfile=/tmp/test\ncontent=test\ncrlf=perhaps\n",
        ]
        for content in cases:
            with self.subTest(content=content):
                (self.directory / "10.conf").write_text(content)
                with self.assertRaises(storage.SetupError):
                    storage.load_config(self.directory)

    def test_duplicate_resources_are_not_merged(self) -> None:
        pairs = [
            (GROUP, GROUP),
            (GROUP, GROUP.replace("group:data", "group:another")),
            (
                GROUP,
                GROUP.replace("group:data", "group:other").replace(
                    "name=data", "name=other"
                ),
            ),
            (GROUP + VOLUME, VOLUME.replace("volume:logs", "volume:another")),
            (FILESYSTEM, FILESYSTEM.replace("filesystem:logs", "filesystem:another")),
            (
                FILESYSTEM + "mount=/data\n",
                FILESYSTEM.replace("filesystem:logs", "filesystem:other").replace(
                    "/dev/data/logs", "/dev/vdc"
                )
                + "mount=/data\n",
            ),
            (
                "[write:a]\nfile=/tmp/output\ncontent=a\n",
                "[write:b]\nfile=/tmp/output\ncontent=b\n",
            ),
        ]
        for first, second in pairs:
            with self.subTest(second=second):
                (self.directory / "10.conf").write_text(first)
                (self.directory / "20.conf").write_text(second)
                with self.assertRaisesRegex(storage.SetupError, "duplicate"):
                    storage.load_config(self.directory)

    def test_parse_failure_precedes_every_execution_phase(self) -> None:
        (self.directory / "10.conf").write_text(GROUP)
        (self.directory / "20.conf").write_text("not INI")
        with patch.object(storage, "check_call") as commands:
            with self.assertRaisesRegex(storage.SetupError, "20.conf"):
                storage.execute(storage.load_config(self.directory), 0)
        commands.assert_not_called()

    def test_unreadable_fragment_is_not_silently_skipped(self) -> None:
        (self.directory / "10.conf").mkdir()
        with self.assertRaisesRegex(storage.SetupError, "10.conf"):
            storage.load_config(self.directory)

    def test_size_boundaries(self) -> None:
        for value, expected in {
            "1": ("1m", "-L"),
            "128MB": ("128m", "-L"),
            "1%VG": ("1%VG", "-l"),
            "100%FREE": ("100%FREE", "-l"),
        }.items():
            self.assertEqual(storage.parse_size(value), expected)
        for value in ("", "0", "0g", "0%VG", "101%PVS", "-1", "1.5G", "1GiB", "1g\n"):
            with self.subTest(value=value), self.assertRaises(storage.SetupError):
                storage.parse_size(value)


class ReadinessTests(unittest.TestCase):
    def test_delayed_alias_is_waited_on_before_resolution(self) -> None:
        with (
            patch.object(
                storage, "block_identity", side_effect=[FileNotFoundError(), 42]
            ) as identify,
            patch.object(storage.time, "sleep") as sleep,
        ):
            storage.wait_for_devices(["/dev/disk/by-id/late"], 60)
        self.assertEqual(identify.call_args_list[0].args, ("/dev/disk/by-id/late",))
        self.assertEqual(identify.call_count, 2)
        sleep.assert_called_once()

    def test_missing_devices_share_one_deadline_and_report_all_paths(self) -> None:
        with (
            patch.object(storage, "block_identity", side_effect=FileNotFoundError()),
            patch.object(storage.time, "monotonic", side_effect=[0, 0, 1]),
            patch.object(storage.time, "sleep") as sleep,
        ):
            with self.assertRaisesRegex(storage.SetupError, "/dev/a, /dev/b"):
                storage.wait_for_devices(["/dev/b", "/dev/a"], 1)
        sleep.assert_called_once_with(1.0)

    def test_regular_and_character_devices_fail_without_waiting(self) -> None:
        with tempfile.NamedTemporaryFile() as regular:
            for path in (regular.name, "/dev/null"):
                with (
                    self.subTest(path=path),
                    patch.object(storage.time, "sleep") as sleep,
                ):
                    with self.assertRaisesRegex(
                        storage.SetupError, "not a block device"
                    ):
                        storage.wait_for_devices([path], 60)
                    sleep.assert_not_called()

    def test_invalid_timeout_and_stat_errors_propagate(self) -> None:
        for timeout in (-1, float("nan"), float("inf")):
            with self.assertRaises(storage.SetupError):
                storage.wait_for_devices([], timeout)
        with patch.object(
            storage, "block_identity", side_effect=PermissionError("denied")
        ):
            with self.assertRaises(PermissionError):
                storage.wait_for_devices(["/dev/a"], 0)


class PreflightTests(ConfigFixture):
    def test_preflight_waits_for_all_external_devices_not_future_volumes(self) -> None:
        config = self.load(
            {
                "10.conf": GROUP
                + VOLUME
                + FILESYSTEM
                + "[filesystem:external]\ndev=/dev/vdc\nfstype=xfs\n"
            }
        )

        def identity(path: str) -> int:
            if path == "/dev/data/logs":
                raise FileNotFoundError(path)
            return {"/dev/vdb": 1, "/dev/vdc": 2}[path]

        with (
            patch.object(storage, "wait_for_devices") as wait,
            patch.object(storage, "block_identity", side_effect=identity),
            patch.object(storage, "query_lvm", return_value=[]),
            patch.object(storage, "signatures", return_value={}),
        ):
            storage.preflight(config, 12)
        wait.assert_called_once_with({"/dev/vdb", "/dev/vdc"}, 12)

    def test_physical_aliases_in_same_or_different_groups_fail(self) -> None:
        for content in (
            GROUP.replace("/dev/vdb", "/dev/vdb /dev/alias"),
            GROUP + "[group:other]\nname=other\ndevices=/dev/alias\n",
        ):
            with self.subTest(content=content):
                config = self.load({"10.conf": content})
                with (
                    patch.object(storage, "wait_for_devices"),
                    patch.object(storage, "block_identity", return_value=1),
                    patch.object(storage, "check_call") as commands,
                ):
                    with self.assertRaisesRegex(storage.SetupError, "aliases"):
                        storage.execute(config, 0)
                commands.assert_not_called()

    def test_foreign_group_and_existing_signatures_fail_before_changes(self) -> None:
        config = self.load({"10.conf": GROUP})
        cases = [
            ([{"pv_name": "/dev/vdb", "vg_name": "other"}], {}),
            ([], {"TYPE": "xfs", "USAGE": "filesystem"}),
            ([], {"PTTYPE": "gpt"}),
            ([{"pv_name": "/dev/vdb", "vg_name": "data"}], {}),
        ]
        for records, properties in cases:
            with (
                self.subTest(records=records, properties=properties),
                patch.object(storage, "wait_for_devices"),
                patch.object(storage, "block_identity", return_value=1),
                patch.object(storage, "query_lvm", return_value=records),
                patch.object(storage, "signatures", return_value=properties),
                patch.object(storage, "check_call") as commands,
            ):
                with self.assertRaises(storage.SetupError):
                    storage.execute(config, 0)
                commands.assert_not_called()

    def test_existing_and_unassigned_pvs_are_accepted(self) -> None:
        config = self.load({"10.conf": GROUP})
        for group in ("data", ""):
            with (
                self.subTest(group=group),
                patch.object(storage, "wait_for_devices"),
                patch.object(storage, "block_identity", return_value=1),
                patch.object(
                    storage,
                    "query_lvm",
                    return_value=[{"pv_name": "/dev/vdb", "vg_name": group}],
                ),
                patch.object(
                    storage, "signatures", return_value={"TYPE": "LVM2_member"}
                ),
            ):
                storage.preflight(config, 0)

    def test_filesystem_cannot_be_a_pv_or_alias_another_filesystem(self) -> None:
        for content in (
            GROUP + FILESYSTEM.replace("/dev/data/logs", "/dev/alias"),
            FILESYSTEM
            + FILESYSTEM.replace("filesystem:logs", "filesystem:other").replace(
                "/dev/data/logs", "/dev/alias"
            ),
        ):
            config = self.load({"10.conf": content})
            with (
                patch.object(storage, "wait_for_devices"),
                patch.object(storage, "block_identity", return_value=1),
                patch.object(storage, "signatures", return_value={}),
            ):
                with self.assertRaises(storage.SetupError):
                    storage.preflight(config, 0)

    def test_future_lv_aliases_are_rejected_without_accessing_disks(self) -> None:
        with self.assertRaisesRegex(storage.SetupError, "multiple filesystem"):
            self.load(
                {
                    "10.conf": GROUP
                    + VOLUME
                    + FILESYSTEM
                    + FILESYSTEM.replace("filesystem:logs", "filesystem:other").replace(
                        "/dev/data/logs", "/dev/mapper/data-logs"
                    )
                }
            )

    def test_alias_of_existing_pv_does_not_change_group_membership(self) -> None:
        config = self.load({"10.conf": GROUP})
        with (
            patch.object(storage, "block_identity", return_value=1),
            patch.object(
                storage,
                "query_lvm",
                side_effect=[
                    [{"pv_name": "/dev/other-alias", "vg_name": "data"}],
                    [{"vg_name": "data"}],
                ],
            ),
            patch.object(storage, "check_call") as commands,
        ):
            storage.process_vg("group:data", config)
        commands.assert_not_called()

    def test_new_group_and_extension_do_not_force_overwrite(self) -> None:
        config = self.load({"10.conf": GROUP})
        with (
            patch.object(storage, "block_identity", return_value=1),
            patch.object(storage, "query_lvm", side_effect=[[], []]),
            patch.object(storage, "signatures", return_value={}),
            patch.object(storage, "check_call") as commands,
        ):
            storage.process_vg("group:data", config)
        commands.assert_called_once_with(["vgcreate", "-s", "4m", "data", "/dev/vdb"])
        with (
            patch.object(storage, "block_identity", return_value=1),
            patch.object(storage, "query_lvm", side_effect=[[], [{"vg_name": "data"}]]),
            patch.object(storage, "signatures", return_value={}),
            patch.object(storage, "check_call") as commands,
        ):
            storage.process_vg("group:data", config)
        self.assertEqual(
            [call.args[0] for call in commands.call_args_list],
            [["pvcreate", "/dev/vdb"], ["vgextend", "data", "/dev/vdb"]],
        )

    def test_volume_creation_and_repeat(self) -> None:
        config = self.load({"10.conf": GROUP + VOLUME})
        with (
            patch.object(storage, "query_lvm", return_value=[]),
            patch.object(storage, "check_call") as commands,
        ):
            storage.process_volume("volume:logs", config)
        commands.assert_called_once_with(["lvcreate", "-n", "logs", "-L", "1g", "data"])
        with (
            patch.object(
                storage,
                "query_lvm",
                return_value=[{"vg_name": "data", "lv_name": "logs"}],
            ),
            patch.object(storage, "check_call") as commands,
        ):
            storage.process_volume("volume:logs", config)
        commands.assert_not_called()

    def test_missing_formatter_fails_before_changes(self) -> None:
        config = self.load({"10.conf": FILESYSTEM})
        with (
            patch.object(storage.shutil, "which", return_value=None),
            patch.object(storage, "wait_for_devices"),
            patch.object(storage, "check_call") as commands,
        ):
            with self.assertRaisesRegex(storage.SetupError, "missing formatter"):
                storage.execute(config, 0)
        commands.assert_not_called()

    def test_output_preconditions_fail_before_storage_commands(self) -> None:
        missing = self.directory / "missing" / "output"
        config = self.load(
            {"10.conf": GROUP + f"[write:test]\nfile={missing}\ncontent=test\n"}
        )
        with (
            patch.object(storage, "wait_for_devices"),
            patch.object(storage, "check_call") as commands,
        ):
            with self.assertRaisesRegex(storage.SetupError, "existing parent"):
                storage.execute(config, 0)
        commands.assert_not_called()

    def test_existing_mount_unit_must_match_configuration(self) -> None:
        mount = self.directory / "mount"
        mount.mkdir()
        unitfile = self.directory / "test.mount"
        unitfile.write_text(f"[Mount]\nWhat=/dev/wrong\nWhere={mount}\nType=xfs\n")
        config = self.load({"10.conf": FILESYSTEM + f"mount={mount}\n"})
        with patch.object(storage, "mount_unit", return_value=str(unitfile)):
            with self.assertRaisesRegex(
                storage.SetupError, "existing mount unit conflicts"
            ):
                storage.preflight_outputs(config)

    def test_execution_orders_phases_globally(self) -> None:
        config = self.load(
            {
                "10.conf": VOLUME + FILESYSTEM,
                "20.conf": GROUP,
                "30.conf": "[write:done]\nfile=/tmp/done\ncontent=done\n",
            }
        )
        events = []
        with contextlib.ExitStack() as stack:
            for function in (
                "preflight",
                "process_vg",
                "process_volume",
                "process_fs",
                "write_file",
            ):
                stack.enter_context(
                    patch.object(
                        storage,
                        function,
                        side_effect=lambda *args, f=function: events.append(f),
                    )
                )
            storage.execute(config, 0)
        self.assertEqual(
            events,
            ["preflight", "process_vg", "process_volume", "process_fs", "write_file"],
        )


class CommandTests(unittest.TestCase):
    def test_selinux_enabled_disabled_and_probe_failure(self) -> None:
        for code in (0, 1, 2):
            with (
                self.subTest(code=code),
                patch.object(
                    storage.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], code),
                ),
                patch.object(storage, "check_call") as commands,
            ):
                if code == 2:
                    with self.assertRaises(storage.SetupError):
                        storage.restore_labels("/data")
                else:
                    storage.restore_labels("/data")
                if code == 0:
                    commands.assert_called_once_with(["restorecon", "-R", "/data"])
                else:
                    commands.assert_not_called()

    def test_mount_initialization_failure_still_unmounts(self) -> None:
        config = ConfigParser(interpolation=None)
        config.read_dict(
            {"filesystem:test": {"dev": "/dev/test", "fstype": "xfs", "mount": "/data"}}
        )
        with (
            patch.object(storage, "wait_for_devices"),
            patch.object(storage, "check_filesystem", return_value=True),
            patch.object(storage, "mount_unit", return_value="data.mount"),
            patch.object(storage.Path, "exists", return_value=False),
            patch.object(storage.os.path, "ismount", return_value=False),
            patch.object(
                storage,
                "restore_labels",
                side_effect=storage.SetupError("relabel failed"),
            ),
            patch.object(storage, "check_call") as commands,
        ):
            with self.assertRaisesRegex(storage.SetupError, "relabel failed"):
                storage.process_fs("filesystem:test", config, 0)
        self.assertEqual(
            [call.args[0] for call in commands.call_args_list],
            [["mount", "/dev/test", "/data", "-t", "xfs"], ["umount", "/data"]],
        )

    def test_blkid_failure_signals_are_not_treated_as_blank_disks(self) -> None:
        with tempfile.NamedTemporaryFile() as device:
            device.write(b"\0" * 512)
            device.flush()
            for code, output, error in (
                (2, "", "I/O error"),
                (4, "", ""),
                (8, "", ""),
                (0, "", ""),
                (0, "malformed", ""),
            ):
                with (
                    self.subTest(code=code, output=output),
                    patch.object(
                        storage.subprocess,
                        "run",
                        return_value=subprocess.CompletedProcess(
                            [], code, output, error
                        ),
                    ),
                ):
                    with self.assertRaises(storage.SetupError):
                        storage.signatures(device.name)
            with patch.object(
                storage.subprocess,
                "run",
                return_value=subprocess.CompletedProcess([], 2, "", ""),
            ):
                self.assertEqual(storage.signatures(device.name), {})

    def test_unreadable_or_short_device_is_not_treated_as_blank(self) -> None:
        with patch.object(storage.subprocess, "run") as probe:
            with self.assertRaisesRegex(storage.SetupError, "complete device sector"):
                storage.signatures("/dev/null")
        probe.assert_not_called()
        with patch.object(storage.Path, "open", side_effect=OSError("I/O error")):
            with self.assertRaisesRegex(storage.SetupError, "/dev/test: cannot read"):
                storage.signatures("/dev/test")

    def test_filesystem_mismatch_and_partition_tables_are_refused(self) -> None:
        for properties in (
            {"TYPE": "ext4", "USAGE": "filesystem"},
            {"TYPE": "xfs", "USAGE": "filesystem", "PTTYPE": "gpt"},
            {"TYPE": "LVM2_member"},
        ):
            with patch.object(storage, "signatures", return_value=properties):
                with self.assertRaises(storage.SetupError):
                    storage.check_filesystem("/dev/test", "xfs")
        with patch.object(
            storage, "signatures", return_value={"TYPE": "xfs", "USAGE": "filesystem"}
        ):
            self.assertTrue(storage.check_filesystem("/dev/test", "xfs"))

    def test_lvm_output_shape_is_checked(self) -> None:
        with patch.object(storage, "check_output", return_value=" /dev/vdb ; data\n"):
            self.assertEqual(
                storage.query_lvm("pvs", "pv_name,vg_name"),
                [{"pv_name": "/dev/vdb", "vg_name": "data"}],
            )
        with patch.object(storage, "check_output", return_value="bad\n"):
            with self.assertRaises(storage.SetupError):
                storage.query_lvm("pvs", "pv_name,vg_name")

    def test_write_once_preserves_existing_content_and_literal_percent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = ConfigParser(interpolation=None)
            path = Path(directory) / "output"
            config.read_dict(
                {
                    "write:test": {
                        "file": str(path),
                        "content": "100% literal",
                        "crlf": "false",
                    }
                }
            )
            storage.write_file("write:test", config)
            self.assertEqual(path.read_bytes(), b"100% literal")
            config.set("write:test", "content", "replacement")
            storage.write_file("write:test", config)
            self.assertEqual(path.read_bytes(), b"100% literal")


@unittest.skipUnless(
    os.environ.get("STORAGE_SETUP_TEST_CONFIG_DIRECTORY") == "/etc/storage-setup.d",
    "CLI tests require the dedicated container's empty configuration tmpfs",
)
class ScriptExecutionTests(ConfigFixture):
    def setUp(self) -> None:
        super().setUp()
        self.directory = Path("/etc/storage-setup.d")
        self.assertEqual(list(self.directory.iterdir()), [])

    def run_script(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-W", "error", str(SCRIPT), *arguments],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def add_fragment(self, text: str) -> None:
        path = self.directory / "10-test.conf"
        path.write_text(text)
        self.addCleanup(path.unlink)

    def test_cli_writes_twice_and_ignores_backup(self) -> None:
        target = Path(self.temporary.name) / "written"
        self.add_fragment(f"[write:test]\nfile={target}\ncontent=100% literal\n")
        backup = self.directory / "20.conf.bak"
        backup.write_text("not INI")
        self.addCleanup(backup.unlink)
        for _ in range(2):
            result = self.run_script()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            self.assertEqual(target.read_bytes(), b"100% literal\n")

    def test_cli_validate_never_accesses_devices(self) -> None:
        self.add_fragment(GROUP + VOLUME + FILESYSTEM)
        result = self.run_script("--validate")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("READ:", result.stdout)
        self.assertNotIn("EXEC:", result.stdout)

    def test_cli_invalid_later_section_prevents_write(self) -> None:
        target = Path(self.temporary.name) / "must-not-exist"
        self.add_fragment(
            f"[write:test]\nfile={target}\ncontent=no\n"
            + GROUP
            + VOLUME.replace("size=1g", "size=0")
        )
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn("volume:logs.size", result.stderr)
        self.assertFalse(target.exists())

    def test_cli_missing_device_fails_before_commands(self) -> None:
        self.add_fragment(GROUP.replace("/dev/vdb", "/dev/storage-setup-missing"))
        result = self.run_script("--device-timeout", "0")
        self.assertEqual(result.returncode, 1)
        self.assertIn("/dev/storage-setup-missing", result.stderr)
        self.assertNotIn("EXEC:", result.stdout)


@unittest.skipUnless(
    os.environ.get("STORAGE_SETUP_TEST_IMAGE_FORMATTING") == "1",
    "real formatting is opt-in and uses temporary regular image files only",
)
class FilesystemExecutionTests(unittest.TestCase):
    def test_real_xfs_formatting_and_repeat_preserves_uuid(self) -> None:
        for command in ("mkfs", "mkfs.xfs", "blkid"):
            self.assertIsNotNone(shutil.which(command), f"missing {command}")
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "filesystem.img"
            with image.open("wb") as output:
                output.truncate(512 * 1024 * 1024)
            self.assertTrue(image.is_file())
            self.assertFalse(image.is_block_device())
            config = ConfigParser(interpolation=None)
            config.read_dict(
                {"filesystem:test": {"dev": str(image), "fstype": "xfs", "mount": ""}}
            )
            real_check_call = storage.check_call

            def format_image(command: list[str]) -> None:
                self.assertEqual(command, ["mkfs", "-t", "xfs", str(image)])
                real_check_call(command)

            def filesystem_uuid() -> str:
                return subprocess.check_output(
                    ["blkid", "-p", "-o", "value", "-s", "UUID", str(image)],
                    text=True,
                    timeout=10,
                ).strip()

            with (
                patch.object(storage, "wait_for_devices"),
                patch.object(
                    storage, "check_call", side_effect=format_image
                ) as formats,
            ):
                storage.process_fs("filesystem:test", config, 0)
                before = filesystem_uuid()
                self.assertTrue(before)
                storage.process_fs("filesystem:test", config, 0)
                self.assertEqual(filesystem_uuid(), before)
                formats.assert_called_once()


if __name__ == "__main__":
    unittest.main(verbosity=2)
