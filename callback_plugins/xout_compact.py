#!/usr/bin/env python3
"""Compact stdout callback for XOUT operator workflows.

The callback intentionally keeps Ansible execution semantics untouched. It only
changes presentation: operator-relevant phases, XOUT debug summaries, failures
and the final result remain visible while routine task/loop/skip noise is hidden.
"""
from __future__ import annotations

import re
import time
from typing import Any

from ansible.plugins.callback import CallbackBase


DOCUMENTATION = r"""
name: xout_compact
type: stdout
short_description: Compact operator output for XOUT orchestration
description:
  - Shows XOUT lifecycle phases, plans, status, warnings, failures and a final summary.
  - Suppresses routine Ansible task, loop and skip noise.
requirements:
  - Set as the stdout callback.
"""


class CallbackModule(CallbackBase):
    CALLBACK_VERSION = 2.0
    CALLBACK_TYPE = "stdout"
    CALLBACK_NAME = "xout_compact"

    _PHASE_RULES = (
        (r"^Build XOUT package plan$", "Preflight / package plan"),
        (r"^Disable Pegasus before", "Pegasus OFF"),
        (r"^Quiesce Portal modules", "Portal modules quiesce"),
        (r"^Stop XOUT environment$", "Stop XOUT"),
        (r"^Wait for manual UDU installation$", "Manual UDU"),
        (r"^Apply planned RPM changes$", "Apply RPM changes"),
        (r"^Ensure CUPS", "CUPS"),
        (r"^Start XOUT environment$", "Start XOUT"),
        (r"^Restore .*Portal module", "Portal modules restore"),
        (r"^Enable Pegasus after", "Pegasus ON"),
        (r"^Enforce disabled boot policy$", "Boot policy"),
        (r"^Stop selected component only$", "Stop selected component"),
        (r"^Apply selected component RPM changes$", "Apply component RPM"),
        (r"^Start selected component only$", "Start selected component"),
        (r"^Verify isolated component result$", "Component postcheck"),
        (r"^Verify final full XOUT runtime state$", "Postcheck"),
        (r"^Show final XOUT status$", "Final status"),
        (r"^Show XOUT environment status$", "Status"),
    )

    _IMPORTANT_DEBUG_PREFIXES = (
        "UDU:",
        "Isolated component mode:",
        "No RPM change",
        "No RPM changes",
        "XOUT already DOWN",
        "XOUT postcheck passed:",
        "Isolated component update passed",
        "Portal module quiesce failed",
        "WARNING:",
        "Warning:",
    )

    def __init__(self) -> None:
        super().__init__()
        self._started = time.monotonic()
        self._phase: str | None = None
        self._phase_failed = False
        self._had_failure = False

    @staticmethod
    def _task_name(task: Any) -> str:
        name = task.get_name().strip()
        if " : " in name:
            name = name.rsplit(" : ", 1)[-1]
        return name

    @staticmethod
    def _result_host(result: Any) -> str:
        data = result._result or {}
        item = data.get("item")
        if isinstance(item, str) and item:
            return item
        delegated = data.get("_ansible_delegated_vars") or {}
        for key in ("inventory_hostname", "ansible_host"):
            value = delegated.get(key)
            if value:
                return str(value)
        return result._host.get_name()

    def _phase_label(self, name: str) -> str | None:
        for pattern, label in self._PHASE_RULES:
            if re.search(pattern, name):
                return label
        return None

    def _finish_phase(self) -> None:
        if self._phase is None:
            return
        if not self._phase_failed:
            self._display.display(f"  [OK]   {self._phase}")
        self._phase = None
        self._phase_failed = False

    def _start_phase(self, label: str) -> None:
        if self._phase == label:
            return
        self._finish_phase()
        self._phase = label
        self._phase_failed = False
        self._display.display(f"  [...]  {label}")

    def _start_result_phase(self, result: Any) -> None:
        """Start a phase only after Ansible actually executed its include/task.

        v2_playbook_on_task_start fires before ``when`` evaluation, so using it
        would render phases for skipped workflows (for example Pegasus OFF during
        a read-only status action). Runner result callbacks occur after condition
        evaluation and therefore reflect real execution.
        """
        label = self._phase_label(self._task_name(result._task))
        if label:
            self._start_phase(label)

    def v2_runner_on_ok(self, result: Any) -> None:
        self._start_result_phase(result)

        task = result._task
        action = str(getattr(task, "action", ""))
        if not action.endswith("debug"):
            return
        msg = (result._result or {}).get("msg")
        if not isinstance(msg, str) or not msg.strip():
            return
        text = msg.strip()
        if text.startswith("XOUT PLAN -"):
            self._render_plan(text)
        elif text.startswith("XOUT STATUS -"):
            self._render_status(text)
        elif text.startswith(self._IMPORTANT_DEBUG_PREFIXES):
            self._display.display(self._compact_whitespace(text))

    def v2_runner_on_failed(self, result: Any, ignore_errors: bool = False) -> None:
        self._start_result_phase(result)
        if ignore_errors:
            self._display.display(
                f"  [WARN] {self._task_name(result._task)} - {self._result_host(result)}"
            )
            self._show_failure_detail(result)
            return
        self._had_failure = True
        if self._phase is not None and not self._phase_failed:
            self._display.display(f"  [FAIL] {self._phase}")
            self._phase_failed = True
        else:
            self._display.display(
                f"  [FAIL] {self._task_name(result._task)} - {self._result_host(result)}"
            )
        self._show_failure_detail(result)

    def v2_runner_on_unreachable(self, result: Any) -> None:
        self._start_result_phase(result)
        self._had_failure = True
        if self._phase is not None and not self._phase_failed:
            self._display.display(f"  [FAIL] {self._phase}")
            self._phase_failed = True
        self._display.display(f"    host: {self._result_host(result)}")
        data = result._result or {}
        self._display.display(f"    unreachable: {data.get('msg', 'connection failed')}")

    def v2_playbook_on_no_hosts_matched(self) -> None:
        self._had_failure = True
        self._display.display("  [FAIL] No hosts matched the play")

    def v2_playbook_on_stats(self, stats: Any) -> None:
        self._finish_phase()
        totals = {"ok": 0, "changed": 0, "failures": 0, "unreachable": 0}
        for host in sorted(stats.processed):
            summary = stats.summarize(host)
            for key in totals:
                totals[key] += int(summary.get(key, 0))
        failed = self._had_failure or totals["failures"] > 0 or totals["unreachable"] > 0
        duration = int(time.monotonic() - self._started)
        minutes, seconds = divmod(duration, 60)

        self._display.display("")
        self._display.display("RESULT")
        self._display.display(f"  {'FAILED' if failed else 'SUCCESS'}")
        self._display.display(f"  Changed     : {totals['changed']}")
        self._display.display(f"  Failed      : {totals['failures']}")
        self._display.display(f"  Unreachable : {totals['unreachable']}")
        self._display.display(f"  Duration    : {minutes:02d}:{seconds:02d}")

    def _show_failure_detail(self, result: Any) -> None:
        data = result._result or {}
        self._display.display(f"    task: {self._task_name(result._task)}")
        self._display.display(f"    host: {self._result_host(result)}")
        if data.get("_ansible_no_log"):
            self._display.display("    detail: hidden because no_log is enabled")
            return
        if "rc" in data:
            self._display.display(f"    rc: {data['rc']}")
        for key in ("msg", "stderr", "stdout", "exception"):
            value = data.get(key)
            if value in (None, ""):
                continue
            value = str(value).strip()
            if not value:
                continue
            self._display.display(f"    {key}:")
            for line in value.splitlines():
                self._display.display(f"      {line}")

    def _render_plan(self, text: str) -> None:
        lines = text.splitlines()
        metadata: list[str] = []
        changes: list[tuple[str, str, str, str, str]] = []
        noop_count = 0
        start_order: list[str] = []
        stop_order: list[str] = []
        section = "meta"
        host = ""
        package_re = re.compile(r"^\s+(\S+)\s+(\S+)\s+->\s+(\S+)\s+\[([^]]+)\]\s*$")
        order_re = re.compile(r"^\s*\d+\.\s+(.+?)\s+->\s+(.+)\s*$")

        for raw in lines:
            line = raw.rstrip()
            stripped = line.strip()
            if not stripped:
                continue
            if stripped == "PACKAGE PLAN":
                section = "packages"
                continue
            if stripped == "START ORDER":
                section = "start"
                continue
            if stripped == "STOP ORDER":
                section = "stop"
                continue

            if section == "meta":
                if stripped.startswith(("XOUT PLAN -", "Target mode:", "Scope:", "Downgrade allowed:")):
                    metadata.append(stripped)
                continue

            if section == "packages":
                match = package_re.match(line)
                if match:
                    package, installed, target, action = match.groups()
                    if action == "noop":
                        noop_count += 1
                    else:
                        changes.append((host, package, installed, target, action))
                elif not line.startswith((" ", "\t")):
                    host = stripped
                elif "PRECHECK FAILED" in stripped:
                    changes.append((host, "PRECHECK", "-", "-", "error"))
                continue

            if section in ("start", "stop"):
                match = order_re.match(line)
                if match:
                    entry = f"{match.group(1)} -> {match.group(2)}"
                    (start_order if section == "start" else stop_order).append(entry)

        self._display.display("")
        for item in metadata:
            self._display.display(item)
        self._display.display("")
        self._display.display("PACKAGE CHANGES")
        if changes:
            for item_host, package, installed, target, action in changes:
                marker = "FAIL" if action == "error" else "CHANGE"
                self._display.display(
                    f"  [{marker}] {item_host}  {package}  {installed} -> {target}  ({action.upper()})"
                )
        else:
            self._display.display("  [OK] No package changes required")
        if noop_count:
            self._display.display(f"  Unchanged targets: {noop_count}")
        if start_order:
            self._display.display("")
            self._display.display("START")
            for entry in start_order:
                self._display.display(f"  {entry}")
        if stop_order:
            self._display.display("STOP")
            for entry in stop_order:
                self._display.display(f"  {entry}")

    def _render_status(self, text: str) -> None:
        self._display.display("")
        for line in text.splitlines():
            if line.strip():
                self._display.display(line.rstrip())

    @staticmethod
    def _compact_whitespace(text: str) -> str:
        return " ".join(text.split())