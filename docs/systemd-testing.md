# Storage setup design and testing

## Ownership and processing boundaries

The Ansible role owns installation and boot enablement, not storage declarations.
Caller roles own root-controlled `/etc/storage-setup.d/*.conf` fragments. The
runtime reads those fragments directly; there is no Ansible device list, generated
device drop-in, or systemd generator to keep synchronized.

The implementation separates four responsibilities:

| Boundary                    | Invariants                                                                                                                                                                                 | Failure behavior                                                         |
| --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------ |
| Load and schema validation  | Every matching file is readable INI; exact supported sections/options; required values and scalar constraints hold                                                                         | Fail with file/section/option context before waiting or storage commands |
| Configuration relationships | One definition per resource; complete VG membership; declared groups for volumes; no PV/filesystem overlap or duplicate future-LV aliases                                                  | Reject conflicting declarations rather than merge or select a winner     |
| Host preflight              | All external devices are block devices; device identities have one owner; existing PV ownership and signatures agree; existing filesystems match; formatters and output preconditions hold | Fail before storage modification; report missing paths and conflicts     |
| Execution                   | All groups precede volumes, then filesystems/mounts, then writes; commands report failures; created mounts are unmounted if initialization fails                                           | Stop on failure, without claiming rollback                               |

`SCHEMA` in the runtime is the authoritative supported-option/default table.
Field validators are shared with execution conversions where needed, such as LVM
size parsing. Configuration stays unchanged after validation. Device identity is
the kernel block-device number, not a pathname spelling; LVM receives the original
caller-selected path, preserving stable aliases.

Before interpreting `blkid` exit status 2 as an unidentified disk, the runtime
also requires a readable, complete 512-byte sector. An unidentified result alone
is not evidence of readable storage: a safe `/dev/null` probe also returned exit
2 with empty stdout and stderr. Read failures and short reads fail preflight.

Execution rechecks PV ownership and new-device signatures before modifying each
group. This narrows races but does not lock storage against hot removal, alias
changes, or another provisioner. Run only one provisioner and keep declarations
stable during execution.

The single service deliberately has one failure boundary: a missing disk or
invalid fragment blocks all groups before mutation. Separate per-group services
would require explicit cross-service dependencies for volumes, filesystems, and
consumers; they are not needed for the current shared-run contract.

## Service lifecycle and hardening

The unit retains normal systemd dependencies, runs after `local-fs.target`, and is
enabled for `multi-user.target`. This is not root or early-boot provisioning.
`blk-availability.service` remains a wanted dependency for the distribution's
block-device shutdown handling; it is not treated as proof of disk readiness.

Readiness now belongs to the script. One monotonic deadline covers the initial
set of external disks, rather than a separate timeout per missing disk. A path
must name a block device. The wait neither resolves an absent by-id link through
udev nor invokes `pvscan`, `vgscan`, global `vgchange`, udev triggers, or global
udev settling. Future logical volumes are not prerequisites for creating their
own groups. Their filesystem phase waits for the resulting device nodes.

`TimeoutStartSec=15min` now includes runtime device waiting. It also bounds hung
subprocesses when run through the service. Operators can override the service
command's `--device-timeout` and the overall service timeout independently.
A direct script invocation has no overall command-execution timeout.

`RemainAfterExit=yes` preserves successful execution as `active (exited)`.
Neither another `start` nor changing a fragment reruns it. An explicit `restart`
does. Missing disks do not schedule automatic retries. Stopping or timing out
provisioning does not restore previous disk contents or membership.

The unchanged unit enables `UMask=0077`, `NoNewPrivileges=yes`,
`LockPersonality=yes`, `RestrictRealtime=yes`, and
`SystemCallArchitectures=native`. Generated mount units receive explicit `0644`
permissions. The role checks systemd 235 or newer for these directives; automated
host tests exercise CentOS Stream 10, not every intervening release.

This is not a sandbox for untrusted fragments. The root process can modify disks,
write caller-selected paths, mount filesystems, and ask systemd to start units.
Do not use `PrivateDevices=yes` or remove required mount/storage privileges merely
to improve a hardening score. SELinux file-context mappings for application paths
remain caller-owned; mount initialization applies the existing mappings with
`restorecon` when SELinux is enabled and reports failures.

## Automated checks

Run from the repository root:

```bash
bash files/storage-setup/check.sh
bash files/storage-setup/tests/run-docker.sh
uv run ansible-lint --offline
ANSIBLE_INJECT_FACT_VARS=False uv run molecule test
ANSIBLE_INJECT_FACT_VARS=False uv run molecule check
```

The independent project in `files/storage-setup/` pins Ruff and ty separately
from Ansible tooling. Checks target Python 3.12, matching the CentOS test image.
The runtime uses Python 3.10-or-newer language/library features. Use isolated uv
environments rather than creating a nested `.venv`, which the repository's
filename linter does not ignore.

The Python suite covers:

- valid multi-group fragments, defaults, cross-file references, backup exclusion,
  and an empty-directory no-op;
- unreadable/malformed fragments, invalid names/paths/sizes/options, unknown
  sections, duplicate resources, and errors late in the configuration;
- delayed aliases, one aggregate device deadline, missing devices, non-block
  paths, invalid timeouts, and permission failures;
- physical/filesystem aliases, foreign or unassigned PV ownership, signature
  mismatches, partition tables, missing formatters, and conflicting output paths;
- global execution phases, non-forced group creation/extension, existing-PV alias
  idempotence, logical-volume creation/repeat behavior, and write-once files;
- sector readability, explicit `blkid` failure handling, SELinux probe outcomes, and unmount cleanup
  when mount initialization fails;
- the installed extensionless CLI's validation-only mode, missing-device failure,
  rejection before writing, and repeated write-only execution; and
- real XFS formatting on a temporary 512 MiB **regular image file**, with UUID
  preservation on the second invocation.

Disk identity, LVM commands, and mounts are mocked in unit tests. CLI tests run
only in the dedicated container's empty configuration tmpfs. The formatting test
bypasses block-device readiness because its target is a regular file; it does
not prove end-to-end block-device provisioning.

The Docker runner uses the exact built image ID, UID/GID `65534`, a read-only
root filesystem, no capabilities, no network, no host mounts or device mappings,
and bounded temporary storage, processes, memory, and CPUs. It never receives
`/dev/mapper/control` or host disks.

Molecule tests installation, boot enablement, zero-change convergence,
warning-free `systemd-analyze verify`, multi-group `--validate`, caller fragment
and drop-in preservation, and real Ansible check mode. It does not run a systemd
manager or perform LVM operations. The scenario uses a role-specific container
name to avoid collisions with other repositories' generic `instance` containers.

An online ansible-lint run exposed an upstream Python `ResourceWarning` during
HTTP cache cleanup. The offline invocation avoids that network path without
filtering diagnostics; it is sufficient because dependencies are already locked
and installed. Missing optional Molecule hooks are not scheduled, so an absent
prepare/cleanup/side-effect playbook is not reported as a skipped test.

## Disposable-VM acceptance checklist

These tests remain the operator's responsibility. Use a throwaway VM with a
separate kernel, no valuable data, and only disposable test disks. Do not run
storage tests in a privileged container on a workstation: loop and device-mapper
objects share its host kernel. Prefer the deployment distribution, storage
stack, and systemd version.

Test the actual script and service; replacing `ExecStart` with a marker command
would bypass the runtime readiness logic and no longer test delayed disks.

1. **Two present groups:** configure distinct dedicated disks in independently
   owned fragments. Include logical volumes and filesystems split across files
   whose filenames sort before their groups. Require correct VG membership,
   successful mounts where requested, and `active (exited)` service state.
2. **Delayed stable alias:** start with one configured disk absent. Attach it
   before the configured runtime deadline. Require no storage modifications in
   either group before all external disks are ready, then successful setup.
3. **Missing disk:** leave one disk absent past the deadline. Require failed
   service execution, a journal message naming the missing path, and unchanged
   storage in the other group. Late arrival alone must not rerun provisioning.
4. **Invalid type and configuration:** use an absent path, a character device,
   malformed later fragment, duplicate VG, and two aliases of one PV. Require
   failure before disk changes; non-block paths must not consume the wait budget.
5. **Existing data and ownership:** test existing filesystems, partition tables,
   foreign VGs, an unassigned PV, and an existing matching VG/filesystem. Refusal
   cases must preserve signatures and data. Supported repeat cases must preserve
   PV/filesystem UUIDs. Confirm LVM's devices-file policy exposes intended disks.
6. **Membership changes:** extend a group using a blank disk, then request removal
   of an unused PV. Verify that removal of an in-use PV fails safely and does not
   imply automatic data migration. No force-wipe option is supported.
7. **Repeated execution and reboot:** verify that another `start` does not rerun
   an exited successful service, while `restart` does. Reboot the built image;
   verify activation, UUID/data preservation, mounts, SELinux labels, and ordering
   of application consumers.
8. **Execution failure and timeout:** induce a controlled command failure or
   short test-only service timeout. Require reported failure and no surviving
   payload. Inspect partial state; do not assume rollback. Restore production
   settings before the reboot test.

Record disk identities and backing files before each test. Clean up only those
resources; never use global loop detach or VG deactivation commands. Destroying
the entire disposable VM is the preferred final cleanup.
