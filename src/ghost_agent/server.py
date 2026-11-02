"""Dashboard server v2: stdlib http.server, binds 127.0.0.1 by default.

Restyled to Hermes / OpenClaw dashboard caliber (sidebar navigation,
one page at a time, overview stat cards, status badges) in the Ghost
violet dark theme — still a single stdlib-served page, no build step.

Auth (Hermes-style, fail-closed):
- Loopback bind: no token needed (the v1 posture; warning banner).
- Non-loopback bind: GHOST_AGENT_DASHBOARD_TOKEN MUST be set or the
  server refuses to start. When a token is set (any bind), every
  /api/* request must carry it via the X-Ghost-Token header or the
  ghost_token cookie (set by the page's token form). The page itself
  always loads so a human can enter the token. Tokens are never logged.
"""
from __future__ import annotations

import json
import os
import re
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__
from .agent import Agent
from .memory import Memory
from .skills import Skills
from .store import Store

WARNING = "LOCAL ONLY - NO AUTHENTICATION - do not expose beyond this machine"
WARNING_TOKEN = ("TOKEN AUTHENTICATION ENABLED - still do not expose "
                 "beyond trusted networks")
TOKEN_ENV = "GHOST_AGENT_DASHBOARD_TOKEN"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", ""}


class DashboardConfigError(RuntimeError):
    """Raised when the dashboard refuses to start (fail-closed auth)."""


def is_loopback(host: str) -> bool:
    return host in LOOPBACK_HOSTS


def env_token() -> str | None:
    tok = os.environ.get(TOKEN_ENV)
    return tok if tok else None


def require_bind_allowed(host: str, token: str | None) -> None:
    """Fail closed: a non-loopback bind without a token is refused."""
    if not is_loopback(host) and not token:
        raise DashboardConfigError(
            f"refusing to start: host {host!r} is not loopback and "
            f"{TOKEN_ENV} is not set. Set {TOKEN_ENV} to require token "
            "authentication, or bind to 127.0.0.1.")


# ---- payloads ------------------------------------------------------------

def board_payload(agent: Agent) -> dict:
    """v1 shape, unchanged: 4 columns + pending_approvals."""
    columns = {"running": [], "waiting_approval": [], "done": [], "failed": []}
    for task in agent.store.list("tasks"):
        status = task.get("status")
        if status in ("pending", "running"):
            columns["running"].append(task)
        elif status in columns:
            columns[status].append(task)
        else:
            columns["running"].append(task)
    return {"columns": columns, "pending_approvals": agent.pending_approvals()}


def _task_summary(task: dict) -> dict:
    steps = task.get("steps", [])
    return {
        "id": task.get("id"),
        "title": task.get("title"),
        "status": task.get("status"),
        "model": task.get("model"),
        "steps_total": len(steps),
        "steps_done": sum(1 for s in steps if s.get("status") == "done"),
        "created_at": task.get("created_at"),
        "updated_at": task.get("updated_at"),
    }


def _enriched_approvals(agent: Agent) -> list[dict]:
    out = []
    for a in agent.pending_approvals():
        task = agent.store.get("tasks", a.get("task_id") or "")
        item = dict(a)
        item["task_title"] = task.get("title") if task else None
        item["task_status"] = task.get("status") if task else None
        out.append(item)
    return out


def overview_payload(store: Store, agent: Agent) -> dict:
    tasks = store.list("tasks")
    counts = {"total": len(tasks), "running": 0, "waiting_approval": 0,
              "done": 0, "failed": 0}
    for t in tasks:
        s = t.get("status")
        if s in ("pending", "running"):
            counts["running"] += 1
        elif s in counts:
            counts[s] += 1
        else:
            counts["running"] += 1
    mem = Memory(store)
    skills = Skills(store).list_all()
    trail = store.read_trail(limit=1_000_000)
    recent = [_task_summary(t) for t in reversed(tasks)][:10]
    return {
        "counts": counts,
        "trusted_memories": len(mem.list_trusted()),
        "candidate_memories": len(mem.list_candidates()),
        "skills_total": len(skills),
        "skills_trusted": sum(1 for s in skills if s.get("trusted")),
        "skills_candidate": sum(1 for s in skills if not s.get("trusted")),
        "trail_events": len(trail),
        "pending_approvals": _enriched_approvals(agent),
        "recent_tasks": recent,
        "version": __version__,
    }


def analytics_payload(store: Store, agent: Agent) -> dict:
    tasks = store.list("tasks")
    by_status: dict[str, int] = {}
    by_model: dict[str, int] = {}
    steps_total = 0
    steps_done = 0
    tokens_total = 0
    cost_total = 0.0
    tokens_tracked = False
    for t in tasks:
        by_status[t.get("status") or "unknown"] = \
            by_status.get(t.get("status") or "unknown", 0) + 1
        by_model[t.get("model") or "unknown"] = \
            by_model.get(t.get("model") or "unknown", 0) + 1
        for s in t.get("steps", []):
            steps_total += 1
            if s.get("status") == "done":
                steps_done += 1
        if isinstance(t.get("tokens"), (int, float)) or \
                isinstance(t.get("cost"), (int, float)):
            tokens_tracked = True
            tokens_total += t.get("tokens") or 0
            cost_total += t.get("cost") or 0
    approvals = store.list("approvals")
    trail = store.read_trail(limit=1_000_000)
    by_type: dict[str, int] = {}
    for e in trail:
        by_type[e.get("type") or "unknown"] = by_type.get(e.get("type") or "unknown", 0) + 1
    out = {
        "tasks_total": len(tasks),
        "tasks_by_status": by_status,
        "tasks_by_model": by_model,
        "steps_total": steps_total,
        "steps_executed": steps_done,
        "approvals_granted": sum(1 for a in approvals if a.get("status") == "approved"),
        "approvals_denied": sum(1 for a in approvals if a.get("status") == "denied"),
        "approvals_pending": sum(1 for a in approvals if a.get("status") == "pending"),
        "trail_total": len(trail),
        "trail_by_type": by_type,
        "tokens_tracked": tokens_tracked,
    }
    if tokens_tracked:
        out["tokens_total"] = tokens_total
        out["cost_total"] = cost_total
    else:
        out["tokens_note"] = "token/cost not tracked yet"
    return out


def system_payload(store: Store, agent: Agent, token: str | None,
                   host: str) -> dict:
    router = agent.router
    if getattr(router, "real", None) is not None:
        adapter = router.real.name
        model_name = router.real.model
    else:
        adapter = router.mock.name
        model_name = None
    return {
        "version": __version__,
        "home": str(store.home),
        "workspace": str(store.workspace),
        "host": host,
        "model_adapter": adapter,
        "model_name": model_name,
        "api_key_present": bool(os.environ.get("GHOST_AGENT_API_KEY")),
        "auth": {
            "token_required": bool(token),
            "token_env": TOKEN_ENV,
            "loopback": is_loopback(host),
            "mode": ("token" if token else
                     "none (loopback only)" if is_loopback(host) else "refused"),
        },
        "warning": WARNING if not token else WARNING_TOKEN,
    }


def trail_search(store: Store, q: str = "", type_filter: str = "",
                 task_id: str = "", limit: int = 200) -> list[dict]:
    """Newest-first filtered trail. q is a case-insensitive substring
    match over the serialized event; type is an exact event-type match."""
    limit = max(1, min(int(limit or 200), 1000))
    events = store.read_trail(task_id=task_id or None, limit=1_000_000)
    ql = q.lower()
    out = []
    for e in reversed(events):  # newest first
        if type_filter and e.get("type") != type_filter:
            continue
        if ql and ql not in json.dumps(e, sort_keys=True).lower():
            continue
        out.append(e)
        if len(out) >= limit:
            break
    return out


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ghost Agent</title>
<style>
:root{--bg:#0d0b14;--panel:#181325;--card:#221a38;--card2:#281e47;--violet:#6716f3;
--violet2:#8b5cf6;--text:#eee;--muted:#a49fc0;--line:#352a55;
--amber:#f5b93f;--green:#3ecf6f;--red:#f26d6d;--blue:#5ea8ff}
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,sans-serif;margin:0;background:var(--bg);color:var(--text)}
.layout{display:flex;min-height:100vh}
.sidebar{width:228px;flex:0 0 228px;background:#120e1f;border-right:1px solid var(--line);
padding:16px 12px;position:sticky;top:0;height:100vh;display:flex;flex-direction:column;gap:2px}
.brand{font-size:19px;font-weight:700;padding:2px 10px}
.brandsub{font-size:11px;color:var(--muted);padding:0 10px 14px}
.navbtn{display:flex;justify-content:space-between;align-items:center;width:100%;text-align:left;
background:transparent;color:var(--text);border:0;border-radius:8px;padding:10px 12px;
font-size:14px;cursor:pointer}
.navbtn:hover{background:var(--card)}
.navbtn.active{background:var(--violet);color:#fff}
.navcount{background:var(--amber);color:#1a1200;border-radius:999px;font-size:11px;
font-weight:700;padding:1px 8px}
.sidefoot{margin-top:auto;font-size:11px;color:var(--muted);padding:8px 10px}
.main{flex:1;min-width:0}
.topbar{display:flex;flex-wrap:wrap;gap:10px;align-items:center;background:var(--panel);
border-bottom:1px solid var(--line);padding:10px 18px;position:sticky;top:0;z-index:5}
.topbar .t{font-size:15px;font-weight:600}
.pill{font-size:12px;border:1px solid var(--line);border-radius:999px;padding:3px 10px;color:var(--muted)}
.pill b{color:var(--text);font-weight:600}
.warn{background:#3a2400;color:#ffcf6e;padding:9px 18px;font-size:13px;border-bottom:1px solid #5a3c00}
.warn.token{background:#12283f;color:#9fd0ff;border-bottom-color:#1d4a73}
content{display:block;padding:16px 18px;max-width:1180px;margin:0 auto}
.page{display:none}
.page.active{display:block}
h2.ptitle{margin:4px 0 2px;font-size:20px}
.seclabel{font-size:11px;text-transform:uppercase;letter-spacing:.08em;color:var(--muted);margin:20px 0 8px}
.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(128px,1fr));gap:10px;margin-top:12px}
.stat{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px}
.stat .num{font-size:24px;font-weight:700}
.stat .lbl{font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-top:2px}
.board{display:grid;grid-template-columns:repeat(auto-fit,minmax(225px,1fr));gap:10px}
.col{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:10px;min-height:90px}
.col h3{margin:2px 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted)}
.card{background:var(--card);border:1px solid transparent;border-radius:10px;padding:10px 12px;
margin:8px 0;font-size:14px;word-break:break-word}
.card:hover{background:var(--card2);border-color:var(--line)}
.card a{color:#c9b3ff;text-decoration:none}
.badge{display:inline-block;font-size:11px;font-weight:700;border-radius:999px;padding:2px 9px;vertical-align:1px}
.b-running{background:#2c2a6e;color:#b9bcff}
.b-waiting_approval{background:#4a3400;color:var(--amber)}
.b-pending{background:#333344;color:#ccc}
.b-done{background:#0f3d22;color:var(--green)}
.b-failed{background:#4a1218;color:var(--red)}
.b-candidate{background:#4a3400;color:var(--amber)}
.b-trusted{background:#0f3d22;color:var(--green)}
.dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:var(--violet2);
margin-right:6px;animation:pulse 1.6s infinite}
@keyframes pulse{0%{opacity:1}50%{opacity:.25}100%{opacity:1}}
button{background:var(--violet);color:#fff;border:0;border-radius:8px;padding:8px 14px;cursor:pointer;font-size:14px}
button:hover{background:#7d33ff}
button.deny{background:#7a1f2b}
button.ghostbtn{background:transparent;border:1px solid var(--line);color:var(--text)}
input,select,textarea{box-sizing:border-box;padding:9px;border-radius:8px;border:1px solid var(--line);
background:var(--panel);color:var(--text);font-size:14px}
input.wide{width:100%}
pre{white-space:pre-wrap;word-break:break-word;background:var(--panel);padding:10px;border-radius:8px;
font-size:12px;border:1px solid var(--line)}
.small{font-size:12px;color:var(--muted)}
.grid2{display:grid;grid-template-columns:minmax(240px,340px) 1fr;gap:14px;align-items:start}
.bars{margin:6px 0}
.bar-row{display:flex;align-items:center;gap:10px;margin:5px 0;font-size:13px}
.bar-row .bl{width:170px;color:var(--muted);word-break:break-word}
.bar-track{flex:1;background:var(--panel);border-radius:6px;height:14px;overflow:hidden}
.bar-fill{height:100%;background:var(--violet);border-radius:6px}
.bar-row .bv{width:44px;text-align:right}
.log{font-family:ui-monospace,Menlo,monospace;font-size:12px}
.log .ev{padding:5px 8px;border-bottom:1px solid #241c3d;word-break:break-word}
.sev-fail{color:var(--red)} .sev-approval{color:var(--amber)} .sev-done{color:var(--green)}
dl{display:grid;grid-template-columns:170px 1fr;gap:6px 14px;font-size:14px}
dt{color:var(--muted)} dd{margin:0;word-break:break-word}
#loginbox{display:none;background:#12283f;border:1px solid #1d4a73;border-radius:10px;
padding:12px;margin:14px 0}
@media (max-width:820px){
 .layout{flex-direction:column}
 .sidebar{width:100%;flex:none;height:auto;position:static;flex-direction:row;flex-wrap:wrap;
  align-items:center;padding:10px}
 .brandsub,.sidefoot{display:none}
 .navbtn{width:auto;padding:8px 10px}
 .grid2{grid-template-columns:1fr}
}
</style></head><body>
<div class="layout">
<nav class="sidebar">
 <div class="brand">Ghost Agent</div>
 <div class="brandsub">Persistent governed operator<br>Ghost Developer Studio</div>
 <button class="navbtn" data-page="overview">Overview</button>
 <button class="navbtn" data-page="board">Board</button>
 <button class="navbtn" data-page="approvals">Approvals <span class="navcount" id="nav-approvals" style="display:none">0</span></button>
 <button class="navbtn" data-page="tasks">Tasks</button>
 <button class="navbtn" data-page="memory">Memory</button>
 <button class="navbtn" data-page="skills">Skills</button>
 <button class="navbtn" data-page="analytics">Analytics</button>
 <button class="navbtn" data-page="logs">Logs</button>
 <button class="navbtn" data-page="system">System</button>
 <div class="sidefoot">v__VERSION__ - zero dependencies<br>stdlib only - $0 to run</div>
</nav>
<div class="main">
<div class="topbar">
 <span class="t">Ghost Agent</span>
 <span class="pill">version <b>v__VERSION__</b></span>
 <span class="pill">model <b id="hdr-model">-</b></span>
 <span class="pill">pending approvals <b id="hdr-pending">0</b></span>
 <span class="pill" id="hdr-auth">auth -</span>
</div>
<div class="warn" id="warnbar">WARNING: __WARNING__</div>
<content>
<div id="loginbox">
 <b>Dashboard token required.</b>
 <p class="small">Enter the GHOST_AGENT_DASHBOARD_TOKEN for this server. It is kept only in a cookie on this browser and sent as a header - never logged by the server.</p>
 <input type="password" id="tokeninput" placeholder="dashboard token">
 <button onclick="unlock()">Unlock</button>
</div>

<section class="page" id="page-overview">
 <h2 class="ptitle">Overview</h2>
 <div class="stats" id="ov-stats"></div>
 <div class="seclabel">New task</div>
 <form id="taskform"><input class="wide" id="goal" placeholder="e.g. write notes.txt with: hello" required>
 <p><button type="submit">Create and run task</button></p></form>
 <div class="seclabel">Needs your attention</div>
 <div id="ov-attention"></div>
 <div class="seclabel">Recent tasks</div>
 <div id="ov-recent"></div>
</section>

<section class="page" id="page-board">
 <h2 class="ptitle">Board</h2>
 <div class="board" id="board"></div>
</section>

<section class="page" id="page-approvals">
 <h2 class="ptitle">Approvals</h2>
 <p class="small">Write and exec steps wait here. Approving runs the step; denying fails the task honestly. Nothing executes before its trail record is written.</p>
 <div id="approvals"></div>
</section>

<section class="page" id="page-tasks">
 <h2 class="ptitle">Tasks</h2>
 <div class="grid2">
  <div id="tasklist"></div>
  <div>
   <h3 id="td-title" style="margin-top:2px">No task selected</h3>
   <p class="small" id="td-meta">Tap a task to see its steps and trail.</p>
   <div id="td-steps"></div>
   <div class="seclabel">Trail search (this task)</div>
   <input class="wide" id="td-q" placeholder="filter this task's trail, e.g. write_file">
   <p><button class="ghostbtn" onclick="trailSearch()">Search trail</button></p>
   <pre id="td-trail"></pre>
  </div>
 </div>
</section>

<section class="page" id="page-memory">
 <h2 class="ptitle">Memory</h2>
 <div class="seclabel">Candidates - waiting for your decision</div>
 <div id="mem-candidates"></div>
 <div class="seclabel">Trusted</div>
 <div id="mem-trusted"></div>
</section>

<section class="page" id="page-skills">
 <h2 class="ptitle">Skills</h2>
 <p class="small">Candidate skills are proposed from successful runs and stay untrusted until you promote them.</p>
 <div id="skills"></div>
</section>

<section class="page" id="page-analytics">
 <h2 class="ptitle">Analytics</h2>
 <div id="analytics"></div>
</section>

<section class="page" id="page-logs">
 <h2 class="ptitle">Logs</h2>
 <p class="small">Live trail tail, newest first. Auto-refreshes with the rest of the dashboard.</p>
 <div style="display:flex;gap:8px;flex-wrap:wrap;margin:10px 0">
  <select id="log-type"><option value="">all types</option></select>
  <input id="log-q" placeholder="search trail text" style="flex:1;min-width:160px">
  <button class="ghostbtn" onclick="refresh()">Apply</button>
 </div>
 <div class="log" id="logs"></div>
</section>

<section class="page" id="page-system">
 <h2 class="ptitle">System</h2>
 <div id="system"></div>
</section>
</content>
</div></div>
<script>
const AUTH_REQUIRED="__AUTH_REQUIRED__"==="1";
let curPage="overview", selectedTask=null, logTypesLoaded=false;
function esc(s){return String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function cookieTok(){const m=document.cookie.match(/(?:^|; )ghost_token=([^;]*)/);return m?decodeURIComponent(m[1]):""}
async function j(u,o){
  o=o||{};o.headers=Object.assign({},o.headers||{});
  const t=cookieTok(); if(t) o.headers["X-Ghost-Token"]=t;
  const r=await fetch(u,o);
  if(r.status===401){document.getElementById("loginbox").style.display="block";throw new Error("401")}
  return r.json();
}
function unlock(){
  const v=document.getElementById("tokeninput").value.trim();
  if(!v) return;
  document.cookie="ghost_token="+encodeURIComponent(v)+"; path=/; SameSite=Strict";
  document.getElementById("loginbox").style.display="none"; refresh();
}
function badge(st){return "<span class='badge b-"+esc(st)+"'>"+(st==="running"?"<span class='dot'></span>":"")+esc(st)+"</span>"}
function showPage(name){
  curPage=name;
  document.querySelectorAll(".page").forEach(p=>p.classList.toggle("active",p.id==="page-"+name));
  document.querySelectorAll(".navbtn").forEach(b=>b.classList.toggle("active",b.dataset.page===name));
  try{history.replaceState(null,"","#"+name)}catch(e){}
  window.scrollTo(0,0);
}
document.querySelectorAll(".navbtn").forEach(b=>b.addEventListener("click",()=>showPage(b.dataset.page)));
function gotoTask(id){showPage("tasks");detail(id)}
const cols=[["running","Running"],["waiting_approval","Waiting approval"],["done","Done"],["failed","Failed"]];
function statCard(num,lbl){return "<div class='stat'><div class='num'>"+num+"</div><div class='lbl'>"+lbl+"</div></div>"}
function bars(obj){
  const entries=Object.entries(obj||{}).sort((a,b)=>b[1]-a[1]);
  const max=Math.max(1,...entries.map(e=>e[1]));
  if(!entries.length) return "<p class='small'>No data yet.</p>";
  return "<div class='bars'>"+entries.map(([k,v])=>
    "<div class='bar-row'><span class='bl'>"+esc(k)+"</span><span class='bar-track'><span class='bar-fill' style='display:block;width:"+Math.round(v/max*100)+"%'></span></span><span class='bv'>"+v+"</span></div>").join("")+"</div>";
}
function sevClass(e){
  const t=e.type||"";
  if(t.includes("fail")||t.includes("denied")||t.includes("deny")) return "sev-fail";
  if(t.includes("approval")) return "sev-approval";
  if(t.includes("done")||t.includes("promoted")||t.includes("approved")) return "sev-done";
  return "";
}
async function detail(id){
  selectedTask=id;
  let d; try{ d=await j("/api/tasks/"+id) }catch(e){ return }
  document.getElementById("td-title").textContent=d.task.title;
  document.getElementById("td-meta").innerHTML="status "+badge(d.task.status)+" - model "+esc(d.task.model)+
    (d.task.fallback_reason?" - router fallback: "+esc(d.task.fallback_reason):"")+
    (d.task.error?" - "+esc(d.task.error):"");
  document.getElementById("td-steps").innerHTML=d.task.steps.map((s,i)=>
    "<div class='card'>"+i+". "+esc(s.tool)+" "+badge(s.status)+
    (s.error?"<div class='small'>"+esc(s.error)+"</div>":"")+
    "<div class='small'>"+esc(JSON.stringify(s.args||{}))+"</div></div>").join("");
  document.getElementById("td-trail").textContent=d.trail.map(e=>e.ts+"  "+e.type+"  "+(e.tool||"")).join("\\n");
}
async function trailSearch(){
  if(!selectedTask) return;
  const q=document.getElementById("td-q").value;
  let d; try{ d=await j("/api/trail?task_id="+encodeURIComponent(selectedTask)+"&q="+encodeURIComponent(q)+"&limit=100") }catch(e){ return }
  document.getElementById("td-trail").textContent=d.trail.map(e=>e.ts+"  "+e.type+"  "+(e.tool||"")).join("\\n")||"(no matching trail events)";
}
async function act(id,what){ try{ await j("/api/tasks/"+id+"/"+what,{method:"POST"}) }catch(e){} refresh() }
async function memAct(id,what){ try{ await j("/api/memory/"+id+"/"+what,{method:"POST"}) }catch(e){} refresh() }
async function skillAct(id){ try{ await j("/api/skills/"+id+"/promote",{method:"POST"}) }catch(e){} refresh() }
async function refresh(){
  let ov,b,m,s,an,sys;
  try{
    [ov,b,m,s,an,sys]=await Promise.all([j("/api/overview"),j("/api/board"),j("/api/memory"),j("/api/skills"),j("/api/analytics"),j("/api/system")]);
  }catch(e){ return }
  // header + sidebar badges
  document.getElementById("hdr-model").textContent=sys.model_adapter+(sys.model_name?" / "+sys.model_name:"");
  document.getElementById("hdr-pending").textContent=ov.pending_approvals.length;
  document.getElementById("hdr-auth").innerHTML="auth <b>"+(sys.auth.token_required?"token required":"none (loopback)")+"</b>";
  const navA=document.getElementById("nav-approvals");
  navA.style.display=ov.pending_approvals.length?"inline-block":"none";
  navA.textContent=ov.pending_approvals.length;
  const wb=document.getElementById("warnbar");
  wb.textContent="WARNING: "+sys.warning;
  wb.classList.toggle("token",!!sys.auth.token_required);
  // overview
  const c=ov.counts;
  document.getElementById("ov-stats").innerHTML=
    statCard(c.total,"Tasks total")+statCard(c.running,"Running")+statCard(c.waiting_approval,"Waiting approval")+
    statCard(c.done,"Done")+statCard(c.failed,"Failed")+statCard(ov.trusted_memories,"Trusted memories")+
    statCard(ov.skills_total,"Skills")+statCard(ov.trail_events,"Trail events");
  document.getElementById("ov-attention").innerHTML=ov.pending_approvals.length?ov.pending_approvals.map(a=>
    "<div class='card'><b>"+esc(a.tool)+"</b> on task "+esc(a.task_title||a.task_id)+
    " - <a href='#' onclick='showPage(\\"approvals\\");return false'>review in Approvals</a></div>").join("")
    :"<p class='small'>Nothing is waiting on you.</p>";
  document.getElementById("ov-recent").innerHTML=ov.recent_tasks.length?ov.recent_tasks.map(t=>
    "<div class='card'><a href='#' onclick='gotoTask(\\""+t.id+"\\");return false'>"+esc(t.title)+"</a> "+badge(t.status)+
    "<div class='small'>model "+esc(t.model||"-")+" - steps "+t.steps_done+"/"+t.steps_total+"</div></div>").join("")
    :"<p class='small'>No tasks yet. Create one above.</p>";
  // board + task list
  document.getElementById("board").innerHTML=cols.map(([k,label])=>
    "<div class='col'><h3>"+label+"</h3>"+(b.columns[k]||[]).map(t=>
      "<div class='card'><a href='#' onclick='gotoTask(\\""+t.id+"\\");return false'>"+esc(t.title)+"</a><br>"+badge(t.status)+
      "<div class='small'>"+esc(t.model||"")+"</div></div>").join("")+"</div>").join("");
  const allTasks=cols.flatMap(([k])=>b.columns[k]||[]);
  document.getElementById("tasklist").innerHTML=allTasks.length?allTasks.map(t=>
    "<div class='card'><a href='#' onclick='detail(\\""+t.id+"\\");return false'>"+esc(t.title)+"</a><br>"+badge(t.status)+"</div>").join("")
    :"<p class='small'>No tasks yet.</p>";
  // approvals
  document.getElementById("approvals").innerHTML=ov.pending_approvals.length?ov.pending_approvals.map(a=>
    "<div class='card'><b>"+esc(a.tool)+"</b> on task <b>"+esc(a.task_title||a.task_id)+"</b> "+badge("waiting_approval")+
    "<pre>"+esc(JSON.stringify(a.args||{},null,1))+"</pre>"+
    "<button onclick='act(\\""+a.task_id+"\\",\\"approve\\")'>Approve</button> "+
    "<button class='deny' onclick='act(\\""+a.task_id+"\\",\\"deny\\")'>Deny</button></div>").join("")
    :"<p class='small'>Nothing waiting.</p>";
  // memory
  document.getElementById("mem-candidates").innerHTML=(m.candidates||[]).length?(m.candidates||[]).map(x=>
    "<div class='card'>"+esc(x.text)+" "+badge("candidate")+
    "<div class='small'>source: "+esc(x.source)+"</div>"+
    "<button onclick='memAct(\\""+x.id+"\\",\\"approve\\")'>Approve</button> "+
    "<button class='deny' onclick='memAct(\\""+x.id+"\\",\\"reject\\")'>Reject</button></div>").join("")
    :"<p class='small'>No candidates waiting.</p>";
  document.getElementById("mem-trusted").innerHTML=(m.trusted||[]).length?(m.trusted||[]).map(x=>
    "<div class='card'>"+esc(x.text)+" "+badge("trusted")+
    "<div class='small'>source: "+esc(x.source)+" - verified: "+(x.verified?"yes":"no")+"</div></div>").join("")
    :"<p class='small'>No trusted memory yet.</p>";
  // skills
  document.getElementById("skills").innerHTML=(s.skills||[]).length?(s.skills||[]).map(x=>
    "<div class='card'><b>"+esc(x.name)+"</b> "+badge(x.trusted?"trusted":"candidate")+
    "<div class='small'>tools: "+esc((x.tools||[]).join(", "))+" - permissions: "+esc((x.permissions||[]).join(", "))+"</div>"+
    (x.trusted?"":"<button onclick='skillAct(\\""+x.id+"\\")'>Promote to trusted</button>")+"</div>").join("")
    :"<p class='small'>No skills yet. Finish a task and one will be proposed.</p>";
  // analytics
  let ah="<div class='stats'>"+statCard(an.steps_executed+"/"+an.steps_total,"Steps executed")+
    statCard(an.approvals_granted,"Approvals granted")+statCard(an.approvals_denied,"Approvals denied")+
    statCard(an.approvals_pending,"Approvals pending")+statCard(an.trail_total,"Trail events")+"</div>";
  ah+="<div class='seclabel'>Tasks by status</div>"+bars(an.tasks_by_status);
  ah+="<div class='seclabel'>Tasks by model</div>"+bars(an.tasks_by_model);
  ah+="<div class='seclabel'>Trail events by type</div>"+bars(an.trail_by_type);
  ah+= an.tokens_tracked
    ? "<div class='seclabel'>Usage</div><p>tokens: "+an.tokens_total+" - cost: "+an.cost_total+"</p>"
    : "<p class='small' style='margin-top:14px'>Usage: "+esc(an.tokens_note||"token/cost not tracked yet")+" - v1/v2 tasks record steps and verdicts, not provider token counts.</p>";
  document.getElementById("analytics").innerHTML=ah;
  // system
  document.getElementById("system").innerHTML="<dl>"+
    "<dt>Version</dt><dd>"+esc(sys.version)+"</dd>"+
    "<dt>GHOST_AGENT_HOME</dt><dd>"+esc(sys.home)+"</dd>"+
    "<dt>Workspace</dt><dd>"+esc(sys.workspace)+"</dd>"+
    "<dt>Bind host</dt><dd>"+esc(sys.host)+"</dd>"+
    "<dt>Model adapter</dt><dd>"+esc(sys.model_adapter)+(sys.model_name?" ("+esc(sys.model_name)+")":"")+"</dd>"+
    "<dt>Model API key present</dt><dd>"+(sys.api_key_present?"yes (value never shown or logged)":"no - MockModel planning")+"</dd>"+
    "<dt>Auth mode</dt><dd>"+esc(sys.auth.mode)+"</dd>"+
    "<dt>Local-only warning</dt><dd>"+esc(sys.warning)+"</dd></dl>"+
    "<p class='small'>The dashboard can approve real actions on this machine. Keep it on loopback, or behind the token gate on a trusted network only.</p>";
  // logs (preserve filter values)
  const lt=document.getElementById("log-type"), lq=document.getElementById("log-q");
  const curType=lt.value, curQ=lq.value;
  if(!logTypesLoaded || true){
    const types=Object.keys(an.trail_by_type||{}).sort();
    lt.innerHTML="<option value=''>all types</option>"+types.map(t=>"<option value='"+esc(t)+"'>"+esc(t)+"</option>").join("");
    lt.value=types.includes(curType)?curType:""; logTypesLoaded=true;
  }
  let lg; try{ lg=await j("/api/trail?limit=100&type="+encodeURIComponent(lt.value)+"&q="+encodeURIComponent(curQ)) }catch(e){ return }
  document.getElementById("logs").innerHTML=lg.trail.length?lg.trail.map(e=>
    "<div class='ev "+sevClass(e)+"'>"+esc(e.ts)+"  <b>"+esc(e.type)+"</b>  "+esc(e.tool||"")+
    "  <span class='small'>"+esc(JSON.stringify(e.detail||{}))+"</span></div>").join("")
    :"<p class='small'>No trail events match.</p>";
  if(selectedTask){ detail(selectedTask) }
  showPage(curPage);
}
document.getElementById("taskform").addEventListener("submit",async e=>{
  e.preventDefault();const g=document.getElementById("goal").value;
  try{ await j("/api/tasks",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({goal:g})}) }catch(err){}
  document.getElementById("goal").value="";refresh();});
if(AUTH_REQUIRED && !cookieTok()) document.getElementById("loginbox").style.display="block";
const startPage=(location.hash||"#overview").slice(1);
showPage(["overview","board","approvals","tasks","memory","skills","analytics","logs","system"].includes(startPage)?startPage:"overview");
refresh();setInterval(refresh,5000);
</script></body></html>"""



def make_handler(store: Store, agent: Agent, token: str | None, host: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet; tokens are never logged
            pass

        def _send(self, code: int, body, ctype="application/json"):
            if isinstance(body, (dict, list)):
                raw = json.dumps(body).encode("utf-8")
            else:
                raw = str(body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except ValueError:
                return {}

        def _authorized(self) -> bool:
            if not token:
                return True
            if self.headers.get("X-Ghost-Token") == token:
                return True
            auth = self.headers.get("Authorization") or ""
            if auth == "Bearer " + token:
                return True
            cookie = self.headers.get("Cookie") or ""
            try:
                jar = SimpleCookie(cookie)
            except Exception:
                jar = None
            if jar is not None and "ghost_token" in jar:
                return jar["ghost_token"].value == token
            return False

        def _require_auth(self) -> bool:
            if self._authorized():
                return True
            self._send(401, {"error": "dashboard token required"})
            return False

        def do_GET(self):
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/":
                page = (PAGE
                        .replace("__WARNING__", WARNING if not token else WARNING_TOKEN)
                        .replace("__VERSION__", __version__)
                        .replace("__AUTH_REQUIRED__", "1" if token else "0"))
                return self._send(200, page, "text/html; charset=utf-8")
            if path.startswith("/api/") and not self._require_auth():
                return
            if path == "/api/board":
                return self._send(200, board_payload(agent))
            if path == "/api/overview":
                return self._send(200, overview_payload(store, agent))
            if path == "/api/analytics":
                return self._send(200, analytics_payload(store, agent))
            if path == "/api/system":
                return self._send(200, system_payload(store, agent, token, host))
            if path == "/api/memory":
                mem = Memory(store)
                return self._send(200, {"trusted": mem.list_trusted(),
                                        "candidates": mem.list_candidates()})
            if path == "/api/skills":
                return self._send(200, {"skills": Skills(store).list_all()})
            if path == "/api/trail":
                qs = parse_qs(parsed.query)
                try:
                    limit = int((qs.get("limit") or ["200"])[0])
                except ValueError:
                    limit = 200
                return self._send(200, {"trail": trail_search(
                    store,
                    q=(qs.get("q") or [""])[0],
                    type_filter=(qs.get("type") or [""])[0],
                    task_id=(qs.get("task_id") or [""])[0],
                    limit=limit)})
            m = re.fullmatch(r"/api/tasks/([A-Za-z0-9_-]+)", path)
            if m:
                task = store.get("tasks", m.group(1))
                if task is None:
                    return self._send(404, {"error": "no such task"})
                return self._send(200, {"task": task,
                                        "trail": store.read_trail(task_id=task["id"])})
            return self._send(404, {"error": "not found"})

        def do_POST(self):
            path = urlparse(self.path).path
            if path.startswith("/api/") and not self._require_auth():
                return
            if path == "/api/tasks":
                data = self._body()
                goal = (data.get("goal") or data.get("title") or "").strip()
                if not goal:
                    return self._send(400, {"error": "goal is required"})
                task = agent.create_task(goal, title=data.get("title"))
                task = agent.run_task(task["id"])
                return self._send(201, {"task": task})
            m = re.fullmatch(r"/api/tasks/([A-Za-z0-9_-]+)/(approve|deny)", path)
            if m:
                task_id, action = m.group(1), m.group(2)
                if store.get("tasks", task_id) is None:
                    return self._send(404, {"error": "no such task"})
                task = (agent.approve_task(task_id) if action == "approve"
                        else agent.deny_task(task_id))
                return self._send(200, {"task": task})
            m = re.fullmatch(r"/api/memory/([A-Za-z0-9_-]+)/(approve|reject)", path)
            if m:
                mem_id, action = m.group(1), m.group(2)
                if store.get("memories", mem_id) is None:
                    return self._send(404, {"error": "no such memory"})
                mem = Memory(store)
                out = (mem.approve(mem_id) if action == "approve"
                       else mem.reject(mem_id))
                if out is None:
                    return self._send(409, {"error": "memory is not a candidate"})
                return self._send(200, {"memory": out})
            m = re.fullmatch(r"/api/skills/([A-Za-z0-9_-]+)/promote", path)
            if m:
                skill_id = m.group(1)
                if store.get("skills", skill_id) is None:
                    return self._send(404, {"error": "no such skill"})
                out = Skills(store).promote(skill_id)
                if out is None:
                    return self._send(409, {"error": "skill is not a candidate"})
                return self._send(200, {"skill": out})
            return self._send(404, {"error": "not found"})

    return Handler


def make_server(host: str = "127.0.0.1", port: int = 8770,
                store: Store | None = None, agent: Agent | None = None,
                token: str | None = None):
    if token is None:
        token = env_token()
    require_bind_allowed(host, token)  # fail closed before anything is served
    store = store or Store()
    agent = agent or Agent(store)
    server = ThreadingHTTPServer((host, port),
                                 make_handler(store, agent, token, host))
    server.ghost_token = token  # presence only; never logged
    server.ghost_host = host
    return server


def serve(host: str = "127.0.0.1", port: int = 8770) -> int:
    server = make_server(host, port)  # raises DashboardConfigError if refused
    print(f"Ghost Agent dashboard on http://{host}:{server.server_address[1]}/")
    print("WARNING: " + (WARNING_TOKEN if server.ghost_token else WARNING))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
