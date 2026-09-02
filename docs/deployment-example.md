# Private Deployment Guide & Configuration Example

This document demonstrates how to configure **Vikunja AI Butler** for a secure, headless private deployment (e.g. running via `systemd --user`).

## 1. Directory Structure

By default, Butler searches for configuration in standard XDG locations before falling back to local repository paths:

```
~/.config/vikunja-ai-butler/
├── config.yaml          # Main butler configuration (permissions: 0600)
└── .env                 # (Optional) Direct credentials file
```

Credentials (such as `VIKUNJA_URL` and `VIKUNJA_TOKEN`) can be loaded either from an existing central `.env` file (e.g., `~/.config/hermes/.env`) or directly from system environment variables.

---

## 2. Example `~/.config/vikunja-ai-butler/config.yaml`

```yaml
# ==============================================================================
# Vikunja AI Butler — Production Configuration Example
# ==============================================================================

# Explicit path to .env file for credentials (expanded automatically)
env_file: "~/.config/hermes/.env"

# Display & date cutoff timezone
timezone: "Asia/Shanghai"

# Vikunja connection settings (URL and Token are loaded from env_file)
vikunja:
  timeout_seconds: 30

# GTD Project Mapping & Classification Whitelist
gtd:
  inbox_project_id: 1
  allowed_target_projects:
    5: "Single Action"
    6: "Project"
    7: "Waiting For"
  forbidden_target_projects:
    - 1 # Inbox itself (strictly forbidden as destination)
    - 8 # Someday / Maybe (defensive guard)
  digest_project_order:
    - 1 # Inbox
    - 5 # Single Action
    - 6 # Project
    - 7 # Waiting For

# LLM CLI or API Wrapper
llm:
  # Command template receiving the prompt via {prompt}
  command: "/path/to/llm-runner.sh \"{prompt}\""
  timeout_seconds: 240

# ActivityWatch Screen Time Integration (Optional)
activitywatch:
  enabled: true
  client_bin: "aw-client"
  queries_dir: "~/.config/aw-client/queries"
  # Shell command wrapper template (e.g. fish -c for custom fish functions/aliases)
  shell_command: "fish -c '{cmd}'"
  background_apps:
    - "cs2"
    - "steam"
    - "screensaver"

# Notification Delivery via Himalaya CLI
notifier:
  type: "himalaya"
  account: "default"
  from: "bot@example.com"
  to: "user@example.com"

# Local Backup & Journal Visibility
backup:
  enabled: true
  file_path: "/tmp/vikunja-ai-butler-last.txt"
```

---

## 3. Security & Fail-Closed Guarantees

1. **Zero Hardcoded Secrets**: Neither API tokens nor mail passwords reside in `config.yaml`. Himalaya resolves credentials via its own account configuration or keyring.
2. **Fail-Closed Fallback**: If LLM, network, or mail sending encounters an error, the digest report is safely written to the local backup file, and the process exits with a non-zero code to notify systemd monitoring.
3. **Trace Preservation**: All vague inbox tasks expanded by the AI preserve the original task title on the first line (`原条目: <title>`).
