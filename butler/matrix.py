"""
Matrix notification delivery and email fallback transport for Vikunja AI Butler.

Safety & Security Principles:
1. Zero hardcoded tokens: Access tokens are exclusively loaded via config or environment
   (MATRIX_HOMESERVER_TOKEN / MATRIX_TOKEN).
2. Leak prevention: Under NO circumstances will sensitive tokens be written to stdout, stderr,
   or included in exception tracebacks.
3. Decoupled dual-channel fallback: If Matrix delivery fails or is unreachable, the system
   automatically falls back to Himalaya MIME email delivery. If both fail, an error is raised
   with zero sensitive credential leakage.
"""

import os
import sys
import json
import urllib.request
import urllib.error
import urllib.parse
from typing import Any, Dict, Optional


class MatrixDeliveryError(Exception):
    """Raised when Matrix notification delivery fails."""
    pass


def send_matrix_message(
    homeserver_url: str,
    room_id: str,
    token: str,
    body: str,
    timeout: int = 30,
) -> Dict[str, Any]:
    """
    Send an m.room.message text event to a Matrix room.
    Endpoint: POST /_matrix/client/v3/rooms/{roomId}/send/m.room.message
    """
    if not token or not str(token).strip():
        raise MatrixDeliveryError("Matrix access token is not configured.")
    if not room_id or not str(room_id).strip():
        raise MatrixDeliveryError("Matrix room_id is not configured.")

    base_url = (homeserver_url or "https://matrix.org").rstrip("/")
    # Build endpoint compatible with standard Matrix Client-Server API
    encoded_room_id = urllib.parse.quote(room_id.strip())
    endpoint = f"{base_url}/_matrix/client/v3/rooms/{encoded_room_id}/send/m.room.message"

    payload = {
        "msgtype": "m.text",
        "body": body,
    }
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Authorization": f"Bearer {token.strip()}",
        "User-Agent": "Vikunja-AI-Butler/1.0",
    }

    req = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw_body = resp.read().decode("utf-8")
            if not raw_body.strip():
                return {}
            try:
                return json.loads(raw_body)
            except json.JSONDecodeError:
                return {"raw": raw_body}
    except urllib.error.HTTPError as e:
        status_code = e.code
        # Mask out any potential token from response or url
        safe_msg = f"Matrix delivery HTTP error {status_code}"
        raise MatrixDeliveryError(safe_msg) from None
    except urllib.error.URLError as e:
        safe_reason = str(e.reason)
        # Ensure token is never in reason
        if token in safe_reason:
            safe_reason = safe_reason.replace(token, "[REDACTED]")
        raise MatrixDeliveryError(f"Matrix network error: {safe_reason}") from None
    except Exception as e:
        safe_err = str(e)
        if token in safe_err:
            safe_err = safe_err.replace(token, "[REDACTED]")
        raise MatrixDeliveryError(f"Unexpected Matrix delivery failure: {safe_err}") from None


def deliver_morning_report(report_text: str, config: Dict[str, Any]) -> None:
    """
    Deliver morning report via Matrix with automatic fallback to Himalaya email.
    If both fail, raises an Exception with zero credential leakage.
    """
    morning_cfg = config.get("morning", {})
    matrix_cfg = morning_cfg.get("matrix", {}) if isinstance(morning_cfg.get("matrix"), dict) else {}

    homeserver_url = matrix_cfg.get("homeserver_url") or os.environ.get("MATRIX_HOMESERVER_URL") or "https://matrix.org"
    room_id = matrix_cfg.get("room_id") or os.environ.get("MATRIX_ROOM_ID") or ""
    token = matrix_cfg.get("token") or ""
    # Ductor (agy-bot) credentials file: primary source after the agy-bot
    # takeover of the 今日待办 delivery. ductor's rotate_token.py keeps
    # access_token fresh there via MAS OAuth2 auto-renewal (~4h expiry), so a
    # runtime read is always near-current. Takes precedence over the Hermes
    # .env's MATRIX_ACCESS_TOKEN (ra1nwalker), which has left the target room.
    if not token:
        creds_file = matrix_cfg.get("credentials_file") or "~/.local/share/ductor/matrix_store/credentials.json"
        try:
            with open(os.path.expanduser(creds_file), "r", encoding="utf-8") as f:
                token = json.load(f).get("access_token") or ""
        except (OSError, json.JSONDecodeError, AttributeError):
            token = ""
    if not token:
        token = (
            os.environ.get("MATRIX_HOMESERVER_TOKEN")
            or os.environ.get("MATRIX_TOKEN")
            or os.environ.get("MATRIX_ACCESS_TOKEN")
            or ""
        )

    matrix_succeeded = False
    matrix_error: Optional[Exception] = None

    if token and room_id:
        try:
            send_matrix_message(
                homeserver_url=homeserver_url,
                room_id=room_id,
                token=token,
                body=report_text,
            )
            matrix_succeeded = True
        except Exception as e:
            matrix_error = e
            print(f"[WARN] Matrix notification delivery failed: {e}. Falling back to email...", file=sys.stderr)
    else:
        matrix_error = MatrixDeliveryError("Matrix token or room_id is missing.")

    if matrix_succeeded:
        return

    # Fallback to Himalaya email delivery
    notifier_cfg = config.get("notifier", {})
    try:
        from butler.digest import send_himalaya_email
        # Derive subject from first line of report_text or default
        first_line = report_text.strip().splitlines()[0] if report_text.strip() else "📬 晨间消息与待办"
        clean_subject = first_line.replace("#", "").strip()
        send_himalaya_email(notifier_cfg, subject=clean_subject, body=report_text)
        print("[OK] Morning report delivered via email fallback.")
    except Exception as email_err:
        # Both channels failed: raise exception without token
        token_to_redact = token or ""
        safe_matrix_msg = str(matrix_error or "Unknown error")
        safe_email_msg = str(email_err)
        if token_to_redact and token_to_redact in safe_matrix_msg:
            safe_matrix_msg = safe_matrix_msg.replace(token_to_redact, "[REDACTED]")
        if token_to_redact and token_to_redact in safe_email_msg:
            safe_email_msg = safe_email_msg.replace(token_to_redact, "[REDACTED]")

        raise RuntimeError(
            f"Both Matrix and Email delivery failed. Matrix: {safe_matrix_msg} | Email: {safe_email_msg}"
        ) from None
