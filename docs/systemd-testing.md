# Storage setup unit review and testing

## Findings and changes

Research date: 2026-06-08. The Docker image used for validation is CentOS Stream
10 with systemd `257-34.el10`. Upstream links below describe current systemd;
the installed version also accepts the resulting unit without diagnostics. The
role checks for systemd 235 or newer because `LockPersonality` first appeared in 235. That minimum is a directive-availability check, not a claim that every
intervening distribution and storage stack has been runtime-tested.

- Restore normal service dependencies. This is late-boot provisioning, not an
  early-boot service. `DefaultDependencies=no` unnecessarily removed normal
  initialization and shutdown ordering.
- Remove `systemd-udev-settle.service`. An empty global udev queue does not prove
  that a particular disk is present, and waiting for unrelated devices slows boot.
- Generate `Requires=` and `After=` for each configured disk's `.device` unit.
  Use the managed host's `systemd-escape --path --suffix=device`, not string
  substitution. For example, `/dev/disk/by-id/example-one` becomes
  `dev-disk-by\x2did-example\x2done.device`.
- Check each configured path with `ExecStartPre=/usr/bin/test -b …`. This fails
  rather than silently skipping when a path is not a block device. It does not
  establish that the disk is empty, correctly identified, or safe to overwrite.
- Keep `blk-availability.service` as a wanted dependency. On this distribution,
  its shutdown action deactivates block devices. Do not start `dm-event.service`
  merely to wait for a disk; LVM manages its monitoring requirements separately.
- Install `systemd-udev`: the minimal test image lacked `udevadm`, which the
  script invokes. Installing packages does not start provisioning during the
  image build.
- Use `RemainAfterExit=yes`. Successful provisioning remains `active (exited)`;
  another `start` does not repeat it. `restart` explicitly runs it again.
- Bound execution with `TimeoutStartSec=15min`. A hung command otherwise leaves
  this oneshot service activating indefinitely. Operators with longer workloads
  can override this setting with a later-sorting service drop-in. Timeout and
  failure do not roll back partial disk changes.

The generated disk dependencies cover only `storage_setup_physical_device`.
Caller-owned configuration needs its own dependencies. The role removes only
`10-devices.conf` when that input becomes empty; it preserves other drop-ins.

`Requires=` alone does not order startup, hence the matching `After=`. It also
does not continuously enforce device presence after provisioning completes.
`BindsTo=` plus `After=` would additionally stop the service when a device
vanishes. That stronger behavior is not enabled: stopping a provisioning process
mid-write is not a recovery strategy. Neither dependency type prevents physical
removal or makes provisioning transactional.

Device waiting and command execution have separate timeouts. The system manager's
device-job timeout applies while waiting for `.device` units. `TimeoutStartSec`
starts when service execution begins; it does not bound dependency waiting. A
missing disk causes a failed start job without necessarily placing the service
itself in the `failed` state. It does not automatically retry when the disk later
appears.

## Hardening boundary

The unit enables:

- `NoNewPrivileges=yes`, preventing privilege gains through executed programs;
- `LockPersonality=yes`, preventing changes to the process ABI personality;
- `RestrictRealtime=yes`, denying realtime scheduling;
- `SystemCallArchitectures=native`, rejecting non-native syscall ABIs; and
- `UMask=0077`, making newly created files private unless the script explicitly
  chooses permissions. Generated mount units still receive the script's explicit
  `0644` mode.

This is limited hardening, not a sandbox for untrusted configuration. The script
runs as root, modifies storage, and can request work from the system manager.
Root controls all configuration in `/etc/storage-setup.d`. Offline
`systemd-analyze security` still reports broad exposure; its score is not a
compatibility test or proof that storage operations are safe.

Several common recommendations conflict with the current script:

- `PrivateDevices=yes` hides disks and device-mapper control.
- `ProtectKernelTunables=yes` makes sysfs read-only, conflicting with
  `udevadm trigger`.
- `ProtectSystem=strict` without an established write allowlist blocks LVM state,
  generated units, and caller-selected output files and mount points.
- Removing `CAP_SYS_ADMIN` or denying mount syscalls breaks its mount operations.
- `RestrictSUIDSGID=yes` conflicts with caller-selected filesystem modes that
  deliberately set directory SGID bits.

Do not add these settings just to improve a security score. A narrower script
contract and real storage tests are prerequisites for further restrictions.

## What Docker can verify

The default Molecule scenario intentionally has no live systemd manager and no
host disk mappings. It tests:

- installation, boot enablement, convergence and idempotence;
- warning-free `systemd-analyze verify` of the installed unit and its drop-ins;
- escaped device names, dependency ordering, and block-device preconditions;
- removal of role-owned configuration and preservation of caller-owned files;
- invalid inputs and maximum-length volume group names; and
- real Ansible `--check` execution, with injected facts disabled.

Run from the repository:

```bash
uv run ansible-lint
ANSIBLE_INJECT_FACT_VARS=False uv run molecule test
ANSIBLE_INJECT_FACT_VARS=False uv run molecule check
```

Use unique container names when running scenarios from several repositories on
the same Docker daemon. Their generic `instance` names otherwise collide. During
this review, validation used a temporary scenario copy with a unique platform
name and separate Molecule state; repository platform settings were unchanged.

A normal Docker container does not run `systemd-udevd`, so it has no working
udev-backed `.device` units. Passing a loop device with `--device` grants access
to that device; it does not supply udev discovery or reproduce boot ordering.
Running systemd as PID 1 also needs compatible cgroup delegation. A writable bind
mount of the host's entire cgroup hierarchy is not an acceptable shortcut.

Do not run this script in a privileged container on a workstation. Its
`wait_for_device` function invokes global `pvscan`, `vgscan`, `vgchange -a y`,
and block-device udev triggers. Loop and device-mapper devices share the host
kernel. In particular, exposing `/dev/mapper/control` is not isolation of one
volume group. Docker's `--privileged` also exposes all host devices.

## Direct execution without systemd

The independent Python project lives in `files/storage-setup/`, with its own
`pyproject.toml`, `uv.lock`, and tests. Checks use isolated uv environments outside
the repository: the generated filename-lint configuration does not ignore nested
`.venv` directories. The root Python project remains dedicated to Ansible tooling.
The role installs only
`files/storage-setup/storage-setup.py`, not the development files beside it.

`files/storage-setup/tests/storage-setup.py` exercises safe script paths without
a service manager. Build and run the dedicated test image from the repository:

```bash
bash files/storage-setup/tests/run-docker.sh
```

The runner builds and uses the exact image ID, avoiding shared image-tag races.
It runs as UID/GID `65534`, with a read-only root filesystem, no capabilities,
network, host mounts, or device mappings. Writable temporary filesystems and
process, memory, and CPU limits contain the tests. All ten tests pass on the
CentOS Stream 10 image:

- whitespace splitting and valid/invalid size parsing;
- optional getter defaults without swallowing conversion errors;
- LVM query argument construction and record parsing with mocked command output;
- preservation of the existing missing-volume-group exception;
- actual file writing, literal `%` handling, newline control, and preservation
  of existing files;
- execution of the full script against a write-only configuration, twice;
- execution of the full script with an invalid size, which exits before any
  LVM command; and
- real `mkfs -t xfs` and `blkid` on a temporary 512 MiB regular image file. A
  second filesystem action skips formatting and preserves the filesystem UUID.

The formatting test calls `process_fs` directly and replaces only
`wait_for_device`, because regular files need no device discovery. Its command
wrappers allow only the exact image-specific `mkfs` and `blkid` commands and
execute those tools for real. There is no mount request. This proves neither
LVM operation nor end-to-end filesystem provisioning through the unmodified
entry point. No loop device, kernel filesystem mount, udev trigger, or systemd
call runs in these tests.

Run only the parser and direct file-writing tests locally with:

```bash
uv run --isolated --locked --directory files/storage-setup python -W error tests/storage-setup.py
```

Local execution skips the container-only CLI and opt-in formatting tests. Strict
warning checks also cover the production script; an invalid `\s` string escape
was changed to a raw regex string without changing its matching behavior.

## Python checks and behavior-preserving review

Ruff checks imports, Python errors, and missing function annotations, and formats
both Python files. ty checks the production script and test harness. Both target
Python 3.12, matching the managed-host test image; the locked development tools
run under Python 3.14. The nested project's `pyproject.toml` and `uv.lock` pin the
tool versions independently of the normalizer-managed root project.

Run Ruff lint and format checks, ty, and local tests from the repository root.
Each command uses the nested lockfile in an isolated environment, without syncing
or changing the Ansible environment:

```bash
bash files/storage-setup/check.sh
```

For manual uv commands, use `--isolated` rather than creating a nested `.venv`
with `uv sync` or a plain `uv run`. For example, apply formatting from the
repository root with:

```bash
uv run --isolated --locked --directory files/storage-setup ruff format
```

Python checks and the isolated Docker suite run manually; they are not wired
into CI or pre-commit hooks. The nested project survives normalization without
changes to the normalizer.

Functions have parameter and return annotations, including optional lookups,
LVM records, callback types, and size-option literals. The subprocess forwarding
arguments deliberately retain `Any`: narrowing the existing positional/keyword
forwarding contract would be a separate interface change. Ruff permits `Any`
only on starred arguments; no individual lint or type diagnostics are ignored.

A syntax-tree comparison found 21 function bodies unchanged after stripping
annotations and normalizing the unused `force` variable name. The remaining
helper, `free_space`, checks for a missing group explicitly while preserving the
same `TypeError` and message. The new characterization tests passed before and
after typing changes. Unused `force` options are still parsed, preserving their
existing validation effects.

No LVM/discovery/mount behavior was redesigned. Shell command arguments, global
activation, discovery waits, mount-unit naming, and write-once unit behavior
remain unchanged. In particular, the existing broad `blkid` error handling and
its mismatch-to-`mkfs` branch remain unchanged. The execution tests cover blank
and already-XFS images, not every probe failure or existing filesystem type.
Changes to these policies need separate storage tests, not formatter autofixes.

## Disposable-VM acceptance tests

These tests remain unexecuted. Run them in a throwaway VM with a separate kernel,
no host disks, and no valuable data. Prefer the deployment distribution and
systemd version. Docker may run inside that VM for script-only experiments, but
use the VM's system manager and udev for `.device` tests.

First test the unit without running the Python storage code. Add a temporary
`99-test-probe.conf` drop-in inside the VM:

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/touch /run/storage-setup-test-started
```

This replaces only the payload, preserving hardening, disk dependencies, and
`ExecStartPre`. Stop the service, remove the marker, reload the manager, and
configure its device input before each case. Never leave this override in an
image or regard a probe pass as successful storage provisioning.

To prepare one disposable loop disk, run the following only as root inside the
throwaway VM. Create the authorization marker there deliberately first; it is
not a substitute for checking which machine you are operating on.

```bash
set -euo pipefail
systemd-detect-virt --quiet --vm
test -f /run/storage-setup-disposable-vm
image=$(mktemp /var/tmp/storage-setup.XXXXXX.img)
truncate -s 256M "$image"
loop=$(losetup --find --show --nooverlap "$image")
udevadm settle
systemctl is-active "$(systemd-escape --path --suffix=device "$loop")"
printf 'image=%s device=%s\n' "$image" "$loop"
```

`--find` allocation is not atomic. Keep this guest single-purpose and serialize
loop allocation. A backed loop device can become ready to systemd; the tested
distribution's udev rules mark unbacked loops `SYSTEMD_READY=0`.

Verify these distinct cases:

1. **Present:** configure the backed loop, start the probe service, and require
   both the marker and `active (exited)` state.
2. **Delayed:** detach that unused loop, wait until its device unit is inactive,
   start the probe with `--no-block`, and confirm the marker is absent. Reattach
   the same image to the same loop node before the device timeout. Require the
   marker only after the device unit becomes active.
3. **Missing:** leave the loop unbacked. Require a nonzero start-job result and
   no marker after the device timeout. Do not test only `systemctl is-failed`.
4. **Repeated start:** after success, another `start` preserves the invocation;
   an explicit `restart` creates a new invocation. Inspect `InvocationID` and
   the journal rather than relying on command exit status alone.
5. **Invalid type:** independently confirm `/usr/bin/test -b` rejects an absent
   path, a regular file, and a character device. Do not fabricate a `.device`
   unit and claim that it tests udev.
6. **Failure and timeout:** temporarily replace the probe with `/usr/bin/false`
   or a sleep exceeding a short test-only `TimeoutStartSec`. Require reported
   failure and no surviving payload process. Restore production settings.

Before changing storage behavior, remove the probe override and test actual VG creation,
repeat execution, existing-data refusal, and an image reboot. Confirm that
existing PV UUIDs and data remain unchanged on repeated execution. RHEL 10's LVM
`system.devices` policy also matters; inspect it rather than assuming the old
`lvm.conf` filter applies. New `vgcreate` device arguments are normally added to
the devices file automatically.

For the probe-only cases, stop the service, detach exactly the recorded loop,
wait for udev processing, and remove exactly the recorded backing file and probe
override. Never use `losetup --detach-all`. Once real LVM state exists, deactivate
and remove only the test VG/PV before detaching; destroying the entire disposable
VM is the preferred final cleanup.

## Sources

- [systemd service defaults and oneshot timeouts](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)
- [requirement, ordering and job-timeout semantics](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html)
- [udev-backed device units](https://www.freedesktop.org/software/systemd/man/latest/systemd.device.html)
- [why global udev settling is discouraged](https://www.freedesktop.org/software/systemd/man/latest/systemd-udev-settle.service.html)
- [execution and sandbox settings](https://www.freedesktop.org/software/systemd/man/latest/systemd.exec.html)
- [path-to-unit escaping](https://www.freedesktop.org/software/systemd/man/latest/systemd-escape.html)
- [systemd container interface and cgroup requirements](https://systemd.io/CONTAINER_INTERFACE/)
- [Docker device access and privileged containers](https://docs.docker.com/engine/containers/run/)
- [loop-device allocation and teardown](https://man7.org/linux/man-pages/man8/losetup.8.html)
- [RHEL 10 LVM device visibility](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/10/html/configuring_and_managing_logical_volumes/limiting-lvm-device-visibility-and-usage)
