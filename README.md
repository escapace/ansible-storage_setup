# ansible-storage_setup

Configures boot-time LVM storage setup on Red Hat-family hosts with systemd 235
or newer.
The role enables `storage-setup.service` but never starts or restarts it during
image construction. Run with root privileges and gathered facts. The booted
host needs udev; normal containers do not provide the required device units.
This is late-boot provisioning, not setup for the root filesystem or filesystems
needed to reach `local-fs.target`.

## Variables

- `storage_setup_volume_group_name` defaults to `default`. It accepts an LVM
  name string of 1–128 letters, digits, `+`, `_`, `.`, or `-`; it cannot start with
  `-` or equal `.` or `..`.
- `storage_setup_physical_device` defaults to `""`. Set it to a string of
  absolute `/dev/` paths reserved for this volume group, separated by spaces or
  tabs with no surrounding whitespace. Paths may contain letters, digits, `_`,
  `.`, `/`, `:`, `+`, and `-`, but not `.` or `..` path components. Prefer stable
  `/dev/disk/by-id/` paths when available.
  An empty string removes the role-owned volume group configuration; `null`
  is invalid.

**Storage setup can overwrite disk contents when the service runs.** Use only
disks dedicated to the requested volume group. Devices must exist on the booted
host, not necessarily on the image builder. Changing the device list can extend
or reduce an existing volume group at the next boot.

```yaml
- name: prepare storage image
  hosts: images
  become: true
  roles:
    - role: ansible-storage_setup
      storage_setup_volume_group_name: data
      storage_setup_physical_device: /dev/vdb
```

The role owns `/etc/filesystems.d/10-volume-group.conf`. Other `*.conf` files in
that directory also feed the boot-time script and remain the caller's
responsibility. Clearing the device variable does not delete existing LVM state
or disable the service. It also removes the role-owned device dependency drop-in,
`/etc/systemd/system/storage-setup.service.d/10-devices.conf`.

The service waits for each configured disk's systemd `.device` unit and checks
that each path names a block device before running. Missing disks fail the start
job after the system manager's device timeout; the 15-minute execution timeout
does not include that dependency wait. Disks in caller-owned configuration need
caller-owned dependencies. Successful setup remains `active (exited)`; another
`start` does not rerun it, but an explicit `restart` does. Execution is not
transactional: failure or timeout can leave partial storage changes.

The unit uses `UMask=0077` for newly created files without an explicit mode.
Callers can use a later-sorting unit drop-in to adjust `TimeoutStartSec` or
`UMask`. See [unit review and test limits](docs/systemd-testing.md) for the
hardening rationale and disposable-disk test procedure.
