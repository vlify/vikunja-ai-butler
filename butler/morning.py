#!/usr/bin/env python3
"""
morning -- Automated Morning News and Task Reconciliation Module (GTD Morning Brief).

Architecture & Safety Principles (SPEC-morning.md final v2.1):
1. Zero Token Priority: GitHub notification parsing, reason mapping, mail queries,
   and Vikunja reconciliation are 100% native Python (0 LLM tokens).
2. Unique LLM Step: Translation of titles/key phrases runs in a SINGLE batch LLM call.
   Timeout/failure gracefully falls back to the original English text.
3. Fail-Closed Idempotency: Reconciles with Vikunja before creating tasks.
   Existing tasks (same prefix + title, or same URL) marked as ⟳已在待办.
4. Read-Only Redline:
   - GitHub: only GET /notifications (NEVER PUT /notifications to mark as read).
   - Himalaya: only search/read (NEVER flag seen or delete).
   - Zero token leakage in logs or exceptions.
5. Strict Markdown Formatting: Bold numbered paragraphs (**1)**), blank lines between items,
   STRICT PROHIBITION of Markdown list syntax (- / * / 1.).
"""

import os
import re
import sys
import json
import shutil
import argparse
import datetime
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple
from email.utils import parsedate_to_datetime

# Ensure project root is in sys.path
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from butler.config import load_config
from butler.vikunja_client import VikunjaClient, VikunjaAPIError
from butler.llm_runner import LLMRunner, LLMError, LLMTimeoutError
from butler.matrix import deliver_morning_report, MatrixDeliveryError

# Reason mapping according to SPEC §2
GITHUB_REASON_MAP: Dict[str, str] = {
    "mention": "提及",
    "review_requested": "请求审查",
    "subscribe": "订阅",
    "assign": "指派",
    "team_mention": "团队提及",
    "author": "作者动态",
    "comment": "评论",
    "ci_activity": "CI",
}

# Actionable reasons: review_requested, mention, assign, team_mention
ACTIONABLE_REASONS = {"review_requested", "mention", "assign", "team_mention"}


class MorningItem:
    """Represents a single morning news or notification item."""
    def __init__(
        self,
        source: str,
        repo_or_project: str,
        title: str,
        url: str,
        updated_at: Optional[datetime.datetime] = None,
        reason: str = "",
        reason_cn: str = "",
        is_actionable: bool = False,
        translated_title: Optional[str] = None,
        status: str = "",
    ):
        self.source = source  # "GitHub" | "GitLab" | "Greasyfork" | "Codeberg" | "Email"
        self.repo_or_project = repo_or_project
        self.title = title
        self.url = url
        self.updated_at = updated_at
        self.reason = reason
        self.reason_cn = reason_cn
        self.is_actionable = is_actionable
        self.translated_title = translated_title or title
        self.status = status  # "✅已入待办" | "⟳已在待办" | ""

    def __repr__(self) -> str:
        return f"<MorningItem [{self.source}] {self.repo_or_project} - {self.title} ({self.status})>"


def parse_github_tsv_line(line: str, tz_name: str = "Asia/Shanghai") -> Optional[MorningItem]:
    """
    Tolerantly parse a single TSV line from GitHub notifications output:
    [updated_at, reason, repo_full_name, title, url]
    Handles extra tabs in title, quotes, backslashes, mixed Chinese/English,
    unknown reasons, missing columns, and invalid timestamps.
    """
    if not line or not line.strip():
        return None

    # Split with maxsplit=4:
    # 0: updated_at, 1: reason, 2: repo, 3: title (may contain remaining tabs), 4: url
    # But wait: if title contains tabs, splitting by 4 from the left would put tabs into title,
    # and last token would be url ONLY IF split from right or structured carefully.
    # Notice: url is always a URL starting with https://.
    parts = line.strip().split("\t")
    if len(parts) < 5:
        return None

    updated_at_raw = parts[0].strip()
    reason_raw = parts[1].strip()
    repo_raw = parts[2].strip()
    # The last part is url
    url_raw = parts[-1].strip()
    # Any intermediate parts form the title
    title_raw = "\t".join(parts[3:-1]).strip() if len(parts) > 5 else parts[3].strip()

    # Timezone conversion
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    dt_obj: Optional[datetime.datetime] = None
    if updated_at_raw:
        try:
            iso_str = updated_at_raw.replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(iso_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            dt_obj = dt.astimezone(tz)
        except Exception:
            dt_obj = datetime.datetime.now(tz)
    else:
        dt_obj = datetime.datetime.now(tz)

    reason_cn = GITHUB_REASON_MAP.get(reason_raw, reason_raw or "未知")
    is_actionable = reason_raw in ACTIONABLE_REASONS

    return MorningItem(
        source="GitHub",
        repo_or_project=repo_raw,
        title=title_raw,
        url=url_raw,
        updated_at=dt_obj,
        reason=reason_raw,
        reason_cn=reason_cn,
        is_actionable=is_actionable,
    )


def fetch_github_notifications(
    gh_bin: str = "/usr/bin/gh",
    tz_name: str = "Asia/Shanghai",
) -> Tuple[List[MorningItem], bool]:
    """
    Fetch GitHub notifications using the read-only gh CLI api command.
    Enforces 'env -u GIT_DIR -u GIT_WORK_TREE' isolation.
    Returns (items, success).
    """
    bin_path = gh_bin if os.path.isabs(gh_bin) and os.path.exists(gh_bin) else shutil.which("gh") or "/usr/bin/gh"

    cmd = (
        f"env -u GIT_DIR -u GIT_WORK_TREE {bin_path} api '/notifications?per_page=30' "
        f"--jq '.[] | [.updated_at, .reason, .repository.full_name, .subject.title, "
        f"(.subject.url | sub(\"api.github.com/repos/\"; \"github.com/\") | sub(\"/pulls/\"; \"/pull/\"))] | @tsv'"
    )

    try:
        res = subprocess.run(
            cmd,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    except Exception as e:
        print(f"[WARN] Failed to execute GitHub notification command: {e}", file=sys.stderr)
        return [], False

    if res.returncode != 0:
        print(f"[WARN] GitHub notifications returned non-zero code {res.returncode}: {res.stderr.strip()}", file=sys.stderr)
        return [], False

    items: List[MorningItem] = []
    for line in res.stdout.splitlines():
        item = parse_github_tsv_line(line, tz_name=tz_name)
        if item:
            items.append(item)

    return items, True


def parse_date_flexible(date_str: str, tz: ZoneInfo) -> Optional[datetime.datetime]:
    """Parse various mail date formats into target timezone."""
    date_str = date_str.strip()
    if not date_str:
        return None

    # Try DD/MM/YYYY HH:MM
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            dt = datetime.datetime.strptime(date_str, fmt)
            return dt.replace(tzinfo=tz)
        except ValueError:
            pass

    # Try RFC 2822
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.astimezone(tz)
    except Exception:
        pass

    # Try ISO
    try:
        dt = datetime.datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(tz)
    except Exception:
        pass

    return None


def parse_gmail_envelope(
    envelope_id: str,
    subject: str,
    sender: str,
    date_str: str,
    now_dt: Optional[datetime.datetime] = None,
    tz_name: str = "Asia/Shanghai",
) -> Optional[MorningItem]:
    """
    Parse envelope metadata and apply strict 24-hour boundary filtering.
    Classify into platform and determine actionability.
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    if now_dt is None:
        now_dt = datetime.datetime.now(tz)
    elif now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=tz)
    else:
        now_dt = now_dt.astimezone(tz)

    mail_dt = parse_date_flexible(date_str, tz)
    if mail_dt is None:
        # If timestamp cannot be parsed, reject or fallback to reject to preserve 24h filter
        return None

    # Strict 24h filter: only retain if within the last 24h
    delta = now_dt - mail_dt
    if delta < datetime.timedelta(seconds=0) or delta > datetime.timedelta(hours=24):
        return None

    # Determine source platform
    sender_lower = sender.lower()
    if "greasyfork" in sender_lower:
        source = "Greasyfork"
    elif "gitlab" in sender_lower:
        source = "GitLab"
    elif "codeberg" in sender_lower:
        source = "Codeberg"
    else:
        source = "Email"

    # Determine actionability:
    # 讨论留言 / issue 评论 -> 需行动; 版本发布 -> 纯信息; 拿不准 -> 纯信息
    subj_lower = subject.lower()
    actionable_keywords = ["issue", "comment", "discussion", "merge request", "pull request", "mention", "review", "reply"]
    info_keywords = ["release", "published", "v1.", "v2.", "version", "announcement"]

    is_actionable = False
    if any(k in subj_lower for k in actionable_keywords):
        is_actionable = True
    elif any(k in subj_lower for k in info_keywords):
        is_actionable = False
    else:
        # "拿不准 -> 纯信息"
        is_actionable = False

    # Try to extract repository or project from subject or sender
    repo_or_project = ""
    # Common pattern: [group/project] Subject
    m = re.search(r"\[([^\]]+)\]", subject)
    if m:
        repo_or_project = m.group(1).strip()
    else:
        repo_or_project = source

    return MorningItem(
        source=source,
        repo_or_project=repo_or_project,
        title=subject,
        url="",  # To be enriched via body read if available
        updated_at=mail_dt,
        reason="mail",
        reason_cn="邮件",
        is_actionable=is_actionable,
    )


def fetch_gmail_messages(
    himalaya_bin: str = "himalaya",
    account: str = "gmail",
    now_dt: Optional[datetime.datetime] = None,
    tz_name: str = "Asia/Shanghai",
) -> Tuple[List[MorningItem], bool]:
    """
    Fetch unread messages from Himalaya Gmail account for greasyfork/gitlab/codeberg.
    Read-only invariant: NEVER flags seen or alters messages.
    If search returns empty, executes envelope list to verify genuinely empty vs command error.
    """
    bin_path = os.path.expanduser(himalaya_bin)
    if not os.path.isabs(bin_path) or not os.path.exists(bin_path):
        bin_path = shutil.which(himalaya_bin) or bin_path

    search_query = "(((from greasyfork) or (from gitlab) or (from codeberg) or (from codeberg.org)) and not flag seen)"
    search_cmd = [bin_path, "envelope", "search", "--account", account, "-s", "10", search_query]

    try:
        res = subprocess.run(
            search_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=30,
        )
    except Exception as e:
        print(f"[WARN] Failed to execute himalaya search: {e}", file=sys.stderr)
        return [], False

    if res.returncode != 0:
        # Check fallback list to distinguish error from empty
        list_cmd = [bin_path, "envelope", "list", "--account", account, "-s", "10"]
        try:
            list_res = subprocess.run(
                list_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
            if list_res.returncode != 0:
                return [], False
        except Exception:
            return [], False
        return [], False

    output = res.stdout.strip()
    if not output:
        # Empty result: run fallback envelope list
        list_cmd = [bin_path, "envelope", "list", "--account", account, "-s", "10"]
        try:
            list_res = subprocess.run(
                list_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
            if list_res.returncode == 0:
                # Confirmed genuinely empty
                return [], True
            else:
                return [], False
        except Exception:
            return [], False

    # Parse search output
    # Himalaya table or tsv: ID | FLAGS | SUBJECT | FROM | DATE
    items: List[MorningItem] = []
    lines = output.splitlines()

    for line in lines:
        line_s = line.strip()
        if not line_s or line_s.startswith("ID") or set(line_s) <= set("-+| "):
            continue

        # Handle tab or pipe delimiters
        if "\t" in line_s:
            parts = [p.strip() for p in line_s.split("\t")]
        else:
            parts = [p.strip() for p in line_s.split("|")]

        if len(parts) < 5:
            continue

        env_id = parts[0]
        subject = parts[2]
        sender = parts[3]
        date_str = parts[4]

        item = parse_gmail_envelope(
            envelope_id=env_id,
            subject=subject,
            sender=sender,
            date_str=date_str,
            now_dt=now_dt,
            tz_name=tz_name,
        )
        if not item:
            continue

        # Fetch message body to extract key phrase and URL
        read_cmd = [bin_path, "message", "read", "--account", account, env_id]
        try:
            read_res = subprocess.run(
                read_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
            if read_res.returncode == 0 and read_res.stdout:
                body_text = read_res.stdout
                # Extract first prominent URL
                urls = re.findall(r"https?://[^\s<>\"']+", body_text)
                if urls:
                    item.url = urls[0].rstrip(".,;)>]")

                # Extract key sentence from body for translation/summary
                # Filter out headers and take first non-empty meaningful sentence
                for bline in body_text.splitlines():
                    bline_s = bline.strip()
                    if bline_s and not bline_s.startswith(("> ", "From:", "To:", "Subject:", "Date:", "--")):
                        # Found key sentence
                        item.title = f"{item.title}: {bline_s}" if len(bline_s) < 80 else item.title
                        break
        except Exception:
            pass

        items.append(item)

    return items, True


def translate_items_batch(items: List[MorningItem], llm: Optional[LLMRunner]) -> None:
    """
    Translate titles/descriptions to Chinese using a SINGLE batch LLM call.
    If items is empty, does NOT invoke LLM.
    If LLM fails, times out, or output is malformed, falls back to original text.
    """
    if not items:
        return

    if llm is None:
        for item in items:
            item.translated_title = item.title
        return

    # Build single batch translation prompt
    lines_to_translate = []
    for idx, item in enumerate(items, 1):
        lines_to_translate.append(f"{idx}. {item.title}")

    prompt = (
        "你是一个专业的技术翻译助手。请将以下待办与通知英文标题准确翻译为简洁的中文（保持专业术语如 CI, MR, PR, Bug, Segfault 不变）。\n"
        "严格返回 JSON 数组格式，不要输出任何其他内容。\n"
        "格式示例: [{\"index\": 1, \"translation\": \"中文翻译\"}]\n\n"
        "待翻译列表:\n" + "\n".join(lines_to_translate)
    )

    try:
        raw_output = llm.run(prompt)
        if not raw_output or not raw_output.strip():
            # Fallback
            for item in items:
                item.translated_title = item.title
            return

        # Attempt to parse JSON
        # Locate JSON array
        m = re.search(r"\[\s*\{.*\}\s*\]", raw_output, re.DOTALL)
        if m:
            json_str = m.group(0)
            data = json.loads(json_str)
            trans_map = {}
            for entry in data:
                if isinstance(entry, dict) and "index" in entry and "translation" in entry:
                    trans_map[int(entry["index"])] = str(entry["translation"]).strip()

            for idx, item in enumerate(items, 1):
                if idx in trans_map and trans_map[idx]:
                    item.translated_title = trans_map[idx]
                else:
                    item.translated_title = item.title
        else:
            # Malformed output -> fallback
            for item in items:
                item.translated_title = item.title
    except Exception as e:
        print(f"[WARN] LLM translation failed or timed out: {e}. Falling back to original titles.", file=sys.stderr)
        for item in items:
            item.translated_title = item.title


def normalize_title_for_dedup(title: str) -> str:
    """Normalize title for fuzzy/case-insensitive matching."""
    t = title.strip().lower()
    # Remove leading platform prefix like [github], [gitlab]
    t = re.sub(r"^\[[a-z0-9_\-]+\]\s*", "", t)
    # Remove excessive whitespaces
    t = re.sub(r"\s+", " ", t)
    return t


def reconcile_with_vikunja(
    actionable_items: List[MorningItem],
    client: Optional[VikunjaClient],
    target_project_id: int = 1,
    dry_run: bool = False,
) -> Tuple[List[str], bool]:
    """
    Reconcile actionable items against existing Vikunja tasks.
    Deduplication rules:
    - Same platform prefix + same title (case-insensitive) -> ⟳已在待办
    - Same description/source link -> ⟳已在待办
    - Otherwise -> creates task (intercepted if dry_run) and marks ✅已入待办
    Returns (pending_task_titles, success).
    """
    if client is None:
        for item in actionable_items:
            item.status = "✅已入待办" if not dry_run else "✅已入待办(DRY-RUN,未实际创建)"
        return [], False

    try:
        tasks = client.get_tasks()
    except VikunjaAPIError as e:
        print(f"[WARN] Failed to fetch tasks from Vikunja: {e}", file=sys.stderr)
        return [], False

    # Extract pending (not done) tasks
    pending_tasks: List[str] = []
    existing_items: List[Tuple[str, str, str]] = []  # (platform_lower, normalized_title, description)

    for t in tasks:
        if not isinstance(t, dict):
            continue
        if not t.get("done", False):
            title = str(t.get("title", "")).strip()
            desc = str(t.get("description", "")).strip()
            if title:
                pending_tasks.append(title)

            m = re.match(r"^\[([a-zA-Z0-9_\-]+)\]", title)
            platform = m.group(1).lower() if m else ""
            norm_title = normalize_title_for_dedup(title)
            existing_items.append((platform, norm_title, desc))

    # Reconcile each actionable item
    for item in actionable_items:
        item_platform = item.source.lower()
        item_norm_title = normalize_title_for_dedup(item.title)
        item_norm_translated = normalize_title_for_dedup(item.translated_title or item.title)
        item_url = item.url.strip()

        is_dup = False
        for ex_platform, ex_norm_title, ex_desc in existing_items:
            # 1) Link match
            if item_url and item_url in ex_desc:
                is_dup = True
                break
            # 2) Same platform prefix AND same title (case-insensitive)
            if item_platform == ex_platform and (item_norm_title == ex_norm_title or item_norm_translated == ex_norm_title):
                is_dup = True
                break

        if is_dup:
            item.status = "⟳已在待办"
        else:
            task_title = f"[{item.source}] {item.translated_title or item.title}".strip()
            task_desc = item.url.strip()

            if dry_run:
                # DRY-RUN: intercept creation!
                print(f"[DRY-RUN] Would create Vikunja task in project {target_project_id}: '{task_title}' ({task_desc})")
                item.status = "✅已入待办(DRY-RUN,未实际创建)"
            else:
                try:
                    client.create_task(
                        project_id=target_project_id,
                        title=task_title,
                        description=task_desc,
                    )
                    item.status = "✅已入待办"
                except Exception as e:
                    print(f"[WARN] Failed to create Vikunja task '{task_title}': {e}", file=sys.stderr)
                    item.status = "创建失败"

    return pending_tasks, True


def format_morning_report(
    target_date: str,
    actionable_items: List[MorningItem],
    info_items: List[MorningItem],
    pending_tasks: List[str],
    gh_ok: bool,
    mail_ok: bool,
    vikunja_ok: bool,
) -> str:
    """
    Format morning brief report strictly conforming to SPEC §4.
    STRICT PROHIBITION OF MARKDOWN LISTS (- item, * item, 1. item).
    Items are separated by blank lines and indexed with bold numbers (**1)**).
    """
    sections: List[str] = [f"📬 **消息与待办 · {target_date}**"]

    # 1. Actionable items
    sections.append("📥 **需要行动**")
    if not gh_ok and not mail_ok:
        sections.append("不可用")
    elif not actionable_items:
        sections.append("无")
    else:
        act_lines = []
        for idx, item in enumerate(actionable_items, 1):
            time_str = item.updated_at.strftime("%H:%M") if item.updated_at else ""
            time_part = f"({time_str})" if time_str else ""
            line = f"**{idx})** [{item.source}] {item.repo_or_project} — {item.translated_title or item.title} {item.url}{time_part}{item.status}".strip()
            act_lines.append(line)
        sections.append("\n\n".join(act_lines))

    # 2. Pure info items
    sections.append("📰 **纯信息**")
    if not gh_ok and not mail_ok:
        sections.append("不可用")
    elif not info_items:
        sections.append("无")
    else:
        info_lines = []
        for idx, item in enumerate(info_items, 1):
            time_str = item.updated_at.strftime("%H:%M") if item.updated_at else ""
            time_part = f"({time_str})" if time_str else ""
            line = f"**{idx})** [{item.source}] {item.repo_or_project} — {item.translated_title or item.title} {item.url}{time_part}".strip()
            info_lines.append(line)
        sections.append("\n\n".join(info_lines))

    # 3. Current pending tasks
    sections.append("📋 **当前未完成待办**")
    if not vikunja_ok:
        sections.append("不可用")
    elif not pending_tasks:
        sections.append("无")
    else:
        pending_lines = []
        for idx, task_title in enumerate(pending_tasks, 1):
            pending_lines.append(f"**{idx})** {task_title}")
        sections.append("\n\n".join(pending_lines))

    return "\n\n".join(sections).strip() + "\n"


def run_morning(
    dry_run: bool = False,
    config_path: Optional[str] = None,
    env_path: Optional[str] = None,
    target_date: Optional[str] = None,
    client: Optional[VikunjaClient] = None,
    llm: Optional[LLMRunner] = None,
) -> int:
    """
    Main orchestration entrypoint for the morning news and reconciliation report.
    Returns exit code (0 on success or graceful degradation, non-zero on delivery failure).
    """
    cfg = load_config(config_path=config_path, env_path=env_path)
    tz_name = cfg.get("timezone", "Asia/Shanghai")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    if not target_date:
        target_date = datetime.datetime.now(tz).strftime("%Y-%m-%d")

    morning_cfg = cfg.get("morning", {})
    gh_cfg = morning_cfg.get("github", {})
    mail_cfg = morning_cfg.get("gmail", {})
    target_project_id = morning_cfg.get("target_project_id", 1)

    # 1. Fetch GitHub notifications
    gh_items: List[MorningItem] = []
    gh_ok = True
    if gh_cfg.get("enabled", True):
        gh_bin = gh_cfg.get("gh_bin", "/usr/bin/gh")
        gh_items, gh_ok = fetch_github_notifications(gh_bin=gh_bin, tz_name=tz_name)

    # 2. Fetch Gmail messages
    mail_items: List[MorningItem] = []
    mail_ok = True
    if mail_cfg.get("enabled", True):
        himalaya_bin = mail_cfg.get("himalaya_bin", "himalaya")
        account = mail_cfg.get("account", "gmail")
        mail_items, mail_ok = fetch_gmail_messages(
            himalaya_bin=himalaya_bin,
            account=account,
            tz_name=tz_name,
        )

    all_items = gh_items + mail_items

    # 3. Translate titles in a single batch LLM call
    if llm is None and cfg.get("llm", {}).get("command"):
        llm_cfg = cfg.get("llm", {})
        llm = LLMRunner(
            command_template=llm_cfg.get("command", ""),
            timeout_seconds=llm_cfg.get("timeout_seconds", 240),
        )

    translate_items_batch(all_items, llm)

    # Separate actionable vs info-only
    actionable_items = [item for item in all_items if item.is_actionable]
    info_items = [item for item in all_items if not item.is_actionable]

    # 4. Reconcile with Vikunja
    if client is None:
        v_cfg = cfg.get("vikunja", {})
        client = VikunjaClient(
            base_url=v_cfg.get("url"),
            token=v_cfg.get("token"),
            timeout=v_cfg.get("timeout_seconds", 30),
        )

    pending_tasks, vikunja_ok = reconcile_with_vikunja(
        actionable_items=actionable_items,
        client=client,
        target_project_id=target_project_id,
        dry_run=dry_run,
    )

    # 5. Format report
    report = format_morning_report(
        target_date=target_date,
        actionable_items=actionable_items,
        info_items=info_items,
        pending_tasks=pending_tasks,
        gh_ok=gh_ok,
        mail_ok=mail_ok,
        vikunja_ok=vikunja_ok,
    )

    # 6. Deliver report or print (dry-run)
    if dry_run:
        print(report)
        return 0

    try:
        deliver_morning_report(report, cfg)
        print("[OK] Morning report delivered successfully.")
        return 0
    except Exception as e:
        print(f"[FAIL] Notification delivery failed: {e}", file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser(description="Morning News and Task Reconciliation for Vikunja")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Dry-run simulation mode without creating tasks or delivering notifications")
    parser.add_argument("-d", "--date", type=str, help="Target date in YYYY-MM-DD format (defaults to today)")
    parser.add_argument("-c", "--config", type=str, help="Path to configuration file")
    parser.add_argument("-e", "--env-file", type=str, help="Path to .env credentials file")
    args = parser.parse_args()

    exit_code = run_morning(
        dry_run=args.dry_run,
        config_path=args.config,
        env_path=args.env_file,
        target_date=args.date,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
