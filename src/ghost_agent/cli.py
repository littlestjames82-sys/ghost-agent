"""CLI: python -m ghost_agent demo|serve|tasks|approve|deny|memory|skills|schedules|schedule-add|schedule-remove"""
from __future__ import annotations

import argparse
import json
import sys

from .agent import Agent
from .memory import Memory
from .skills import Skills
from .store import Store


def _agent() -> tuple[Store, Agent]:
    store = Store()
    return store, Agent(store)


def cmd_demo(args) -> int:
    store, agent = _agent()
    task = agent.create_task("demo", title="Ghost Agent demo")
    task = agent.run_task(task["id"])
    print(f"task {task['id']}: after first run -> {task['status']}")
    if task["status"] == "waiting_approval":
        pending = agent.pending_approvals()
        print(f"approval requested: {pending[0]['tool']} (demo approves it, as a human would)")
        task = agent.approve_task(task["id"])
    print(f"task {task['id']}: final -> {task['status']}")
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
    sub.add_parser("demo", help="seed and run one full demo task (MockModel)")
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
    args = parser.parse_args(argv)
    fn = {"demo": cmd_demo, "serve": cmd_serve, "tasks": cmd_tasks,
          "approve": cmd_approve, "deny": cmd_deny,
          "memory": cmd_memory, "skills": cmd_skills,
          "schedules": cmd_schedules, "schedule-add": cmd_schedule_add,
          "schedule-remove": cmd_schedule_remove}[args.cmd]
    return fn(args)


if __name__ == "__main__":
    sys.exit(main())
