"""Execute safe script paths; real filesystem tests use only temporary image files."""

import contextlib
import io
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from configparser import ConfigParser
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(
    os.environ.get("STORAGE_SETUP_SCRIPT")
    or Path(__file__).resolve().parent.parent / "storage-setup.py"
).resolve()
storage = types.ModuleType("storage_setup_under_test")
exec(compile(SCRIPT.read_text(), str(SCRIPT), "exec"), storage.__dict__)


def configuration(section: str, **options: str) -> ConfigParser:
    parser = ConfigParser(interpolation=None)
    parser.read_dict({section: options})
    return parser


class ParsingTests(unittest.TestCase):
    def test_whitespace_split(self) -> None:
        self.assertEqual(storage.safe_split("  /dev/a\t/dev/b\n"), ["/dev/a", "/dev/b"])
        self.assertEqual(storage.safe_split(""), [])

    def test_valid_sizes(self) -> None:
        cases = {
            "1": ("1", "L", "m"),
            "128MB": (128, "L", "m"),
            "2g": (2, "L", "g"),
            "100%FREE": ("100%FREE", "l", ""),
            "25%VG": ("25%VG", "l", ""),
        }
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(storage.parse_size(value), expected)

    def test_invalid_sizes(self) -> None:
        for value in (None, "", "-1", "1.5G", "1GiB", "101%FREE", "garbage"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    storage.parse_size(value)
                self.assertEqual(error.exception.code, 1)

    def test_optional_getter_preserves_conversion_errors(self) -> None:
        parser = configuration("group:test", force="invalid")
        self.assertEqual(storage.optional(parser.getint, "group:test", "pesize", 4), 4)
        with self.assertRaises(ValueError):
            storage.optional(parser.getboolean, "group:test", "force", False)

    def test_lvm_query_arguments_and_record_parsing(self) -> None:
        with patch.object(
            storage,
            "check_output",
            return_value="LVM2_VG_NAME=data;LVM2_VG_FREE= 128 \n",
        ) as output:
            records = list(
                storage.query_lvm("vgs", o="vg_name,vg_free", extra=["data"])
            )
        self.assertEqual(records, [{"vg_name": "data", "vg_free": "128"}])
        output.assert_called_once_with(
            [
                "vgs",
                "--nameprefix",
                "--noheadings",
                "--unquoted",
                "-o",
                "vg_name,vg_free",
                "--separator",
                ";",
                "data",
            ]
        )

    def test_free_space_preserves_missing_group_failure(self) -> None:
        with patch.object(storage, "query_lvm", return_value=iter(())):
            with self.assertRaises(TypeError) as error:
                storage.free_space("missing", "m")
        self.assertEqual(str(error.exception), "'NoneType' object is not subscriptable")

    def test_write_without_newline_preserves_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "output"
            parser = configuration(
                "write:test", file=str(target), content="100% literal", crlf="false"
            )
            storage.write_file("write:test", parser)
            self.assertEqual(target.read_bytes(), b"100% literal")
            before = target.stat().st_mtime_ns
            parser.set("write:test", "content", "replacement")
            storage.write_file("write:test", parser)
            self.assertEqual(target.read_bytes(), b"100% literal")
            self.assertEqual(target.stat().st_mtime_ns, before)


@unittest.skipUnless(
    os.environ.get("STORAGE_SETUP_TEST_CONFIG_DIRECTORY") == "/etc/storage-setup.d",
    "CLI tests require the dedicated container's empty configuration tmpfs",
)
class ScriptExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config_directory = Path("/etc/storage-setup.d")
        self.assertEqual(self.config_directory.stat().st_uid, os.getuid())
        self.assertEqual(list(self.config_directory.iterdir()), [])
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)

    def add_configuration(self, name: str, text: str) -> None:
        path = self.config_directory / name
        path.write_text(text)
        self.addCleanup(path.unlink)

    def run_script(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-W", "error", str(SCRIPT)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def test_execute_write_configuration_twice(self) -> None:
        target = Path(self.temporary.name) / "written-by-script"
        self.add_configuration(
            "10-write.conf",
            f"[write:test]\nfile = {target}\ncontent = 100% literal\n",
        )
        first = self.run_script()
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(first.stderr, "")
        self.assertIn("==> ALL DONE!", first.stdout)
        self.assertEqual(target.read_bytes(), b"100% literal\n")
        before = target.stat().st_mtime_ns
        second = self.run_script()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(second.stderr, "")
        self.assertNotIn("WRITE:", second.stdout)
        self.assertEqual(target.read_bytes(), b"100% literal\n")
        self.assertEqual(target.stat().st_mtime_ns, before)

    def test_invalid_size_exits_before_lvm_commands(self) -> None:
        self.add_configuration(
            "10-invalid.conf",
            "[volume:test]\nvolume = data\ngroup = test\nsize = 101%FREE\n",
        )
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn("Size percentage cannot be larger than 100%", result.stderr)
        self.assertNotIn("READ:", result.stdout)
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
            parser = configuration("filesystem:test", dev=str(image), fstype="xfs")
            real_check_call = storage.check_call
            real_check_output = storage.check_output

            def format_image(command: list[str]) -> int:
                self.assertEqual(command, ["mkfs", "-t", "xfs", str(image)])
                self.assertTrue(image.is_file())
                return real_check_call(command, timeout=30)

            def probe_image(command: list[str]) -> str:
                self.assertEqual(
                    command,
                    [
                        "blkid",
                        "-c",
                        "/dev/null",
                        "-o",
                        "value",
                        "-s",
                        "TYPE",
                        str(image),
                    ],
                )
                return real_check_output(command, timeout=10)

            def filesystem_uuid() -> str:
                return subprocess.check_output(
                    ["blkid", "-p", "-o", "value", "-s", "UUID", str(image)],
                    text=True,
                    timeout=10,
                ).strip()

            # No disk discovery/activation: this is a regular file, not a device.
            # Any unexpected command fails the test before it can execute.
            with (
                patch.object(storage, "wait_for_device") as wait,
                patch.object(
                    storage, "check_call", side_effect=format_image
                ) as format_call,
                patch.object(storage, "check_output", side_effect=probe_image) as probe,
            ):
                storage.process_fs("filesystem:test", parser)
                before = filesystem_uuid()
                self.assertTrue(before)
                storage.process_fs("filesystem:test", parser)
                self.assertEqual(filesystem_uuid(), before)
                format_call.assert_called_once()
                self.assertEqual(probe.call_count, 2)
                self.assertEqual(wait.call_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
