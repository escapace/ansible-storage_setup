#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from configparser import ConfigParser, NoOptionError
from contextlib import closing
from typing import Any, Literal, NoReturn, TypeVar

DMSETUP_CMD = "/usr/sbin/dmsetup"
PVS_CMD = "/sbin/pvs"
VGS_CMD = "/sbin/vgs"
LVS_CMD = "/sbin/lvs"
PVCREATE_CMD = "/sbin/pvcreate"
VGCREATE_CMD = "/sbin/vgcreate"
LVCREATE_CMD = "/sbin/lvcreate"
VGEXTEND_CMD = "/sbin/vgextend"
VGREDUCE_CMD = "/sbin/vgreduce"
LVCONVERT_CMD = "/sbin/lvconvert"

systemd_units: list[str] = []

_Value = TypeVar("_Value")
_Default = TypeVar("_Default")


def fail(*a: str) -> NoReturn:
    sys.stderr.write("ERROR:" + " ".join(a) + "\n")
    sys.exit(1)


def safe_split(s: str) -> list[str]:
    return [_f for _f in re.compile(r"\s+").split(s.strip()) if _f]


def optional(
    call: Callable[[str, str], _Value],
    sec: str,
    opt: str,
    default: _Default,
) -> _Value | _Default:
    try:
        return call(sec, opt)
    except NoOptionError:
        return default


def check_output(c: list[str], *a: Any, **kw: Any) -> str:
    print("READ: ", c)
    return subprocess.check_output(c, *a, **kw, encoding="utf-8")


def check_call(c: list[str], *a: Any, **kw: Any) -> int:
    print("EXEC: ", c)
    return subprocess.check_call(c, *a, **kw)


def mapper_device_name(dm_device: str) -> str:
    dm_name = check_output(
        [DMSETUP_CMD, "info", "-C", "--noheadings", "-o", "name", dm_device]
    )
    return "/dev/mapper/{}".format(dm_name.rstrip())


def query_lvm(
    mod: str, o: str = "all", extra: list[str] | None = None
) -> Iterator[dict[str, str]]:
    query = [
        mod,
        "--nameprefix",
        "--noheadings",
        "--unquoted",
        "-o",
        o,
        "--separator",
        ";",
    ]
    if extra:
        query.extend(extra)
    output = check_output(query)

    def chunk(ch: str) -> tuple[str, str]:
        k, v = ch.split("=")
        return k.replace("LVM2_", "").lower(), v.strip()

    for line in output.splitlines():
        yield dict([chunk(each) for each in line.split(";")])


def lvm_profile_dir() -> str:
    return (
        check_output(["lvm", "config", "config/profile_dir"])
        .splitlines()[0]
        .split("=")[1]
        .replace('"', "")
    )


def pvs() -> Iterator[dict[str, str]]:
    for each in query_lvm(PVS_CMD, o="pv_all,vg_name"):
        if each["pv_name"].startswith("/dev/dm-"):
            each["pv_name"] = mapper_device_name(each["pv_name"])
        yield each


def vgs() -> Iterator[dict[str, str]]:
    return query_lvm(VGS_CMD)


def free_space(vg: str, units: str) -> int:
    volume_group = find_this(
        query_lvm(VGS_CMD, extra=["--nosuffix", "--units", units, vg]), "vg_name", vg
    )
    if volume_group is None:
        # Preserve the existing failure policy for a missing volume group.
        raise TypeError("'NoneType' object is not subscriptable")
    return int(volume_group["vg_free"])


def lvs(vg: str, units: str) -> Iterator[dict[str, str]]:
    return query_lvm(LVS_CMD, extra=["--units", units, vg])


def find_this(
    lst: Iterable[dict[str, str]], key: str, val: str
) -> dict[str, str] | None:
    for each in lst:
        if each[key] == val:
            return each


def device_name(name: str) -> str:
    return check_output(
        [
            "bash",
            "-c",
            "udevadm info --query=property --name={} | grep DEVNAME= | cut -d= -f2-".format(
                name
            ),
        ]
    ).strip()


def process_vg(sec: str, params: ConfigParser) -> None:
    name = params.get(sec, "name")
    _force = optional(params.getboolean, sec, "force", False)
    pesize = optional(params.getint, sec, "pesize", 4)
    vgoptions = safe_split(optional(params.get, sec, "options", ""))

    # split and check for symlinks
    dev_list = set(
        map(device_name, map(os.path.realpath, safe_split(params.get(sec, "devices"))))
    )

    current_devs: set[str] = set()

    for test_dev in dev_list:
        wait_for_device(test_dev)

    # check for device files
    for test_dev in dev_list:
        if not os.path.exists(test_dev):
            fail("Device not found: {}".format(test_dev))

    # check pv for already used devices
    for each in pvs():
        if each["pv_name"] in dev_list and each["vg_name"] != name:
            fail(
                "Device {} is already in {} volume group.".format(
                    each["pv_name"], each["vg_name"]
                )
            )
        if each["vg_name"] == name:
            current_devs.add(os.path.realpath(each["pv_name"]))

    this = find_this(vgs(), "vg_name", name)

    if this:
        devs_to_remove = current_devs - dev_list
        devs_to_add = dev_list - current_devs

        for each in devs_to_add:
            check_call([PVCREATE_CMD, "-ff", "-y", each])
            check_call([VGEXTEND_CMD, "-f", "-y", name, each])
        for each in devs_to_remove:
            check_call([VGREDUCE_CMD, name, each])

    else:
        check_call(
            [VGCREATE_CMD, "-f", "-y", "-Zy"]
            + vgoptions
            + ["-s", str(pesize), name]
            + list(dev_list)
        )


pct_re = re.compile(r"^(\d+)%(PVS|VG|FREE)$")
size_re = re.compile(r"^(\d+)([bskmgtpe])b?$")


def parse_size(size: str | None) -> tuple[int | str, Literal["L", "l"], str]:
    if size:
        m = pct_re.match(size)
        if m:
            if int(m.group(1)) > 100:
                fail("Size percentage cannot be larger than 100%")
            return size, "l", ""
        m = size_re.match(size.lower())
        if m:
            return int(m.group(1)), "L", m.group(2)
        if size.isdigit():
            return size, "L", "m"
        fail("Bad size specification")
    fail("size not specified")


def wait_for_device(dev: str) -> None:
    check_call(["pvscan", "--cache", "-aay"])
    check_call(["vgscan", "--mknodes", "-v"])
    check_call(["vgchange", "-a", "y"])
    check_call(["udevadm", "trigger", "--action=add", "--subsystem-match=block"])
    # check_call(["udevadm", "settle"])

    print("--> Wait for device {}".format(dev))
    for sec in range(0, 60):
        if os.path.exists(dev):
            print("--> Device {} appears in {} seconds".format(dev, sec))
            break
        time.sleep(1)


def process_volume(sec: str, params: ConfigParser) -> None:
    lv = params.get(sec, "volume")
    vg = params.get(sec, "group")
    size: str | int = params.get(sec, "size")
    _force = optional(params.getboolean, sec, "force", False)
    size, size_opt, size_unit = parse_size(size)

    this = find_this(lvs(vg, size_opt == "l" and "m" or size_unit), "lv_name", lv)

    if not this:
        print("--> Create volume {}".format(lv))
        check_call([LVCREATE_CMD, "-n", lv, "-" + size_opt, str(size) + size_unit, vg])
    else:
        print("--> Do nothing. Volume {} already exists".format(lv))


UNIT_TEMPLATE = """[Unit]
Before=local-fs.target {before}

[Mount]
What={what}
Where={where}
Type={type}

[Install]
WantedBy=local-fs.target {wanted_by}
RequiredBy={required_by}
"""


def process_fs(sec: str, params: ConfigParser) -> None:
    dev = params.get(sec, "dev")
    if not params.has_option(sec, "fstype"):
        return

    fstype = params.get(sec, "fstype")

    wait_for_device(dev)
    try:
        exist_fs = check_output(
            ["blkid", "-c", "/dev/null", "-o", "value", "-s", "TYPE", dev]
        ).strip()
    except subprocess.CalledProcessError:
        exist_fs = ""

    # FIXME: should we stop, if it contain FS of different type?
    if exist_fs != fstype:
        print("--> Formatting filesystem {} with {}".format(dev, fstype))
        check_call(["mkfs", "-t", fstype, dev])
    else:
        print("--> Do nothing. Filesystem {} already formatted".format(dev))

    if params.has_option(sec, "mount"):
        print("--> Create mount for {}".format(dev))
        mount = params.get(sec, "mount")
        mountname = mount.lstrip("/").replace("/", "-")
        unit = "{}.mount".format(mountname)
        unitfile = os.path.join("/etc/systemd/system/", unit)

        required_by = safe_split(optional(params.get, sec, "required_by", ""))
        wanted_by = safe_split(optional(params.get, sec, "wanted_by", ""))

        user = optional(params.get, sec, "user", False)
        group = optional(params.get, sec, "group", False)
        mode = optional(params.get, sec, "mode", False)

        if not os.path.exists(unitfile):
            if not os.path.ismount(mount):
                check_call(["mount", dev, mount, "-t", fstype])
                check_call(
                    ["bash", "-c", "selinuxenabled && restorecon -R " + mount, "| true"]
                )

                if isinstance(user, str):
                    check_call(["chown", user, mount])

                if isinstance(group, str):
                    check_call(["chgrp", group, mount])

                if isinstance(mode, str):
                    check_call(["chmod", mode, mount])

                check_call(["umount", mount])
            with closing(open(unitfile, "w")) as f:
                print("--> Writing {}".format(unitfile))
                f.write(
                    UNIT_TEMPLATE.format(
                        what=dev,
                        where=mount,
                        type=fstype,
                        wanted_by=" ".join(wanted_by),
                        required_by=" ".join(required_by),
                        before=" ".join(wanted_by + required_by),
                    )
                )

            check_call(["chmod", "644", unitfile])
            check_call(["systemctl", "daemon-reload"])
        else:
            print("--> Not writing {}, it already exists".format(unitfile))

        # check_call(["systemctl", "enable", unit])
        check_call(["systemctl", "start", unit])
    else:
        print("--> Not mounting {}".format(dev))


def write_file(sec: str, params: ConfigParser) -> None:
    filename = params.get(sec, "file")
    content = params.get(sec, "content")
    crlf = optional(params.getboolean, sec, "crlf", True)

    if not os.path.exists(filename):
        with closing(open(filename, "w")) as f:
            print("WRITE: " + filename)
            f.write(content)
            if crlf:
                f.write("\n")


def iterate_config(
    prefix: str, fun: Callable[[str, ConfigParser], None], cp: ConfigParser
) -> None:
    for each in sorted(cp.sections()):
        if each.startswith(prefix):
            fun(each, cp)


def main() -> None:
    dir = "/etc/storage-setup.d"

    for each in sorted(os.listdir(dir)):
        cp = ConfigParser(interpolation=None)
        fn = os.path.join(dir, each)
        print("==> Processing: " + fn)
        cp.read(fn)
        iterate_config("group", process_vg, cp)
        iterate_config("volume", process_volume, cp)
        iterate_config("filesystem", process_fs, cp)
        iterate_config("write", write_file, cp)

    print("==> ALL DONE!")


if __name__ == "__main__":
    main()
