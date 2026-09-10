# Ansible Role XOUT

XOUT lifecycle and package deployment automation for the existing HB/NDS inventories.

The repository uses three layers:

1. `xoutctl` / Ansible playbook: environment-level orchestration and operator UI.
2. `mraibo.serviceflow`: strict cross-host service dependency order.
3. `/work/dms/rollout/xmanager.py`: local XOUT package, service and Portal-module operations.

Inventory files remain in their existing INI format for now.

## XOUT service order

Start order:

```text
xout-modeshape
→ xout-activemq-artemis
→ xout-pmc
→ xout-portal
→ xout-web
→ xout-batchsplitter
```

Stop order is the exact reverse. Hosts belonging to the same component group may be transitioned in parallel, but the next component group must wait until the previous group is complete.

XOUT services intentionally remain disabled for boot-time autostart because they require orchestrated startup order. `cups` is not part of the XOUT lifecycle; Portal hosts only verify that it is active and start it when necessary.

## xmanager.py 3.0

`xmanager.py` is the local execution layer and requires Python 3.12 plus standard Linux tools already used by XOUT (`systemctl`, `rpm`, `zypper`, and optionally the JDK dump tools). It uses only the Python standard library.

Existing administrator commands remain available:

```bash
/work/dms/rollout/xmanager.py list
/work/dms/rollout/xmanager.py modules
/work/dms/rollout/xmanager.py updates
/work/dms/rollout/xmanager.py update
/work/dms/rollout/xmanager.py start all
/work/dms/rollout/xmanager.py stop all
/work/dms/rollout/xmanager.py restart all
/work/dms/rollout/xmanager.py dump
/work/dms/rollout/xmanager.py interactive
```

Automation-oriented commands were added:

```bash
xmanager.py package-plan
xmanager.py package-apply
xmanager.py modules-quiesce
xmanager.py modules-restore
xmanager.py ensure-disabled
```

Use `--json --non-interactive` from Ansible.

### Package target semantics

There are deliberately two modes.

**No release file:** every selected component targets the newest version found in `xout-repo`.

```bash
xmanager.py package-plan \
  --package modeshape \
  --package activemq \
  --package pmc \
  --package portal \
  --package web \
  --json
```

**Release file supplied:** only packages present in `[packages]` are changed. Selected packages that are absent from the file become `noop`. This makes component-specific hotfixes safe.

Example `examples/Release_25.10.ini`:

```ini
[release]
name=Release_25.10

[packages]
modeshape=25.10
portal=25.10.1
pmc=25.10
web=25.10.1
activemq=2.40.0.0
```

Then:

```bash
xmanager.py package-plan \
  --package modeshape \
  --package activemq \
  --package pmc \
  --package portal \
  --package web \
  --release-file /path/to/Release_25.10.ini \
  --json
```

JSON release files are also supported:

```json
{
  "release": "Release_25.10",
  "packages": {
    "modeshape": "25.10",
    "portal": "25.10.1",
    "activemq": "2.40.0.0"
  }
}
```

Accepted aliases include `modeshape`, `activemq`, `artemis`, `pmc`, `portal`, `web`, and `batchsplitter` as well as canonical RPM names.

A requested short version such as `25.10.1` resolves to the newest matching full RPM edition such as `25.10.1-3`. If no matching edition exists, the plan fails before package changes.

### Downgrades

Downgrades are blocked by default. A plan that would downgrade reports an error and exits before changing RPMs. Explicitly enable only when required:

```bash
xmanager.py package-plan \
  --package web \
  --release-file old-release.ini \
  --allow-downgrade
```

`package-apply` uses the same guard.

### Package apply does not manage service lifecycle

This is intentional:

```bash
xmanager.py package-apply --package portal --json --non-interactive
```

`package-apply` only applies RPM targets and verifies installed versions. The distributed environment must already be stopped by Ansible/ServiceFlow. After package changes, corresponding XOUT systemd units are enforced as `disabled`.

The legacy standalone `xmanager.py update` still performs a local stop/update/start cycle for direct administration.

### Portal module state

Before an orchestrated Portal shutdown:

```bash
xmanager.py modules-quiesce \
  --state-file /var/tmp/xmanager-portal-modules.json \
  --json
```

The command records only modules that were `RUNNING`, then stops them in the XOUT module priority order.

After Portal is healthy again:

```bash
xmanager.py modules-restore \
  --state-file /var/tmp/xmanager-portal-modules.json \
  --json
```

On successful restore the state file is deleted automatically. On failure it remains available for recovery. `--keep-state` can explicitly preserve it after success.

### Help

```bash
xmanager.py --help
xmanager.py package-plan --help
xmanager.py package-apply --help
xmanager.py modules-quiesce --help
xmanager.py modules-restore --help
```

## ServiceFlow dependency

The new XOUT lifecycle uses `mraibo.serviceflow >= 0.3.0`, which supports parallel transitions of hosts within one logical service while retaining strict service barriers and reverse-order stop.

Recommended installation on the Ansible controller:

```bash
ansible-galaxy collection install mraibo.serviceflow:0.3.0 --force
```

## Tests

The xmanager tests do not require XOUT or zypper and can run on any Python 3.12 system:

```bash
python3.12 -m py_compile xmanager.py
python3.12 -m unittest discover -s tests -v
```

They validate RPM edition ordering, reverse service order, zypper XML parsing, INI/JSON release files, subset semantics, downgrade protection and CLI help.
