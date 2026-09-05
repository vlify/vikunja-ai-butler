"""
Himalaya Gmail messages collector for morning notification digests.
"""

import os
import re
import sys
import shutil
import datetime
import subprocess
from zoneinfo import ZoneInfo
from typing import List, Optional, Tuple

from butler.morning_app.models import MorningItem, parse_date_flexible


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
        is_actionable = False

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
                return [], True
            else:
                return [], False
        except Exception:
            return [], False

    items: List[MorningItem] = []
    lines = output.splitlines()

    for line in lines:
        line_s = line.strip()
        if not line_s or line_s.startswith("ID") or set(line_s) <= set("-+| "):
            continue

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
                urls = re.findall(r"https?://[^\s<>\"']+", body_text)
                if urls:
                    item.url = urls[0].rstrip(".,;)>]")

                for bline in body_text.splitlines():
                    bline_s = bline.strip()
                    if bline_s and not bline_s.startswith(("> ", "From:", "To:", "Subject:", "Date:", "--")):
                        item.title = f"{item.title}: {bline_s}" if len(bline_s) < 80 else item.title
                        break
        except Exception:
            pass

        items.append(item)

    return items, True
