#!/usr/bin/env python3
"""Create a 24-hour normal live-order release after all gates are satisfied."""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
import json

from modules.brokers.models import BrokerError
from modules.live_gate import authorize_release


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--required-shadow-sessions", type=int, default=20)
    parser.add_argument("--commit", required=True, help="verified deployed git commit")
    parser.add_argument("--confirm", default="")
    args = parser.parse_args()
    if args.confirm != "I_AUTHORIZE_NORMAL_LIVE_ORDERS_FOR_24_HOURS":
        print("explicit --confirm value is required", file=sys.stderr)
        return 2
    try:
        result = authorize_release(args.required_shadow_sessions, args.commit)
        print(json.dumps({"status": "ok", "release": result}, ensure_ascii=False, indent=2))
        return 0
    except BrokerError as exc:
        print(json.dumps({"status": "blocked", "code": exc.code, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
