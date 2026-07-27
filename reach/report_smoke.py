"""One-off smoke: run the release gate then print the built report for a run.
Usage: python report_smoke.py <run_id>   (reads DATABASE_URL from env)
"""
import sys
from pipeline import release_gate, report

run_id = int(sys.argv[1])
problems = release_gate.check(run_id)
print("GATE:", "PASS" if not problems else f"BLOCK {problems}")
if not problems:
    print("\n----- REPORT -----\n")
    print(report.build(run_id))
