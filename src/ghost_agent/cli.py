"""CLI: python -m ghost_agent demo|serve|tasks|approve|deny|memory|skills|schedules|schedule-add|schedule-remove|export-training"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .agent import Agent
from .memory import Memory
from .skills import Skills
from .store import Store


def _agent() -> tuple[Store, Agent]:
    store = Store()
    return store, Agent(store)


def cmd_demo(args) -> int:
    store, agent = _agent()
    try:
        task = agent.create_task(
            "demo", title="Ghost Agent demo",
            profile=getattr(args, "profile", None),
            verify=getattr(args, "verify", False))
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    task = agent.run_task(task["id"])
    print(f"task {task['id']}: after first run -> {task['status']}")
    if task["status"] == "waiting_approval":
        pending = agent.pending_approvals()
        print(f"approval requested: {pending[0]['tool']} (demo approves it, as a human would)")
        task = agent.approve_task(task["id"])
    print(f"task {task['id']}: final -> {task['status']}")
    if task.get("verification"):
        v = task["verification"]
        print(f"verification: {v.get('outcome')}"
              + (f" ({v.get('reason')})" if v.get("reason") else ""))
    print("steps:")
    for i, s in enumerate(task["steps"]):
        print(f"  {i}. {s['tool']}: {s['status']}")
    out = store.workspace / "demo_output.txt"
    print(f"demo_output.txt exists: {out.is_file()}")
    if out.is_file():
        print(f"demo_output.txt content: {out.read_text(encoding='utf-8')!r}")
    if task.get("skill_id"):
        skill = store.get("skills", task["skill_id"])
        print(f"candidate skill proposed (untrusted): {skill['name']} tools={skill['tools']}")
    print(f"trail events: {len(store.read_trail(task_id=task['id']))}")
    return 0 if task["status"] == "done" else 1


def cmd_tasks(args) -> int:
    store, _ = _agent()
    for t in store.list("tasks"):
        print(f"{t['id']}  {t['status']:<16}  {t.get('title', '')}")
    return 0


def cmd_approve(args) -> int:
    _, agent = _agent()
    task = agent.approve_task(args.task_id)
    print(json.dumps({"id": args.task_id, "status": task["status"] if task else "not-found"}))
    return 0 if task else 2


def cmd_deny(args) -> int:
    _, agent = _agent()
    task = agent.deny_task(args.task_id)
    print(json.dumps({"id": args.task_id, "status": task["status"] if task else "not-found"}))
    return 0 if task else 2


def cmd_memory(args) -> int:
    store, _ = _agent()
    mem = Memory(store)
    for m in mem.list_trusted():
        print(f"{m['id']}  [trusted]  {m['text']}")
    for m in mem.list_candidates():
        print(f"{m['id']}  [candidate]  {m['text']}")
    return 0


def cmd_skills(args) -> int:
    store, _ = _agent()
    for s in Skills(store).list_all():
        print(f"{s['id']}  {'trusted' if s['trusted'] else 'candidate'}  {s['name']}  tools={s['tools']}")
    return 0


def cmd_schedules(args) -> int:
    store, _ = _agent()
    rows = store.list("schedules")
    if not rows:
        print("no schedules")
        return 0
    for s in rows:
        kind = (f"daily at {s.get('time_hhmm')}" if s.get("kind") == "daily"
                else f"every {s.get('interval_minutes')} min")
        print(f"{s['id']}  {'on ' if s.get('enabled') else 'off'}  {kind:<16}  "
              f"next={s.get('next_run_at')}  {s.get('title', '')}")
    return 0


def cmd_schedule_add(args) -> int:
    from .scheduler import create_schedule
    store, _ = _agent()
    try:
        s = create_schedule(store, args.goal, kind=args.kind,
                            interval_minutes=args.interval,
                            time_hhmm=args.time, title=args.title)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"id": s["id"], "kind": s["kind"],
                      "next_run_at": s["next_run_at"]}))
    return 0


def cmd_schedule_remove(args) -> int:
    store, _ = _agent()
    if store.delete("schedules", args.schedule_id):
        print(json.dumps({"deleted": args.schedule_id}))
        return 0
    print(json.dumps({"id": args.schedule_id, "status": "not-found"}))
    return 2


def cmd_export_training(args) -> int:
    """The Level 2 on-ramp (v0.11.0): write the training-data
    JSONL (done task runs; failed ones only with --include-failed)
    and print the data card — counts by status and provider, and
    total tokens across the exported tasks. Without --out the
    JSONL goes to stdout and the card to stderr, so the JSONL
    stays pipe-clean."""
    from .server import training_jsonl
    store = Store()
    text, card = training_jsonl(store,
                                include_failed=args.include_failed)
    card_lines = [
        f"training export: {card['tasks']} task(s)",
        "  by status: " + (", ".join(
            f"{k}={v}" for k, v in sorted(card["by_status"].items()))
            or "(none)"),
        "  by provider: " + (", ".join(
            f"{k}={v}" for k, v in sorted(card["by_provider"].items()))
            or "(none)"),
        f"  total tokens: {card['total_tokens']}",
    ]
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
        for line in card_lines:
            print(line)
    else:
        sys.stdout.write(text)
        for line in card_lines:
            print(line, file=sys.stderr)
    return 0


def cmd_serve(args) -> int:
    from .server import DashboardConfigError, serve
    try:
        return serve(args.host, args.port)
    except DashboardConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ghost-agent", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_demo = sub.add_parser("demo", help="seed and run one full demo task (MockModel)")
    p_demo.add_argument("--profile", default=None,
                        help="provider profile for the task (providers.json); "
                             "default: the router strategy decides")
    p_demo.add_argument("--verify", action="store_true",
                        help="run the verifier pass when the task "
                             "finishes (recorded on the task; a "
                             "MockModel demo records a skip)")
    p_serve = sub.add_parser("serve", help="run the dashboard server")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8770)
    sub.add_parser("tasks", help="list tasks")
    p_a = sub.add_parser("approve", help="approve a waiting task's pending step")
    p_a.add_argument("task_id")
    p_d = sub.add_parser("deny", help="deny a waiting task's pending step")
    p_d.add_argument("task_id")
    sub.add_parser("memory", help="list trusted + candidate memory")
    sub.add_parser("skills", help="list skills")
    sub.add_parser("schedules", help="list schedules")
    p_sa = sub.add_parser("schedule-add", help="create a schedule")
    p_sa.add_argument("goal")
    p_sa.add_argument("--kind", default="interval", choices=["interval", "daily"])
    p_sa.add_argument("--interval", type=int, default=60,
                      help="minutes between runs (interval kind)")
    p_sa.add_argument("--time", default=None,
                      help="HH:MM local time (daily kind)")
    p_sa.add_argument("--title", default=None)
    p_sr = sub.add_parser("schedule-remove", help="delete a schedule")
    p_sr.add_argument("schedule_id")
    p_et = sub.add_parser(
        "export-training",
        help="export completed task runs as training-data JSONL")
    p_et.add_argument("--out", default=None,
                      help="write the JSONL to this path (default: stdout)")
    p_et.add_argument("--include-failed", action="store_true",
                      help="include failed task runs (status marked)")
    args = parser.parse_args(argv)
    fn = {"demo": cmd_demo, "serve": cmd_serve, "tasks": cmd_tasks,
          "approve": cmd_approve, "deny": cmd_deny,
          "memory": cmd_memory, "skills": cmd_skills,
          "schedules": cmd_schedules, "schedule-add": cmd_schedule_add,
          "schedule-remove": cmd_schedule_remove,
          "export-training": cmd_export_training}[args.cmd]
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
