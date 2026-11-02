"""Dashboard server: stdlib http.server, binds 127.0.0.1 by default.

LOCAL ONLY. NO AUTHENTICATION. Anyone who can reach this port can
approve agent actions on this machine. Do not expose it beyond your
own machine. The UI says the same thing, prominently.
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .agent import Agent
from .memory import Memory
from .skills import Skills
from .store import Store

WARNING = "LOCAL ONLY - NO AUTHENTICATION - do not expose beyond this machine"

PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ghost Agent</title>
<style>
body{font-family:system-ui,sans-serif;margin:0;background:#0d0b14;color:#eee}
header{background:#6716f3;color:#fff;padding:12px 16px}
header h1{margin:0;font-size:20px}
.warn{background:#3a2400;color:#ffcf6e;padding:8px 16px;font-size:13px}
main{padding:12px 16px;max-width:1100px;margin:0 auto}
.board{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:10px}
.col{background:#181325;border-radius:10px;padding:10px;min-height:80px}
.col h3{margin:2px 0 8px;font-size:14px;text-transform:uppercase;letter-spacing:.04em}
.card{background:#221a38;border-radius:8px;padding:8px;margin:6px 0;font-size:14px;word-break:break-word}
.card a{color:#c9b3ff}
button{background:#6716f3;color:#fff;border:0;border-radius:6px;padding:8px 12px;cursor:pointer}
button.deny{background:#7a1f2b}
input,textarea{width:100%;box-sizing:border-box;padding:8px;border-radius:6px;border:1px solid #444;background:#181325;color:#eee}
section{margin:18px 0}
pre{white-space:pre-wrap;word-break:break-word;background:#181325;padding:10px;border-radius:8px;font-size:12px}
.small{font-size:12px;color:#aaa}
</style></head><body>
<header><h1>Ghost Agent</h1><div class="small" style="color:#e6dcff">Persistent governed operator - Ghost Developer Studio</div></header>
<div class="warn">WARNING: __WARNING__</div>
<main>
<section><h2>New task</h2>
<form id="taskform"><input id="goal" placeholder="e.g. write notes.txt with: hello" required>
<p><button type="submit">Create and run task</button></p></form></section>
<section><h2>Board</h2><div class="board" id="board"></div></section>
<section><h2>Approvals queue</h2><div id="approvals"></div></section>
<section><h2>Task detail + trail</h2><div id="detail"><p class="small">Tap a task on the board.</p></div></section>
<section><h2>Memory (trusted)</h2><div id="memory"></div></section>
<section><h2>Skills</h2><div id="skills"></div></section>
</main>
<script>
const cols=[["running","Running"],["waiting_approval","Waiting approval"],["done","Done"],["failed","Failed"]];
async function j(u,o){const r=await fetch(u,o);return r.json()}
function esc(s){return String(s??"").replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
async function detail(id){
  const d=await j("/api/tasks/"+id);
  let h="<h3>"+esc(d.task.title)+"</h3><p class='small'>status: "+esc(d.task.status)+" - model: "+esc(d.task.model)+"</p>";
  d.task.steps.forEach((s,i)=>{h+="<div class='card'>"+i+". "+esc(s.tool)+" - "+esc(s.status)+(s.error?" - "+esc(s.error):"")+"</div>"});
  h+="<pre>"+esc(d.trail.map(e=>e.ts+" "+e.type+" "+(e.tool||"")).join("\\n"))+"</pre>";
  document.getElementById("detail").innerHTML=h;
}
async function refresh(){
  const b=await j("/api/board");
  document.getElementById("board").innerHTML=cols.map(([k,label])=>
    "<div class='col'><h3>"+label+"</h3>"+(b.columns[k]||[]).map(t=>
      "<div class='card'><a href='#' onclick='detail(\\""+t.id+"\\");return false'>"+esc(t.title)+"</a><div class='small'>"+esc(t.status)+"</div></div>").join("")+"</div>").join("");
  const pend=b.pending_approvals||[];
  document.getElementById("approvals").innerHTML=pend.length?pend.map(a=>
    "<div class='card'>"+esc(a.tool)+" on task "+esc(a.task_id)+"<br><button onclick='act(\\""+a.task_id+"\\",\\"approve\\")'>Approve</button> "+
    "<button class='deny' onclick='act(\\""+a.task_id+"\\",\\"deny\\")'>Deny</button></div>").join(""):"<p class='small'>Nothing waiting.</p>";
  const m=await j("/api/memory");
  document.getElementById("memory").innerHTML=(m.trusted||[]).map(x=>"<div class='card'>"+esc(x.text)+"<div class='small'>source: "+esc(x.source)+"</div></div>").join("")||"<p class='small'>No trusted memory yet.</p>";
  const s=await j("/api/skills");
  document.getElementById("skills").innerHTML=(s.skills||[]).map(x=>"<div class='card'>"+esc(x.name)+" - "+(x.trusted?"trusted":"candidate (untrusted)")+"<div class='small'>tools: "+esc((x.tools||[]).join(", "))+"</div></div>").join("")||"<p class='small'>No skills yet.</p>";
}
async function act(id,what){await j("/api/tasks/"+id+"/"+what,{method:"POST"});refresh()}
document.getElementById("taskform").addEventListener("submit",async e=>{
  e.preventDefault();const g=document.getElementById("goal").value;
  await j("/api/tasks",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({goal:g})});
  document.getElementById("goal").value="";refresh();});
refresh();setInterval(refresh,5000);
</script></body></html>"""


def board_payload(agent: Agent) -> dict:
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


def make_handler(store: Store, agent: Agent):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # quiet
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

        def do_GET(self):
            path = urlparse(self.path).path
            if path == "/":
                return self._send(200, PAGE.replace("__WARNING__", WARNING),
                                  "text/html; charset=utf-8")
            if path == "/api/board":
                return self._send(200, board_payload(agent))
            if path == "/api/memory":
                mem = Memory(store)
                return self._send(200, {"trusted": mem.list_trusted(),
                                        "candidates": mem.list_candidates()})
            if path == "/api/skills":
                return self._send(200, {"skills": Skills(store).list_all()})
            if path == "/api/trail":
                return self._send(200, {"trail": store.read_trail()})
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
            return self._send(404, {"error": "not found"})

    return Handler


def make_server(host: str = "127.0.0.1", port: int = 8770,
                store: Store | None = None, agent: Agent | None = None):
    store = store or Store()
    agent = agent or Agent(store)
    server = ThreadingHTTPServer((host, port), make_handler(store, agent))
    return server


def serve(host: str = "127.0.0.1", port: int = 8770) -> int:
    server = make_server(host, port)
    print(f"Ghost Agent dashboard on http://{host}:{server.server_address[1]}/")
    print("WARNING: " + WARNING)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0
