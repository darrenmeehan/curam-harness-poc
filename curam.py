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
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def main() -> int:
    argv = sys.argv[1:]

    # strip global flags so ui (the task runner) sees them too; but first
    # check whether this is a curam-level command
    cmd = argv[0] if argv else None

    if cmd in ("report", "eval", "tasks"):
        return _run_command(cmd, argv[1:])

    # otherwise: interactive REPL / one-shot task → ui handles it with its
    # own argparse (task text, --config, --db, --sli, --jsonl, --quiet)
    import ui
    return ui.main()


def _run_command(cmd: str, rest: list[str]) -> int:
    from report import render_report

    if cmd == "report":
        p = _report_parser()
        args = p.parse_args(rest)
        print(render_report(config=args.config, db=args.db, since=args.since,
                            as_json=args.json))
        return 0
    if cmd == "eval":
        return subprocess.call([sys.executable, str(ROOT / "eval_corpus.py")])
    if cmd == "tasks":
        manifest = ROOT / "eval_tasks" / "manifest.json"
        if not manifest.exists():
            print("eval_tasks not shipped with the installed package — "
                  "run `curam tasks` from the repo checkout")
            return 1
        m = json.loads(manifest.read_text())
        for t in m:
            print(f"  {t['name']:<20} {t['kind']:<10} "
                  f"expect_ask={t.get('expect_ask', False)}")
        return 0
    return 1


def _report_parser():
    import argparse
    p = argparse.ArgumentParser(prog="curam report")
    p.add_argument("--config", default=str(ROOT / "curam.toml"))
    p.add_argument("--db", default=None)
    p.add_argument("--since", default=None,
                   help="only sessions in the last N (2h/30m/1d)")
    p.add_argument("--json", action="store_true")
    return p


if __name__ == "__main__":
    sys.exit(main())
