"""
ActivityWatch screen time collection and timeline analysis for evening digest.
"""

import ast
import json
import re
import datetime
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple


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


def build_hourly_timeline_events(raw_json: str, target_date: str, tz_name: str) -> List[Tuple[int, float, str]]:
    """
    Parse aw-client --json raw events and aggregate active seconds per local hour.
    Events spanning hour boundaries are split proportionally.
    Returns list of (hour, seconds, dominant_app) sorted by hour.
    """
    try:
        data = json.loads(raw_json)
        if isinstance(data, list) and data and isinstance(data[0], list):
            events = data[0]
        elif isinstance(data, list):
            events = data
        else:
            return []
    except Exception:
        return []

    try:
        tz = ZoneInfo(tz_name)
        day = datetime.date.fromisoformat(target_date)
        day_start = datetime.datetime(day.year, day.month, day.day, tzinfo=tz)
        day_end = day_start + datetime.timedelta(days=1)
    except Exception:
        return []

    hour_secs: Dict[int, float] = {}
    hour_apps: Dict[int, Dict[str, float]] = {}
    for ev in events:
        if not isinstance(ev, dict):
            continue
        try:
            ts = datetime.datetime.fromisoformat(
                str(ev.get("timestamp", "")).replace("Z", "+00:00")
            ).astimezone(tz)
            dur = float(ev.get("duration", 0))
            app = str((ev.get("data") or {}).get("app", "未知应用"))
        except Exception:
            continue
        seg_start = max(ts, day_start)
        seg_end = min(ts + datetime.timedelta(seconds=dur), day_end)
        while seg_start < seg_end:
            hour_end = seg_start.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
            seg_stop = min(seg_end, hour_end)
            secs = (seg_stop - seg_start).total_seconds()
            h = seg_start.hour
            hour_secs[h] = hour_secs.get(h, 0.0) + secs
            hour_apps.setdefault(h, {})
            hour_apps[h][app] = hour_apps[h].get(app, 0.0) + secs
            seg_start = seg_stop

    result: List[Tuple[int, float, str]] = []
    for h in sorted(hour_secs):
        top_app = max(hour_apps[h].items(), key=lambda kv: kv[1])[0]
        result.append((h, hour_secs[h], top_app))
    return result


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

    def run_query(query_file: Path, extra_args: Optional[List[str]] = None) -> str:
        if not query_file.is_file():
            return ""
        base_cmd = [
            client_bin, "query", str(query_file),
            "--start", f"{target_date}T00:00:00",
            "--stop", f"{target_date}T23:59:59",
            "--timezone", tz_name,
        ] + list(extra_args or [])
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
    timeline_raw = run_query(queries_dir / "hourly-timeline.txt", extra_args=["--json"])

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

    # 分时段小时级时间线(供 AI 点评还原全天节奏)
    hourly = build_hourly_timeline_events(timeline_raw, target_date, tz_name)
    if hourly:
        lines.append("\n### 分时段时间线 (每小时活跃时长)")
        for h, secs, top_app in hourly:
            lines.append(f"- {h:02d}时: {format_seconds_to_duration(secs)} (主要: {top_app})")

    return "\n".join(lines)
