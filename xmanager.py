#!/usr/bin/env python3.12
from __future__ import annotations

import argparse
import configparser
import datetime as dt
import fcntl
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence

VERSION = "3.0.2"

START_ORDER = [
    "xout-modeshape",
    "xout-activemq-artemis",
    "xout-artemis",
    "xout-pmc",
    "xout-portal",
    "xout-web",
    "xout-batchsplitter",
]
STOP_ORDER = list(reversed(START_ORDER))

EXTRA_XOUT_PACKAGES = {"activemq-artemis-xout"}

PACKAGE_SERVICE_MAP = {
    "activemq-artemis-xout": "xout-activemq-artemis",
    "xout-activemq-artemis": "xout-activemq-artemis",
    "xout-artemis": "xout-activemq-artemis",
    "xout-modeshape": "xout-modeshape",
    "xout-pmc": "xout-pmc",
    "xout-portal": "xout-portal",
    "xout-web": "xout-web",
    "xout-batchsplitter": "xout-batchsplitter",
}

SERVICE_PACKAGE_CANDIDATES = {
    "xout-activemq-artemis": [
        "activemq-artemis-xout",
        "xout-activemq-artemis",
        "xout-artemis",
    ],
    "xout-artemis": [
        "activemq-artemis-xout",
        "xout-artemis",
        "xout-activemq-artemis",
    ],
}

PACKAGE_ALIASES = {
    "modeshape": "xout-modeshape",
    "xout-modeshape": "xout-modeshape",
    "activemq": "activemq-artemis-xout",
    "artemis": "activemq-artemis-xout",
    "activemq-artemis-xout": "activemq-artemis-xout",
    "xout-activemq-artemis": "activemq-artemis-xout",
    "xout-artemis": "activemq-artemis-xout",
    "pmc": "xout-pmc",
    "xout-pmc": "xout-pmc",
    "portal": "xout-portal",
    "xout-portal": "xout-portal",
    "web": "xout-web",
    "xout-web": "xout-web",
    "batchsplitter": "xout-batchsplitter",
    "xout-batchsplitter": "xout-batchsplitter",
}

UPDATE_STOP_MAP: dict[str, str | list[str]] = {
    "xout-modeshape": "all",
    "activemq-artemis-xout": "all",
    "xout-activemq-artemis": "all",
    "xout-artemis": "all",
    "xout-pmc": ["xout-pmc", "xout-portal", "xout-web", "xout-batchsplitter"],
    "xout-portal": "all",
    "xout-web": ["xout-web"],
    "xout-batchsplitter": ["xout-batchsplitter"],
}

DEFAULT_REPO_ALIAS = os.environ.get("REPO_ALIAS", "xout-repo")
DEFAULT_REPO_NAME = os.environ.get("REPO_NAME", "xout-rollout-repo")
DEFAULT_REPO_PATH = os.environ.get("REPO_PATH", "/work/dms/")
DEFAULT_HOST = os.environ.get("XOUT_HOST", socket.gethostname())
DEFAULT_PORT = int(os.environ.get("XOUT_PORT", "5000"))
DEFAULT_PORTAL_USER = os.environ.get("PORTAL_USER", "portal")
DEFAULT_LOCK_FILE = os.environ.get("LOCK_FILE", "/tmp/xmanager.lock")
DEFAULT_DUMP_BASE = os.environ.get("DUMP_BASE_DIR", f"/work/dms/dumps/{socket.gethostname()}")
DEFAULT_MODULE_STATE = os.environ.get("XOUT_MODULE_STATE", "/var/tmp/xmanager-portal-modules.json")

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_POSTCHECK = 2
EXIT_PRECHECK = 3
EXIT_LOCKED = 4
EXIT_UPDATES_AVAILABLE = 10


@dataclass
class RunResult:
    rc: int
    stdout: str = ""
    stderr: str = ""


@dataclass
class ServiceInfo:
    name: str
    active: str
    enabled: str
    pid: str
    version: str
    memory: str


@dataclass
class PackageCandidate:
    name: str
    edition: str
    arch: str = ""
    repository: str = ""
    status: str = ""


@dataclass
class PackagePlanItem:
    package: str
    service: str
    installed: str | None
    target: str | None
    action: str
    reason: str
    available: list[str]


@dataclass
class ModuleInfo:
    name: str
    state: str
    group: str


class Colors:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.red = "\033[0;31m" if enabled else ""
        self.green = "\033[0;32m" if enabled else ""
        self.yellow = "\033[1;33m" if enabled else ""
        self.blue = "\033[0;34m" if enabled else ""
        self.cyan = "\033[0;36m" if enabled else ""
        self.bold = "\033[1m" if enabled else ""
        self.dim = "\033[2m" if enabled else ""
        self.nc = "\033[0m" if enabled else ""

    def color(self, text: str, color: str) -> str:
        return f"{color}{text}{self.nc}" if self.enabled else text


class Table:
    def __init__(self, headers: list[str], widths: list[int], ascii_mode: bool = False) -> None:
        self.headers = headers
        self.widths = widths
        self.rows: list[list[str]] = []
        if ascii_mode:
            self.tl = self.tr = self.bl = self.br = "+"
            self.h = "-"
            self.v = "|"
            self.lt = self.rt = self.tt = self.bt = self.ct = "+"
        else:
            self.tl, self.tr, self.bl, self.br = "╭", "╮", "╰", "╯"
            self.h, self.v = "─", "│"
            self.lt, self.rt, self.tt, self.bt, self.ct = "├", "┤", "┬", "┴", "┼"

    @staticmethod
    def strip_ansi(text: str) -> str:
        return re.sub(r"\x1b\[[0-9;]*m", "", text)

    def pad(self, text: str, width: int, align: str = "left") -> str:
        text = str(text)
        plain = self.strip_ansi(text)
        if plain == text and len(text) > width:
            text = text[: max(0, width - 3)] + ("..." if width >= 3 else "")
        padding = " " * max(0, width - len(self.strip_ansi(text)))
        return padding + text if align == "right" else text + padding

    def line(self, left: str, mid: str, right: str) -> str:
        return left + mid.join(self.h * (width + 2) for width in self.widths) + right

    def add_row(self, row: Iterable[Any]) -> None:
        self.rows.append([str(value) for value in row])

    def render(self, aligns: list[str] | None = None) -> str:
        aligns = aligns or ["left"] * len(self.headers)
        out = [self.line(self.tl, self.tt, self.tr)]
        out.append(self.v + self.v.join(f" {self.pad(h, w)} " for h, w in zip(self.headers, self.widths)) + self.v)
        out.append(self.line(self.lt, self.ct, self.rt))
        for row in self.rows:
            out.append(
                self.v
                + self.v.join(
                    f" {self.pad(cell, width, aligns[index] if index < len(aligns) else 'left')} "
                    for index, (cell, width) in enumerate(zip(row, self.widths))
                )
                + self.v
            )
        out.append(self.line(self.bl, self.bt, self.br))
        return "\n".join(out)


def unique_preserve(items: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def shlex_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:=@%+,\-]+", value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def normalize_package_name(name: str) -> str:
    key = name.strip().lower()
    if key not in PACKAGE_ALIASES:
        raise ValueError(f"Unbekanntes XOUT-Paket/Alias: {name}")
    return PACKAGE_ALIASES[key]


def _rpm_segments(value: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    index = 0
    while index < len(value):
        if value[index] == "~":
            out.append((-1, "~"))
            index += 1
            continue
        if not value[index].isalnum():
            index += 1
            continue
        numeric = value[index].isdigit()
        end = index + 1
        while end < len(value) and value[end].isalnum() and value[end].isdigit() == numeric:
            end += 1
        segment = value[index:end]
        if numeric:
            segment = segment.lstrip("0") or "0"
            out.append((2, segment))
        else:
            out.append((1, segment))
        index = end
    return out


def _rpm_part_cmp(left: str, right: str) -> int:
    if left == right:
        return 0
    a, b = _rpm_segments(left), _rpm_segments(right)
    index = 0
    while index < len(a) or index < len(b):
        if index >= len(a):
            return 1 if b[index][0] == -1 else -1
        if index >= len(b):
            return -1 if a[index][0] == -1 else 1
        ta, va = a[index]
        tb, vb = b[index]
        if ta != tb:
            if ta == -1:
                return -1
            if tb == -1:
                return 1
            return 1 if ta > tb else -1
        if ta == 2 and len(va) != len(vb):
            return 1 if len(va) > len(vb) else -1
        if va != vb:
            return 1 if va > vb else -1
        index += 1
    return 0


def _split_evr(value: str) -> tuple[int, str, str]:
    epoch = 0
    rest = value
    if ':' in rest:
        raw_epoch, rest = rest.split(':', 1)
        if raw_epoch.isdigit():
            epoch = int(raw_epoch)
    if '-' in rest:
        version, release = rest.rsplit('-', 1)
    else:
        version, release = rest, ''
    return epoch, version, release


def rpmvercmp(left: str, right: str) -> int:
    if left == right:
        return 0
    le, lv, lr = _split_evr(left)
    re_, rv, rr = _split_evr(right)
    if le != re_:
        return 1 if le > re_ else -1
    version_cmp = _rpm_part_cmp(lv, rv)
    if version_cmp:
        return version_cmp
    return _rpm_part_cmp(lr, rr)


def sort_editions(editions: Iterable[str], reverse: bool = False) -> list[str]:
    from functools import cmp_to_key
    return sorted(unique_preserve(editions), key=cmp_to_key(rpmvercmp), reverse=reverse)


def parse_release_file(path: Path) -> tuple[str, dict[str, str]]:
    if not path.is_file():
        raise ValueError(f"Release-Datei nicht gefunden: {path}")
    if path.suffix.lower() == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Ungültige JSON-Release-Datei {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError("JSON-Release-Datei muss ein Objekt sein")
        name = str(data.get("release") or data.get("name") or path.stem)
        raw_packages = data.get("packages", data)
        if not isinstance(raw_packages, dict):
            raise ValueError("JSON-Feld 'packages' muss ein Objekt sein")
    else:
        cfg = configparser.ConfigParser(interpolation=None)
        try:
            with path.open("r", encoding="utf-8") as handle:
                cfg.read_file(handle)
        except (OSError, configparser.Error) as exc:
            raise ValueError(f"Ungültige INI-Release-Datei {path}: {exc}") from exc
        if not cfg.has_section("packages"):
            raise ValueError("INI-Release-Datei benötigt Abschnitt [packages]")
        name = cfg.get("release", "name", fallback=path.stem)
        raw_packages = dict(cfg.items("packages"))

    packages: dict[str, str] = {}
    for raw_name, raw_version in raw_packages.items():
        if raw_name in {"release", "name", "packages"}:
            continue
        package = normalize_package_name(str(raw_name))
        version = str(raw_version).strip()
        if not version:
            raise ValueError(f"Leere Version für {raw_name}")
        packages[package] = version
    if not packages:
        raise ValueError("Release-Datei enthält keine Pakete")
    return name, packages


def parse_zypper_solvables(xml_text: str, requested: set[str]) -> dict[str, list[PackageCandidate]]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise ValueError(f"Ungültige zypper XML-Ausgabe: {exc}") from exc
    result: dict[str, list[PackageCandidate]] = {name: [] for name in requested}
    for element in root.iter():
        if element.tag.rsplit("}", 1)[-1] != "solvable":
            continue
        name = element.attrib.get("name", "")
        if name not in requested:
            continue
        edition = element.attrib.get("edition") or element.attrib.get("version") or ""
        if not edition:
            continue
        result[name].append(
            PackageCandidate(
                name=name,
                edition=edition,
                arch=element.attrib.get("arch", ""),
                repository=element.attrib.get("repository", ""),
                status=element.attrib.get("status", ""),
            )
        )
    return result


class XManager:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.ascii_mode = args.ascii or not self.terminal_supports_unicode()
        self.c = Colors((not args.no_color) and sys.stdout.isatty())
        self.sudo_prefix: list[str] = [] if os.geteuid() == 0 else ["sudo"]
        self.repo_existed = False
        self.repo_was_enabled = False
        self.repo_touched = False
        self.lock_handle: Any = None
        self.jolokia_root = f"http://{args.host}:{args.port}/jolokia"
        self.jolokia_modules_url = self.jolokia_root + "/read/portal.server.modules:*/CurrentModuleInformation"

    @staticmethod
    def terminal_supports_unicode() -> bool:
        text = " ".join([sys.stdout.encoding or "", os.environ.get("LC_ALL", ""), os.environ.get("LC_CTYPE", ""), os.environ.get("LANG", "")]).lower()
        return "utf" in text

    def log(self, level: str, message: str) -> None:
        colors = {"INFO": self.c.blue, "SUCCESS": self.c.green, "WARN": self.c.yellow, "ERROR": self.c.red, "DRY-RUN": self.c.yellow, "CMD": self.c.dim}
        stream = sys.stderr if self.args.json or level in {"WARN", "ERROR"} else sys.stdout
        print(f"{self.c.color(f'[{level}]', colors.get(level, ''))} {message}", file=stream)

    def die(self, message: str, code: int = EXIT_ERROR) -> None:
        self.log("ERROR", message)
        raise SystemExit(code)

    @staticmethod
    def env_c() -> dict[str, str]:
        env = os.environ.copy()
        env["LC_ALL"] = env["LANG"] = "C"
        return env

    def run(self, cmd: Sequence[str], *, sudo: bool = False, check: bool = False, capture: bool = True, dry_run_changes: bool = False, env: dict[str, str] | None = None, stdout_file: Path | None = None, as_user: str | None = None) -> RunResult:
        full_cmd = list(cmd)
        if as_user and getpass.getuser() != as_user:
            prefix = ["sudo"] + (["-n"] if self.args.non_interactive else [])
            full_cmd = [*prefix, "-u", as_user, *full_cmd]
        elif sudo and self.sudo_prefix:
            prefix = [*self.sudo_prefix]
            if self.args.non_interactive:
                prefix.append("-n")
            full_cmd = [*prefix, *full_cmd]
        if self.args.verbose or (self.args.dry_run and dry_run_changes):
            self.log("CMD", " ".join(shlex_quote(value) for value in full_cmd))
        if self.args.dry_run and dry_run_changes:
            self.log("DRY-RUN", "Würde ausführen: " + " ".join(shlex_quote(value) for value in full_cmd))
            return RunResult(0)
        try:
            if stdout_file is not None:
                stdout_file.parent.mkdir(parents=True, exist_ok=True)
                with stdout_file.open("w", encoding="utf-8", errors="replace") as handle:
                    proc = subprocess.run(full_cmd, text=True, stdout=handle, stderr=subprocess.PIPE, env=env)
                    result = RunResult(proc.returncode, "", proc.stderr or "")
            elif capture:
                proc = subprocess.run(full_cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
                result = RunResult(proc.returncode, proc.stdout or "", proc.stderr or "")
            else:
                proc = subprocess.run(full_cmd, text=True, env=env)
                result = RunResult(proc.returncode)
        except FileNotFoundError:
            result = RunResult(127, "", f"Befehl nicht gefunden: {full_cmd[0]}")
        if check and result.rc != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"Exit-Code {result.rc}"
            self.die("Befehl fehlgeschlagen: " + " ".join(shlex_quote(v) for v in full_cmd) + f"\n{detail}")
        return result

    def check_tools(self, tools: Iterable[str]) -> None:
        missing = [tool for tool in tools if shutil.which(tool) is None]
        if missing:
            self.die("Fehlende Abhängigkeiten: " + ", ".join(missing), EXIT_PRECHECK)

    def ensure_sudo(self, required: bool = True) -> None:
        if not required or os.geteuid() == 0:
            return
        if self.args.non_interactive:
            return
        self.log("INFO", "sudo-Berechtigung wird geprüft. Falls nötig, bitte Passwort eingeben.")
        if self.run(["sudo", "-v"], capture=False).rc != 0:
            self.die("sudo-Berechtigung konnte nicht bestätigt werden.", EXIT_PRECHECK)

    def acquire_lock(self) -> None:
        path = Path(self.args.lock_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(str(path), flags, 0o666)
            os.fchmod(fd, 0o666)
            handle = os.fdopen(fd, "r+", encoding="utf-8")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                self.die(f"Ein anderer xmanager-Lauf ist bereits aktiv. Lock: {path}", EXIT_LOCKED)
            handle.seek(0)
            handle.truncate()
            handle.write(f"pid={os.getpid()} user={getpass.getuser()} host={socket.gethostname()} time={dt.datetime.now().isoformat()}\n")
            handle.flush()
            self.lock_handle = handle
        except SystemExit:
            raise
        except OSError as exc:
            self.die(f"Lock-Datei kann nicht geöffnet werden: {path}: {exc}", EXIT_LOCKED)

    def release_lock(self) -> None:
        if self.lock_handle is None:
            return
        path = Path(self.args.lock_file)
        try:
            fcntl.flock(self.lock_handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.lock_handle.close()
            self.lock_handle = None
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            self.log("WARN", f"Lock-Datei konnte nicht gelöscht werden: {path}: {exc}")

    def discover_services(self) -> list[str]:
        result = self.run(["systemctl", "list-unit-files", "--type=service", "--no-legend"], check=True)
        installed = {fields[0][:-8] for fields in (line.split() for line in result.stdout.splitlines()) if fields and fields[0].startswith("xout-") and fields[0].endswith(".service")}
        return [s for s in START_ORDER if s in installed] + sorted(installed - set(START_ORDER))

    @staticmethod
    def order_services(services: Iterable[str], order: list[str]) -> list[str]:
        selected = set(services)
        return [s for s in order if s in selected] + sorted(selected - set(order))

    def systemctl_show(self, service: str, prop: str) -> str:
        result = self.run(["systemctl", "show", service, "-p", prop, "--value"])
        return result.stdout.strip() if result.rc == 0 else ""

    def service_active_bool(self, service: str) -> bool:
        return self.run(["systemctl", "is-active", "--quiet", service]).rc == 0

    def service_active_text(self, service: str) -> str:
        result = self.run(["systemctl", "is-active", service])
        return result.stdout.strip() or "unknown"

    def service_enabled_text(self, service: str) -> str:
        result = self.run(["systemctl", "is-enabled", service])
        return result.stdout.strip() or "unknown"

    def get_installed_package_version(self, package: str) -> str | None:
        result = self.run(["rpm", "-q", package, "--queryformat", "%{VERSION}-%{RELEASE}\n"])
        return result.stdout.strip().splitlines()[0] if result.rc == 0 and result.stdout.strip() else None

    def get_package_version_for_service(self, service: str) -> str:
        for package in SERVICE_PACKAGE_CANDIDATES.get(service, [service]):
            version = self.get_installed_package_version(package)
            if version:
                return version
        return "-"

    @staticmethod
    def format_bytes(value: str) -> str:
        try:
            number = int(value)
        except (ValueError, TypeError):
            number = 0
        return f"{number / 1024 / 1024:.1f} MB"

    def child_pids_recursive(self, pid: str) -> list[str]:
        result = self.run(["pgrep", "-P", pid])
        if result.rc != 0:
            return []
        children = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return children + [grand for child in children for grand in self.child_pids_recursive(child)]

    def memory_for_service(self, service: str) -> str:
        current = self.systemctl_show(service, "MemoryCurrent")
        if current and current not in {"[not set]", "0"}:
            return self.format_bytes(current) + " cgroup"
        pid = self.systemctl_show(service, "MainPID")
        if not pid or pid == "0":
            return "0 MB"
        pids = unique_preserve([pid, *self.child_pids_recursive(pid)])
        result = self.run(["ps", "-o", "rss=", "-p", ",".join(pids)])
        kb = sum(int(line.strip()) for line in result.stdout.splitlines() if line.strip().isdigit())
        return f"{kb / 1024:.1f} MB rss" if kb else "0 MB"

    def collect_service_info(self) -> list[ServiceInfo]:
        infos = []
        for service in self.discover_services():
            pid = self.systemctl_show(service, "MainPID") or "-"
            if pid == "0":
                pid = "-"
            infos.append(ServiceInfo(service, self.service_active_text(service), self.service_enabled_text(service), pid, self.get_package_version_for_service(service), self.memory_for_service(service)))
        return infos

    def print_services(self, infos: list[ServiceInfo]) -> None:
        table = Table(["Nr", "Service", "Version", "PID", "Status", "Enabled", "Memory"], [3, 25, 18, 9, 11, 12, 17], self.ascii_mode)
        for idx, info in enumerate(infos, 1):
            table.add_row([idx, info.name, info.version, info.pid, info.active, info.enabled, info.memory])
        print("\nServices")
        print(table.render(["right", "left", "left", "right", "left", "left", "right"]))

    def select_services(self, selector: str) -> list[str]:
        services = self.discover_services()
        if selector == "all":
            return services
        if re.fullmatch(r"[0-9,]+", selector or ""):
            selected = []
            for part in selector.split(","):
                index = int(part)
                if not 1 <= index <= len(services):
                    self.die(f"Ungültige Servicenummer: {index}")
                selected.append(services[index - 1])
            return unique_preserve(selected)
        pattern = (selector or "").lower()
        selected = [s for s in services if pattern in s.lower() or pattern in s.removeprefix("xout-").lower()]
        if not selected:
            self.die(f"Keine Services gefunden für Pattern: {selector}")
        return selected

    def wait_service(self, service: str, active: bool) -> bool:
        deadline = time.time() + self.args.max_wait
        while time.time() < deadline:
            if self.service_active_bool(service) == active:
                return True
            time.sleep(self.args.wait_interval)
        return False

    def start_service_list(self, services: Iterable[str]) -> bool:
        ok = True
        for service in self.order_services(services, START_ORDER):
            if self.service_active_bool(service):
                self.log("INFO", f"{service} ist bereits aktiv.")
                continue
            self.log("INFO", f"Starte Dienst: {service}")
            result = self.run(["systemctl", "start", service], sudo=True, dry_run_changes=True)
            if result.rc != 0 or (not self.args.dry_run and not self.wait_service(service, True)):
                self.log("ERROR", f"Start von {service} fehlgeschlagen.")
                ok = False
        return ok

    def stop_service_list(self, services: Iterable[str]) -> bool:
        ok = True
        for service in self.order_services(services, STOP_ORDER):
            if not self.service_active_bool(service):
                self.log("INFO", f"{service} ist bereits inaktiv.")
                continue
            self.log("INFO", f"Stoppe Dienst: {service}")
            result = self.run(["systemctl", "stop", service], sudo=True, dry_run_changes=True)
            if result.rc != 0 or (not self.args.dry_run and not self.wait_service(service, False)):
                self.log("ERROR", f"Stop von {service} fehlgeschlagen.")
                ok = False
        return ok

    def start_services(self, selector: str) -> None:
        self.ensure_sudo()
        self.acquire_lock()
        try:
            if not self.start_service_list(self.select_services(selector)):
                self.die("Mindestens ein Service konnte nicht sauber gestartet werden.", EXIT_POSTCHECK)
        finally:
            self.release_lock()

    def stop_services(self, selector: str) -> None:
        self.ensure_sudo()
        self.acquire_lock()
        try:
            if not self.stop_service_list(self.select_services(selector)):
                self.die("Mindestens ein Service konnte nicht sauber gestoppt werden.", EXIT_POSTCHECK)
        finally:
            self.release_lock()

    def restart_services(self, selector: str) -> None:
        self.ensure_sudo()
        self.acquire_lock()
        try:
            selected = self.select_services(selector)
            was_active = [s for s in selected if self.service_active_bool(s)]
            if not self.stop_service_list(selected):
                self.die("Stop vor Restart fehlgeschlagen.", EXIT_POSTCHECK)
            if not self.start_service_list(was_active):
                self.die("Start nach Restart fehlgeschlagen.", EXIT_POSTCHECK)
        finally:
            self.release_lock()

    @staticmethod
    def clean_module_name(raw: str) -> str:
        return re.sub(r'^portal\.server\.modules:module="|"$', "", raw)

    @staticmethod
    def module_group(name: str, state: str) -> str:
        lower = name.lower()
        if state != "RUNNING":
            return "Nicht-RUNNING"
        if any(token in lower for token in ("import", "router", "merger")):
            return "Import/Router/Merger"
        return "Andere RUNNING"

    @staticmethod
    def module_priority(name: str) -> tuple[int, str]:
        lower = name.lower()
        if "import" in lower:
            return 1, lower
        if "router" in lower:
            return 2, lower
        if "merger" in lower:
            return 3, lower
        return 4, lower

    def http_json_get(self, url: str) -> Any:
        request = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self.args.http_timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if isinstance(payload, dict) and str(payload.get("status", "200")) != "200":
                raise RuntimeError(payload.get("error", "Jolokia error"))
            return payload
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            raise RuntimeError(f"Jolokia GET fehlgeschlagen: {url}: {exc}") from exc

    def http_json_post(self, payload: dict[str, Any]) -> Any:
        request = urllib.request.Request(self.jolokia_root + "/", data=json.dumps(payload).encode("utf-8"), method="POST", headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.args.http_timeout) as response:
                result = json.loads(response.read().decode("utf-8"))
            if str(result.get("status", "")) != "200":
                raise RuntimeError(result.get("error", "Jolokia error"))
            return result
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            raise RuntimeError(f"Jolokia POST fehlgeschlagen: {exc}") from exc

    def collect_modules(self) -> list[ModuleInfo]:
        data = self.http_json_get(self.jolokia_modules_url)
        value = data.get("value") if isinstance(data, dict) else None
        if not isinstance(value, dict):
            raise RuntimeError("Ungültige Jolokia-Antwort: .value fehlt")
        modules = []
        for raw, details in value.items():
            state = "UNKNOWN"
            if isinstance(details, dict) and isinstance(details.get("CurrentModuleInformation"), dict):
                state = str(details["CurrentModuleInformation"].get("state", "UNKNOWN"))
            name = self.clean_module_name(raw)
            modules.append(ModuleInfo(name, state, self.module_group(name, state)))
        order = {"Import/Router/Merger": 1, "Andere RUNNING": 2, "Nicht-RUNNING": 3}
        return sorted(modules, key=lambda m: (order.get(m.group, 9), m.name.lower()))

    def module_status_map(self) -> dict[str, str]:
        return {m.name: m.state for m in self.collect_modules()}

    def module_exec(self, name: str, operation: str) -> None:
        self.http_json_post({"mbean": f'portal.server.modules:module="{name}"', "arguments": ["localhost/127.0.0.1"], "type": "EXEC", "operation": operation})

    def wait_module_state(self, name: str, running: bool) -> bool:
        deadline = time.time() + self.args.max_wait
        while time.time() < deadline:
            state = self.module_status_map().get(name, "UNKNOWN")
            if (state == "RUNNING") == running and state != "UNKNOWN":
                return True
            time.sleep(self.args.wait_interval)
        return False

    def stop_module(self, name: str) -> bool:
        self.log("INFO", f"Stoppe Portal-Modul: {name}")
        if self.args.dry_run:
            return True
        try:
            self.module_exec(name, "stopModule")
            return self.wait_module_state(name, False)
        except RuntimeError as exc:
            self.log("ERROR", str(exc))
            return False

    def start_module(self, name: str) -> bool:
        if self.args.dry_run:
            return True
        if self.module_status_map().get(name) == "RUNNING":
            return True
        self.log("INFO", f"Starte Portal-Modul: {name}")
        try:
            self.module_exec(name, "startModule")
            return self.wait_module_state(name, True)
        except RuntimeError as exc:
            self.log("ERROR", str(exc))
            return False

    def print_modules(self, modules: list[ModuleInfo]) -> None:
        table = Table(["Modul", "Status", "Gruppe"], [48, 24, 24], self.ascii_mode)
        for module in modules:
            table.add_row([module.name, module.state, module.group])
        print("\nPortal-Module")
        print(table.render())

    @staticmethod
    def _write_state_file(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        except BaseException:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def modules_quiesce(self, state_file: Path) -> dict[str, Any]:
        self.acquire_lock()
        try:
            if not self.service_active_bool("xout-portal"):
                payload = {"schema_version": 1, "host": socket.gethostname(), "created": dt.datetime.now().isoformat(), "modules": []}
                if not self.args.dry_run:
                    self._write_state_file(state_file, payload)
                return payload
            running = sorted((m.name for m in self.collect_modules() if m.state == "RUNNING"), key=self.module_priority)
            payload = {"schema_version": 1, "host": socket.gethostname(), "created": dt.datetime.now().isoformat(), "modules": running}
            if not self.args.dry_run:
                self._write_state_file(state_file, payload)
            failed = [name for name in running if not self.stop_module(name)]
            if failed:
                self.die("Portal-Module konnten nicht gestoppt werden: " + ", ".join(failed), EXIT_POSTCHECK)
            return payload
        finally:
            self.release_lock()

    def modules_restore(self, state_file: Path, keep_state: bool = False) -> dict[str, Any]:
        self.acquire_lock()
        try:
            if not state_file.is_file():
                self.die(f"Module-State-Datei fehlt: {state_file}", EXIT_PRECHECK)
            try:
                payload = json.loads(state_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                self.die(f"Module-State-Datei ist ungültig: {exc}", EXIT_PRECHECK)
            modules = payload.get("modules", [])
            if not isinstance(modules, list) or not all(isinstance(x, str) for x in modules):
                self.die("Module-State-Datei enthält ungültige modules", EXIT_PRECHECK)
            if modules and not self.service_active_bool("xout-portal"):
                self.die("xout-portal ist nicht aktiv; Restore nicht möglich.", EXIT_POSTCHECK)
            failed = [name for name in sorted(modules, key=self.module_priority) if not self.start_module(name)]
            if failed:
                self.die("Portal-Module konnten nicht wiederhergestellt werden: " + ", ".join(failed), EXIT_POSTCHECK)
            if not keep_state and not self.args.dry_run:
                state_file.unlink(missing_ok=True)
            return {"restored": modules, "state_file_removed": not keep_state and not self.args.dry_run}
        finally:
            self.release_lock()

    @staticmethod
    def is_managed_package(name: str) -> bool:
        return name.startswith("xout-") or name in EXTRA_XOUT_PACKAGES

    def zypper_lr(self) -> dict[str, dict[str, str]]:
        result = self.run(["zypper", "lr"], env=self.env_c(), check=True)
        repos = {}
        for line in result.stdout.splitlines():
            if "|" not in line:
                continue
            parts = [part.strip() for part in line.split("|")]
            if len(parts) >= 4 and parts[0] not in {"#", "---"}:
                repos[parts[1]] = {"name": parts[2], "enabled": parts[3]}
        return repos

    def ensure_repo(self) -> None:
        row = self.zypper_lr().get(self.args.repo_alias)
        self.repo_existed = row is not None
        self.repo_was_enabled = bool(row and row.get("enabled", "").lower() in {"yes", "ja", "true", "1"})
        if not self.repo_existed:
            self.log("INFO", f"Repository {self.args.repo_alias} fehlt; füge es temporär hinzu.")
            self.run(["zypper", "addrepo", "--check", "--refresh", "--name", self.args.repo_name, self.args.repo_path, self.args.repo_alias], sudo=True, check=True, capture=self.args.json, env=self.env_c())
        self.run(["zypper", "mr", "-e", self.args.repo_alias], sudo=True, check=True, capture=self.args.json, env=self.env_c())
        self.repo_touched = True
        self.run(["zypper", "--gpg-auto-import-keys", "ref", "-r", self.args.repo_alias], sudo=True, check=True, capture=self.args.json, env=self.env_c())

    def restore_repo(self) -> None:
        if not self.repo_touched:
            return
        try:
            if not self.repo_existed:
                self.run(["zypper", "rr", self.args.repo_alias], sudo=True, env=self.env_c())
            elif not self.repo_was_enabled:
                self.run(["zypper", "mr", "-d", self.args.repo_alias], sudo=True, env=self.env_c())
        finally:
            self.repo_touched = False

    def query_repo_candidates(self, packages: Iterable[str]) -> dict[str, list[PackageCandidate]]:
        requested = sorted(set(packages))
        combined: dict[str, list[PackageCandidate]] = {name: [] for name in requested}
        for package in requested:
            cmd = ["zypper", "--xmlout", "--non-interactive", "--no-refresh", "--ignore-unknown", "search", "-s", "--match-exact", "-r", self.args.repo_alias, package]
            result = self.run(cmd, sudo=True, env=self.env_c(), check=True)
            parsed = parse_zypper_solvables(result.stdout, {package})
            combined[package].extend(parsed.get(package, []))
        return combined

    def normalize_package_selection(self, raw_packages: Sequence[str] | None) -> list[str]:
        if raw_packages:
            try:
                return unique_preserve(normalize_package_name(name) for name in raw_packages)
            except ValueError as exc:
                self.die(str(exc), EXIT_PRECHECK)
        installed = []
        for package in PACKAGE_SERVICE_MAP:
            normalized = PACKAGE_ALIASES.get(package, package)
            if normalized in installed:
                continue
            if self.get_installed_package_version(package):
                installed.append(normalized)
        return installed

    def build_package_plan(self, selected: list[str], release_file: Path | None, allow_downgrade: bool) -> dict[str, Any]:
        release_name = None
        release_targets: dict[str, str] | None = None
        if release_file:
            try:
                release_name, release_targets = parse_release_file(release_file)
            except ValueError as exc:
                self.die(str(exc), EXIT_PRECHECK)
        candidates = self.query_repo_candidates(selected)
        items: list[PackagePlanItem] = []
        for package in selected:
            installed = self.get_installed_package_version(package)
            available = sort_editions((c.edition for c in candidates.get(package, [])), reverse=True)
            if release_targets is not None and package not in release_targets:
                items.append(PackagePlanItem(package, PACKAGE_SERVICE_MAP.get(package, "-"), installed, installed, "noop", "not_in_release_file", available))
                continue
            requested_target = release_targets.get(package) if release_targets is not None else None
            if requested_target is None:
                if not available:
                    if installed is not None:
                        items.append(PackagePlanItem(package, PACKAGE_SERVICE_MAP.get(package, "-"), installed, installed, "noop", "package_not_in_repository_keep_installed", available))
                    else:
                        items.append(PackagePlanItem(package, PACKAGE_SERVICE_MAP.get(package, "-"), installed, None, "error", "package_not_found_in_repository", available))
                    continue
                target = available[0]
            else:
                matches = [edition for edition in available if edition == requested_target or edition.startswith(requested_target + "-")]
                if not matches:
                    items.append(PackagePlanItem(package, PACKAGE_SERVICE_MAP.get(package, "-"), installed, requested_target, "error", "requested_version_not_found", available))
                    continue
                target = matches[0]
            if installed is None:
                action, reason = "install", "package_not_installed"
            else:
                cmp = rpmvercmp(target, installed)
                if cmp == 0:
                    action, reason = "noop", "already_at_target"
                elif cmp > 0:
                    action, reason = "upgrade", "target_is_newer"
                elif allow_downgrade:
                    action, reason = "downgrade", "downgrade_explicitly_allowed"
                else:
                    action, reason = "error", "downgrade_requires_allow_downgrade"
            items.append(PackagePlanItem(package, PACKAGE_SERVICE_MAP.get(package, "-"), installed, target, action, reason, available))
        return {"schema_version": 1, "host": socket.gethostname(), "repository": self.args.repo_alias, "mode": "release_file" if release_file else "latest", "release": release_name, "release_file": str(release_file) if release_file else None, "allow_downgrade": allow_downgrade, "packages": [asdict(item) for item in items], "has_changes": any(item.action in {"install", "upgrade", "downgrade"} for item in items), "has_errors": any(item.action == "error" for item in items)}

    def package_plan_command(self, selected: list[str], release_file: Path | None, allow_downgrade: bool) -> dict[str, Any]:
        self.ensure_sudo()
        self.acquire_lock()
        try:
            self.ensure_repo()
            return self.build_package_plan(selected, release_file, allow_downgrade)
        finally:
            self.restore_repo()
            self.release_lock()

    def print_package_plan(self, plan: dict[str, Any]) -> None:
        table = Table(["Paket", "Service", "Installiert", "Ziel", "Aktion", "Grund"], [28, 25, 18, 18, 10, 27], self.ascii_mode)
        for item in plan["packages"]:
            table.add_row([item["package"], item["service"], item["installed"] or "-", item["target"] or "-", item["action"], item["reason"]])
        print(f"\nPackage plan ({plan['mode']})")
        print(table.render())

    def ensure_services_disabled(self, services: Iterable[str]) -> dict[str, Any]:
        changed, checked = [], []
        for service in unique_preserve(services):
            if not service or service == "-":
                continue
            enabled = self.service_enabled_text(service)
            checked.append({"service": service, "before": enabled})
            if enabled in {"enabled", "enabled-runtime"}:
                self.run(["systemctl", "disable", service], sudo=True, check=True, dry_run_changes=True)
                changed.append(service)
            checked[-1]["after"] = "disabled" if service in changed and not self.args.dry_run else self.service_enabled_text(service)
        return {"checked": checked, "changed": changed}

    def package_apply_command(self, selected: list[str], release_file: Path | None, allow_downgrade: bool) -> dict[str, Any]:
        self.ensure_sudo()
        self.acquire_lock()
        try:
            self.ensure_repo()
            plan = self.build_package_plan(selected, release_file, allow_downgrade)
            if plan["has_errors"]:
                errors = [f"{i['package']}: {i['reason']}" for i in plan["packages"] if i["action"] == "error"]
                self.die("Package preflight fehlgeschlagen: " + "; ".join(errors), EXIT_PRECHECK)
            applied = []
            for item in plan["packages"]:
                action = item["action"]
                if action == "noop":
                    continue
                spec = f"{item['package']}={item['target']}"
                cmd = ["zypper", "-n", "--no-gpg-checks", "install", "--repo", self.args.repo_alias, "--allow-vendor-change"]
                if action == "downgrade":
                    cmd.append("--oldpackage")
                cmd.append(spec)
                self.log("INFO", f"{action}: {spec}")
                self.run(cmd, sudo=True, check=True, capture=self.args.json, dry_run_changes=True, env=self.env_c())
                if not self.args.dry_run:
                    actual = self.get_installed_package_version(item["package"])
                    if actual != item["target"]:
                        self.die(f"Versionsprüfung fehlgeschlagen: {item['package']}: erwartet {item['target']}, installiert {actual}", EXIT_POSTCHECK)
                applied.append({"package": item["package"], "target": item["target"], "action": action})
            disable = self.ensure_services_disabled(i["service"] for i in plan["packages"] if i["service"] != "-")
            return {"plan": plan, "applied": applied, "disabled": disable}
        finally:
            self.restore_repo()
            self.release_lock()

    def updates_command(self) -> dict[str, Any]:
        selected = self.normalize_package_selection(None)
        return self.package_plan_command(selected, None, False)

    def affected_services_for_plan(self, plan: dict[str, Any]) -> list[str]:
        installed = self.discover_services()
        installed_set = set(installed)
        if self.args.stop_all_xout:
            return self.order_services(installed, STOP_ORDER)
        affected: set[str] = set()
        for item in plan["packages"]:
            if item["action"] not in {"install", "upgrade", "downgrade"}:
                continue
            policy = UPDATE_STOP_MAP.get(item["package"])
            if policy == "all":
                return self.order_services(installed, STOP_ORDER)
            if isinstance(policy, list):
                affected.update(s for s in policy if s in installed_set)
            elif item["service"] in installed_set:
                affected.add(item["service"])
            else:
                return self.order_services(installed, STOP_ORDER)
        return self.order_services(affected, STOP_ORDER)

    def update_command(self) -> int:
        self.ensure_sudo()
        self.acquire_lock()
        try:
            self.ensure_repo()
            selected = self.normalize_package_selection(None)
            plan = self.build_package_plan(selected, None, False)
            if plan["has_errors"]:
                self.die("Update-Preflight fehlgeschlagen.", EXIT_PRECHECK)
            if self.args.json:
                print(json.dumps(plan, indent=2, ensure_ascii=False))
            else:
                self.print_package_plan(plan)
            if not plan["has_changes"]:
                return EXIT_OK
            affected = self.affected_services_for_plan(plan)
            was_active = [s for s in affected if self.service_active_bool(s)]
            state_file = Path(self.args.module_state_file)
            if "xout-portal" in affected and self.service_active_bool("xout-portal") and not self.args.skip_module_stop:
                running = sorted((m.name for m in self.collect_modules() if m.state == "RUNNING"), key=self.module_priority)
                payload = {"schema_version": 1, "host": socket.gethostname(), "created": dt.datetime.now().isoformat(), "modules": running}
                if not self.args.dry_run:
                    self._write_state_file(state_file, payload)
                for module in running:
                    if not self.stop_module(module):
                        self.die(f"Portal-Modul konnte nicht gestoppt werden: {module}", EXIT_POSTCHECK)
            if not self.stop_service_list(affected):
                self.die("Service-Stop fehlgeschlagen.", EXIT_POSTCHECK)
            for item in plan["packages"]:
                if item["action"] not in {"install", "upgrade", "downgrade"}:
                    continue
                cmd = ["zypper", "-n", "--no-gpg-checks", "install", "--repo", self.args.repo_alias, "--allow-vendor-change"]
                if item["action"] == "downgrade":
                    cmd.append("--oldpackage")
                cmd.append(f"{item['package']}={item['target']}")
                self.run(cmd, sudo=True, check=True, capture=self.args.json, dry_run_changes=True, env=self.env_c())
            self.ensure_services_disabled(i["service"] for i in plan["packages"] if i["service"] != "-")
            if not self.start_service_list(was_active):
                self.die("Service-Start nach Update fehlgeschlagen.", EXIT_POSTCHECK)
            if state_file.is_file() and self.service_active_bool("xout-portal") and not self.args.dry_run:
                payload = json.loads(state_file.read_text(encoding="utf-8"))
                failed = [m for m in payload.get("modules", []) if not self.start_module(m)]
                if failed:
                    self.die("Module-Restore fehlgeschlagen: " + ", ".join(failed), EXIT_POSTCHECK)
                state_file.unlink(missing_ok=True)
            return EXIT_OK
        finally:
            self.restore_repo()
            self.release_lock()

    def create_dumps(self) -> None:
        self.ensure_sudo()
        self.acquire_lock()
        try:
            pid = self.systemctl_show("xout-portal", "MainPID")
            if not pid or pid == "0":
                self.die("xout-portal läuft nicht oder hat keine MainPID.")
            timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            directory = Path(self.args.dump_base_dir) / timestamp
            if not self.args.dry_run:
                directory.mkdir(parents=True, exist_ok=True)
            thread_file = directory / f"xout-portal_{pid}_{timestamp}_thread.txt"
            heap_file = directory / f"xout-portal_{pid}_{timestamp}_heap.hprof"
            if shutil.which("jcmd"):
                self.run(["jcmd", pid, "Thread.print", "-l"], stdout_file=thread_file, as_user=self.args.portal_user, dry_run_changes=True)
                self.run(["jcmd", pid, "GC.heap_dump", str(heap_file)], as_user=self.args.portal_user, capture=False, dry_run_changes=True)
            else:
                self.check_tools(["jstack", "jmap"])
                self.run(["jstack", "-l", pid], stdout_file=thread_file, as_user=self.args.portal_user, dry_run_changes=True)
                self.run(["jmap", f"-dump:format=b,file={heap_file}", pid], as_user=self.args.portal_user, capture=False, dry_run_changes=True)
            self.log("SUCCESS", f"Dumps erstellt unter: {directory}")
        finally:
            self.release_lock()

    def interactive(self) -> None:
        while True:
            os.system("clear")
            self.print_services(self.collect_service_info())
            print("\nBefehle: l=list, m=modules, u=updates, U=update, s=start, p=stop, r=restart, d=dump, q=quit")
            try:
                raw = input("Befehl> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not raw:
                continue
            command, _, argument = raw.partition(" ")
            try:
                if command.lower() in {"q", "quit", "exit"}:
                    return
                if command.lower() in {"l", "list"}:
                    continue
                if command.lower() in {"m", "modules"}:
                    self.print_modules(self.collect_modules())
                elif command == "u":
                    self.print_package_plan(self.updates_command())
                elif command == "U":
                    self.update_command()
                elif command.lower() in {"s", "start"}:
                    self.start_services(argument)
                elif command.lower() in {"p", "stop"}:
                    self.stop_services(argument)
                elif command.lower() in {"r", "restart"}:
                    self.restart_services(argument)
                elif command.lower() in {"d", "dump"}:
                    self.create_dumps()
                else:
                    self.log("ERROR", "Ungültiger Befehl")
            except SystemExit as exc:
                self.log("ERROR", f"Befehl abgebrochen (Exit {exc.code}).")
            input("\nWeiter mit Enter...")


def add_package_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--package", action="append", dest="packages", help="XOUT package/alias; repeatable. Examples: portal, web, activemq")
    parser.add_argument("--release-file", type=Path, help="Optional INI/JSON file. If omitted, latest repository version is targeted.")
    parser.add_argument("--allow-downgrade", action="store_true", help="Explicitly allow target versions older than installed versions")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xmanager.py",
        description="XOUT Manager: lokale Services, Portal-Module und RPM-Pakete verwalten; geeignet für Ansible-Automation.",
        epilog="Target semantics: without --release-file, selected packages target the newest version in xout-repo; already installed packages absent from the repository are kept unchanged. With --release-file, only packages listed in [packages] are changed; other selected packages are NOOP.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {VERSION}")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Jolokia host (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Jolokia port (default: {DEFAULT_PORT})")
    parser.add_argument("--repo-alias", default=DEFAULT_REPO_ALIAS, help=f"Zypper repository alias (default: {DEFAULT_REPO_ALIAS})")
    parser.add_argument("--repo-name", default=DEFAULT_REPO_NAME, help=f"Zypper repository name (default: {DEFAULT_REPO_NAME})")
    parser.add_argument("--repo-path", default=DEFAULT_REPO_PATH, help=f"Repository path (default: {DEFAULT_REPO_PATH})")
    parser.add_argument("--lock-file", default=DEFAULT_LOCK_FILE, help=f"Lock file (default: {DEFAULT_LOCK_FILE})")
    parser.add_argument("--dump-base-dir", default=DEFAULT_DUMP_BASE)
    parser.add_argument("--portal-user", default=DEFAULT_PORTAL_USER)
    parser.add_argument("--module-state-file", default=DEFAULT_MODULE_STATE, help=f"Default Portal module state file (default: {DEFAULT_MODULE_STATE})")
    parser.add_argument("--max-wait", type=int, default=60)
    parser.add_argument("--wait-interval", type=int, default=2)
    parser.add_argument("--http-timeout", type=int, default=10)
    parser.add_argument("--dry-run", "-d", action="store_true")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--ascii", action="store_true")
    parser.add_argument("--json", action="store_true", help="Machine-readable stdout; log messages go to stderr")
    parser.add_argument("--non-interactive", action="store_true", help="Do not prompt for sudo password")
    parser.add_argument("--skip-module-stop", action="store_true", help="Standalone update only: skip Portal module quiesce")
    parser.add_argument("--stop-all-xout", action="store_true", help="Standalone update only: stop every local XOUT service")
    parser.add_argument("--fail-on-updates", action="store_true", help="updates: exit 10 when changes are available")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("list", help="Show local XOUT services")
    sub.add_parser("modules", help="Show Portal modules through Jolokia")
    sub.add_parser("updates", help="Plan latest updates for installed XOUT packages")
    sub.add_parser("update", help="Standalone local update with local lifecycle (legacy-compatible)")
    sub.add_parser("dump", help="Create thread/heap dumps for xout-portal")
    sub.add_parser("interactive", aliases=["i"], help="Interactive local administrator mode")
    plan = sub.add_parser("package-plan", help="Preflight package targets without changing RPMs or services")
    add_package_options(plan)
    apply = sub.add_parser("package-apply", help="Apply a validated RPM target set WITHOUT service lifecycle")
    add_package_options(apply)
    quiesce = sub.add_parser("modules-quiesce", help="Save RUNNING Portal modules and stop them in safe order")
    quiesce.add_argument("--state-file", type=Path, default=Path(DEFAULT_MODULE_STATE))
    restore = sub.add_parser("modules-restore", help="Restore Portal modules from a state file and delete the file on success")
    restore.add_argument("--state-file", type=Path, default=Path(DEFAULT_MODULE_STATE))
    restore.add_argument("--keep-state", action="store_true", help="Keep state file after a successful restore")
    disabled = sub.add_parser("ensure-disabled", help="Ensure selected/all local XOUT systemd units remain disabled")
    disabled.add_argument("selector", nargs="?", default="all", help="all or service pattern")
    for command in ("start", "stop", "restart"):
        child = sub.add_parser(command, help=f"Local XOUT services {command}")
        child.add_argument("selector", help="all, numbers (1,2) or pattern such as portal/web")
    return parser


def preprocess_argv(argv: list[str]) -> list[str]:
    if len(argv) >= 2 and argv[1] in {"-update", "--update"}:
        argv = [argv[0], "update", *argv[2:]]
    elif len(argv) >= 2 and argv[1] in {"-i", "--interactive"}:
        argv = [argv[0], "interactive", *argv[2:]]
    commands = {"list", "modules", "updates", "update", "dump", "interactive", "i", "start", "stop", "restart", "package-plan", "package-apply", "modules-quiesce", "modules-restore", "ensure-disabled"}
    global_flags = {"--dry-run", "-d", "--verbose", "-v", "--no-color", "--ascii", "--json", "--non-interactive", "--skip-module-stop", "--stop-all-xout", "--fail-on-updates"}
    global_values = {"--host", "--port", "--repo-alias", "--repo-name", "--repo-path", "--lock-file", "--dump-base-dir", "--portal-user", "--module-state-file", "--max-wait", "--wait-interval", "--http-timeout"}
    args = argv[1:]
    command_index = next((i for i, value in enumerate(args) if value in commands), None)
    if command_index is None:
        return argv
    before, command, rest = args[:command_index], args[command_index], args[command_index + 1 :]
    moved, remaining = [], []
    i = 0
    while i < len(rest):
        value = rest[i]
        if value in global_flags:
            moved.append(value)
            i += 1
        elif value in global_values and i + 1 < len(rest):
            moved.extend([value, rest[i + 1]])
            i += 2
        elif any(value.startswith(option + "=") for option in global_values):
            moved.append(value)
            i += 1
        else:
            remaining.append(value)
            i += 1
    return [argv[0], *before, *moved, command, *remaining]


def main() -> int:
    parser = build_parser()
    args = parser.parse_args(preprocess_argv(sys.argv)[1:])
    if not args.command:
        args.command = "list"
    manager = XManager(args)
    try:
        if args.command == "list":
            manager.check_tools(["systemctl", "rpm", "ps", "pgrep"])
            info = manager.collect_service_info()
            print(json.dumps([asdict(x) for x in info], indent=2, ensure_ascii=False)) if args.json else manager.print_services(info)
            return EXIT_OK
        if args.command == "modules":
            modules = manager.collect_modules()
            print(json.dumps([asdict(x) for x in modules], indent=2, ensure_ascii=False)) if args.json else manager.print_modules(modules)
            return EXIT_OK
        if args.command == "updates":
            manager.check_tools(["rpm", "zypper"] + (["sudo"] if os.geteuid() else []))
            plan = manager.updates_command()
            print(json.dumps(plan, indent=2, ensure_ascii=False)) if args.json else manager.print_package_plan(plan)
            return EXIT_UPDATES_AVAILABLE if args.fail_on_updates and plan["has_changes"] else EXIT_OK
        if args.command in {"package-plan", "package-apply"}:
            manager.check_tools(["rpm", "zypper", "systemctl"] + (["sudo"] if os.geteuid() else []))
            selected = manager.normalize_package_selection(args.packages)
            if not selected:
                manager.die("Keine XOUT-Pakete ausgewählt/gefunden.", EXIT_PRECHECK)
            if args.command == "package-plan":
                result = manager.package_plan_command(selected, args.release_file, args.allow_downgrade)
            else:
                result = manager.package_apply_command(selected, args.release_file, args.allow_downgrade)
            print(json.dumps(result, indent=2, ensure_ascii=False)) if args.json else manager.print_package_plan(result if args.command == "package-plan" else result["plan"])
            return EXIT_PRECHECK if args.command == "package-plan" and result["has_errors"] else EXIT_OK
        if args.command == "modules-quiesce":
            result = manager.modules_quiesce(args.state_file)
            if args.json:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            return EXIT_OK
        if args.command == "modules-restore":
            result = manager.modules_restore(args.state_file, args.keep_state)
            if args.json:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            return EXIT_OK
        if args.command == "ensure-disabled":
            manager.ensure_sudo()
            manager.acquire_lock()
            try:
                services = manager.discover_services() if args.selector == "all" else manager.select_services(args.selector)
                result = manager.ensure_services_disabled(services)
            finally:
                manager.release_lock()
            if args.json:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            return EXIT_OK
        if args.command == "update":
            manager.check_tools(["rpm", "zypper", "systemctl"] + (["sudo"] if os.geteuid() else []))
            return manager.update_command()
        if args.command == "start":
            manager.start_services(args.selector)
            return EXIT_OK
        if args.command == "stop":
            manager.stop_services(args.selector)
            return EXIT_OK
        if args.command == "restart":
            manager.restart_services(args.selector)
            return EXIT_OK
        if args.command == "dump":
            manager.create_dumps()
            return EXIT_OK
        if args.command in {"interactive", "i"}:
            manager.interactive()
            return EXIT_OK
        return EXIT_ERROR
    except KeyboardInterrupt:
        return EXIT_ERROR
    except SystemExit:
        raise
    except Exception as exc:
        manager.log("ERROR", str(exc))
        return EXIT_ERROR
    finally:
        manager.release_lock()


if __name__ == "__main__":
    raise SystemExit(main())
