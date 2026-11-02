"""Sandboxed tools. Every path is resolved inside the workspace root;
anything escaping the root is refused. Kinds drive governance:
read = auto-allow, write/exec = human approval required.

Ecosystem tools (v0.8.0) are all READ class and registry-bound:
they inspect only projects registered in the ecosystem registry
(~/workspace/ghost-agent/src/ghost_agent/ecosystem.py), and
read_project_file never leaves a registered project's local root.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

TOOL_KINDS = {
    "read_file": "read",
    "list_dir": "read",
    "echo_note": "read",
    "search_files": "read",
    "list_projects": "read",
    "repo_status": "read",
    "read_project_file": "read",
    "write_file": "write",
    "append_file": "write",
    "shell": "exec",
}

SEARCH_MAX_RESULTS = 50
SEARCH_MAX_FILE_BYTES = 1_000_000
PROJECT_FILE_MAX_CHARS = 200_000
PROJECT_FILE_TRUNCATION = ("\n[truncated: file is larger than the "
                           "200KB read cap]")
GIT_TIMEOUT_SECONDS = 10


def tool_kind(name: str) -> str | None:
    return TOOL_KINDS.get(name)


def validate_args(name: str, args) -> str | None:
    """Check a proposed step's args against the tool's signature.

    Returns an error string (same wording the dispatcher raises,
    e.g. "missing args for search_files: ['query']") or None when
    the args are executable. Unknown tools return None here on
    purpose: they are governance's deny-by-default path, not an
    argument problem. Used by the agent loop (v0.7.0) to feed
    validation errors back to the model instead of dying on them.
    """
    if name not in TOOL_KINDS:
        return None
    if not isinstance(args, dict):
        return f"args for {name} must be an object"
    import inspect
    fn = getattr(Tools, name)
    params = inspect.signature(fn).parameters
    missing = [k for k, p in params.items()
               if k != "self" and p.default is inspect.Parameter.empty
               and k not in args]
    if missing:
        return f"missing args for {name}: {missing}"
    return None


class ToolError(Exception):
    pass


class Tools:
    def __init__(self, workspace: str | Path, ecosystem=None):
        self.root = Path(workspace).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        # The ecosystem registry (ghost_agent.ecosystem.Ecosystem)
        # backing the v0.8.0 project tools. Optional: without one,
        # those tools answer a plain error instead of guessing.
        self.ecosystem = ecosystem

    def _resolve(self, rel: str) -> Path:
        if rel is None or rel == "":
            rel = "."
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ToolError(f"path escapes workspace sandbox: {rel!r}")
        return p

    # ---- individual tools ----------------------------------------------
    def read_file(self, path: str) -> dict:
        p = self._resolve(path)
        if not p.is_file():
            return {"ok": False, "error": f"no such file: {path}", "output": ""}
        return {"ok": True, "output": p.read_text(encoding="utf-8", errors="replace")}

    def list_dir(self, path: str = ".") -> dict:
        p = self._resolve(path)
        if not p.is_dir():
            return {"ok": False, "error": f"no such directory: {path}", "output": ""}
        names = sorted(e.name + ("/" if e.is_dir() else "") for e in p.iterdir())
        return {"ok": True, "output": "\n".join(names), "entries": names}

    def echo_note(self, text: str = "") -> dict:
        return {"ok": True, "output": str(text)}

    def search_files(self, query: str, path: str = ".") -> dict:
        """Literal substring search across the workspace (read tool).

        Returns matching files as workspace-relative paths with line
        numbers and the matched line text. Caps at SEARCH_MAX_RESULTS
        matches, skips binary files and files over 1 MB, and cannot
        escape the workspace root: the base path goes through the
        same _resolve sandbox check as every other tool, and each
        candidate file is re-checked for containment (symlinks
        included). Matching is case-sensitive and literal — this is
        a finder, not a regex engine.
        """
        query = str(query or "")
        if not query:
            return {"ok": False, "error": "query is required",
                    "output": "", "matches": []}
        base = self._resolve(path)  # raises ToolError outside the root
        if base.is_file():
            candidates = [base]
        elif base.is_dir():
            candidates = sorted(p for p in base.rglob("*") if p.is_file())
        else:
            return {"ok": False, "error": f"no such path: {path}",
                    "output": "", "matches": []}
        matches: list[dict] = []
        truncated = False
        for f in candidates:
            try:
                resolved = f.resolve()
            except OSError:
                continue
            if resolved != self.root and self.root not in resolved.parents:
                continue  # symlink escaping the sandbox
            try:
                if f.stat().st_size > SEARCH_MAX_FILE_BYTES:
                    continue
                data = f.read_bytes()
            except OSError:
                continue
            if b"\x00" in data[:8192]:
                continue  # binary
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                continue
            rel = resolved.relative_to(self.root).as_posix()
            for lineno, line in enumerate(text.splitlines(), start=1):
                if query in line:
                    matches.append({"path": rel, "line": lineno,
                                    "text": line[:300]})
                    if len(matches) >= SEARCH_MAX_RESULTS:
                        truncated = True
                        break
            if truncated:
                break
        output = "\n".join(f"{m['path']}:{m['line']}: {m['text']}"
                           for m in matches)
        return {"ok": True, "output": output, "matches": matches,
                "truncated": truncated}

    # ---- ecosystem tools (v0.8.0, all READ class) ----------------------
    _REGISTRY_FIX = (" — add it to the ecosystem registry "
                     "(ecosystem.json in the agent home) to make it "
                     "visible")

    def _registry(self):
        """(ecosystem, error_result): the registry, or the plain
        error the tool answers when none was wired in."""
        if self.ecosystem is None:
            return None, {"ok": False,
                          "error": "ecosystem registry is not available "
                                   "in this context",
                          "output": ""}
        return self.ecosystem, None

    def _unknown_project(self, project: str) -> dict:
        return {"ok": False,
                "error": f"unknown project {project!r}: not in the "
                         f"ecosystem registry{self._REGISTRY_FIX}",
                "output": ""}

    def list_projects(self, layer: str = "") -> dict:
        """List the ecosystem registry (optionally one layer).
        has_local is computed live: whether the entry's local
        tree exists on this machine."""
        eco, err = self._registry()
        if err is not None:
            err["projects"] = []
            return err
        projects = eco.list_entries(layer=layer or None)
        lines = []
        for p in projects:
            local = "local tree present" if p["has_local"] \
                else "no local tree"
            lines.append(f"{p['name']} [{p['layer']}] {p['role']} "
                         f"({local})")
        return {"ok": True, "output": "\n".join(lines),
                "projects": projects}

    def _git(self, path: Path, *args: str) -> str | None:
        """One read-only git command in a project tree. Returns
        stripped stdout, or None on any failure (not a repo, git
        missing, timeout) — callers phrase the outcome."""
        try:
            proc = subprocess.run(
                ["git", "-C", str(path), *args],
                capture_output=True, text=True,
                timeout=GIT_TIMEOUT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        return proc.stdout.strip()

    def repo_status(self, project: str) -> dict:
        """Status of one REGISTERED project: local tree presence,
        git branch, dirty count, and last commit. Local facts
        only — no network, no GitHub API (v0.8.0 scope)."""
        eco, err = self._registry()
        if err is not None:
            return err
        entry = eco.get(project)
        if entry is None:
            return self._unknown_project(project)
        name = entry["name"]
        base = {"project": name, "repo": entry.get("repo"),
                "local_path": entry.get("local_path")}
        local_path = entry.get("local_path")
        if not local_path or not Path(local_path).expanduser().exists():
            output = f"{name}: no local tree on this machine"
            if entry.get("repo"):
                output += f" (repo: {entry['repo']})"
            return {**base, "ok": True, "output": output,
                    "has_local": False, "is_git_repo": None,
                    "branch": None, "dirty_count": None,
                    "last_commit": None}
        root = Path(local_path).expanduser()
        if self._git(root, "rev-parse", "--git-dir") is None:
            return {**base, "ok": True,
                    "output": f"{name}: local tree at {local_path} is "
                              f"not a git repository",
                    "has_local": True, "is_git_repo": False,
                    "branch": None, "dirty_count": None,
                    "last_commit": None}
        branch = self._git(root, "rev-parse", "--abbrev-ref", "HEAD") \
            or "unknown"
        porcelain = self._git(root, "status", "--porcelain")
        dirty = len([ln for ln in (porcelain or "").splitlines()
                     if ln.strip()]) if porcelain is not None else None
        last_commit = None
        log = self._git(root, "log", "-1", "--format=%h%x1f%cI%x1f%s")
        if log:
            parts = log.split("\x1f")
            if len(parts) == 3:
                last_commit = {"hash": parts[0], "date": parts[1],
                               "subject": parts[2]}
        output = (f"{name}: branch {branch} - "
                  f"{dirty if dirty is not None else 'unknown'} "
                  f"uncommitted change(s)")
        if last_commit:
            output += (f" - last commit {last_commit['hash']} "
                       f"({last_commit['date']}) "
                       f"{last_commit['subject']}")
        else:
            output += " - no commits yet"
        return {**base, "ok": True, "output": output,
                "has_local": True, "is_git_repo": True,
                "branch": branch, "dirty_count": dirty,
                "last_commit": last_commit}

    def read_project_file(self, project: str, path: str) -> dict:
        """Read a text file inside a REGISTERED project's local
        root. The resolved path must stay under the root (the
        registry is the allowlist; traversal is refused, raising
        ToolError like the workspace sandbox). Output is capped
        at 200KB of text with a truncation marker; binary files
        get a clear error instead of garbage."""
        eco, err = self._registry()
        if err is not None:
            return err
        entry = eco.get(project)
        if entry is None:
            return self._unknown_project(project)
        local_path = entry.get("local_path")
        if not local_path or not Path(local_path).expanduser().is_dir():
            return {"ok": False,
                    "error": f"project {entry['name']!r} has no local "
                             f"tree on this machine",
                    "output": ""}
        root = Path(local_path).expanduser().resolve()
        candidate = Path(path).expanduser()
        target = candidate if candidate.is_absolute() \
            else (root / candidate)
        target = target.resolve()
        if target != root and root not in target.parents:
            raise ToolError(f"path escapes project root: {path!r}")
        if not target.is_file():
            return {"ok": False,
                    "error": f"no such file in {entry['name']}: {path}",
                    "output": ""}
        try:
            data = target.read_bytes()
        except OSError as exc:
            return {"ok": False, "error": f"could not read file: {exc}",
                    "output": ""}
        if b"\x00" in data[:8192]:
            return {"ok": False,
                    "error": f"file looks binary (not text): {path}",
                    "output": ""}
        text = data.decode("utf-8", errors="replace")
        truncated = len(text) > PROJECT_FILE_MAX_CHARS
        if truncated:
            text = text[:PROJECT_FILE_MAX_CHARS] + PROJECT_FILE_TRUNCATION
        return {"ok": True, "output": text, "truncated": truncated}

    def write_file(self, path: str, content: str = "") -> dict:
        p = self._resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"ok": True, "output": f"wrote {len(content)} chars to {path}"}

    def append_file(self, path: str, content: str = "") -> dict:
        """Append text exactly as given (no newline added). WRITE
        class: governance pauses it for approval like write_file."""
        p = self._resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(content)
        return {"ok": True, "output": f"appended {len(content)} chars to {path}"}

    def shell(self, command: str, timeout: int = 15) -> dict:
        try:
            proc = subprocess.run(
                command, shell=True, cwd=str(self.root),
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"shell timed out after {timeout}s",
                    "output": "", "exit_code": None}
        out = (proc.stdout or "") + (proc.stderr or "")
        return {"ok": proc.returncode == 0, "output": out,
                "exit_code": proc.returncode}

    # ---- dispatcher ------------------------------------------------------
    def execute(self, name: str, args: dict | None = None) -> dict:
        args = dict(args or {})
        if name not in TOOL_KINDS:
            raise ToolError(f"unknown tool: {name}")
        fn = getattr(self, name)
        # Only pass arguments the tool actually declares, by name.
        import inspect
        params = inspect.signature(fn).parameters
        call_args = {k: v for k, v in args.items() if k in params}
        missing = [k for k, p in params.items()
                   if p.default is inspect.Parameter.empty and k not in call_args]
        if missing:
            raise ToolError(f"missing args for {name}: {missing}")
        return fn(**call_args)
