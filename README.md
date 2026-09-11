# Ansible Role XOUT

Orchestrated lifecycle, installation and update automation for the existing HB/NDS XOUT environments.

The solution deliberately separates environment orchestration from local host operations:

1. `xoutctl` provides the operator interface and selects an existing INI inventory.
2. `xout_update_role.yml` runs once on the Ansible controller (`localhost`).
3. `role_xout_update` builds the environment topology and orchestrates the workflow.
4. `mraibo.serviceflow` provides strict cross-host service ordering with parallel hosts inside one component group.
5. `/work/dms/rollout/xmanager.py` performs local RPM/Zypper, service-policy and Portal-module operations.

The existing inventory files remain in INI format. Password handling and inventory layout are intentionally unchanged in this version.

## Service lifecycle

Start order:

```text
ModeShape
  ↓
ActiveMQ
  ↓
PMC
  ↓
Portal
  ↓
Web / Tobi
  ↓
Batchsplitter
```

Stop order is the exact reverse:

```text
Batchsplitter
  ↓
Web / Tobi
  ↓
Portal
  ↓
PMC
  ↓
ActiveMQ
  ↓
ModeShape
```

Hosts inside one component group may transition in parallel. The next component group is not processed until the current group has completed its systemd transition and readiness checks.

Important readiness checks:

- ModeShape: new log entry containing `Started ModeShapeServer in`.
- PMC: new log entry containing `WFLYSRV0212: Resuming server`.
- Other components: systemd readiness.

XOUT systemd units intentionally remain `disabled` for boot-time autostart. `cups.service` is not part of the XOUT dependency chain; Portal hosts only ensure that CUPS is running and start it when necessary.

## Controller requirements

- `ansible-core >= 2.15`
- `mraibo.serviceflow 0.3.0`
- access to the selected inventory hosts
- existing `/ansible/XOUT/install.properties` when your environment uses it

Install the pinned collection dependency from this repository:

```bash
ansible-galaxy collection install -r requirements.yml --force
```

Verify:

```bash
ansible-galaxy collection list mraibo.serviceflow
```

Expected release:

```text
mraibo.serviceflow  0.3.0
```

## Managed host requirement

Every XOUT host must contain the new execution layer:

```text
/work/dms/rollout/xmanager.py
```

Verify it before the first orchestration run:

```bash
/work/dms/rollout/xmanager.py --version
```

Expected:

```text
xmanager.py 3.0.0
```

`xmanager.py` uses Python 3.12 and the Python standard library only.

## Operator interface: xoutctl

Make the wrapper executable when needed:

```bash
chmod +x xoutctl
```

Interactive menu:

```bash
./xoutctl
```

Direct commands:

```bash
./xoutctl NDS_TEST status
./xoutctl NDS_TEST plan
./xoutctl NDS_TEST update
./xoutctl NDS_TEST update examples/Release_25.10.ini
./xoutctl NDS_TEST stop
./xoutctl NDS_TEST start
./xoutctl NDS_TEST restart
```

Environment aliases are accepted, for example `NDS_PROD` → `NDS_PRODUKTION` and `HB_MIG` → `HB_MIGRATION`.

For `update` and `install`, `xoutctl` always executes a read-only `plan` first and asks for confirmation before the real operation. `start`, `stop` and `restart` also require confirmation. `--yes` skips the wrapper confirmation; it does not skip the intentional manual UDU confirmation inside Ansible.

## Actions

### status

Reads `xmanager --json list` from all XOUT hosts and prints an aggregated environment view. No lifecycle changes are made.

```bash
./xoutctl NDS_TEST status
```

### plan

Performs package/repository preflight on all XOUT hosts and prints:

- installed and target RPM editions;
- `install`, `upgrade`, `downgrade`, `noop` or preflight errors;
- complete START order;
- complete reverse STOP order;
- whether a manual UDU delivery exists.

No XOUT service, Portal module or Pegasus state is changed.

```bash
./xoutctl NDS_TEST plan
```

### update

Use for an already installed XOUT environment.

Workflow when maintenance is required:

```text
full package preflight
→ Pegasus OFF
→ save + quiesce running Portal modules
→ ServiceFlow reverse STOP
→ manual UDU confirmation when required
→ exact per-host RPM apply in parallel
→ verify/start CUPS on Portal hosts
→ ServiceFlow ordered START + readiness
→ restore previously running Portal modules
→ Pegasus ON
→ enforce XOUT units disabled
→ final status
```

If no RPM change and no UDU delivery are detected, the role does not bounce the XOUT environment.

`update` deliberately refuses an expected component that is not installed. Use `install` while the environment is stopped to add missing packages.

### install

Use for initial installation or to add missing XOUT components. Install mode requires existing XOUT services on the selected hosts to be stopped before RPM changes.

```bash
./xoutctl NDS_TEST install
```

The role applies the package plan, starts the environment in dependency order, enables Pegasus after successful startup, and leaves XOUT units disabled for boot autostart.

### stop

```text
Pegasus OFF
→ save/quiesce running Portal modules
→ reverse ServiceFlow STOP
→ enforce disabled boot policy
```

The Portal state file remains available for a later `start`.

### start

```text
ensure CUPS
→ ordered ServiceFlow START/readiness
→ restore saved Portal module state
→ Pegasus ON
→ enforce disabled boot policy
```

### restart

Performs the full stop sequence followed by the full start sequence.

## Version selection

There are two explicit modes.

### Latest versions

Do not pass a release file:

```bash
./xoutctl NDS_TEST plan
./xoutctl NDS_TEST update
```

For every component assigned to a host, `xmanager` selects the newest edition available in `xout-repo`.

### Fixed release/component versions

Pass an INI or JSON release file. Only packages listed in the file are changed; selected components omitted from the file remain `noop`.

Example:

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

Run:

```bash
./xoutctl NDS_TEST update examples/Release_25.10.ini
```

This matches XOUT deliveries where different components of one release have different RPM versions.

A short version such as `25.10.1` resolves to the newest matching full RPM edition. Before any service is stopped, Ansible freezes the resolved editions into an exact per-host execution file. A newer RPM appearing in the repository during maintenance therefore cannot silently change the running deployment.

## Downgrades

Downgrades are blocked by default.

Explicitly allow one only when intended:

```bash
./xoutctl NDS_TEST update Release_old.ini --allow-downgrade
```

The flag is passed both to planning and execution.

## UDU

When `INSTALLVERSION` is available, the role checks:

```text
/software/Xout/01_Releases/Auslieferung_<INSTALLVERSION>/<KASSE>/udu
```

UDU is not present for every release. If found during `update`, XOUT is first brought down safely and Ansible then pauses so the operator can perform the UDU manually. Press Enter only after the manual step is complete.

The `plan` action reports the UDU requirement without prompting or changing the environment.

## Pegasus

Pegasus is managed directly on the single `[ora_db]` host from the selected inventory. Lifecycle-changing operations require exactly one Oracle host when `xout_manage_pegasus=true`.

- maintenance/stop/restart: `active=0`
- successful start: `active=1`

SQL credentials remain sourced from the existing inventory model.

## Portal module state

Before Portal shutdown, `xmanager modules-quiesce` saves only modules that are actually `RUNNING` to:

```text
/var/tmp/xmanager-portal-modules.json
```

The saved modules are stopped in the XOUT module priority order. After Portal is healthy, `modules-restore` starts only the saved set and deletes the state file on success.

If restore fails, the file is intentionally retained for recovery. A new stop/update refuses to overwrite an unresolved state file.

## Failure behaviour

The workflow is intentionally fail-safe:

- package/repository errors during `plan` abort before service changes;
- a package failure after shutdown leaves the environment DOWN;
- already updated hosts are not rolled back automatically;
- rerunning the same exact target turns already completed packages into `noop`;
- a START/readiness failure triggers reverse ServiceFlow STOP so the environment returns to DOWN;
- Pegasus/module state is retained when recovery still needs to be completed.

There is no automatic RPM rollback.

## Direct Ansible usage

The wrapper is optional. The orchestrator playbook runs on localhost and delegates work to hosts from the selected inventory:

```bash
ansible-playbook \
  -i NDS_TEST_XOUT.ini \
  xout_update_role.yml \
  -e xout_action=plan
```

Latest update:

```bash
ansible-playbook \
  -i NDS_TEST_XOUT.ini \
  xout_update_role.yml \
  -e xout_action=update
```

Fixed release:

```bash
ansible-playbook \
  -i NDS_TEST_XOUT.ini \
  xout_update_role.yml \
  -e xout_action=update \
  -e xout_release_file=/path/to/Release_25.10.ini
```

## xmanager.py 3.0

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

Automation-oriented commands:

```bash
xmanager.py package-plan
xmanager.py package-apply
xmanager.py modules-quiesce
xmanager.py modules-restore
xmanager.py ensure-disabled
```

`package-apply` deliberately does not manage distributed service lifecycle. That responsibility belongs to Ansible + ServiceFlow.

## CI / validation

GitHub Actions validates:

- Python compilation;
- xmanager unit tests;
- CLI help for automation commands;
- `bash -n xoutctl`;
- all six existing INI inventories with `ansible-inventory`;
- `ansible-playbook --syntax-check` for the orchestrator playbook against every inventory.

Local checks:

```bash
python3.12 -m py_compile xmanager.py
python3.12 -m unittest discover -s tests -v
bash -n xoutctl
```

The first safe environment tests should be:

```bash
./xoutctl NDS_TEST status
./xoutctl NDS_TEST plan
```

Only after reviewing the plan should the first real TEST lifecycle/update be executed.
