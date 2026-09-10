#!/usr/bin/env python3.12
from __future__ import annotations

import argparse
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
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

# -----------------------------------------------------------------------------
# XOUT configuration
# -----------------------------------------------------------------------------

DEFAULT_SERVICES = [
    "xout-modeshape",
    "xout-activemq-artemis",
    "xout-artemis",  # legacy/alternative unit name
    "xout-pmc",
    "xout-portal",
    "xout-web",
    "xout-batchsplitter",
]

# Operational order requested for XOUT.
STOP_ORDER = DEFAULT_SERVICES[:]
START_ORDER = DEFAULT_SERVICES[:]

# Packages that belong to XOUT but do not start with "xout-".
EXTRA_XOUT_PACKAGES = {
    "activemq-artemis-xout",
}

# RPM package -> primary systemd service.
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

# A service can have different RPM names over time.
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

# Package update -> services that must be stopped.
# "all" means all installed xout-* services.
UPDATE_STOP_MAP: dict[str, str | list[str]] = {
    "xout-modeshape": "all",
    "activemq-artemis-xout": "all",
    "xout-activemq-artemis": "all",
    "xout-artemis": "all",
    "xout-pmc": [
        "xout-pmc",
        "xout-portal",
        "xout-web",
        "xout-batchsplitter",
    ],
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
DEFAULT_DUMP_BASE = os.environ.get(
    "DUMP_BASE_DIR",
    f"/work/dms/dumps/{socket.gethostname()}",
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_POSTCHECK = 2
EXIT_PRECHECK = 3
EXIT_LOCKED = 4
EXIT_UPDATES_AVAILABLE = 10


# -----------------------------------------------------------------------------
# Data classes
# -----------------------------------------------------------------------------

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
class PackageInfo:
    name: str
    installed: str = "-"
    available: str = "-"
    arch: str = "-"
    has_update: bool = False
    zypper_status: str = ""


@dataclass
class ModuleInfo:
    name: str
    state: str
    group: str


# -----------------------------------------------------------------------------
# Output helpers
# -----------------------------------------------------------------------------

class Colors:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.red = "\033[0;31m" if enabled else ""
        self.green = "\033[0;32m" if enabled else ""
        self.yellow = "\033[1;33m" if enabled else ""
        self.blue = "\033[0;34m" if enabled else ""
        self.cyan = "\033[0;36m" if enabled else ""
        self.purple = "\033[0;35m" if enabled else ""
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
        self.ascii_mode = ascii_mode
        if ascii_mode:
            self.tl = self.tr = self.bl = self.br = "+"
            self.h = "-"
            self.v = "|"
            self.lt = self.rt = self.tt = self.bt = self.ct = "+"
        else:
            self.tl = "╭"
            self.tr = "╮"
            self.bl = "╰"
            self.br = "╯"
            self.h = "─"
            self.v = "│"
            self.lt = "├"
            self.rt = "┤"
            self.tt = "┬"
            self.bt = "┴"
            self.ct = "┼"

    @staticmethod
    def strip_ansi(text: str) -> str:
        return re.sub(r"\x1b\[[0-9;]*m", "", text)

    def visible_len(self, text: str) -> int:
        return len(self.strip_ansi(text))

    def truncate(self, text: str, width: int) -> str:
        plain = self.strip_ansi(text)
        if plain != text:
            # Colored values in this program are deliberately short.
            return text
        if len(text) <= width:
            return text
        if width <= 3:
            return text[:width]
        return text[: width - 3] + "..."

    def pad(self, text: str, width: int, align: str = "left") -> str:
        text = self.truncate(str(text), width)
        visible = self.visible_len(text)
        if visible >= width:
            return text
        padding = " " * (width - visible)
        return padding + text if align == "right" else text + padding

    def line(self, left: str, mid: str, right: str) -> str:
        chunks = [self.h * (width + 2) for width in self.widths]
        return left + mid.join(chunks) + right

    def add_row(self, row: Iterable[Any]) -> None:
        self.rows.append([str(value) for value in row])

    def render(self, aligns: list[str] | None = None) -> str:
        aligns = aligns or ["left"] * len(self.headers)
        out = [self.line(self.tl, self.tt, self.tr)]
        out.append(
            self.v
            + self.v.join(
                f" {self.pad(header, width)} "
                for header, width in zip(self.headers, self.widths)
            )
            + self.v
        )
        out.append(self.line(self.lt, self.ct, self.rt))
        for row in self.rows:
            cells = []
            for index, (cell, width) in enumerate(zip(row, self.widths)):
                align = aligns[index] if index < len(aligns) else "left"
                cells.append(f" {self.pad(cell, width, align)} ")
            out.append(self.v + self.v.join(cells) + self.v)
        out.append(self.line(self.bl, self.bt, self.br))
        return "\n".join(out)


# -----------------------------------------------------------------------------
# XOUT manager
# -----------------------------------------------------------------------------

class XManager:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.ascii_mode = args.ascii or not self.terminal_supports_unicode()
        color_enabled = (not args.no_color) and sys.stdout.isatty()
        self.c = Colors(color_enabled)
        self.sudo_prefix: list[str] = [] if os.geteuid() == 0 else ["sudo"]
        self.repo_existed = False
        self.repo_was_enabled = False
        self.repo_touched = False
        self.lock_handle: Any = None
        self.stopped_services: list[str] = []
        self.stopped_modules: list[str] = []
        self.jolokia_root = f"http://{args.host}:{args.port}/jolokia"
        self.jolokia_modules_url = (
            f"http://{args.host}:{args.port}/jolokia/read/"
            "portal.server.modules:*/CurrentModuleInformation"
        )

    # ----- generic ----------------------------------------------------------

    @staticmethod
    def terminal_supports_unicode() -> bool:
        encoding = (sys.stdout.encoding or "").lower()
        locale_vars = " ".join(
            [
                os.environ.get("LC_ALL", ""),
                os.environ.get("LC_CTYPE", ""),
                os.environ.get("LANG", ""),
            ]
        ).lower()
        return "utf" in encoding or "utf" in locale_vars

    def log(self, level: str, message: str) -> None:
        colors = {
            "INFO": self.c.blue,
            "SUCCESS": self.c.green,
            "WARN": self.c.yellow,
            "ERROR": self.c.red,
            "DRY-RUN": self.c.yellow,
            "CMD": self.c.dim,
        }
        prefix = self.c.color(f"[{level}]", colors.get(level, ""))
        # Keep stdout clean when --json is requested.
        if self.args.json or level in {"WARN", "ERROR"}:
            stream = sys.stderr
        else:
            stream = sys.stdout
        print(f"{prefix} {message}", file=stream)

    def die(self, message: str, code: int = EXIT_ERROR) -> None:
        self.log("ERROR", message)
        raise SystemExit(code)

    @staticmethod
    def env_c() -> dict[str, str]:
        env = os.environ.copy()
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
        return env

    def run(
        self,
        cmd: list[str],
        *,
        sudo: bool = False,
        check: bool = False,
        capture: bool = True,
        dry_run_changes: bool = False,
        env: dict[str, str] | None = None,
        stdout_file: Path | None = None,
        as_user: str | None = None,
    ) -> RunResult:
        full_cmd = list(cmd)

        if as_user and getpass.getuser() != as_user:
            sudo_cmd = ["sudo"]
            if self.args.non_interactive:
                sudo_cmd.append("-n")
            full_cmd = [*sudo_cmd, "-u", as_user, *full_cmd]
        elif sudo and self.sudo_prefix:
            full_cmd = [*self.sudo_prefix, *full_cmd]

        if self.args.verbose or (self.args.dry_run and dry_run_changes):
            self.log("CMD", " ".join(shlex_quote(value) for value in full_cmd))

        if self.args.dry_run and dry_run_changes:
            self.log(
                "DRY-RUN",
                "Würde ausführen: " + " ".join(shlex_quote(value) for value in full_cmd),
            )
            return RunResult(0, "", "")

        try:
            if stdout_file is not None:
                stdout_file.parent.mkdir(parents=True, exist_ok=True)
                with stdout_file.open("w", encoding="utf-8", errors="replace") as handle:
                    proc = subprocess.run(
                        full_cmd,
                        text=True,
                        stdout=handle,
                        stderr=subprocess.PIPE,
                        env=env,
                    )
                    result = RunResult(proc.returncode, "", proc.stderr or "")
            elif capture:
                proc = subprocess.run(
                    full_cmd,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    env=env,
                )
                result = RunResult(proc.returncode, proc.stdout or "", proc.stderr or "")
            else:
                proc = subprocess.run(full_cmd, text=True, env=env)
                result = RunResult(proc.returncode, "", "")
        except FileNotFoundError:
            result = RunResult(127, "", f"Befehl nicht gefunden: {full_cmd[0]}")

        if check and result.rc != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"Exit-Code {result.rc}"
            self.die(
                "Befehl fehlgeschlagen: "
                + " ".join(shlex_quote(value) for value in full_cmd)
                + f"\n{detail}"
            )
        return result

    def ensure_sudo(self, required: bool = True) -> None:
        if not required or os.geteuid() == 0:
            return
        if self.args.non_interactive:
            result = self.run(["sudo", "-n", "true"])
            if result.rc != 0:
                self.die(
                    "sudo ohne Passwort ist nicht möglich. Für UC4/root ausführen "
                    "oder sudoers anpassen.",
                    EXIT_PRECHECK,
                )
            return
        self.log("INFO", "sudo-Berechtigung wird geprüft. Falls nötig, bitte Passwort eingeben.")
        result = self.run(["sudo", "-v"], capture=False)
        if result.rc != 0:
            self.die("sudo-Berechtigung konnte nicht bestätigt werden.", EXIT_PRECHECK)

    def check_tools(self, tools: Iterable[str]) -> None:
        missing = [tool for tool in tools if shutil.which(tool) is None]
        if missing:
            self.die("Fehlende Abhängigkeiten: " + ", ".join(missing), EXIT_PRECHECK)

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
        except OSError as exc:
            self.die(
                f"Lock-Datei kann nicht geöffnet werden: {path}: {exc}",
                EXIT_LOCKED,
            )
    
        try:
            # Vorhandene Lock-Datei auch für andere Benutzer beschreibbar machen.
            os.fchmod(fd, 0o666)
    
            handle = os.fdopen(fd, "r+", encoding="utf-8")
            fd = -1
    
            try:
                fcntl.flock(
                    handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError:
                handle.close()
                self.die(
                    f"Ein anderer xmanager-Lauf ist bereits aktiv. Lock: {path}",
                    EXIT_LOCKED,
                )
    
            # Erst NACH erfolgreichem flock() Inhalt aktualisieren.
            handle.seek(0)
            handle.truncate()
    
            handle.write(
                f"pid={os.getpid()} "
                f"user={getpass.getuser()} "
                f"host={socket.gethostname()} "
                f"time={dt.datetime.now().isoformat()}\n"
            )
            handle.flush()
    
            self.lock_handle = handle
    
        except BaseException:
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            raise

    def release_lock(self) -> None:
        if self.lock_handle is None:
            return
    
        path = Path(self.args.lock_file)
    
        try:
            fcntl.flock(
                self.lock_handle.fileno(),
                fcntl.LOCK_UN,
            )
        finally:
            try:
                self.lock_handle.close()
            finally:
                self.lock_handle = None
    
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            self.log(
                "WARN",
                f"Lock-Datei konnte nicht gelöscht werden: {path}: {exc}",
            )



    # ----- services ---------------------------------------------------------

    def service_exists(self, service: str) -> bool:
        result = self.run(
            ["systemctl", "list-unit-files", "--type=service", "--no-legend", f"{service}.service"]
        )
        if result.rc != 0:
            return False
        return any(
            fields and fields[0] == f"{service}.service"
            for fields in (line.split() for line in result.stdout.splitlines())
        )

    def discover_services(self) -> list[str]:
        result = self.run(
            ["systemctl", "list-unit-files", "--type=service", "--no-legend"],
            check=True,
        )
        installed = set()
        for line in result.stdout.splitlines():
            fields = line.split()
            if fields and fields[0].startswith("xout-") and fields[0].endswith(".service"):
                installed.add(fields[0][:-8])
        ordered = [service for service in DEFAULT_SERVICES if service in installed]
        extras = sorted(service for service in installed if service not in set(DEFAULT_SERVICES))
        return ordered + extras

    def systemctl_show(self, service: str, prop: str) -> str:
        result = self.run(["systemctl", "show", service, "-p", prop, "--value"])
        return result.stdout.strip() if result.rc == 0 else ""

    def service_active_bool(self, service: str) -> bool:
        return self.run(["systemctl", "is-active", "--quiet", service]).rc == 0

    def service_active_text(self, service: str) -> str:
        result = self.run(["systemctl", "is-active", service])
        return (result.stdout.strip() or "unknown") if result.rc in (0, 3) else "unknown"

    def service_enabled_text(self, service: str) -> str:
        result = self.run(["systemctl", "is-enabled", service])
        return (result.stdout.strip() or "unknown") if result.rc in (0, 1) else "unknown"

    def get_package_version(self, service: str) -> str:
        candidates = SERVICE_PACKAGE_CANDIDATES.get(service, [service])
        for package in candidates:
            result = self.run(
                ["rpm", "-q", package, "--queryformat", "%{VERSION}-%{RELEASE}\n"]
            )
            if result.rc == 0 and result.stdout.strip():
                return result.stdout.strip().splitlines()[0]
        return "-"

    @staticmethod
    def format_bytes(value: str) -> str:
        if not value or value in {"[not set]", "0"}:
            return "0 MB"
        try:
            number = int(value)
        except ValueError:
            return "0 MB"
        return f"{number / 1024 / 1024:.1f} MB"

    def child_pids_recursive(self, pid: str) -> list[str]:
        children: list[str] = []
        result = self.run(["pgrep", "-P", pid])
        if result.rc != 0:
            return children
        for child in result.stdout.splitlines():
            child = child.strip()
            if child:
                children.append(child)
                children.extend(self.child_pids_recursive(child))
        return children

    def memory_for_service(self, service: str) -> str:
        memory_current = self.systemctl_show(service, "MemoryCurrent")
        if memory_current and memory_current not in {"[not set]", "0"}:
            return self.format_bytes(memory_current) + " cgroup"

        main_pid = self.systemctl_show(service, "MainPID")
        if not main_pid or main_pid == "0":
            return "0 MB"

        pids = sorted(
            set([main_pid, *self.child_pids_recursive(main_pid)]),
            key=lambda value: int(value),
        )
        result = self.run(["ps", "-o", "rss=", "-p", ",".join(pids)])
        total_kb = sum(
            int(line.strip())
            for line in result.stdout.splitlines()
            if line.strip().isdigit()
        )
        return f"{total_kb / 1024:.1f} MB rss" if total_kb else "0 MB"

    def collect_service_info(self) -> list[ServiceInfo]:
        infos = []
        for service in self.discover_services():
            pid = self.systemctl_show(service, "MainPID") or "-"
            if pid == "0":
                pid = "-"
            infos.append(
                ServiceInfo(
                    name=service,
                    active=self.service_active_text(service),
                    enabled=self.service_enabled_text(service),
                    pid=pid,
                    version=self.get_package_version(service),
                    memory=self.memory_for_service(service),
                )
            )
        return infos

    def print_services(self, infos: list[ServiceInfo]) -> None:
        table = Table(
            ["Nr", "Service", "Version", "PID", "Status", "Enabled", "Memory"],
            [3, 25, 18, 9, 11, 12, 17],
            self.ascii_mode,
        )
        for index, info in enumerate(infos, 1):
            status = info.active
            enabled = info.enabled
            if self.c.enabled:
                if info.active == "active":
                    status = self.c.color(status, self.c.green)
                elif info.active in {"failed", "inactive"}:
                    status = self.c.color(status, self.c.red)
                else:
                    status = self.c.color(status, self.c.yellow)
                if info.enabled == "enabled":
                    enabled = self.c.color(enabled, self.c.green)
                elif info.enabled == "disabled":
                    enabled = self.c.color(enabled, self.c.red)
                else:
                    enabled = self.c.color(enabled, self.c.yellow)
            table.add_row(
                [index, info.name, info.version, info.pid, status, enabled, info.memory]
            )
        print(self.c.color("\nServices", self.c.bold + self.c.cyan))
        print(
            table.render(
                aligns=["right", "left", "left", "right", "left", "left", "right"]
            )
        )

    def select_services(self, selector: str) -> list[str]:
        services = self.discover_services()
        if not selector:
            self.die("Keine Services angegeben.")
        if selector == "all":
            return services
        if re.fullmatch(r"[0-9,]+", selector):
            selected = []
            for part in selector.split(","):
                index = int(part)
                if index < 1 or index > len(services):
                    self.die(f"Ungültige Servicenummer: {index}")
                selected.append(services[index - 1])
            return unique_preserve(selected)

        pattern = selector.lower()
        selected = [
            service
            for service in services
            if pattern in service.lower()
            or pattern in service.removeprefix("xout-").lower()
        ]
        if not selected:
            self.die(f"Keine Services gefunden für Pattern: {selector}")
        return selected

    @staticmethod
    def order_services(services: list[str], order: list[str]) -> list[str]:
        selected = set(services)
        result = [service for service in order if service in selected]
        result.extend(sorted(service for service in services if service not in set(order)))
        return result

    def wait_service_active(self, service: str) -> bool:
        deadline = time.time() + self.args.max_wait
        while time.time() < deadline:
            if self.service_active_bool(service):
                return True
            time.sleep(self.args.wait_interval)
        return False

    def wait_service_inactive(self, service: str) -> bool:
        deadline = time.time() + self.args.max_wait
        while time.time() < deadline:
            if not self.service_active_bool(service):
                return True
            time.sleep(self.args.wait_interval)
        return False

    def start_service_list(self, services: list[str], *, postcheck: bool = True) -> bool:
        ok = True
        for service in self.order_services(services, START_ORDER):
            if self.service_active_bool(service):
                self.log("INFO", f"{service} ist bereits aktiv.")
                continue
            self.log("INFO", f"Starte Dienst: {service}")
            result = self.run(
                ["systemctl", "start", service],
                sudo=True,
                dry_run_changes=True,
            )
            if result.rc != 0:
                self.log("ERROR", f"Start von {service} fehlgeschlagen: {result.stderr.strip()}")
                ok = False
                continue
            if postcheck and not self.args.dry_run and not self.wait_service_active(service):
                self.log("ERROR", f"{service} ist nach {self.args.max_wait}s nicht aktiv.")
                ok = False
            elif not self.args.dry_run:
                self.log("SUCCESS", f"{service} ist aktiv.")
        return ok

    def start_services(self, selector: str) -> None:
        self.ensure_sudo(True)
        self.acquire_lock()
        try:
            self.start_service_list(self.select_services(selector))
        finally:
            self.release_lock()

    def stop_services(self, selector: str) -> None:
        self.ensure_sudo(True)
        self.acquire_lock()
        try:
            for service in self.order_services(self.select_services(selector), STOP_ORDER):
                if not self.service_active_bool(service):
                    self.log("INFO", f"{service} ist nicht aktiv.")
                    continue
                self.log("INFO", f"Stoppe Dienst: {service}")
                self.run(
                    ["systemctl", "stop", service],
                    sudo=True,
                    check=True,
                    dry_run_changes=True,
                )
                if not self.args.dry_run and not self.wait_service_inactive(service):
                    self.die(
                        f"{service} ist nach {self.args.max_wait}s noch aktiv.",
                        EXIT_POSTCHECK,
                    )
        finally:
            self.release_lock()

    def restart_services(self, selector: str) -> None:
        self.ensure_sudo(True)
        self.acquire_lock()
        try:
            selected = self.select_services(selector)
            was_active = [service for service in selected if self.service_active_bool(service)]
            for service in self.order_services(selected, STOP_ORDER):
                if self.service_active_bool(service):
                    self.log("INFO", f"Stoppe Dienst: {service}")
                    self.run(
                        ["systemctl", "stop", service],
                        sudo=True,
                        check=True,
                        dry_run_changes=True,
                    )
                    if not self.args.dry_run and not self.wait_service_inactive(service):
                        self.die(f"{service} konnte nicht sauber gestoppt werden.", EXIT_POSTCHECK)
            if not self.start_service_list(was_active):
                self.die("Mindestens ein Service konnte nicht sauber gestartet werden.", EXIT_POSTCHECK)
        finally:
            self.release_lock()

    # ----- Jolokia / modules ------------------------------------------------

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
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                result = json.loads(response.read().decode("utf-8"))
                if isinstance(result, dict) and result.get("status") not in (None, 200, "200"):
                    raise RuntimeError(
                        f"Jolokia status={result.get('status')} error={result.get('error')}"
                    )
                return result
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            raise RuntimeError(f"Jolokia GET fehlgeschlagen: {url}: {exc}") from exc

    def http_json_post(self, url: str, payload: dict[str, Any]) -> Any:
        data = json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.args.http_timeout) as response:
                if response.status != 200:
                    raise RuntimeError(f"HTTP {response.status}")
                result = json.loads(response.read().decode("utf-8"))
                if str(result.get("status", "")) != "200":
                    raise RuntimeError(
                        f"Jolokia status={result.get('status')} error={result.get('error')}"
                    )
                return result
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, RuntimeError) as exc:
            raise RuntimeError(f"Jolokia POST fehlgeschlagen: {url}: {exc}") from exc

    def collect_modules(self) -> list[ModuleInfo]:
        data = self.http_json_get(self.jolokia_modules_url)
        value = data.get("value") if isinstance(data, dict) else None
        if not isinstance(value, dict):
            raise RuntimeError("Ungültige Jolokia-Antwort: Feld .value fehlt oder ist kein Objekt")
        modules = []
        for raw_name, details in value.items():
            name = self.clean_module_name(raw_name)
            state = "UNKNOWN"
            if isinstance(details, dict):
                info = details.get("CurrentModuleInformation")
                if isinstance(info, dict):
                    state = str(info.get("state", "UNKNOWN"))
            modules.append(ModuleInfo(name, state, self.module_group(name, state)))
        group_order = {"Import/Router/Merger": 1, "Andere RUNNING": 2, "Nicht-RUNNING": 3}
        return sorted(modules, key=lambda module: (group_order.get(module.group, 9), module.name.lower()))

    def print_modules(self, modules: list[ModuleInfo]) -> None:
        print(self.c.color("\nPortal-Module", self.c.bold + self.c.cyan))
        for group in ["Import/Router/Merger", "Andere RUNNING", "Nicht-RUNNING"]:
            group_modules = [module for module in modules if module.group == group]
            print(self.c.color(f"\n{group}", self.c.bold + self.c.blue))
            table = Table(["Modul", "Status"], [48, 24], self.ascii_mode)
            if not group_modules:
                table.add_row(["(keine Treffer)", "-"])
            for module in group_modules:
                state = module.state
                if self.c.enabled:
                    if module.state == "RUNNING":
                        state = self.c.color(state, self.c.green)
                    elif module.state == "NOT_IN_WORKING_TIME":
                        state = self.c.color(state, self.c.yellow)
                    else:
                        state = self.c.color(state, self.c.red)
                table.add_row([module.name, state])
            print(table.render())

    def module_status_map(self) -> dict[str, str]:
        return {module.name: module.state for module in self.collect_modules()}

    def module_exec(self, module_name: str, operation: str) -> bool:
        payload = {
            "mbean": f'portal.server.modules:module="{module_name}"',
            "arguments": ["localhost/127.0.0.1"],
            "type": "EXEC",
            "operation": operation,
        }
        try:
            self.http_json_post(self.jolokia_root + "/", payload)
            return True
        except RuntimeError as exc:
            self.log("ERROR", str(exc))
            return False

    def stop_module(self, module_name: str) -> bool:
        self.log("INFO", f"Stoppe Portal-Modul: {module_name}")
        if self.args.dry_run:
            self.log("DRY-RUN", f"Würde Portal-Modul stoppen: {module_name}")
            return True
        if not self.module_exec(module_name, "stopModule"):
            return False

        deadline = time.time() + self.args.max_wait
        while time.time() < deadline:
            state = self.module_status_map().get(module_name, "UNKNOWN")
            if state not in {"RUNNING", "UNKNOWN"}:
                self.log("SUCCESS", f"{module_name} gestoppt. Status: {state}")
                return True
            time.sleep(self.args.wait_interval)
        self.log("ERROR", f"Timeout: {module_name} ist nach {self.args.max_wait}s nicht sauber gestoppt")
        return False

    def start_module(self, module_name: str) -> bool:
        if self.args.dry_run:
            self.log("DRY-RUN", f"Würde Portal-Modul starten: {module_name}")
            return True
        current = self.module_status_map().get(module_name, "UNKNOWN")
        if current == "RUNNING":
            self.log("INFO", f"Portal-Modul {module_name} läuft bereits.")
            return True

        self.log("INFO", f"Starte Portal-Modul: {module_name}")
        if not self.module_exec(module_name, "startModule"):
            return False
        deadline = time.time() + self.args.max_wait
        while time.time() < deadline:
            state = self.module_status_map().get(module_name, "UNKNOWN")
            if state == "RUNNING":
                self.log("SUCCESS", f"{module_name} ist RUNNING.")
                return True
            time.sleep(self.args.wait_interval)
        self.log("ERROR", f"Timeout: {module_name} ist nach {self.args.max_wait}s nicht RUNNING")
        return False

    def stop_running_portal_modules(self) -> bool:
        if self.args.skip_module_stop:
            self.log("WARN", "Portal-Modul-Stop wurde per --skip-module-stop übersprungen.")
            return True
        if not self.service_active_bool("xout-portal"):
            self.log("INFO", "xout-portal ist nicht aktiv. Portal-Module müssen nicht gestoppt werden.")
            return True

        modules = [module for module in self.collect_modules() if module.state == "RUNNING"]
        modules.sort(key=lambda module: self.module_priority(module.name))
        self.stopped_modules = [module.name for module in modules]

        if not modules:
            self.log("INFO", "Keine laufenden Portal-Module gefunden.")
            return True

        self.log("INFO", f"{len(modules)} laufende Portal-Module werden gestoppt.")
        ok = True
        for module in modules:
            if not self.stop_module(module.name):
                ok = False
        return ok

    def restart_previously_running_modules(self) -> bool:
        if not self.stopped_modules:
            return True
        if not self.service_active_bool("xout-portal"):
            self.log("ERROR", "xout-portal ist nicht aktiv; Portal-Module können nicht gestartet werden.")
            return False
        ordered = sorted(self.stopped_modules, key=self.module_priority)
        ok = True
        for module in ordered:
            if not self.start_module(module):
                ok = False
        return ok

    def print_module_postcheck(self) -> None:
        if not self.stopped_modules:
            return
        try:
            states = self.module_status_map()
        except Exception:
            states = {}
        table = Table(["Modul", "Status nach Update"], [48, 24], self.ascii_mode)
        for module in sorted(self.stopped_modules, key=self.module_priority):
            table.add_row([module, states.get(module, "UNKNOWN")])
        print(self.c.color("\nPortal-Modul-Postcheck", self.c.bold + self.c.cyan))
        print(table.render())

    # ----- repository / packages -------------------------------------------

    @staticmethod
    def is_managed_package(name: str) -> bool:
        return name.startswith("xout-") or name in EXTRA_XOUT_PACKAGES

    def zypper_lr(self) -> dict[str, dict[str, str]]:
        result = self.run(["zypper", "lr"], env=self.env_c(), check=True)
        repos: dict[str, dict[str, str]] = {}
        for line in result.stdout.splitlines():
            if "|" not in line:
                continue
            parts = [part.strip() for part in line.split("|")]
            if len(parts) >= 5 and parts[0] and parts[0] not in {"#", "---"}:
                alias = parts[1]
                repos[alias] = {
                    "alias": alias,
                    "name": parts[2] if len(parts) > 2 else "",
                    "enabled": parts[3] if len(parts) > 3 else "",
                }
        return repos

    def remember_repo_state(self) -> None:
        row = self.zypper_lr().get(self.args.repo_alias)
        self.repo_existed = row is not None
        self.repo_was_enabled = bool(
            row and row.get("enabled", "").lower() in {"yes", "ja", "true", "1"}
        )

    def ensure_repo(self) -> None:
        self.remember_repo_state()
        capture = self.args.json

        if not self.repo_existed:
            self.log("INFO", f"Repository {self.args.repo_alias} nicht gefunden. Füge es temporär hinzu.")
            self.run(
                [
                    "zypper",
                    "addrepo",
                    "--check",
                    "--refresh",
                    "--gpg-auto-import-keys",
                    "--name",
                    self.args.repo_name,
                    self.args.repo_path,
                    self.args.repo_alias,
                ],
                sudo=True,
                check=True,
                capture=capture,
                env=self.env_c(),
            )
            self.repo_touched = True
        else:
            self.log("INFO", f"Repository {self.args.repo_alias} ist vorhanden.")

        self.log("INFO", f"Aktiviere Repository {self.args.repo_alias} temporär für die Updateprüfung.")
        self.run(
            ["zypper", "mr", "-e", self.args.repo_alias],
            sudo=True,
            check=True,
            capture=capture,
            env=self.env_c(),
        )
        self.repo_touched = True

        self.log("INFO", f"Aktualisiere Repository {self.args.repo_alias}.")
        self.run(
            ["zypper", "--gpg-auto-import-keys", "ref", "-r", self.args.repo_alias],
            sudo=True,
            check=True,
            capture=capture,
            env=self.env_c(),
        )

    def restore_repo(self) -> None:
        if not self.repo_touched:
            return
        try:
            if not self.repo_existed:
                self.run(
                    ["zypper", "rr", self.args.repo_alias],
                    sudo=True,
                    capture=True,
                    env=self.env_c(),
                )
            elif self.repo_was_enabled:
                self.run(
                    ["zypper", "mr", "-e", self.args.repo_alias],
                    sudo=True,
                    capture=True,
                    env=self.env_c(),
                )
            else:
                self.run(
                    ["zypper", "mr", "-d", self.args.repo_alias],
                    sudo=True,
                    capture=True,
                    env=self.env_c(),
                )
        except Exception as exc:
            self.log("WARN", f"Repository-Zustand konnte nicht vollständig wiederhergestellt werden: {exc}")
        finally:
            self.repo_touched = False

    def collect_installed_packages(self) -> dict[str, PackageInfo]:
        result = self.run(
            ["rpm", "-qa", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\t%{ARCH}\n"],
            env=self.env_c(),
            check=True,
        )
        packages = {}
        for line in result.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and self.is_managed_package(parts[0]):
                packages[parts[0]] = PackageInfo(
                    name=parts[0],
                    installed=parts[1],
                    arch=parts[2],
                )
        return dict(sorted(packages.items()))

    def collect_updates(self) -> dict[str, PackageInfo]:
        result = self.run(
            [
                "zypper",
                "--no-refresh",
                "--non-interactive",
                "list-updates",
                "-a",
                "-r",
                self.args.repo_alias,
            ],
            sudo=True,
            env=self.env_c(),
            check=True,
        )
        updates = {}
        for line in result.stdout.splitlines():
            if "|" not in line:
                continue
            parts = [part.strip() for part in line.split("|")]

            # With repo column:
            # v | xout-rollout-repo | xout-web | old | new | arch
            if len(parts) >= 6:
                zypper_status = parts[0]
                name = parts[2]
                current = parts[3]
                available = parts[4]
                arch = parts[5]
            # With -r repo:
            # v | xout-web | old | new | arch
            elif len(parts) >= 5:
                zypper_status = parts[0]
                name = parts[1]
                current = parts[2]
                available = parts[3]
                arch = parts[4]
            else:
                continue

            if not self.is_managed_package(name):
                continue
            if current.lower() in {"current version", "aktuelle version"}:
                continue
            if available.lower() in {"available version", "verfügbare version"}:
                continue

            updates[name] = PackageInfo(
                name=name,
                installed=current,
                available=available,
                arch=arch,
                has_update=True,
                zypper_status=zypper_status,
            )
            if self.args.verbose:
                self.log(
                    "INFO",
                    f"Update erkannt: {name} {current} -> {available} [{zypper_status or '-'}]",
                )
        return dict(sorted(updates.items()))

    def collect_package_overview(self) -> dict[str, PackageInfo]:
        installed = self.collect_installed_packages()
        updates = self.collect_updates()
        combined = dict(installed)
        for name, update in updates.items():
            installed_version = installed.get(
                name,
                PackageInfo(name=name, installed=update.installed),
            ).installed
            combined[name] = PackageInfo(
                name=name,
                installed=installed_version or update.installed,
                available=update.available,
                arch=update.arch,
                has_update=True,
                zypper_status=update.zypper_status,
            )
        return dict(sorted(combined.items()))

    def service_for_package_display(self, package: str) -> str:
        mapped = PACKAGE_SERVICE_MAP.get(package)
        if mapped:
            return mapped
        return package if package.startswith("xout-") else "-"

    def print_package_table(self, packages: dict[str, PackageInfo]) -> None:
        print(self.c.color("\nPaketübersicht", self.c.bold + self.c.cyan))
        table = Table(
            ["Nr", "Paket", "Service", "Installiert", "Verfügbar", "Status"],
            [3, 29, 25, 18, 18, 14],
            self.ascii_mode,
        )
        if not packages:
            table.add_row(["-", "Keine XOUT-Pakete", "-", "-", "-", "-"])
        for index, package in enumerate(packages.values(), 1):
            if package.has_update:
                text = "[UPD] Update" if self.ascii_mode else "↑ Update"
                status = self.c.color(text, self.c.yellow)
            else:
                text = "[OK] Aktuell" if self.ascii_mode else "✓ Aktuell"
                status = self.c.color(text, self.c.green)
            table.add_row(
                [
                    index,
                    package.name,
                    self.service_for_package_display(package.name),
                    package.installed,
                    package.available,
                    status,
                ]
            )
        print(table.render(aligns=["right", "left", "left", "left", "left", "left"]))
        update_count = sum(1 for package in packages.values() if package.has_update)
        if update_count:
            print(self.c.color(f"\nUpdates verfügbar: {update_count}", self.c.yellow + self.c.bold))
        else:
            print(self.c.color("\nKeine Updates verfügbar.", self.c.green + self.c.bold))

    def updates_command(self) -> dict[str, PackageInfo]:
        self.ensure_sudo(True)
        self.acquire_lock()
        try:
            self.ensure_repo()
            return self.collect_package_overview()
        finally:
            self.restore_repo()
            self.release_lock()

    def resolve_service_for_package(self, package: str, installed: set[str]) -> str | None:
        preferred = PACKAGE_SERVICE_MAP.get(package)
        candidates = []
        if preferred:
            candidates.append(preferred)
        if package in {"activemq-artemis-xout", "xout-activemq-artemis", "xout-artemis"}:
            candidates.extend(["xout-activemq-artemis", "xout-artemis"])
        if package.startswith("xout-"):
            candidates.append(package)
        for candidate in unique_preserve(candidates):
            if candidate in installed:
                return candidate
        return None

    def affected_services_for_updates(self, packages: dict[str, PackageInfo]) -> list[str]:
        installed_services = self.discover_services()
        installed_set = set(installed_services)
        update_names = [package.name for package in packages.values() if package.has_update]

        if self.args.stop_all_xout:
            return self.order_services(installed_services, STOP_ORDER)

        affected: set[str] = set()
        for package in update_names:
            policy = UPDATE_STOP_MAP.get(package)
            if policy == "all":
                self.log("INFO", f"{package}: alle XOUT-Dienste werden für das Update gestoppt.")
                return self.order_services(installed_services, STOP_ORDER)
            if isinstance(policy, list):
                affected.update(service for service in policy if service in installed_set)
                continue

            service = self.resolve_service_for_package(package, installed_set)
            if service:
                affected.add(service)
            else:
                # Unknown managed package: fail safe and stop all XOUT services.
                self.log(
                    "WARN",
                    f"Keine sichere Service-Zuordnung für {package}. "
                    "Aus Sicherheitsgründen werden alle XOUT-Dienste gestoppt.",
                )
                return self.order_services(installed_services, STOP_ORDER)

        return self.order_services(list(affected), STOP_ORDER)

    def install_updates(self, packages: dict[str, PackageInfo]) -> None:
        updates = [package for package in packages.values() if package.has_update]
        if not updates:
            self.log("INFO", "Keine Updates zu installieren.")
            return

        names = [package.name for package in updates]
        command = [
            "zypper",
            "-n",
            "--no-gpg-checks",
            "update",
        ]
        if any(package.zypper_status.lower() == "v" for package in updates):
            command.append("--allow-vendor-change")
            self.log("WARN", "Vendor-Change-Update erkannt; --allow-vendor-change wird verwendet.")
        command.extend(["--repo", self.args.repo_alias, *names])

        self.log("INFO", "Installiere Updates: " + ", ".join(names))
        self.run(
            command,
            sudo=True,
            check=True,
            dry_run_changes=True,
            capture=self.args.json,
            env=self.env_c(),
        )

    def verify_updated_packages(self, packages: dict[str, PackageInfo]) -> bool:
        ok = True
        for package in packages.values():
            if not package.has_update:
                continue
            result = self.run(
                ["rpm", "-q", package.name, "--queryformat", "%{VERSION}-%{RELEASE}\n"]
            )
            actual = result.stdout.strip().splitlines()[0] if result.rc == 0 and result.stdout.strip() else "-"
            if self.args.dry_run:
                continue
            if actual != package.available:
                self.log(
                    "ERROR",
                    f"Versionsprüfung fehlgeschlagen: {package.name}: "
                    f"erwartet {package.available}, installiert {actual}",
                )
                ok = False
            else:
                self.log("SUCCESS", f"{package.name} erfolgreich aktualisiert auf {actual}.")
        return ok

    def recover_update_state(self) -> None:
        if self.args.dry_run:
            return

        if self.stopped_services:
            self.log(
                "WARN",
                "Fehler während des Updates. Versuche zuvor laufende Dienste wieder zu starten.",
            )
            self.start_service_list(self.stopped_services, postcheck=True)

        if self.stopped_modules and self.service_active_bool("xout-portal"):
            self.log(
                "WARN",
                "Versuche zuvor laufende Portal-Module wiederherzustellen.",
            )
            self.restart_previously_running_modules()

    def update_command(self) -> int:
        self.ensure_sudo(True)
        self.acquire_lock()
        service_postcheck_failed = False
        package_postcheck_failed = False
        module_postcheck_failed = False

        try:
            self.ensure_repo()
            packages = self.collect_package_overview()
            if self.args.json:
                print(
                    json.dumps(
                        {"packages": [package.__dict__ for package in packages.values()]},
                        indent=2,
                        ensure_ascii=False,
                    )
                )
            else:
                self.print_package_table(packages)

            update_names = [package.name for package in packages.values() if package.has_update]
            if not update_names:
                self.log("SUCCESS", "Keine Updates verfügbar. Es wird nichts geändert.")
                return EXIT_OK

            affected = self.affected_services_for_updates(packages)
            if affected:
                self.log("INFO", "Betroffene Dienste: " + ", ".join(affected))
            else:
                self.log("INFO", "Keine betroffenen systemd-Dienste gefunden.")

            # Whenever portal will be stopped, stop ALL currently running portal modules first.
            if "xout-portal" in affected and self.service_active_bool("xout-portal"):
                if not self.stop_running_portal_modules():
                    self.die(
                        "Mindestens ein Portal-Modul konnte nicht sauber gestoppt werden. "
                        "Update wird aus Sicherheitsgründen abgebrochen.",
                        EXIT_POSTCHECK,
                    )

            for service in affected:
                if not self.service_active_bool(service):
                    self.log("INFO", f"{service} ist nicht aktiv. Kein Stop nötig.")
                    continue
                self.log("INFO", f"Stoppe Dienst: {service}")
                self.run(
                    ["systemctl", "stop", service],
                    sudo=True,
                    check=True,
                    dry_run_changes=True,
                )
                self.stopped_services.append(service)
                if not self.args.dry_run and not self.wait_service_inactive(service):
                    self.die(
                        f"{service} konnte nicht innerhalb von {self.args.max_wait}s gestoppt werden.",
                        EXIT_POSTCHECK,
                    )

            self.install_updates(packages)
            if not self.verify_updated_packages(packages):
                package_postcheck_failed = True

            if not self.start_service_list(self.stopped_services, postcheck=True):
                service_postcheck_failed = True

            # Explicitly restore all modules that were RUNNING before the update.
            if self.stopped_modules and not self.args.dry_run:
                if not self.restart_previously_running_modules():
                    module_postcheck_failed = True
                self.print_module_postcheck()

            if package_postcheck_failed or service_postcheck_failed or module_postcheck_failed:
                return EXIT_POSTCHECK
            return EXIT_OK

        except BaseException:
            # Do not leave previously running services down after an install/precheck failure.
            self.recover_update_state()
            raise
        finally:
            self.restore_repo()
            self.release_lock()

    # ----- dumps ------------------------------------------------------------

    def create_dumps(self) -> None:
        self.ensure_sudo(True)
        self.acquire_lock()
        try:
            service = "xout-portal"
            pid = self.systemctl_show(service, "MainPID")
            if not pid or pid == "0":
                self.die("xout-portal läuft nicht oder hat keine MainPID.")

            timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
            dump_dir = Path(self.args.dump_base_dir) / timestamp
            if self.args.dry_run:
                self.log("DRY-RUN", f"Würde Dump-Verzeichnis erstellen: {dump_dir}")
            else:
                dump_dir.mkdir(parents=True, exist_ok=True)

            thread_file = dump_dir / f"xout-portal_{pid}_{timestamp}_thread.txt"
            heap_file = dump_dir / f"xout-portal_{pid}_{timestamp}_heap.hprof"

            if shutil.which("jcmd"):
                self.log("INFO", f"Erstelle Thread-Dump: {thread_file}")
                self.run(
                    ["jcmd", pid, "Thread.print", "-l"],
                    dry_run_changes=True,
                    stdout_file=thread_file,
                    as_user=self.args.portal_user,
                )
                self.log("INFO", f"Erstelle Heap-Dump: {heap_file}")
                self.run(
                    ["jcmd", pid, "GC.heap_dump", str(heap_file)],
                    dry_run_changes=True,
                    as_user=self.args.portal_user,
                    capture=False,
                )
            else:
                if not shutil.which("jstack") or not shutil.which("jmap"):
                    self.die("Weder jcmd noch jstack/jmap gefunden. Dumps können nicht erstellt werden.")
                self.log("INFO", f"Erstelle Thread-Dump: {thread_file}")
                self.run(
                    ["jstack", "-l", pid],
                    dry_run_changes=True,
                    stdout_file=thread_file,
                    as_user=self.args.portal_user,
                )
                self.log("INFO", f"Erstelle Heap-Dump: {heap_file}")
                self.run(
                    ["jmap", f"-dump:format=b,file={heap_file}", pid],
                    dry_run_changes=True,
                    as_user=self.args.portal_user,
                    capture=False,
                )
            self.log("SUCCESS", f"Dumps erstellt unter: {dump_dir}")
        finally:
            self.release_lock()

    # ----- interactive ------------------------------------------------------

    def interactive(self) -> None:
        while True:
            try:
                os.system("clear")
                self.print_services(self.collect_service_info())
                print("\nBefehle:")
                print("  l                  Services neu laden")
                print("  m                  Portal-Module anzeigen")
                print("  u                  Updates prüfen")
                print("  U                  Updates installieren")
                print("  s 1,2|all|portal   Services starten")
                print("  p 1,2|all|portal   Services stoppen")
                print("  r 1,2|all|portal   Services neu starten")
                print("  d                  Portal-Dumps erstellen")
                print("  q                  Beenden")
                raw = input("\nBefehl> ").strip()
                if not raw:
                    continue
                parts = raw.split(maxsplit=1)
                command = parts[0]
                argument = parts[1] if len(parts) > 1 else ""

                if command.lower() in {"q", "quit", "exit"}:
                    return
                if command.lower() in {"l", "list"}:
                    continue
                if command.lower() in {"m", "modules"}:
                    self.print_modules(self.collect_modules())
                elif command == "u":
                    packages = self.updates_command()
                    self.print_package_table(packages)
                elif command == "U":
                    rc = self.update_command()
                    if rc != 0:
                        self.log("WARN", f"Update endete mit Exit-Code {rc}")
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
                input("\nWeiter mit Enter...")
            except KeyboardInterrupt:
                print()
                return
            except SystemExit as exc:
                self.log("ERROR", f"Befehl abgebrochen (Exit {exc.code}).")
                input("\nWeiter mit Enter...")
            except Exception as exc:
                self.log("ERROR", str(exc))
                input("\nWeiter mit Enter...")


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def unique_preserve(items: Iterable[str]) -> list[str]:
    seen = set()
    output = []
    for item in items:
        if item not in seen:
            output.append(item)
            seen.add(item)
    return output


def shlex_quote(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9_./:=@%+,\-]+", value):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xmanager.py",
        description="XOUT Manager: Services, Portal-Module, Updates und Dumps verwalten.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Jolokia Host, Default: {DEFAULT_HOST}")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Jolokia Port, Default: {DEFAULT_PORT}")
    parser.add_argument("--repo-alias", default=DEFAULT_REPO_ALIAS, help=f"Zypper Repo Alias, Default: {DEFAULT_REPO_ALIAS}")
    parser.add_argument("--repo-name", default=DEFAULT_REPO_NAME, help=f"Zypper Repo Name, Default: {DEFAULT_REPO_NAME}")
    parser.add_argument("--repo-path", default=DEFAULT_REPO_PATH, help=f"Zypper Repo Pfad, Default: {DEFAULT_REPO_PATH}")
    parser.add_argument("--lock-file", default=DEFAULT_LOCK_FILE, help=f"Lock-Datei, Default: {DEFAULT_LOCK_FILE}")
    parser.add_argument("--dump-base-dir", default=DEFAULT_DUMP_BASE, help=f"Dump-Basisverzeichnis, Default: {DEFAULT_DUMP_BASE}")
    parser.add_argument("--portal-user", default=DEFAULT_PORTAL_USER, help=f"Portal-User für Dumps, Default: {DEFAULT_PORTAL_USER}")
    parser.add_argument("--max-wait", type=int, default=60, help="Maximale Wartezeit für Services/Module")
    parser.add_argument("--wait-interval", type=int, default=2, help="Warteintervall in Sekunden")
    parser.add_argument("--http-timeout", type=int, default=10, help="HTTP Timeout für Jolokia")
    parser.add_argument("--dry-run", "-d", action="store_true", help="Änderungen nur simulieren")
    parser.add_argument("--verbose", "-v", action="store_true", help="Ausführliche Ausgabe")
    parser.add_argument("--no-color", action="store_true", help="Farben deaktivieren")
    parser.add_argument("--ascii", action="store_true", help="ASCII statt Unicode-Tabellen")
    parser.add_argument("--json", action="store_true", help="Maschinenlesbare JSON-Ausgabe")
    parser.add_argument("--non-interactive", action="store_true", help="Kein sudo-Passwortprompt")
    parser.add_argument("--skip-module-stop", action="store_true", help="Portal-Module beim Update nicht stoppen")
    parser.add_argument("--stop-all-xout", action="store_true", help="Bei jedem Update alle XOUT-Dienste stoppen")
    parser.add_argument("--fail-on-updates", action="store_true", help="Exit 10 wenn Updates vorhanden sind")

    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("list", help="Services anzeigen")
    subparsers.add_parser("modules", help="Portal-Module anzeigen")
    subparsers.add_parser("updates", help="Installierte und verfügbare Updates anzeigen")
    subparsers.add_parser("update", help="Updates installieren")
    subparsers.add_parser("dump", help="Thread- und Heap-Dumps für xout-portal erstellen")
    subparsers.add_parser("interactive", aliases=["i"], help="Interaktiven Modus starten")

    for command in ("start", "stop", "restart"):
        subparser = subparsers.add_parser(command, help=f"Services {command}")
        subparser.add_argument("selector", help="all, Nummern 1,2 oder Pattern wie portal/web")
    return parser


def preprocess_argv(argv: list[str]) -> list[str]:
    """Allow global options both before and after the subcommand."""
    if len(argv) >= 2 and argv[1] in {"-update", "--update"}:
        argv = [argv[0], "update", *argv[2:]]
    elif len(argv) >= 2 and argv[1] in {"-i", "--interactive"}:
        argv = [argv[0], "interactive", *argv[2:]]

    commands = {
        "list",
        "modules",
        "updates",
        "update",
        "dump",
        "interactive",
        "i",
        "start",
        "stop",
        "restart",
    }
    flag_options = {
        "--dry-run",
        "-d",
        "--verbose",
        "-v",
        "--no-color",
        "--ascii",
        "--json",
        "--non-interactive",
        "--skip-module-stop",
        "--stop-all-xout",
        "--fail-on-updates",
    }
    value_options = {
        "--host",
        "--port",
        "--repo-alias",
        "--repo-name",
        "--repo-path",
        "--lock-file",
        "--dump-base-dir",
        "--portal-user",
        "--max-wait",
        "--wait-interval",
        "--http-timeout",
    }

    args = argv[1:]
    command_index = next((i for i, value in enumerate(args) if value in commands), None)
    if command_index is None:
        return argv

    before = args[:command_index]
    command = args[command_index]
    rest = args[command_index + 1 :]
    moved = []
    remaining = []
    index = 0
    while index < len(rest):
        value = rest[index]
        if value in flag_options:
            moved.append(value)
            index += 1
            continue
        if value in value_options:
            if index + 1 < len(rest):
                moved.extend([value, rest[index + 1]])
                index += 2
            else:
                remaining.append(value)
                index += 1
            continue
        if any(value.startswith(option + "=") for option in value_options):
            moved.append(value)
            index += 1
            continue
        remaining.append(value)
        index += 1

    return [argv[0], *before, *moved, command, *remaining]


def main() -> int:
    argv = preprocess_argv(sys.argv)
    parser = build_parser()
    args = parser.parse_args(argv[1:])
    if not args.command:
        args.command = "list"

    manager = XManager(args)
    try:
        if args.command == "list":
            manager.check_tools(["systemctl", "rpm", "ps", "pgrep"])
            infos = manager.collect_service_info()
            if args.json:
                print(json.dumps([info.__dict__ for info in infos], indent=2, ensure_ascii=False))
            else:
                manager.print_services(infos)
            return EXIT_OK

        if args.command == "modules":
            modules = manager.collect_modules()
            if args.json:
                print(json.dumps([module.__dict__ for module in modules], indent=2, ensure_ascii=False))
            else:
                manager.print_modules(modules)
            return EXIT_OK

        if args.command == "updates":
            tools = ["rpm", "zypper"] + (["sudo"] if os.geteuid() != 0 else [])
            manager.check_tools(tools)
            packages = manager.updates_command()
            if args.json:
                print(json.dumps([package.__dict__ for package in packages.values()], indent=2, ensure_ascii=False))
            else:
                manager.print_package_table(packages)
            update_count = sum(1 for package in packages.values() if package.has_update)
            return EXIT_UPDATES_AVAILABLE if update_count and args.fail_on_updates else EXIT_OK

        if args.command == "update":
            tools = ["rpm", "zypper", "systemctl"] + (["sudo"] if os.geteuid() != 0 else [])
            manager.check_tools(tools)
            return manager.update_command()

        if args.command == "start":
            manager.check_tools(["systemctl"] + (["sudo"] if os.geteuid() != 0 else []))
            manager.start_services(args.selector)
            return EXIT_OK

        if args.command == "stop":
            manager.check_tools(["systemctl"] + (["sudo"] if os.geteuid() != 0 else []))
            manager.stop_services(args.selector)
            return EXIT_OK

        if args.command == "restart":
            manager.check_tools(["systemctl"] + (["sudo"] if os.geteuid() != 0 else []))
            manager.restart_services(args.selector)
            return EXIT_OK

        if args.command == "dump":
            manager.check_tools(["systemctl"] + (["sudo"] if os.geteuid() != 0 else []))
            manager.create_dumps()
            return EXIT_OK

        if args.command in {"interactive", "i"}:
            manager.interactive()
            return EXIT_OK

        parser.print_help()
        return EXIT_ERROR

    except KeyboardInterrupt:
        print()
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
