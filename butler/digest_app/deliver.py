"""
Delivery mechanisms for evening digest (Himalaya CLI MIME email and backup persistence).
"""

import os
import sys
import shutil
import tempfile
import subprocess
from pathlib import Path
from email.message import EmailMessage
from email.policy import SMTP
from typing import Any, Dict, Optional


def send_himalaya_email(
    notifier_cfg: Dict[str, Any],
    subject: str,
    body: str,
) -> None:
    """
    Send digest email via Himalaya CLI using RFC 5322 MIME format.
    Generates EML with email.policy.SMTP (CRLF line endings and Base64 wrapping).
    """
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


def save_backup_report(full_report: str, backup_path: Optional[str] = None) -> Optional[str]:
    """
    Safely backup the generated digest report to local file with 0600 permissions.
    If backup_path is not specified, creates a secure temporary file via tempfile.mkstemp.
    Returns the resolved path of the backup file.
    """
    try:
        if backup_path and str(backup_path).strip():
            b_p = Path(backup_path).expanduser().resolve()
            b_p.parent.mkdir(parents=True, exist_ok=True)
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            fd = os.open(str(b_p), flags, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(full_report)
            os.chmod(str(b_p), 0o600)
            return str(b_p)
        else:
            fd, tmp_path = tempfile.mkstemp(prefix="vikunja-ai-butler-digest-", suffix=".txt")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(full_report)
            os.chmod(tmp_path, 0o600)
            return tmp_path
    except Exception as e:
        print(f"[WARN] Failed to write backup report to {backup_path}: {e}", file=sys.stderr)
        return None
