# Vikunja AI Butler

An intelligent, fail-closed GTD (Getting Things Done) assistant and evening summary digest for [Vikunja](https://vikunja.io/).

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/Python-3.9%2B-green.svg)](https://www.python.org/)
[![Tests: 100% Green](https://img.shields.io/badge/Tests-100%25%20Passing-brightgreen.svg)](tests/)

---

## Overview

**Vikunja AI Butler** bridges your self-hosted Vikunja task manager with local or cloud LLMs to deliver an automated, secure GTD workflow:

1. **Autonomous Inbox Classification**: Periodically scans your Vikunja Inbox and categorizes tasks into GTD target lists (e.g., Single Action, Project, Waiting For).
2. **Traceable Action Expansion**: Clarifies vague or ambiguous inbox items (e.g., `"learn spark"`) into actionable steps, while strictly preserving original wording for rollback traceability (`原条目: <original_title>`).
3. **Fail-Closed Security Guardrails**: Any malformed JSON, unmapped task ID, duplicate entry, or forbidden field immediately aborts the entire batch. Task completion status (`done`) is strictly read-only.
4. **Comprehensive Evening Digest**: Aggregates tasks completed today (with strict timezone alignment), presents active GTD pending lists, correlates optional ActivityWatch screen time, and generates an evening reflection.
5. **Delivery Agnostic**: Supports local console output, file backup, or standard RFC 5322 MIME email via [Himalaya](https://github.com/pimalaya/himalaya).

---

## Architecture

```
                       +------------------------+
                       |    Vikunja Server      |
                       |       (REST API)       |
                       +-----------+------------+
                                   |
                     Read Inbox / Fetch Done
                                   v
+------------------+      +------------------+      +-------------------+
|  ActivityWatch   | ---> |  AI Butler Core  | <--- |   LLM Command     |
| (Optional Time)  |      | (classify/digest)|      | (agy / curl / CLI)|
+------------------+      +--------+---------+      +-------------------+
                                   |
                         Fail-Closed Validation
                       (Whitelist / Trace / Done)
                                   |
                  +----------------+----------------+
                  |                                 |
                  v                                 v
        [Execute Mutation]                  [Evening Digest]
      - Move to GTD Projects              - Completed today (CST/UTC)
      - Update expanded text              - Pending GTD breakdown
      - Retain vague/skipped              - Screen time breakdown
                                          - Email / Journal backup
```

---

## Features

- **Strict Fail-Closed Operation**:
  - Whitelist-enforced project targets: only pre-configured project IDs are allowed.
  - Read-only task state: `done` status is physically immutable by the classifier.
  - Atomic batch guard: If any single item fails validation, zero mutations occur.
  - Zero token waste: If the inbox is empty, the classifier exits cleanly without invoking the LLM.
- **Trace-Preserving Item Expansion**:
  - Vague items receive an actionable `expanded_title` and `expanded_description`.
  - The first line of `expanded_description` is guaranteed to be `原条目: <original_title>`, making rollback and audit trivial.
  - Well-defined tasks are untouched (`null` expansion).
- **Timezone-Aware Evening Digest**:
  - Accurately converts UTC task completion timestamps to your local timezone (e.g., `Asia/Shanghai`) to prevent day-boundary misattributions.
  - Graceful degradation: If LLM reflection times out or fails, pending tasks and completed summaries remain 100% visible.
- **Model & Runner Agnostic**:
  - Configurable CLI execution template (`{prompt}`). Compatible with Antigravity CLI (`agy`), OpenAI-compatible API `curl` scripts, Ollama, or local Python wrappers.
- **Zero Hardcoded Secrets**:
  - Fully decoupled credentials via `.env` and configuration via `config.yaml` / `config.json`.

---

## Prior Art & Deduplication Analysis

*Analysis conducted on 2026-09-02 across GitHub and open-source productivity tooling.*

| Project / Solution | Transport / Protocol | Classification & Expansion | Fail-Closed Safety | Daily Digest & Screen Time |
| :--- | :--- | :--- | :--- | :--- |
| **Vikunja AI Butler** (This repo) | Native REST API (Zero heavy deps) | Autonomous GTD + Traceable expansion (`原条目:`) | Strict (All-or-nothing, `done` immutable) | Timezone-aware digest + optional ActivityWatch + Himalaya |
| **vja** (`cernst72/vja`) | Standalone CLI utility | None (Interactive task tool) | N/A (Manual user commands) | None |
| **taskwarrior-sync** / bots | CLI / Hooks | Tag synchronization | Partial | None |
| **Vikunja Webhook Bots** | Generic Webhooks | Basic notification triggers | None | None |

### Deduplication Conclusion
No existing open-source project provides autonomous GTD classification with fail-closed safety, rollback trace preservation, and multi-source evening summarization tailored for Vikunja.

---

## Design Notes: Evaluation of `vja`

During architectural planning, [vja](https://github.com/cernst72/vja) (a Python CLI for Vikunja) was evaluated as a candidate transport layer to avoid custom HTTP calls.

### Subcommand Coverage Verification
Inspection of `vja` (version 6.0.3) confirmed support for:
1. Task Listing: `vja ls --json` / `vja ls --project <id>`
2. Moving Tasks: `vja edit <id> --project <id>`
3. Updating Descriptions: `vja edit <id> --note "<text>"`
4. Updating Titles: `vja edit <id> --title "<text>"`

### Architectural Decision & Trade-offs
Although `vja` supports individual edit subcommands, **direct Vikunja REST API integration was chosen as the default transport engine** for the following critical reasons:
1. **Headless & Non-Interactive Safety**: `vja` relies strictly on a local configuration file hierarchy (`~/.config/vja/config.rc` and `token.json`). If tokens expire or configs are missing, `vja` triggers interactive terminal prompts (`click.prompt`), which hangs non-interactive systemd timers or cron jobs.
2. **Minimal Dependencies**: Direct REST API communication requires only Python's standard library (`urllib.request`), eliminating extra package installations (`pipx install vja`) and avoiding coupling to upstream CLI breaking changes.
3. **Atomic Multi-Field Updates**: Moving a task, updating its title, and writing its expanded description can be performed in a single standard HTTP payload (`POST /api/v1/tasks/<id>`), minimizing API roundtrips and race conditions.

---

## Security & Data Integrity

- **Environment-Isolated Credentials**: `VIKUNJA_TOKEN` and `VIKUNJA_URL` are strictly read from `.env` or system environment variables. They are never written to disk, passed via command-line arguments, or exposed in LLM prompts.
- **Least-Privilege Mutation Principle**: During inbox classification, only three fields are ever permitted in API mutation payloads:
  - `project_id`
  - `title`
  - `description`
  Attempts to modify task state (`done`), create tasks, or delete tasks are rejected prior to execution.
- **Pre-Mutation Validation**: LLM output is parsed into strongly-typed structures and validated against strict integrity rules before any mutating API call is dispatched.

---

## Quickstart

### 1. Prerequisites
- Python 3.9 or newer.
- A running [Vikunja](https://vikunja.io/) instance (API v1).
- An API Token generated from Vikunja (`Settings -> API Tokens`) with Read/Write permissions on Tasks and Projects.

### 2. Installation
Clone the repository:
```bash
git clone https://github.com/your-username/vikunja-ai-butler.git
cd vikunja-ai-butler
```

Verify your environment:
```bash
./install.sh
```

### 3. Configuration
Copy the configuration and environment templates:
```bash
cp .env.example .env
cp config.example.yaml config.yaml
```

Edit `.env` with your Vikunja credentials:
```bash
chmod 600 .env
# Set VIKUNJA_URL and VIKUNJA_TOKEN
```

Customize `config.yaml` to match your GTD project IDs:
```yaml
gtd:
  inbox_project_id: 1
  allowed_target_projects:
    5: "Single Action"
    6: "Project"
    7: "Waiting For"
```

Configure your LLM command in `config.yaml` or provide an executable wrapper:

> [!IMPORTANT]
> **LLM Runner Setup Required**:
> The default configuration references `./examples/openai-curl.sh "{prompt}"`, which is provided as an example template (`examples/openai-curl.sh.example`).
> Before running the classifier or digest, you **must set up an executable LLM runner**:
> 
> 1. **Using OpenAI / Compatible API**:
>    ```bash
>    cp examples/openai-curl.sh.example examples/openai-curl.sh
>    chmod +x examples/openai-curl.sh
>    # Set your API key and base URL in .env or your environment:
>    # export OPENAI_API_KEY="sk-..."
>    # export OPENAI_BASE_URL="https://api.openai.com/v1"
>    ```
> 2. **Using Antigravity CLI (`agy`)**:
>    ```bash
>    cp examples/agy-run.sh.example examples/agy-run.sh
>    chmod +x examples/agy-run.sh
>    ```
>    And update `config.yaml`:
>    ```yaml
>    llm:
>      command: "./examples/agy-run.sh -p \"{prompt}\""
>    ```
> 3. **Using Ollama or Any Custom CLI Tool**:
>    Point `llm.command` to any command that accepts the `{prompt}` placeholder and writes raw JSON to stdout (e.g. `ollama run mistral "{prompt}"`).
> 
> The butler will fail-closed and abort gracefully if the LLM runner is missing or returns invalid JSON.

---

## Usage

### Run Inbox Classifier
Run in dry-run mode (safe simulation, no tasks modified):
```bash
./butler/classify.py --dry-run
```

Run in live execution mode:
```bash
./butler/classify.py
```

### Run Evening Digest
Preview today's summary in terminal:
```bash
./butler/digest.py --dry-run
```

Generate summary for a specific date:
```bash
./butler/digest.py --dry-run --date 2026-09-02
```

---

## Automation (Systemd User Timers)

Templates for systemd user services and timers are provided in `systemd/`:

1. Install user units:
```bash
./install.sh --install-units
```

2. Enable and start timers:
```bash
systemctl --user enable --now vikunja-butler-classify.timer
systemctl --user enable --now vikunja-butler-digest.timer
```

3. Check timer schedules:
```bash
systemctl --user list-timers
```

---

## Testing

The project includes an automated test suite covering all normal paths, edge cases, and fail-closed violations:

```bash
./tests/run_tests.sh
```

Or run via pytest:
```bash
pytest -v tests/
```

---

## License

This project is open-source software licensed under the [MIT License](LICENSE).
