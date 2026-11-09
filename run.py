#!/usr/bin/env python3
"""Full pipeline in one command: deploy -> unit, integration and E2E tests -> cleanup.

    python run.py              everything, including the slow disruptive tests
    python run.py --quick      skip tests that restart the database or redeploy
    python run.py --keep       leave the system running afterwards

Reports are written to reports/:
    junit.xml                   machine-readable results (for CI)
    test-case-results.csv       one row per test case ID, for docs/v<release>/test-management.xlsx
"""
import argparse
import subprocess
import sys


def run(cmd):
    print(f"\n$ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd).returncode


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--quick", action="store_true", help="skip disruptive tests")
    parser.add_argument("--keep", action="store_true", help="do not clean up afterwards")
    args = parser.parse_args()

    print("[1/4] Installing test dependencies...")
    if run([sys.executable, "-m", "pip", "install", "-q", "-r", "tests/requirements.txt"]) != 0:
        sys.exit("[ERROR] Could not install test dependencies.")
    # The browser for the E2E tests (does nothing if it is already installed).
    if run([sys.executable, "-m", "playwright", "install", "chromium"]) != 0:
        sys.exit("[ERROR] Could not install the Playwright browser.")

    print("\n[2/4] Deploying...")
    if run([sys.executable, "deploy.py"]) != 0:
        sys.exit("[ERROR] Deployment failed, tests not run.")

    print("\n[3/4] Running unit, integration and E2E tests...")
    pytest = [sys.executable, "-m", "pytest", "tests/unit", "tests/integration", "tests/e2e",
              "--junitxml=reports/junit.xml"]
    if args.quick:
        pytest += ["-m", "not disruptive"]
    tests_rc = run(pytest)

    if args.keep:
        print("\n[4/4] Skipping cleanup (--keep).")
    else:
        print("\n[4/4] Cleaning up...")
        run([sys.executable, "cleanup.py"])

    print("\n[OK] All tests passed." if tests_rc == 0 else "\n[FAIL] Some tests failed, see above.")
    sys.exit(tests_rc)


if __name__ == "__main__":
    main()
