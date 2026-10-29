# ansible-storage_setup

Installs and enables boot-time LVM storage provisioning on Red Hat-family hosts
with systemd 235 or newer. Run the role with root privileges and gathered facts.
The role checks that `python3` is Python 3.10 or newer; automated host tests use
CentOS Stream 10 and Python 3.12.

The role installs packages, `/usr/bin/storage-setup`, `storage-setup.service`,
and `/etc/storage-setup.d/`. It **does not configure storage, start the service,
or restart it during image construction**. There are no storage-specific
Ansible variables. Other roles own their configuration fragments and any service
overrides; this role does not edit or remove them.

This is late-boot provisioning, after `local-fs.target`. Do not use it for the
root filesystem or storage needed to reach that target.

## Caller-owned configuration

Run this role before roles that write root-owned `*.conf` files into
`/etc/storage-setup.d/`. Use mode `0644` or stricter and separate filenames for
each owner. Prefer stable `/dev/disk/by-id/` paths.

For example, a database role can own `20-database.conf`:

```ini
[group:database]
name = database
devices = /dev/disk/by-id/database-disk

[volume:database-data]
group = database
volume = data
size = 100%FREE

[filesystem:database-data]
dev = /dev/database/data
fstype = xfs
```

A logging role can independently own `30-logs.conf`:

```ini
[group:logs]
name = logs
devices = /dev/disk/by-id/log-disk
```

Each volume group MUST have one owner and one complete physical-device list.
The list describes desired membership, not disks to append. Changing it can
extend or reduce an existing group. Multiple roles can define different logical
volumes within a group, but they cannot independently redefine that group.

After all consuming roles have written their fragments, validate them without
waiting for disks or running storage commands:

```bash
/usr/bin/storage-setup --validate
```

Ansible callers can run that command in a final task with `changed_when: false`.
The installer deliberately does not validate incomplete configuration midway
through a play.

## Configuration contract

Only filenames ending in `.conf` are loaded. Backup files with other suffixes
are ignored. Matching files that cannot be read or parsed cause failure. An
empty directory is a successful no-op. INI interpolation is disabled, so `%`
characters are literal.

Section names use `kind:label`, where `kind` is one of the following and `label`
contains letters, digits, `_`, `.`, `+`, or `-`. Labels are globally unique within
each kind. Unknown sections, unknown options, duplicate sections/options, and
`[DEFAULT]` values are rejected.

| Kind         | Required options                       | Optional options                                             |
| ------------ | -------------------------------------- | ------------------------------------------------------------ |
| `group`      | `name`, whitespace-separated `devices` | `pesize`: extent size in MiB, default `4`                    |
| `volume`     | `group`, `volume`, `size`              | None                                                         |
| `filesystem` | `dev`, `fstype`                        | `mount`, `user`, `group`, `mode`, `wanted_by`, `required_by` |
| `write`      | `file`, `content`                      | `crlf`: append a newline, default `true`                     |

The option/default table in `files/storage-setup/storage-setup.py` is the runtime
schema. In particular:

- LVM names have 1–128 letters, digits, `+`, `_`, `.`, or `-`; they cannot start
  with `-` or equal `.` or `..`. Reserved logical-volume names are rejected.
- Paths are normalized absolute paths using letters, digits, `_`, `.`, `/`,
  `:`, `+`, or `-`. No whitespace, dot/parent components, or repeated slashes are
  allowed. Device paths are below `/dev/`; mounting `/` is not supported.
- `pesize` is a power of two from 1 to 16384 MiB. Sizes are positive integers
  (MiB by default), integer LVM sizes such as `2g`, or `1`–`100` percent of
  `PVS`, `VG`, or `FREE`. Volumes reference groups defined in the configuration.
- A logical volume, filesystem device, mount path, or write destination has one
  definition. Aliases of the same block device cannot have multiple owners.
  Physical devices cannot also be filesystem targets or volumes created by this
  configuration.
- Filesystem types are simple names such as `xfs`. The corresponding formatter
  is required on the booted host; the role installs XFS tools. Mount-related
  options require `mount`. Callers create mount directories and write-parent
  directories. Existing mount units must match the configured device, path,
  and filesystem type.
- `user` and `group` accept existing account names or numeric IDs. `mode` is a
  three- or four-digit octal value. `wanted_by` and `required_by` are
  whitespace-separated service, target, or mount unit names without escapes.
- Existing logical volumes are not resized. Existing mount units and regular
  write destinations are not overwritten. Mount ownership, mode, and SELinux
  labels are initialized when creating a mount unit, not reconciled on every
  run. Callers own persistent SELinux file-context mappings for custom paths.

## Readiness, execution, and failure

The script first validates **all** fragments. It then waits for all external
physical/filesystem devices together, checks block-device identities, LVM
ownership, signatures, formatters, and output preconditions. Only after that
preflight succeeds does it execute these phases across all fragments:

1. Volume groups.
2. Logical volumes.
3. Filesystems and mounts.
4. Write-once files.

Sections sort by name within each phase. Filenames do not establish dependencies
between phases. When several volumes consume percentages of free space, their
section order matters.

Device waiting reads paths and block-device metadata. It does not run global
LVM activation, trigger udev, or wait for unrelated devices. It retains the
caller's stable path when invoking LVM. No generated device dependency drop-in
is needed; configuration files are the single source of device requirements.

The default wait is 60 seconds for all external devices together. Filesystem
devices created by this run are excluded from that initial wait and get a bounded
wait when their phase starts. Missing devices fail with their paths; existing
non-block devices fail immediately. A missing disk blocks the whole shared run,
not just its group. Device arrival after failure does not trigger an automatic
retry.

The service's `TimeoutStartSec=15min` bounds the whole execution, including
runtime device waits. To change the device-wait policy, use a caller-owned
service drop-in:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/storage-setup --device-timeout 120
```

Adjust `TimeoutStartSec` separately for longer provisioning workloads. A direct
script invocation has bounded device waits but no overall execution timeout.

**Use only disks dedicated to the declared storage.** New PVs require no detected
signatures; existing PVs must be unassigned or belong to the requested group.
The script refuses conflicting filesystem/partition signatures and probe
failures rather than treating them as blank disks. It does not use force-wipe
flags. These checks cannot prove that unrecognized data is absent or that a disk
is safe to erase.

Preflight is not a transaction or a lock. Command failures, device removal,
timeouts, or external storage changes can leave partial results. Do not run
concurrent provisioning or alter the storage configuration while it executes.
Removing a fragment does not delete existing storage, generated units, or files.

Successful execution remains `active (exited)`. Another `start` does not rerun it;
`restart` explicitly does. Editing fragments alone does not rerun provisioning.
Services needing the resulting storage need explicit ordering and failure
dependencies on `storage-setup.service` or the relevant mount unit.

See [design and testing](docs/systemd-testing.md) for invariants, automated checks,
and the disposable-VM acceptance checklist.
