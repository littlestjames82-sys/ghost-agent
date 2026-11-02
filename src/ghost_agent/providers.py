"""Provider profiles (v0.10.0): named model providers, as data.

A providers.json in the agent home describes where the agent's
model calls may go:

  {"strategy": "default" | "chain" | "cheapest" | "best",
   "default": <profile name or null>,
   "chain": [<profile name>, ...],
   "profiles": {"<name>": {"base_url": ..., "model": ...,
                           "api_key_env": <env var name or null>,
                           "kind": "openai-compatible" | "anthropic",
                           "tier": "frontier" | "standard" | "small"
                                   (optional)}}}

The home file is the source of truth; on first run (no home
file) it is seeded from the presets below and written to the
home, so the user owns an editable copy. Seeding pins NO default
profile (``"default": null``): with nothing named, the router
keeps the pre-v0.10.0 legacy behavior — the implicit "env"
profile built from GHOST_AGENT_API_KEY / GHOST_AGENT_BASE_URL /
GHOST_AGENT_MODEL — exactly.

KEY RULE: a profile names the environment variable that holds
its API key (api_key_env); the key VALUE is never written to
providers.json, the store, or the trail. Adapters read the named
variable at call time only. A profile is "configured" when
api_key_env is null (keyless local servers: Ollama, LM Studio)
or the named variable is set and non-empty.

Loading is defensive, like the ecosystem registry: a file that
cannot be parsed, or entries missing required fields, never
crash startup. The failure stays visible — `load_error` /
`skipped` on the instance (surfaced by GET /api/system) plus a
best-effort trail event — and the router falls back to the
legacy env behavior. A broken home file is the user's file: it
is never overwritten by us.

Tiers (v0.11.0): a profile may carry a "tier" — "frontier",
"standard", or "small" — used by the "best" router strategy.
The EFFECTIVE tier of a profile is its own tier when valid,
else the preset tier for a same-named profile, else None: a
providers.json written by v0.10.0 (no tier keys anywhere) still
yields the preset tiers, because the merge happens at read
time. The user's file is never rewritten to add tiers. An
invalid tier value is ignored for selection but stays visible
raw in the /api/system block as "tier_configured".
"""
from __future__ import annotations

import json
import os
from pathlib import Path

STRATEGIES = ("default", "chain", "cheapest", "best")
KINDS = ("openai-compatible", "anthropic")

# Capability tiers for the "best" strategy (v0.11.0), best first.
TIERS = ("frontier", "standard", "small")
TIER_RANK = {"frontier": 3, "standard": 2, "small": 1}

# The presets seeded into a fresh home's providers.json. Model
# names are each provider's documented default-class choice;
# users edit the seeded file to change them.
PRESET_PROFILES: dict[str, dict] = {
    "meta": {"base_url": "https://api.meta.ai/v1",
             "model": "muse-spark-1.3",
             "api_key_env": "META_API_KEY",
             "kind": "openai-compatible",
             "tier": "frontier"},
    "openai": {"base_url": "https://api.openai.com/v1",
               "model": "gpt-4o-mini",
               "api_key_env": "OPENAI_API_KEY",
               "kind": "openai-compatible",
               "tier": "standard"},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1",
                   "model": "openai/gpt-4o-mini",
                   "api_key_env": "OPENROUTER_API_KEY",
                   "kind": "openai-compatible",
                   "tier": "standard"},
    "fireworks": {"base_url": "https://api.fireworks.ai/inference/v1",
                  "model": "accounts/fireworks/models/llama-v3p3-70b-instruct",
                  "api_key_env": "FIREWORKS_API_KEY",
                  "kind": "openai-compatible",
                  "tier": "standard"},
    "groq": {"base_url": "https://api.groq.com/openai/v1",
             "model": "llama-3.3-70b-versatile",
             "api_key_env": "GROQ_API_KEY",
             "kind": "openai-compatible",
             "tier": "standard"},
    "together": {"base_url": "https://api.together.xyz/v1",
                 "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
                 "api_key_env": "TOGETHER_API_KEY",
                 "kind": "openai-compatible",
                 "tier": "standard"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
               "model": "gemini-2.5-flash",
               "api_key_env": "GEMINI_API_KEY",
               "kind": "openai-compatible",
               "tier": "standard"},
    "ollama": {"base_url": "http://127.0.0.1:11434/v1",
               "model": "llama3.1",
               "api_key_env": None,
               "kind": "openai-compatible",
               "tier": "small"},
    "lmstudio": {"base_url": "http://127.0.0.1:1234/v1",
                 "model": "local-model",
                 "api_key_env": None,
                 "kind": "openai-compatible",
                 "tier": "small"},
    "anthropic": {"base_url": "https://api.anthropic.com",
                  "model": "claude-sonnet-4-5",
                  "api_key_env": "ANTHROPIC_API_KEY",
                  "kind": "anthropic",
                  "tier": "frontier"},
}


def seed_document() -> dict:
    """The providers.json written to a fresh home: the presets,
    strategy "default", NO default pinned (legacy env behavior
    until the user names one), and the chain preset to every
    profile in preset order (unconfigured entries are skipped
    by the chain strategy, so this is safe to ship)."""
    return {
        "strategy": "default",
        "default": None,
        "chain": list(PRESET_PROFILES),
        "profiles": {name: dict(p) for name, p in PRESET_PROFILES.items()},
    }


def _validate_profile(raw) -> dict | None:
    """Normalize one profile entry, or return None when it is
    unusable (not an object, missing base_url/model, a non-string
    api_key_env, or an unknown kind)."""
    if not isinstance(raw, dict):
        return None
    base_url, model = raw.get("base_url"), raw.get("model")
    if not (isinstance(base_url, str) and base_url.strip()) \
            or not (isinstance(model, str) and model.strip()):
        return None
    key_env = raw.get("api_key_env")
    if key_env is not None and not (isinstance(key_env, str) and key_env.strip()):
        return None
    kind = raw.get("kind") or "openai-compatible"
    if kind not in KINDS:
        return None
    return {
        "base_url": base_url.strip(),
        "model": model.strip(),
        "api_key_env": key_env.strip() if isinstance(key_env, str) else None,
        "kind": kind,
        # The tier as configured, kept verbatim (None when the
        # file does not name one). Validity is a selection-time
        # question — see Providers.effective_tier — so an invalid
        # value survives loading and stays visible in /api/system.
        "tier": raw.get("tier"),
    }


def _parse(text: str, what: str) -> tuple[dict, int]:
    """Parse providers JSON text into a normalized config
    {strategy, default, chain, profiles} plus a skipped-entry
    count. Raises ValueError with a plain-language reason on bad
    shape."""
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{what} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ValueError(f"{what} must be a JSON object")
    raw_profiles = raw.get("profiles") or {}
    if not isinstance(raw_profiles, dict):
        raise ValueError(f"{what}: 'profiles' must be an object")
    profiles: dict[str, dict] = {}
    skipped = 0
    for name, entry in raw_profiles.items():
        profile = _validate_profile(entry)
        if profile is None or not (isinstance(name, str) and name.strip()):
            skipped += 1
            continue
        profiles[name.strip()] = profile
    strategy = raw.get("strategy") or "default"
    if strategy not in STRATEGIES:
        strategy = "default"
    default = raw.get("default")
    if not (isinstance(default, str) and default.strip()):
        default = None
    else:
        default = default.strip()
    chain_raw = raw.get("chain") or []
    chain = [c.strip() for c in chain_raw
             if isinstance(c, str) and c.strip()] \
        if isinstance(chain_raw, list) else []
    return {"strategy": strategy, "default": default, "chain": chain,
            "profiles": profiles}, skipped


class Providers:
    """The provider configuration for one agent home. Load
    order: the home file when present (always wins), otherwise
    the presets, which are also written to the home as the
    user's copy."""

    def __init__(self, store):
        self.store = store
        self.home = store.home
        self.path = self.home / "providers.json"
        self.strategy = "default"
        self.default: str | None = None
        self.chain: list[str] = []
        self.profiles: dict[str, dict] = {}
        self.load_error: str | None = None
        self.skipped = 0
        self.seeded = False
        self._load()

    # ---- loading -----------------------------------------------------
    def _apply(self, config: dict) -> None:
        self.strategy = config["strategy"]
        self.default = config["default"]
        self.chain = config["chain"]
        self.profiles = config["profiles"]

    def _load(self) -> None:
        if self.path.exists():
            try:
                text = self.path.read_text(encoding="utf-8")
            except OSError as exc:
                self.load_error = f"providers config could not be read: {exc}"
                self.store.try_trail(
                    "providers_load_failed", {"error": self.load_error})
                return
            try:
                config, self.skipped = _parse(
                    text, "providers.json in the agent home")
            except ValueError as exc:
                self.load_error = str(exc)
                self.store.try_trail(
                    "providers_load_failed", {"error": self.load_error})
                return
            self._apply(config)
            if self.skipped:
                self.store.try_trail(
                    "providers_entries_skipped", {"skipped": self.skipped})
            return
        # First run: seed from the presets and give the user the
        # editable copy. Seeding writes no trail event — a fresh
        # home's trail belongs to the user's activity.
        doc = seed_document()
        try:
            config, self.skipped = _parse(
                json.dumps(doc), "the bundled provider presets")
        except ValueError as exc:  # the presets are ours; belt + braces
            self.load_error = f"provider presets could not be loaded: {exc}"
            return
        self._apply(config)
        self.seeded = True
        try:
            self.path.write_text(
                json.dumps(doc, indent=2, sort_keys=True) + "\n",
                encoding="utf-8")
        except OSError:
            pass  # config is loaded for this process regardless

    # ---- queries -------------------------------------------------------
    def get(self, name: str) -> dict | None:
        """The named profile (exact name), or None."""
        if not name:
            return None
        return self.profiles.get(name)

    @staticmethod
    def key_present(profile: dict) -> bool:
        """True when the profile names an env var that is set and
        non-empty. Keyless profiles report False (there is no key
        to be present) and are told apart by api_key_env itself."""
        env = profile.get("api_key_env")
        if not env:
            return False
        return bool(os.environ.get(env))

    @classmethod
    def configured(cls, profile: dict) -> bool:
        """A profile can serve when it is keyless (api_key_env
        null — local servers) or its key is present."""
        if not profile.get("api_key_env"):
            return True
        return cls.key_present(profile)

    def configured_names(self) -> list[str]:
        """Profile names that can serve right now, in file order."""
        return [name for name, p in self.profiles.items()
                if self.configured(p)]

    @staticmethod
    def effective_tier(name: str, profile: dict) -> str | None:
        """The tier the "best" strategy ranks by: the profile's
        own tier when it is one of TIERS, else the preset tier
        for a same-named profile, else None. The merge is done
        here, at read time, so a providers.json written before
        tiers existed still ranks its preset-named profiles —
        and the user's file is never rewritten to add tiers."""
        own = (profile or {}).get("tier")
        if isinstance(own, str) and own in TIERS:
            return own
        preset = PRESET_PROFILES.get(name) or {}
        preset_tier = preset.get("tier")
        if isinstance(preset_tier, str) and preset_tier in TIERS:
            return preset_tier
        return None

    # ---- dashboard -----------------------------------------------------
    def system_block(self) -> dict:
        """The /api/system providers block. Names, models,
        endpoints, kinds, and presence BOOLEANS only — a key value
        can never appear here because it is never read here."""
        return {
            "strategy": self.strategy,
            "default": self.default,
            "chain": list(self.chain),
            "load_error": self.load_error,
            "skipped_entries": self.skipped,
            "profiles": {
                name: {
                    "model": p["model"],
                    "base_url": p["base_url"],
                    "kind": p["kind"],
                    "api_key_env": p["api_key_env"],
                    "configured": self.configured(p),
                    "key_present": self.key_present(p),
                    # v0.11.0: the effective tier the "best"
                    # strategy ranks by (or null), plus the tier
                    # exactly as configured in the file (or null)
                    # so an invalid value stays visible instead
                    # of being silently normalized away.
                    "tier": self.effective_tier(name, p),
                    "tier_configured": p.get("tier"),
                }
                for name, p in self.profiles.items()
            },
        }


def get_providers(store) -> Providers:
    """One Providers per store, cached on the store object so
    the agent, the router, and the dashboard all share the same
    loaded configuration (and the same load outcome)."""
    prov = getattr(store, "_providers", None)
    if prov is None:
        prov = Providers(store)
        store._providers = prov
    return prov
