"""Sandboxed tools. Every path is resolved inside the workspace root;
anything escaping the root is refused. Kinds drive governance:
read = auto-allow, write/exec = human approval required.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

TOOL_KINDS = {
    "read_file": "read",
    "list_dir": "read",
    "echo_note": "read",
    "write_file": "write",
    "shell": "exec",
}


def tool_kind(name: str) -> str | None:
    return TOOL_KINDS.get(name)


class ToolError(Exception):
    pass


class Tools:
    def __init__(self, workspace: str | Path):
        self.root = Path(workspace).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

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

    def write_file(self, path: str, content: str = "") -> dict:
        p = self._resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"ok": True, "output": f"wrote {len(content)} chars to {path}"}

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
