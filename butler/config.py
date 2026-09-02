"""
Configuration and environment loader for Vikunja AI Butler.

Fail-closed security principle:
- Credentials (token, URL) MUST come from environment variables or .env file.
- Project IDs and whitelists MUST be explicitly configured.
- No sensitive user IDs, personal paths, or tokens are hardcoded.
"""

import os
import sys
import json
from pathlib import Path
from typing import Any, Dict, Optional

# Optional PyYAML support with safe fallback
try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False


def load_env_file(env_path: Optional[str] = None, override: bool = False) -> None:
    """
    Parse a .env file and inject variables into os.environ.
    Avoids hard dependency on python-dotenv.
    """
    candidate_paths = []
    if env_path:
        candidate_paths.append(Path(env_path))
    elif "ENV_FILE" in os.environ:
        candidate_paths.append(Path(os.environ["ENV_FILE"]))
    else:
        candidate_paths.extend([
            Path.cwd() / ".env",
            Path(__file__).resolve().parent.parent / ".env",
        ])

    for p in candidate_paths:
        if p.is_file():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        key, val = line.split("=", 1)
                        key = key.strip()
                        val = val.strip().strip("'\"")
                        if key:
                            if override or key not in os.environ:
                                os.environ[key] = val
                break
            except Exception as e:
                print(f"[WARN] Failed to load .env from {p}: {e}", file=sys.stderr)


DEFAULT_CONFIG: Dict[str, Any] = {
    "timezone": "Asia/Shanghai",
    "vikunja": {
        "url": "",
        "token": "",
        "timeout_seconds": 30,
    },
    "gtd": {
        "inbox_project_id": 1,
        "allowed_target_projects": {
            5: "Single Action",
            6: "Project",
            7: "Waiting For",
        },
        "forbidden_target_projects": [1, 8],
        "digest_project_order": [1, 5, 6, 7],
    },
    "llm": {
        "command": "./examples/openai-curl.sh \"{prompt}\"",
        "timeout_seconds": 240,
    },
    "activitywatch": {
        "enabled": False,
        "client_bin": "aw-client",
        "queries_dir": "",
        "background_apps": ["cs2", "steam", "screensaver"],
    },
    "notifier": {
        "type": "none",
        "himalaya": {
            "bin": "himalaya",
            "account": "default",
            "mail_from": "",
            "mail_to": "",
        },
    },
    "backup": {
        "enabled": True,
        "file_path": "/tmp/vikunja-ai-butler-last.txt",
    },
}


def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge configuration dictionaries."""
    merged = base.copy()
    for k, v in override.items():
        if k in merged and isinstance(merged[k], dict) and isinstance(v, dict):
            merged[k] = deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def load_config(config_path: Optional[str] = None, env_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Load merged configuration from file and environment variables.
    """
    load_env_file(env_path)

    config = DEFAULT_CONFIG.copy()

    # Search for config file
    candidate_paths = []
    if config_path:
        candidate_paths.append(Path(config_path))
    elif "BUTLER_CONFIG_PATH" in os.environ:
        candidate_paths.append(Path(os.environ["BUTLER_CONFIG_PATH"]))
    else:
        root_dir = Path(__file__).resolve().parent.parent
        candidate_paths.extend([
            Path.cwd() / "config.yaml",
            Path.cwd() / "config.json",
            root_dir / "config.yaml",
            root_dir / "config.json",
        ])

    loaded_from_file: Dict[str, Any] = {}
    for p in candidate_paths:
        if p.is_file():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    if p.suffix in (".yaml", ".yml"):
                        if HAS_YAML:
                            loaded_from_file = yaml.safe_load(f) or {}
                        else:
                            print(f"[WARN] PyYAML not installed. Cannot parse {p}", file=sys.stderr)
                    elif p.suffix == ".json":
                        loaded_from_file = json.load(f) or {}
                break
            except Exception as e:
                print(f"[WARN] Failed to read config file {p}: {e}", file=sys.stderr)

    if loaded_from_file:
        config = deep_merge(config, loaded_from_file)

    # Normalize integer keys in allowed_target_projects
    target_projects = config.get("gtd", {}).get("allowed_target_projects", {})
    normalized_targets = {}
    for k, v in target_projects.items():
        try:
            normalized_targets[int(k)] = str(v)
        except (ValueError, TypeError):
            normalized_targets[k] = str(v)
    config.setdefault("gtd", {})["allowed_target_projects"] = normalized_targets

    # Environment variable overrides
    if "VIKUNJA_URL" in os.environ and os.environ["VIKUNJA_URL"]:
        config.setdefault("vikunja", {})["url"] = os.environ["VIKUNJA_URL"].rstrip("/")
    if "VIKUNJA_TOKEN" in os.environ and os.environ["VIKUNJA_TOKEN"]:
        config.setdefault("vikunja", {})["token"] = os.environ["VIKUNJA_TOKEN"]
    if "BUTLER_TIMEZONE" in os.environ and os.environ["BUTLER_TIMEZONE"]:
        config["timezone"] = os.environ["BUTLER_TIMEZONE"]
    if "LLM_COMMAND" in os.environ and os.environ["LLM_COMMAND"]:
        config.setdefault("llm", {})["command"] = os.environ["LLM_COMMAND"]

    return config
