"""curam CLI entrypoint.

Wraps ui.py (the interactive REPL / one-shot agent) and adds a `report`
command that turns the session journal into an evaluation summary - the o11y
surface for judging how the harness actually performed.

Commands:
  curam                      interactive REPL
  curam "task text"          one-shot task
  curam --sli                journal summary for default db
  curam report               full performance report for the latest session(s)
  curam report --since 2h    report for sessions in the last 2 hours
  curam report --json        emit JSON instead of text
  curam eval                 run the offline eval corpus
  curam tasks                list eval tasks
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser(description="curam harness CLI")
    ap.add_argument("--config", default=str(ROOT / "curam.toml"))
    ap.add_argument("--db", default=None)
    sub = ap.add_subparsers(dest="cmd")

    p_report = sub.add_parser(
        "report", help="performance report from the session journal"
    )
    p_report.add_argument(
        "--since", default=None, help="only sessions in the last N (2h/30m/1d)"
    )
    p_report.add_argument("--json", action="store_true", help="emit JSON")

    sub.add_parser("eval", help="run the offline eval corpus")
    sub.add_parser("tasks", help="list eval tasks")

    args, _rest = ap.parse_known_args()

    if args.cmd == "report":
        from report import render_report
        print(
            render_report(
                config=args.config, db=args.db, since=args.since, as_json=args.json
            )
        )
        return 0

    if args.cmd == "eval":
        return subprocess.call([sys.executable, str(ROOT / "eval_corpus.py")])

    if args.cmd == "tasks":
        m = json.loads((ROOT / "eval_tasks" / "manifest.json").read_text())
        for t in m:
            print(
                f"  {t['name']:<20} {t['kind']:<10} "
                f"expect_ask={t.get('expect_ask', False)}"
            )
        return 0

    # default: ui REPL / one-shot
    runpy.run_path(str(ROOT / "ui.py"), run_name="__main__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
