"""Shared pytest setup for every test level.

Each test is linked to a test case in docs/test-management.xlsx with a marker:

    @pytest.mark.tc("UT-001")

After a run, reports/test-case-results.csv lists every test case ID that ran,
with the columns of the Test Results sheet (ID, Status, Executed on, Notes),
so the results can be copied into the workbook.

Status rules (same values as the workbook's Status list):
    Pass     every test linked to the ID passed
    Fail     at least one linked test failed
    Blocked  a linked test could not run (skipped, or its setup failed,
             e.g. the cluster was not reachable)
"""
import csv
from datetime import date
from pathlib import Path

import pytest

# Fixtures for the integration and E2E tests (api, new_user, set_stock, ...).
from system_fixtures import *  # noqa: F401,F403

REPORT = Path(__file__).resolve().parents[1] / "reports" / "test-case-results.csv"
SEVERITY = {"Pass": 0, "Blocked": 1, "Fail": 2}
_results = {}  # test case id -> {"status": str, "notes": [str]}


def _record(tc_id, status, note):
    entry = _results.setdefault(tc_id, {"status": "Pass", "notes": []})
    if SEVERITY[status] > SEVERITY[entry["status"]]:
        entry["status"] = status
    if note:
        entry["notes"].append(note)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    marker = item.get_closest_marker("tc")
    if marker is None:
        return
    tc_id = marker.args[0]
    if report.when == "setup" and report.failed:
        _record(tc_id, "Blocked", f"{item.name}: setup failed")
    elif report.skipped:
        _record(tc_id, "Blocked", f"{item.name}: skipped")
    elif report.when == "call":
        if report.failed:
            _record(tc_id, "Fail", f"{item.name}: failed")
        else:
            _record(tc_id, "Pass", "")


def pytest_sessionfinish(session, exitstatus):
    if not _results:
        return
    REPORT.parent.mkdir(exist_ok=True)
    today = date.today().isoformat()
    with REPORT.open("w", newline="", encoding="utf-8-sig") as f:
        # "sep=;" makes Excel open the file correctly in every locale.
        f.write("sep=;\n")
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["ID", "Status", "Executed on", "Notes / defect"])
        for tc_id in sorted(_results):
            entry = _results[tc_id]
            notes = "; ".join(entry["notes"]) or "Automated (pytest)"
            writer.writerow([tc_id, entry["status"], today, notes])
