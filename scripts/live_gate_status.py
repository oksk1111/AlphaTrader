#!/usr/bin/env python3
"""Print the evidence required before authorizing normal live orders."""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import json

from modules.live_gate import gate_status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--required-shadow-sessions", type=int, default=20)
    args = parser.parse_args()
    report = gate_status(args.required_shadow_sessions)
    report["status"] = "ready" if all(
        checks["shadow_ok"] and checks["preflight_ok"] and checks["smoke_ok"]
        for checks in report["markets"].values()
    ) else "blocked"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "ready" else 1


if __name__ == "__main__":
    sys.exit(main())
