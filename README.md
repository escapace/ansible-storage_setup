lvm role

* installs required software and tools
* enables lvmetad daemon
* creates lvm volume group and adds provided block device to it as physical volume
* reigsers volume group name fact

## Role Variables

Available variables are listed below:

```yaml
lvm_volume_group_name: default
lvm_physical_device: /dev/xvdh
```
