"""curam report - turn the session journal into an evaluation report.

The o11y surface: given the SQLite journal (or a JSONL export), render a
per-session performance summary. This is what answers "how did the harness
actually perform?" after using it - cost, tool usage, asks, skills, guardrail
events, completion, timing.

Usage (via curam.py):
  curam report
  curam report --since 2h
  curam report --json
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import agent as agentmod
from journal import Journal
from ui import sli_summary

APP_DIR = Path.home() / ".local" / "share" / "curam"
DEFAULT_DB = APP_DIR / "curam.db"


def _default_db(config: str | None, db: str | None) -> Path:
    if db:
        return Path(os.path.expanduser(db))
    if config:
        try:
            cfg = agentmod.load_config(config)
            j = cfg.get("journal")
            if j:
                return Path(os.path.expanduser(j))
        except FileNotFoundError:
            pass
    return DEFAULT_DB


def _parse_since(s: str) -> timedelta:
    s = s.strip().lower()
    n = int(s[:-1])
    unit = s[-1]
    unit_map = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return timedelta(seconds=n * unit_map.get(unit, 3600))


def _aggregate(journal: Journal, since: timedelta | None = None) -> dict:
    """Collapse events into per-session + totals. Returns a report dict."""
    evs = journal.events()
    cutoff = (
        datetime.now(timezone.utc) - since if since else None
    )
    totals = {
        "events": 0,
        "sessions": 0,
        "tool_calls": 0,
        "tool_errors": 0,
        "denied": 0,
        "asks": 0,
        "approvals_requested": 0,
        "approvals_granted": 0,
        "cost_usd": 0.0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "errors": 0,
        "skills_used": 0,
        "tool_by_name": {},
        "skill_by_name": {},
    }
    per_session: dict[str, dict] = {}

    def _sess(sid: str) -> dict:
        if sid not in per_session:
            per_session[sid] = {
                "events": 0, "tool_calls": 0, "tool_errors": 0, "denied": 0,
                "asks": 0, "cost_usd": 0.0, "prompt_tokens": 0,
                "completion_tokens": 0, "errors": 0, "skills_used": 0,
                "tool_by_name": {}, "skill_by_name": {},
            }
        return per_session[sid]

    for e in evs:
        if cutoff is not None and datetime.fromtimestamp(e["ts"], timezone.utc) < cutoff:
            continue
        totals["events"] += 1
        sid = e["session_id"]
        s = _sess(sid)
        s["events"] += 1
        et = e["etype"]
        p = e["payload"]

        if et == "tool_call":
            totals["tool_calls"] += 1
            s["tool_calls"] += 1
            name = p.get("name", "?")
            totals["tool_by_name"][name] = totals["tool_by_name"].get(name, 0) + 1
            s["tool_by_name"][name] = s["tool_by_name"].get(name, 0) + 1
        elif et == "tool_denied":
            totals["denied"] += 1
            s["denied"] += 1
        elif et == "ask_human":
            totals["asks"] += 1
            s["asks"] += 1
        elif et == "approval_requested":
            totals["approvals_requested"] += 1
        elif et == "approval_granted":
            totals["approvals_granted"] += 1
        elif et == "cost":
            c = p.get("cost_usd", 0) or 0
            totals["cost_usd"] += float(c)
            s["cost_usd"] += float(c)
            totals["prompt_tokens"] += p.get("prompt_tokens", 0) or 0
            totals["completion_tokens"] += p.get("completion_tokens", 0) or 0
            s["prompt_tokens"] += p.get("prompt_tokens", 0) or 0
            s["completion_tokens"] += p.get("completion_tokens", 0) or 0
        elif et == "error":
            totals["errors"] += 1
            s["errors"] += 1
        elif et == "skill_used":
            totals["skills_used"] += 1
            s["skills_used"] += 1
            name = p.get("name", "?")
            totals["skill_by_name"][name] = totals["skill_by_name"].get(name, 0) + 1
            s["skill_by_name"][name] = s["skill_by_name"].get(name, 0) + 1
        # tool_errors: we don't have a dedicated event; approximate from
        # tool_result with ok:false
        elif et == "tool_result" and not p.get("result", {}).get("ok", True):
            totals["tool_errors"] += 1
            s["tool_errors"] += 1

    per_session["__totals__"] = totals
    return per_session


def _fmt_usd(x: float) -> str:
    return f"${x:.4f}"


def _eval_section() -> str:
    """If a persisted eval run exists, summarise it (N1/N2/N4 style)."""
    latest = Path(__file__).resolve().parent / "eval_runs" / "results-latest.json"
    if not latest.exists():
        return ""
    try:
        rows = json.loads(latest.read_text())
    except (json.JSONDecodeError, OSError):
        return ""
    n = len(rows)
    passed = sum(1 for r in rows if r.get("pass"))
    lines = [
        "",
        "eval corpus (last run):",
        f"  tasks: {n}   passed: {passed}   rate: {passed/n:.2f}" if n else "  (empty)",
    ]
    for r in rows:
        flag = "PASS" if r.get("pass") else "FAIL"
        lines.append(
            f"    [{flag}] {r.get('task'):<18} {r.get('kind',''):<10} "
            f"${r.get('cost_usd',0):.4f}"
        )
    return "\n".join(lines)


def render_report(
    config: str | None = None,
    db: str | None = None,
    since: str | None = None,
    as_json: bool = False,
) -> str:
    path = _default_db(config, db)
    try:
        j = Journal(path)
    except Exception as e:  # noqa: BLE001
        return f"could not open journal at {path}: {e}"

    since_td = _parse_since(since) if since else None

    data = _aggregate(j, since_td)
    totals = data.pop("__totals__", {})
    t = totals

    if as_json:
        out = {
            "source": str(path),
            "since": since or "all",
            "totals": t,
            "sessions": data,
        }
        return json.dumps(out, indent=2, default=str)

    n_sessions = len(data)
    lines = [
        f"curam report  ({path})",
        f"  since:        {since or 'all time'}",
        f"  sessions:     {n_sessions}",
        f"  events:       {t['events']}",
        f"  tool_calls:   {t['tool_calls']}  (denied {t['denied']}, errors {t['tool_errors']})",
        f"  asks:         {t['asks']}  approvals {t['approvals_requested']} "
        f"({t['approvals_granted']} granted)",
        f"  skills used:  {t['skills_used']}",
        f"  cost:         {_fmt_usd(t['cost_usd'])}",
        f"  tokens:       {t['prompt_tokens']} in / {t['completion_tokens']} out",
        f"  errors:       {t['errors']}",
    ]
    if t["tool_by_name"]:
        lines.append(f"  tools by name: {dict(sorted(t['tool_by_name'].items()))}")
    if t["skill_by_name"]:
        lines.append(f"  skills by name: {dict(sorted(t['skill_by_name'].items()))}")

    lines.append("")
    for sid in sorted(data):
        s = data[sid]
        tt = s
        name = f"session {sid[:12]}"
        lines.append(
            f"  {name}: {tt['tool_calls']} calls, {tt['asks']} asks, "
            f"{_fmt_usd(tt['cost_usd'])} cost, {tt['events']} events"
        )
        if tt["tool_by_name"]:
            lines.append(f"      tools: {dict(sorted(tt['tool_by_name'].items()))}")
        if tt["skill_by_name"]:
            lines.append(f"      skills: {dict(sorted(tt['skill_by_name'].items()))}")
    lines.append(_eval_section())
    return "\n".join(lines)


if __name__ == "__main__":
    print(render_report())
