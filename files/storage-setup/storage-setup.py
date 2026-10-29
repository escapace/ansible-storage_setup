#!/usr/bin/env python3
"""Validate caller-owned fragments, preflight external disks, then provision."""

from __future__ import annotations

import argparse
import grp
import math
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from configparser import ConfigParser
from configparser import Error as ConfigError
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

CONFIG_DIRECTORY = Path("/etc/storage-setup.d")
DEVICE_TIMEOUT = 60.0


class SetupError(Exception):
    """An actionable configuration, readiness, or storage conflict."""


@dataclass(frozen=True)
class Field:
    validate: Callable[[str], object]
    default: str | None = None


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SetupError(message)


def name(value: str) -> None:
    require(
        re.fullmatch(r"[a-zA-Z0-9+_.][a-zA-Z0-9+_.-]{0,127}", value) is not None
        and value not in (".", ".."),
        "expected an LVM name of 1–128 characters, not starting with '-'",
    )


def volume_name(value: str) -> None:
    name(value)
    require(
        not value.startswith(("snapshot", "pvmove"))
        and not any(
            part in value
            for part in (
                "_cdata",
                "_cmeta",
                "_corig",
                "_mimage",
                "_mlog",
                "_pmspare",
                "_rimage",
                "_rmeta",
                "_tdata",
                "_tmeta",
                "_vorigin",
                "_vdata",
                "_vpool",
            )
        ),
        "reserved LVM logical volume name",
    )


def absolute_path(value: str) -> None:
    require(
        re.fullmatch(r"/[a-zA-Z0-9_./:+-]+", value) is not None
        and os.path.normpath(value) == value
        and not value.startswith("//")
        and value != "/",
        "expected a normalized absolute path without whitespace or shell metacharacters",
    )


def device_path(value: str) -> None:
    absolute_path(value)
    require(value.startswith("/dev/"), "expected a path below /dev/")


def devices(value: str) -> None:
    paths = value.split()
    require(bool(paths), "expected at least one physical device")
    for path in paths:
        device_path(path)
    require(len(paths) == len(set(paths)), "duplicate physical device")


def extent_size(value: str) -> None:
    require(
        value.isascii() and value.isdigit(), "expected an integer extent size in MiB"
    )
    size = int(value)
    require(
        1 <= size <= 16384 and size & (size - 1) == 0,
        "extent size must be a power of two from 1 to 16384 MiB",
    )


def parse_size(value: str) -> tuple[str, Literal["-L", "-l"]]:
    match = re.fullmatch(r"([0-9]+)%(PVS|VG|FREE)", value)
    if match:
        require(1 <= int(match[1]) <= 100, "size percentage must be from 1 to 100")
        return value, "-l"
    match = re.fullmatch(r"([0-9]+)([bskmgtpe])b?", value, re.IGNORECASE)
    if match:
        require(int(match[1]) > 0, "size must be positive")
        return match[1] + match[2].lower(), "-L"
    require(
        re.fullmatch(r"[0-9]+", value) is not None and int(value) > 0,
        "expected a positive LVM size or percentage",
    )
    return value + "m", "-L"


def boolean(value: str) -> None:
    require(value.lower() in ConfigParser.BOOLEAN_STATES, "expected a boolean")


def filesystem_type(value: str) -> None:
    require(
        re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", value) is not None,
        "invalid filesystem type",
    )


def account(value: str, *, group: bool = False) -> None:
    if not value:
        return
    if value.isascii() and value.isdigit():
        require(int(value) < 2**32 - 1, "account ID is out of range")
        return
    try:
        if group:
            grp.getgrnam(value)
        else:
            pwd.getpwnam(value)
    except KeyError as error:
        raise SetupError(f"unknown {'group' if group else 'user'}: {value}") from error


def group_account(value: str) -> None:
    account(value, group=True)


def mode(value: str) -> None:
    require(
        not value or re.fullmatch(r"[0-7]{3,4}", value) is not None,
        "expected a three- or four-digit octal mode",
    )


def optional_path(value: str) -> None:
    if value:
        absolute_path(value)


def units(value: str) -> None:
    for unit in value.split():
        require(
            re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_.@:-]*\.(service|target|mount)", unit)
            is not None,
            f"invalid unit name: {unit}",
        )


# Required fields have no default. This is the authoritative option/default table.
SCHEMA = {
    "group": {
        "name": Field(name),
        "devices": Field(devices),
        "pesize": Field(extent_size, "4"),
    },
    "volume": {
        "volume": Field(volume_name),
        "group": Field(name),
        "size": Field(parse_size),
    },
    "filesystem": {
        "dev": Field(device_path),
        "fstype": Field(filesystem_type),
        "mount": Field(optional_path, ""),
        "user": Field(account, ""),
        "group": Field(group_account, ""),
        "mode": Field(mode, ""),
        "wanted_by": Field(units, ""),
        "required_by": Field(units, ""),
    },
    "write": {
        "file": Field(absolute_path),
        "content": Field(str),
        "crlf": Field(boolean, "true"),
    },
}


def sections(config: ConfigParser, kind: str) -> list[str]:
    return sorted(
        section for section in config.sections() if section.startswith(kind + ":")
    )


def volume_devices(config: ConfigParser, section: str) -> set[str]:
    group = config.get(section, "group")
    volume = config.get(section, "volume")
    return {
        f"/dev/{group}/{volume}",
        f"/dev/mapper/{group.replace('-', '--')}-{volume.replace('-', '--')}",
    }


def generated_devices(config: ConfigParser) -> set[str]:
    return {
        path
        for section in sections(config, "volume")
        for path in volume_devices(config, section)
    }


def load_config(directory: Path) -> ConfigParser:
    config = ConfigParser(interpolation=None)
    sources: dict[str, Path] = {}
    for path in sorted(directory.iterdir()):
        if path.suffix != ".conf":
            continue
        fragment = ConfigParser(interpolation=None)
        try:
            require(path.is_file(), "configuration fragment must be a regular file")
            with path.open(encoding="utf-8") as stream:
                fragment.read_file(stream)
            require(not fragment.defaults(), "[DEFAULT] is not supported")
            for section in fragment.sections():
                kind, separator, label = section.partition(":")
                require(
                    separator == ":"
                    and kind in SCHEMA
                    and re.fullmatch(r"[a-zA-Z0-9_.+-]+", label) is not None,
                    f"invalid section: {section}",
                )
                require(
                    section not in sources,
                    f"duplicate section {section}; first defined in {sources.get(section)}",
                )
                fields = SCHEMA[kind]
                require(
                    not (fragment[section].keys() - fields.keys()),
                    f"{section}: unknown options: {sorted(fragment[section].keys() - fields.keys())}",
                )
                config.add_section(section)
                sources[section] = path
                for option, field in fields.items():
                    value = fragment.get(section, option, fallback=field.default)
                    if value is None:
                        raise SetupError(f"{section}: missing {option}")
                    try:
                        field.validate(value)
                    except (SetupError, ValueError) as error:
                        raise SetupError(f"{section}.{option}: {error}") from error
                    config.set(section, option, value)
        except (OSError, UnicodeError, ConfigError, SetupError) as error:
            raise SetupError(f"{path}: {error}") from error

    def unique(keys: Iterable[tuple[str, str]], resource: str) -> None:
        owners: dict[str, str] = {}
        for key, section in keys:
            require(
                key not in owners,
                f"{sources[section]}: {section}: duplicate {resource} {key}; "
                f"also owned by {owners.get(key)}",
            )
            owners[key] = section

    unique(
        ((config.get(s, "name"), s) for s in sections(config, "group")), "volume group"
    )
    unique(
        (
            (f"{config.get(s, 'group')}/{config.get(s, 'volume')}", s)
            for s in sections(config, "volume")
        ),
        "logical volume",
    )
    unique(
        ((config.get(s, "dev"), s) for s in sections(config, "filesystem")),
        "filesystem device",
    )
    unique(
        (
            (os.path.realpath(config.get(s, "mount")), s)
            for s in sections(config, "filesystem")
            if config.get(s, "mount")
        ),
        "mount path",
    )
    unique(
        (
            (os.path.realpath(config.get(s, "file")), s)
            for s in sections(config, "write")
        ),
        "output file",
    )
    unique(
        (
            (device, s)
            for s in sections(config, "group")
            for device in config.get(s, "devices").split()
        ),
        "physical device",
    )
    groups = {config.get(s, "name") for s in sections(config, "group")}
    for section in sections(config, "volume"):
        require(
            config.get(section, "group") in groups,
            f"{sources[section]}: {section}: group must be defined in the configuration",
        )
    physical = {
        device
        for section in sections(config, "group")
        for device in config.get(section, "devices").split()
    }
    require(
        not physical.intersection(generated_devices(config)),
        "physical devices cannot be logical volumes created by this configuration",
    )
    filesystem_devices = {config.get(s, "dev") for s in sections(config, "filesystem")}
    require(
        not physical.intersection(filesystem_devices),
        "a physical device cannot also have a filesystem definition",
    )
    for section in sections(config, "volume"):
        require(
            len(filesystem_devices.intersection(volume_devices(config, section))) <= 1,
            f"{sources[section]}: {section}: multiple filesystem definitions for one logical volume",
        )
    for section in sections(config, "filesystem"):
        if not config.get(section, "mount"):
            require(
                not any(
                    config.get(section, key)
                    for key in ("user", "group", "mode", "wanted_by", "required_by")
                ),
                f"{sources[section]}: {section}: mount options require a mount path",
            )
    return config


def check_output(command: list[str]) -> str:
    print("READ:", command, flush=True)
    return subprocess.check_output(command, text=True, stdin=subprocess.DEVNULL)


def check_call(command: list[str]) -> None:
    print("EXEC:", command, flush=True)
    subprocess.run(command, check=True, stdin=subprocess.DEVNULL)


def query_lvm(command: str, fields: str) -> list[dict[str, str]]:
    output = check_output([command, "--noheadings", "--separator", ";", "-o", fields])
    keys = fields.split(",")
    records = []
    for line in output.splitlines():
        values = [value.strip() for value in line.split(";")]
        require(len(values) == len(keys), f"{command}: malformed LVM output: {line!r}")
        for key, value in zip(keys, values, strict=True):
            require(
                bool(value) or (command == "pvs" and key == "vg_name"),
                f"{command}: empty {key} in LVM output: {line!r}",
            )
        records.append(dict(zip(keys, values, strict=True)))
    return records


def block_identity(path: str) -> int:
    info = os.stat(path)
    require(stat.S_ISBLK(info.st_mode), f"not a block device: {path}")
    return info.st_rdev


def wait_for_devices(paths: Iterable[str], timeout: float) -> None:
    require(
        math.isfinite(timeout) and timeout >= 0,
        "device timeout must be finite and nonnegative",
    )
    pending = set(paths)
    deadline = time.monotonic() + timeout
    while pending:
        for path in sorted(pending):
            try:
                block_identity(path)
            except FileNotFoundError:
                continue
            pending.remove(path)
        if not pending:
            return
        remaining = deadline - time.monotonic()
        require(
            remaining > 0,
            f"timed out waiting for block devices: {', '.join(sorted(pending))}",
        )
        time.sleep(min(1.0, remaining))


def signatures(device: str) -> dict[str, str]:
    # blkid exit 2 means unidentified, not necessarily readable (for example an
    # unbacked device). Establish readability before treating that result as blank.
    try:
        with Path(device).open("rb") as stream:
            header = stream.read(512)
    except OSError as error:
        raise SetupError(f"{device}: cannot read device header: {error}") from error
    require(len(header) == 512, f"{device}: cannot read a complete device sector")
    command = ["blkid", "-p", "-o", "export", device]
    print("READ:", command, flush=True)
    result = subprocess.run(
        command, capture_output=True, text=True, stdin=subprocess.DEVNULL
    )
    if result.returncode == 2 and not result.stdout and not result.stderr:
        return {}
    require(
        result.returncode == 0 and bool(result.stdout) and not result.stderr,
        f"blkid failed for {device} (exit {result.returncode}): {result.stderr.strip()}",
    )
    properties = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        require(
            separator == "=" and key not in properties,
            f"malformed blkid output for {device}: {line!r}",
        )
        properties[key] = value
    return properties


def check_filesystem(device: str, fstype: str) -> bool:
    properties = signatures(device)
    if not properties:
        return False
    require(
        properties.get("TYPE") == fstype
        and properties.get("USAGE") == "filesystem"
        and not any(key.startswith("PT") for key in properties),
        f"{device}: existing signature does not match requested filesystem {fstype}: {properties}",
    )
    return True


def mount_unit(mount: str) -> str:
    unit = check_output(["systemd-escape", "--path", "--suffix=mount", mount]).strip()
    require(
        unit.endswith(".mount") and "/" not in unit and "\n" not in unit,
        "invalid escaped mount unit",
    )
    return unit


def preflight_outputs(config: ConfigParser) -> None:
    write_paths = {
        os.path.realpath(config.get(s, "file")) for s in sections(config, "write")
    }
    for section in sections(config, "write"):
        path = Path(config.get(section, "file"))
        require(
            path.is_file()
            if path.exists() or path.is_symlink()
            else path.parent.is_dir(),
            f"{section}: output must be a regular file or have an existing parent directory: {path}",
        )
    for section in sections(config, "filesystem"):
        formatter = "mkfs." + config.get(section, "fstype")
        require(
            shutil.which(formatter) is not None,
            f"{section}: missing formatter: {formatter}",
        )
        mount = config.get(section, "mount")
        if not mount:
            continue
        require(
            Path(mount).is_dir(), f"{section}: mount directory does not exist: {mount}"
        )
        device = config.get(section, "dev")
        if os.path.ismount(mount):
            source = check_output(
                [
                    "findmnt",
                    "--noheadings",
                    "--raw",
                    "--output",
                    "SOURCE",
                    "--mountpoint",
                    mount,
                ]
            ).strip()
            require(
                block_identity(source) == block_identity(device),
                f"{section}: mount path is occupied by another device: {source}",
            )
        unitfile = Path("/etc/systemd/system") / mount_unit(mount)
        require(
            os.path.realpath(unitfile) not in write_paths,
            f"{section}: mount unit is also a write destination: {unitfile}",
        )
        if unitfile.exists():
            existing = ConfigParser(interpolation=None, strict=False)
            try:
                with unitfile.open(encoding="utf-8") as stream:
                    existing.read_file(stream)
            except (OSError, UnicodeError, ConfigError) as error:
                raise SetupError(f"{unitfile}: {error}") from error
            require(
                existing.get("Mount", "Where", fallback="") == mount
                and existing.get("Mount", "Type", fallback="")
                == config.get(section, "fstype")
                and os.path.realpath(existing.get("Mount", "What", fallback=""))
                == os.path.realpath(device),
                f"{section}: existing mount unit conflicts with configuration: {unitfile}",
            )


def preflight(config: ConfigParser, timeout: float) -> None:
    """Check external inputs without mutation; retain caller-selected stable paths."""
    physical = {
        path
        for section in sections(config, "group")
        for path in config.get(section, "devices").split()
    }
    generated = generated_devices(config)
    filesystem_devices = {config.get(s, "dev") for s in sections(config, "filesystem")}
    wait_for_devices(physical | (filesystem_devices - generated), timeout)
    preflight_outputs(config)
    claims: dict[int, str] = {}
    for section in sections(config, "group"):
        for path in config.get(section, "devices").split():
            identity = block_identity(path)
            require(
                identity not in claims,
                f"{section}: {path} aliases a physical device owned by {claims.get(identity)}",
            )
            claims[identity] = section
    fs_claims: dict[int, str] = {}
    for section in sections(config, "filesystem"):
        device = config.get(section, "dev")
        try:
            identity = block_identity(device)
        except FileNotFoundError:
            require(device in generated, f"{section}: device disappeared: {device}")
            continue
        require(
            identity not in claims,
            f"{section}: filesystem device is also a physical device",
        )
        require(
            identity not in fs_claims,
            f"{section}: filesystem aliases the device owned by {fs_claims.get(identity)}",
        )
        fs_claims[identity] = section
        check_filesystem(device, config.get(section, "fstype"))
    if physical:
        records = query_lvm("pvs", "pv_name,vg_name")
        existing: dict[int, str] = {}
        managed_groups = {config.get(s, "name") for s in sections(config, "group")}
        for record in records:
            try:
                identity = block_identity(record["pv_name"])
            except FileNotFoundError:
                require(
                    record["vg_name"] not in managed_groups,
                    f"missing existing PV in managed group: {record}",
                )
                continue  # Unrelated missing PVs are not managed by this run.
            require(
                identity not in existing,
                f"duplicate PV identity in LVM output: {record}",
            )
            existing[identity] = record["vg_name"]
        for identity, section in claims.items():
            group = config.get(section, "name")
            require(
                existing.get(identity, "") in ("", group),
                f"{section}: physical device belongs to volume group {existing.get(identity)}",
            )
        for path in sorted(physical):
            properties = signatures(path)
            if block_identity(path) in existing:
                require(
                    properties.get("TYPE") == "LVM2_member",
                    f"{path}: PV metadata and disk signature disagree",
                )
            else:
                require(
                    not properties,
                    f"{path}: refusing to overwrite existing signatures: {properties}",
                )


def process_vg(section: str, config: ConfigParser) -> None:
    group = config.get(section, "name")
    paths = config.get(section, "devices").split()
    desired = {block_identity(path): path for path in paths}
    require(
        len(desired) == len(paths),
        f"{section}: physical device identities changed since preflight",
    )
    records = query_lvm("pvs", "pv_name,vg_name")
    current: dict[int, str] = {}
    initialized: set[int] = set()
    for record in records:
        try:
            identity = block_identity(record["pv_name"])
        except FileNotFoundError:
            require(record["vg_name"] != group, f"missing existing PV: {record}")
            continue
        initialized.add(identity)
        if identity in desired:
            require(
                record["vg_name"] in ("", group),
                f"{desired[identity]}: PV ownership changed since preflight",
            )
        if record["vg_name"] == group:
            current[identity] = record["pv_name"]
    groups = {r["vg_name"] for r in query_lvm("vgs", "vg_name")}
    for identity in desired.keys() - initialized:
        device = desired[identity]
        require(not signatures(device), f"{device}: signatures changed since preflight")
    if group not in groups:
        require(
            not current, f"{section}: volume group metadata changed since preflight"
        )
        check_call(
            [
                "vgcreate",
                "-s",
                config.get(section, "pesize") + "m",
                group,
                *sorted(desired.values()),
            ]
        )
        return
    for identity in sorted(desired.keys() - current.keys()):
        device = desired[identity]
        if identity not in initialized:
            check_call(["pvcreate", device])
        check_call(["vgextend", group, device])
    for identity in sorted(current.keys() - desired.keys()):
        check_call(["vgreduce", group, current[identity]])


def process_volume(section: str, config: ConfigParser) -> None:
    group = config.get(section, "group")
    volume = config.get(section, "volume")
    records = query_lvm("lvs", "vg_name,lv_name")
    if any(r["vg_name"] == group and r["lv_name"] == volume for r in records):
        return
    size, option = parse_size(config.get(section, "size"))
    check_call(["lvcreate", "-n", volume, option, size, group])


def restore_labels(mount: str) -> None:
    result = subprocess.run(["selinuxenabled"], stdin=subprocess.DEVNULL, check=False)
    require(
        result.returncode in (0, 1),
        f"selinuxenabled failed with exit {result.returncode}",
    )
    if result.returncode == 0:
        check_call(["restorecon", "-R", mount])


def process_fs(section: str, config: ConfigParser, timeout: float) -> None:
    device = config.get(section, "dev")
    fstype = config.get(section, "fstype")
    wait_for_devices([device], timeout)
    if not check_filesystem(device, fstype):
        check_call(["mkfs", "-t", fstype, device])
    mount = config.get(section, "mount")
    if not mount:
        return
    unit = mount_unit(mount)
    unitfile = Path("/etc/systemd/system") / unit
    if not unitfile.exists():
        if not os.path.ismount(mount):
            check_call(["mount", device, mount, "-t", fstype])
            try:
                restore_labels(mount)
                for option, command in (
                    ("user", "chown"),
                    ("group", "chgrp"),
                    ("mode", "chmod"),
                ):
                    value = config.get(section, option)
                    if value:
                        check_call([command, value, mount])
            finally:
                check_call(["umount", mount])
        wanted_by = " ".join(config.get(section, "wanted_by").split())
        required_by = " ".join(config.get(section, "required_by").split())
        unitfile.write_text(
            f"[Unit]\nBefore={wanted_by} {required_by}\n\n"
            f"[Mount]\nWhat={device}\nWhere={mount}\nType={fstype}\n\n"
            f"[Install]\nWantedBy={wanted_by}\n"
            f"RequiredBy={required_by}\n",
            encoding="utf-8",
        )
        unitfile.chmod(0o644)
        check_call(["systemctl", "daemon-reload"])
    check_call(["systemctl", "start", unit])


def write_file(section: str, config: ConfigParser) -> None:
    path = Path(config.get(section, "file"))
    if path.exists():
        return
    content = config.get(section, "content")
    if config.getboolean(section, "crlf"):
        content += "\n"
    with path.open("x", encoding="utf-8") as stream:
        stream.write(content)


def execute(config: ConfigParser, timeout: float) -> None:
    preflight(config, timeout)
    for section in sections(config, "group"):
        process_vg(section, config)
    for section in sections(config, "volume"):
        process_volume(section, config)
    for section in sections(config, "filesystem"):
        process_fs(section, config, timeout)
    for section in sections(config, "write"):
        write_file(section, config)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--validate",
        action="store_true",
        help="validate fragments without accessing disks",
    )
    parser.add_argument(
        "--device-timeout",
        type=float,
        default=DEVICE_TIMEOUT,
        help="seconds to wait for external disks together, and for each created filesystem device (default: 60)",
    )
    args = parser.parse_args()
    try:
        require(
            math.isfinite(args.device_timeout) and args.device_timeout >= 0,
            "device timeout must be finite and nonnegative",
        )
        config = load_config(CONFIG_DIRECTORY)
        if not args.validate:
            execute(config, args.device_timeout)
        print("==> ALL DONE!", flush=True)
        return 0
    except (SetupError, OSError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
