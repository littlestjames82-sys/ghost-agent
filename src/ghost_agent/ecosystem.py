"""Ecosystem registry (v0.8.0): the studio's projects, as data.

The registry is a JSON list of {name, repo, local_path, layer,
role, source} entries. The agent home's ecosystem.json is the
source of truth; on first run (no home file) it is seeded from
the bundled resource (ecosystem_seed.json, 72 projects) and
written to the home, so the user owns an editable copy.

Loading is defensive: a registry file that cannot be parsed, or
entries missing required fields (name, layer), never crash
startup. The failure stays visible — `load_error` / `skipped`
on the instance (surfaced by GET /api/ecosystem) and, for a
broken home file, a best-effort trail event. A broken home file
is the user's file: it is never overwritten by us.

The registry is the allowlist for the ecosystem tools
(tools.list_projects / repo_status / read_project_file): only
registered projects can be inspected, and reads never leave a
registered project's local root. Layers keep the seed's values;
LAYER_ORDER is the display order used by the dashboard page.
"""
from __future__ import annotations

import json
from pathlib import Path

SEED_PATH = Path(__file__).resolve().with_name("ecosystem_seed.json")

LAYER_ORDER = ["kernel", "law", "language", "rails", "hosting",
               "bodies", "products", "tools", "other"]

_REQUIRED = ("name", "layer")


def _opt_str(value) -> str | None:
    return value if isinstance(value, str) and value else None


def _validate(raw) -> tuple[list[dict], int]:
    """Normalize a parsed registry list. Returns (entries,
    skipped): entries missing a non-empty string name or layer,
    and non-object entries, are skipped and counted."""
    entries: list[dict] = []
    skipped = 0
    for item in raw:
        if not isinstance(item, dict):
            skipped += 1
            continue
        name, layer = item.get("name"), item.get("layer")
        if not (isinstance(name, str) and name.strip()) \
                or not (isinstance(layer, str) and layer.strip()):
            skipped += 1
            continue
        entries.append({
            "name": name.strip(),
            "repo": _opt_str(item.get("repo")),
            "local_path": _opt_str(item.get("local_path")),
            "layer": layer.strip(),
            "role": item.get("role") if isinstance(item.get("role"), str) else "",
            "source": item.get("source") if isinstance(item.get("source"), str) else "",
        })
    return entries, skipped


def _parse(text: str, what: str) -> tuple[list[dict], int]:
    """Parse registry JSON text into validated entries. Raises
    ValueError with a plain-language reason on bad shape."""
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{what} is not valid JSON: {exc}") from exc
    if not isinstance(raw, list):
        raise ValueError(f"{what} must be a JSON list of project entries")
    return _validate(raw)


class Ecosystem:
    """The registry for one agent home. Load order: the home
    file when present (always wins), otherwise the bundled seed,
    which is also written to the home as the user's copy."""

    def __init__(self, store):
        self.store = store
        self.home = store.home
        self.path = self.home / "ecosystem.json"
        self.entries: list[dict] = []
        self.load_error: str | None = None
        self.skipped = 0
        self.seeded = False
        self._load()

    # ---- loading -----------------------------------------------------
    def _load(self) -> None:
        if self.path.exists():
            try:
                text = self.path.read_text(encoding="utf-8")
            except OSError as exc:
                self.load_error = f"ecosystem registry could not be read: {exc}"
                self.store.try_trail(
                    "ecosystem_load_failed", {"error": self.load_error})
                return
            try:
                self.entries, self.skipped = _parse(
                    text, "ecosystem.json in the agent home")
            except ValueError as exc:
                self.load_error = str(exc)
                self.store.try_trail(
                    "ecosystem_load_failed", {"error": self.load_error})
                return
            if self.skipped:
                self.store.try_trail(
                    "ecosystem_entries_skipped", {"skipped": self.skipped})
            return
        # First run: seed from the bundled resource and give the
        # user the editable copy. Seeding writes no trail event —
        # a fresh home's trail belongs to the user's activity.
        try:
            seed_text = SEED_PATH.read_text(encoding="utf-8")
            self.entries, self.skipped = _parse(
                seed_text, "the bundled ecosystem seed")
        except (OSError, ValueError) as exc:
            self.load_error = f"ecosystem seed could not be loaded: {exc}"
            return
        self.seeded = True
        try:
            self.path.write_text(seed_text, encoding="utf-8")
        except OSError:
            pass  # entries are loaded for this process regardless

    # ---- queries -------------------------------------------------------
    @staticmethod
    def has_local(entry: dict) -> bool:
        """True when the entry's local_path exists as a directory
        on THIS machine (computed live — registries travel, trees
        do not)."""
        local_path = entry.get("local_path")
        if not local_path:
            return False
        return Path(local_path).expanduser().is_dir()

    def get(self, name: str) -> dict | None:
        """Look a project up by name: exact match first, then
        case-insensitive. Returns the registry entry or None."""
        if not name:
            return None
        for e in self.entries:
            if e["name"] == name:
                return e
        lowered = str(name).strip().lower()
        for e in self.entries:
            if e["name"].lower() == lowered:
                return e
        return None

    def list_entries(self, layer: str | None = None) -> list[dict]:
        """Entries (optionally one layer), each with has_local
        computed live for this machine."""
        out = []
        for e in self.entries:
            if layer and e["layer"] != layer:
                continue
            entry = dict(e)
            entry["has_local"] = self.has_local(e)
            out.append(entry)
        return out

    def raw_entries(self) -> list[dict]:
        """The registry as stored (no computed fields) — the
        shape /api/export carries."""
        return [dict(e) for e in self.entries]


def get_ecosystem(store) -> Ecosystem:
    """One Ecosystem per store, cached on the store object so
    the agent, the tools, and the dashboard all share the same
    loaded registry (and the same load outcome)."""
    eco = getattr(store, "_ecosystem", None)
    if eco is None:
        eco = Ecosystem(store)
        store._ecosystem = eco
    return eco
