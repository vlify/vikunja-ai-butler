#!/usr/bin/env python3
"""
digest -- Automated Daily Evening GTD & ActivityWatch Summary Report.

SAFETY AND RESILIENCE PRINCIPLES:
1. Fallback & Graceful Degradation: If LLM fails or times out, the digest NEVER
   crashes. It falls back to displaying completed tasks and pending items cleanly.
2. Timezone Integrity: Completed tasks are converted to the target timezone (e.g., CST)
   before date filtering to prevent UTC boundary misattributions.
3. Notification Decoupling: If notifier is 'none' or in dry-run, output stays
   local (stdout and backup file) with zero external network side-effects.
4. Clean MIME Composition: Built via Python standard library email.message
   using email.policy.SMTP for RFC 5322 compliance.
"""

import os
import sys
import re
import ast
import json
import argparse
import tempfile
import datetime
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple

# Ensure project root is in sys.path for direct CLI and systemd invocation
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from butler.config import load_config
from butler.vikunja_client import VikunjaClient, VikunjaAPIError
from butler.llm_runner import LLMRunner, LLMError, LLMTimeoutError


def get_weekday_str(dt: datetime.datetime) -> str:
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    return weekdays[dt.weekday()]


def format_completed_tasks(
    done_tasks: List[Dict[str, Any]],
    target_date_str: str,
    tz_name: str = "Asia/Shanghai",
) -> Tuple[str, int]:
    """
    Filter and format tasks completed on target_date_str in specified timezone.
    Returns (markdown_text, count).
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    completed_today: List[Tuple[datetime.datetime, str, str]] = []

    for t in done_tasks:
        if not isinstance(t, dict):
            continue
        done_at_raw = t.get("done_at")
        if not done_at_raw or not isinstance(done_at_raw, str):
            continue

        try:
            iso_str = done_at_raw.replace("Z", "+00:00")
            dt = datetime.datetime.fromisoformat(iso_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            dt_local = dt.astimezone(tz)

            if dt_local.strftime("%Y-%m-%d") == target_date_str:
                title = t.get("title", "未命名任务").strip()
                time_str = dt_local.strftime("%H:%M")
                completed_today.append((dt_local, title, time_str))
        except Exception:
            continue

    completed_today.sort(key=lambda x: x[0])
    count = len(completed_today)

    lines = [f"## ✅ 今日已完成 ({count} 条)"]
    if count == 0:
        lines.append("今日暂无完成记录——一份清单的意义在于划掉它")
    else:
        for _, title, time_str in completed_today:
            lines.append(f"- [x] {title} (完成于 {time_str})")

    return "\n".join(lines), count


def format_pending_tasks(
    tasks: List[Dict[str, Any]],
    allowed_projects: Dict[int, str],
    project_order: List[int],
) -> str:
    """
    Filter and format active GTD pending tasks grouped by project.
    Preserves hierarchical subtask relationships (indents subtasks under parent).
    """
    task_map: Dict[int, Dict[str, Any]] = {
        t["id"]: t for t in tasks if isinstance(t, dict) and "id" in t
    }
    parent_to_children: Dict[int, List[int]] = {}
    child_to_parent: Dict[int, int] = {}

    for t in tasks:
        if not isinstance(t, dict) or t.get("done", False):
            continue
        tid = t.get("id")
        if not tid:
            continue
        related = t.get("related_tasks") or {}
        parents = related.get("parenttask") or []
        if parents and isinstance(parents, list):
            p_id = parents[0].get("id")
            if p_id and p_id in task_map:
                child_to_parent[tid] = p_id
                parent_to_children.setdefault(p_id, []).append(tid)

    lines = ["## 📋 今日待办"]
    has_any_pending = False

    for pid in project_order:
        pname = allowed_projects.get(pid)
        if not pname:
            continue
        proj_tasks = [
            t for t in tasks
            if isinstance(t, dict) and not t.get("done", False) and t.get("project_id") == pid
        ]
        if not proj_tasks:
            continue

        has_any_pending = True
        lines.append(f"\n### {pname}")

        for t in proj_tasks:
            tid = t.get("id")
            if tid in child_to_parent:
                # Subtask will be rendered nested under its parent
                continue
            title = t.get("title", "未命名任务").strip()
            lines.append(f"- [ ] {title}")

            # Render child subtasks indented
            if tid in parent_to_children:
                for cid in parent_to_children[tid]:
                    child = task_map.get(cid)
                    if child and not child.get("done", False):
                        c_title = child.get("title", "未命名任务").strip()
                        lines.append(f"  - [ ] {c_title}")

    if not has_any_pending:
        return "## 📋 今日待办\n\n（今日无未完成待办事项）"

    return "\n".join(lines)


def parse_duration_to_seconds(d_str: str) -> float:
    parts = d_str.strip().split(":")
    if len(parts) == 3:
        try:
            h = float(parts[0])
            m = float(parts[1])
            s = float(parts[2])
            return h * 3600 + m * 60 + s
        except ValueError:
            return 0.0
    return 0.0


def format_seconds_to_duration(sec: float) -> str:
    total_int = int(round(sec))
    h = total_int // 3600
    m = (total_int % 3600) // 60
    s = total_int % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def query_activitywatch_breakdown(
    aw_cfg: Dict[str, Any],
    target_date: str,
    tz_name: str = "Asia/Shanghai",
) -> str:
    """
    Query ActivityWatch screen time data if enabled.
    """
    if not aw_cfg.get("enabled", False):
        return ""

    client_bin = aw_cfg.get("client_bin", "aw-client")
    queries_dir = Path(aw_cfg.get("queries_dir", ""))
    shell_command = (aw_cfg.get("shell_command") or "").strip()
    bg_apps = set(x.strip().lower() for x in aw_cfg.get("background_apps", []))

    app_query_file = queries_dir / "app-breakdown.txt"
    title_query_file = queries_dir / "title-breakdown.txt"

    def run_query(query_file: Path) -> str:
        if not query_file.is_file():
            return ""
        base_cmd = [
            client_bin, "query", str(query_file),
            "--start", f"{target_date}T00:00:00",
            "--stop", f"{target_date}T23:59:59",
            "--timezone", tz_name,
        ]
        try:
            if shell_command:
                import shlex
                if "{cmd}" in shell_command:
                    pattern = r"^(\S+)\s+-c\s+['\"]?\{cmd\}['\"]?$"
                    m = re.match(pattern, shell_command)
                    if m:
                        shell_bin = m.group(1)
                        res = subprocess.run(
                            [shell_bin, "-c", " ".join(shlex.quote(x) for x in base_cmd)],
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                            timeout=15,
                        )
                    else:
                        formatted_cmd = shell_command.format(cmd=" ".join(shlex.quote(x) for x in base_cmd))
                        res = subprocess.run(
                            formatted_cmd,
                            shell=True,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                            timeout=15,
                        )
                else:
                    res = subprocess.run(
                        [shell_command, "-c", " ".join(shlex.quote(x) for x in base_cmd)],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        timeout=15,
                    )
            else:
                res = subprocess.run(
                    base_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=15,
                )
            if res.returncode == 0:
                return res.stdout
        except Exception:
            pass
        return ""

    app_raw = run_query(app_query_file)
    title_raw = run_query(title_query_file)

    # Parse app breakdown
    apps: List[Tuple[str, float, str]] = []
    for line in app_raw.splitlines():
        if line.strip().startswith("- Duration:"):
            m = re.search(r"- Duration:\s+([0-9:]+)\s+Data:\s+(\{.*\})", line)
            if m:
                dur_str = m.group(1)
                try:
                    data_dict = ast.literal_eval(m.group(2))
                    app_name = data_dict.get("app", "未知应用")
                except Exception:
                    app_name = "未知应用"
                dur_sec = parse_duration_to_seconds(dur_str)
                apps.append((dur_str, dur_sec, app_name))

    active_apps: List[Tuple[str, float, str]] = []
    bg_detected: List[Tuple[str, float, str]] = []

    for dur_str, dur_sec, app_name in apps:
        if app_name.strip().lower() in bg_apps:
            bg_detected.append((dur_str, dur_sec, app_name))
        else:
            active_apps.append((dur_str, dur_sec, app_name))

    active_total_sec = sum(x[1] for x in active_apps)
    active_total_str = format_seconds_to_duration(active_total_sec)

    # Parse title breakdown
    titles: List[Tuple[str, str]] = []
    for line in title_raw.splitlines():
        if line.strip().startswith("- Duration:"):
            m = re.search(r"- Duration:\s+([0-9:]+)\s+Data:\s+(\{.*\})", line)
            if m:
                dur_str = m.group(1)
                try:
                    data_dict = ast.literal_eval(m.group(2))
                    app_name = data_dict.get("app", "")
                    title_name = data_dict.get("title", "").strip()
                except Exception:
                    app_name = ""
                    title_name = ""
                if title_name:
                    disp = f"{app_name} | {title_name}" if app_name else title_name
                    titles.append((dur_str, disp))

    lines = ["## ⏱️ 屏幕时间"]
    if active_total_sec <= 0 and not active_apps and not bg_detected:
        lines.append("（今日无活动记录或处于锁屏空窗）")
        return "\n".join(lines)

    lines.append(f"- 活跃总时长: {active_total_str}")
    if active_apps:
        lines.append("\n### 应用使用占比 (Top 10)")
        active_apps.sort(key=lambda x: x[1], reverse=True)
        for dur_str, dur_sec, app_name in active_apps[:10]:
            pct = (dur_sec / active_total_sec * 100) if active_total_sec > 0 else 0
            lines.append(f"- {app_name}: {dur_str} ({pct:.1f}%)")

    if titles:
        lines.append("\n### 窗口标题 (Top 10)")
        for dur_str, disp in titles[:10]:
            lines.append(f"- [{dur_str}] {disp}")

    return "\n".join(lines)


def build_digest_ai_insight(
    llm: Optional[LLMRunner],
    target_date: str,
    completed_section: str,
    pending_section: str,
    aw_section: str,
) -> str:
    """
    Generate integrated AI insight paragraph linking completed tasks and screen time allocation.
    Gracefully degrades on timeout or failure.
    """
    if llm is None:
        return ""

    prompt = f"""你是一个客观、敏锐、富于洞察力的个人数字化效能管家。
请将用户今天的【今日已完成任务】与【ActivityWatch 屏幕时间分配】两部分数据深度结合起来，进行融为一体的晚间效能与精力复盘（约 150-200 字）。

核心要求：
1. 【深度交叉印证，严禁机械割裂】：
   - 绝不要把“完成了什么”和“屏幕时间几小时”拆成两半分别叙述！
   - 必须分析【时间投入】与【实际产出】的映射关系（时间投产比）：观察屏幕上的核心应用时长（如终端、开发环境、浏览器、文档等）是否真实印证并支撑了今日完成的硬核成果。
2. 【洞察专注度与节奏】：
   - 若高耗时集中于主力生产力工具且拿下关键交付，给予精准的专注度肯定；
   - 若发现时间投入与产出存在偏差（例如长时间处于配置/排查泥潭，或出现零碎应用切换），敏锐客观地指出真实的精力消耗点。
3. 【文风要求】：
   - 亲切诚恳、犀利敏锐，严禁官话、空话与说教。
   - 专注于今天真实的“付出与斩获”，不唠叨未竟事项。

今日汇总数据：
日期：{target_date}

{completed_section}

{aw_section}
"""
    try:
        raw = llm.run(prompt)
        text = raw.strip()
        return f"## 💡 AI 点评\n\n{text}"
    except LLMTimeoutError as e:
        return "## 💡 AI 点评\n\n⚠️ AI 点评生成失败 (退出状态码: 124)"
    except LLMError as e:
        return f"## 💡 AI 点评\n\n⚠️ AI 点评生成失败 (退出状态码: {e.exit_code})"
    except Exception as e:
        return f"## 💡 AI 点评\n\n⚠️ AI 点评生成失败: {e}"


def send_himalaya_email(
    notifier_cfg: Dict[str, Any],
    subject: str,
    body: str,
) -> None:
    """
    Send digest email via Himalaya CLI using RFC 5322 MIME format.
    Generates EML with email.policy.SMTP (CRLF line endings and Base64 wrapping).
    """
    import shutil
    from email.message import EmailMessage
    from email.policy import SMTP

    h_nested = notifier_cfg.get("himalaya", {}) if isinstance(notifier_cfg.get("himalaya"), dict) else {}
    mail_from = (
        notifier_cfg.get("from")
        or notifier_cfg.get("mail_from")
        or h_nested.get("from")
        or h_nested.get("mail_from")
        or ""
    )
    mail_to = (
        notifier_cfg.get("to")
        or notifier_cfg.get("mail_to")
        or h_nested.get("to")
        or h_nested.get("mail_to")
        or ""
    )
    account = notifier_cfg.get("account") or h_nested.get("account") or "default"
    bin_path = notifier_cfg.get("bin") or h_nested.get("bin") or "himalaya"

    if not mail_from or not mail_to:
        raise ValueError("Both 'from' and 'to' must be configured for notifier.himalaya.")

    if not shutil.which(bin_path) and not os.path.exists(bin_path):
        raise FileNotFoundError(f"Himalaya binary '{bin_path}' not found.")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = mail_from
    msg["To"] = mail_to
    msg.set_content(body, subtype="plain", charset="utf-8")

    with tempfile.NamedTemporaryFile("wb", suffix=".eml", delete=False) as tf:
        tf.write(msg.as_bytes(policy=SMTP))
        eml_path = tf.name

    try:
        # himalaya v2 position argument syntax: himalaya message send --account <account> -- <file.eml>
        cmd = [bin_path, "message", "send", "--account", account, "--", eml_path]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=30)
        if res.returncode != 0:
            err_msg = res.stderr.strip() or res.stdout.strip()
            raise RuntimeError(f"Himalaya send failed (exit {res.returncode}): {err_msg}")
    finally:
        if os.path.exists(eml_path):
            try:
                os.remove(eml_path)
            except OSError:
                pass


def run_digest(
    dry_run: bool = False,
    target_date: Optional[str] = None,
    config_path: Optional[str] = None,
    env_path: Optional[str] = None,
    client: Optional[VikunjaClient] = None,
    llm: Optional[LLMRunner] = None,
) -> int:
    """
    Execute evening digest generation cycle.
    """
    cfg = load_config(config_path=config_path, env_path=env_path)
    tz_name = cfg.get("timezone", "Asia/Shanghai")

    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")

    if not target_date:
        target_date = datetime.datetime.now(tz).strftime("%Y-%m-%d")

    try:
        dt_obj = datetime.datetime.strptime(target_date, "%Y-%m-%d")
        weekday_str = get_weekday_str(dt_obj)
    except Exception:
        weekday_str = ""

    report_title = f"🌙 晚间总结 · {target_date}({weekday_str})" if weekday_str else f"🌙 晚间总结 · {target_date}"

    # Prepare Vikunja client
    if client is None:
        v_cfg = cfg.get("vikunja", {})
        client = VikunjaClient(
            base_url=v_cfg.get("url"),
            token=v_cfg.get("token"),
            timeout=v_cfg.get("timeout_seconds", 30),
        )

    # 1. Fetch Vikunja tasks
    all_tasks: List[Dict[str, Any]] = []
    done_tasks: List[Dict[str, Any]] = []
    try:
        all_tasks = client.get_tasks()
        done_tasks = client.get_done_tasks()
    except VikunjaAPIError as e:
        print(f"[WARN] Failed to fetch tasks from Vikunja: {e}", file=sys.stderr)

    # 2. Format Completed Tasks
    completed_section, completed_count = format_completed_tasks(
        done_tasks=done_tasks,
        target_date_str=target_date,
        tz_name=tz_name,
    )

    # 3. Format Pending Tasks
    gtd_cfg = cfg.get("gtd", {})
    allowed_projects = gtd_cfg.get("allowed_target_projects", {
        1: "收件箱 (Inbox)",
        5: "单次执行清单",
        6: "项目执行清单",
        7: "等待回执清单",
    })
    # Ensure Inbox is also included in allowed for display
    inbox_pid = gtd_cfg.get("inbox_project_id", 1)
    if inbox_pid not in allowed_projects:
        allowed_projects[inbox_pid] = "收件箱 (Inbox)"

    project_order = gtd_cfg.get("digest_project_order", [inbox_pid, 5, 6, 7])

    pending_section = format_pending_tasks(
        tasks=all_tasks,
        allowed_projects=allowed_projects,
        project_order=project_order,
    )

    # 4. ActivityWatch Screen Time
    aw_cfg = cfg.get("activitywatch", {})
    aw_section = query_activitywatch_breakdown(aw_cfg, target_date=target_date, tz_name=tz_name)

    # 5. LLM AI Insight
    if llm is None and cfg.get("llm", {}).get("command"):
        llm_cfg = cfg.get("llm", {})
        llm = LLMRunner(
            command_template=llm_cfg.get("command", ""),
            timeout_seconds=llm_cfg.get("timeout_seconds", 240),
        )

    ai_section = build_digest_ai_insight(
        llm=llm,
        target_date=target_date,
        completed_section=completed_section,
        pending_section=pending_section,
        aw_section=aw_section,
    )

    # Assemble complete report
    sections = [f"# {report_title}", completed_section, pending_section]
    if aw_section:
        sections.append(aw_section)
    if ai_section:
        sections.append(ai_section)

    full_report = "\n\n".join(sections).strip() + "\n"

    # Backup report to file if configured
    backup_cfg = cfg.get("backup", {})
    backup_path = os.environ.get("BACKUP_FILE") or backup_cfg.get("file_path")
    if backup_path:
        try:
            b_p = Path(backup_path)
            b_p.parent.mkdir(parents=True, exist_ok=True)
            with open(b_p, "w", encoding="utf-8") as f:
                f.write(full_report)
        except Exception as e:
            print(f"[WARN] Failed to write backup report to {backup_path}: {e}", file=sys.stderr)

    # Output or Deliver
    if dry_run or cfg.get("notifier", {}).get("type") == "none":
        print(full_report)
        return 0

    notifier_cfg = cfg.get("notifier", {})
    n_type = notifier_cfg.get("type")
    if n_type == "himalaya":
        h_nested = notifier_cfg.get("himalaya", {}) if isinstance(notifier_cfg.get("himalaya"), dict) else {}
        mail_to = (
            notifier_cfg.get("to")
            or notifier_cfg.get("mail_to")
            or h_nested.get("to")
            or h_nested.get("mail_to")
            or ""
        )
        account = notifier_cfg.get("account") or h_nested.get("account") or "default"
        try:
            send_himalaya_email(notifier_cfg, subject=report_title, body=full_report)
            print(f"[OK] Daily digest successfully sent via Himalaya to {mail_to} (account: {account})")
            return 0
        except Exception as e:
            print(f"[FAIL] Notification delivery failed: {e}", file=sys.stderr)
            if backup_path:
                print(f"[INFO] Report is safely preserved at backup file: {backup_path}", file=sys.stderr)
            return 1
    else:
        print(full_report)

    return 0


def main():
    parser = argparse.ArgumentParser(description="Daily Evening GTD Summary Digest for Vikunja")
    parser.add_argument("-n", "--dry-run", action="store_true", help="Dry-run simulation mode without sending notifications")
    parser.add_argument("-d", "--date", type=str, help="Target date in YYYY-MM-DD format (defaults to today)")
    parser.add_argument("-c", "--config", type=str, help="Path to configuration file")
    parser.add_argument("-e", "--env-file", type=str, help="Path to .env credentials file")
    args = parser.parse_args()

    exit_code = run_digest(
        dry_run=args.dry_run,
        target_date=args.date,
        config_path=args.config,
        env_path=args.env_file,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
