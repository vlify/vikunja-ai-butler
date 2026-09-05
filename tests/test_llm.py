"""
Unit tests for LLM Provider abstraction and security fixes.

Coverage:
1. Base Protocol and Exceptions (LLMError, LLMTimeoutError, LLMExecutionError)
2. CLIProvider & Legacy migration:
   - shell=False execution with argv
   - prompt delivered via stdin
   - {prompt} removal and one-time stderr migration warning
   - Command injection PoC neutralization
3. OpenAICompatProvider:
   - HTTP POST {base_url}/chat/completions with standard urllib
   - Bearer token authentication & secret masking in errors
   - Timeout and HTTP status code handling
4. Factory create_llm_provider:
   - provider: 'openai-compat'
   - provider: 'cli'
   - legacy config with command and {prompt} auto-migration
5. Security fix: Matrix fail-closed when room_id or homeserver_url is empty
6. Security fix: Backup report writes with 0600 permissions via tempfile.mkstemp
"""

import os
import sys
import stat
import json
import socket
import unittest
import tempfile
import urllib.error
from unittest.mock import patch, MagicMock

# Import targets (will fail until implemented)
from butler.llm import (
    LLMError,
    LLMTimeoutError,
    LLMExecutionError,
    LLMProvider,
    CLIProvider,
    OpenAICompatProvider,
    LLMRunner,
    create_llm_provider,
)
from butler.matrix import send_matrix_message, deliver_morning_report, MatrixDeliveryError
from butler.digest_app.deliver import save_backup_report


class TestLLMBaseAndCLI(unittest.TestCase):
    """Test LLMProvider protocol and CLIProvider execution."""

    def test_cli_provider_stdin_execution(self):
        """Verify CLIProvider feeds prompt exclusively via stdin without shell."""
        # Using 'cat' as a deterministic stdin echoer
        provider = CLIProvider(command=["cat"])
        res = provider.run("Hello from stdin!")
        self.assertEqual(res, "Hello from stdin!")

    def test_cli_provider_legacy_cat_template_and_pwned_payload(self):
        """
        Verification A:
        Template 'cat "{prompt}"' + payload '$PWNED' -> output = literal payload ($ is not evaluated).
        Using a temporary file named '$PWNED' containing '$PWNED' to demonstrate cat executes
        with pure literal argv without shell variable expansion.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            payload = "$PWNED"
            with open(os.path.join(tmpdir, payload), "w", encoding="utf-8") as f:
                f.write(payload)

            old_cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                import butler.llm.cli
                butler.llm.cli._legacy_warning_emitted = False
                with patch("sys.stderr.write") as mock_stderr:
                    provider = CLIProvider(command='cat "{prompt}"')
                    self.assertEqual(provider.argv, ["cat", "{prompt}"])
                    res = provider.run(payload)
                    # Output is exact literal payload ($PWNED) without shell variable interpolation
                    self.assertEqual(res, "$PWNED")
                    mock_stderr.assert_called()
                    warn_calls = "".join(call[0][0] for call in mock_stderr.call_args_list)
                    self.assertIn("Legacy '{prompt}' template detected", warn_calls)
            finally:
                os.chdir(old_cwd)

    def test_cli_provider_production_agy_template(self):
        """
        Verification B:
        Production template 'agy-run.sh "{prompt}"' -> argv[0] == 'agy-run.sh' and prompt fully passed into argv.
        """
        provider = CLIProvider(command='agy-run.sh "{prompt}"')
        self.assertEqual(provider.argv[0], "agy-run.sh")
        self.assertEqual(provider.argv, ["agy-run.sh", "{prompt}"])

        test_prompt = "Classify this GTD inbox task: Buy coffee"
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            provider.run(test_prompt)
            mock_run.assert_called_once()
            called_argv = mock_run.call_args[0][0]
            self.assertEqual(called_argv[0], "agy-run.sh")
            self.assertEqual(called_argv[1], test_prompt)
            self.assertEqual(called_argv, ["agy-run.sh", test_prompt])
            self.assertFalse(mock_run.call_args[1].get("shell", True))

    def test_cli_provider_composite_element_replacement(self):
        """Verify composite elements like --model={prompt} are replaced within the token."""
        provider = CLIProvider(command='llm-tool --model={prompt} -v')
        self.assertEqual(provider.argv[0], "llm-tool")
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            provider.run("glm")
            called_argv = mock_run.call_args[0][0]
            self.assertEqual(called_argv, ["llm-tool", "--model=glm", "-v"])
            self.assertFalse(mock_run.call_args[1].get("shell", True))


    def test_cli_provider_command_injection_poc_neutralized(self):
        """
        SECURITY VULNERABILITY PoC VERIFICATION:
        In the old implementation (shell=True + {prompt} string replace):
        A prompt like '$(touch /tmp/injection_marker)' would be evaluated by the subshell.
        In the new CLIProvider (shell=False + stdin):
        The prompt is pure data piped to stdin; shell metacharacters are never evaluated!
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            marker_file = os.path.join(tmpdir, "pwned.txt")
            # Malicious prompt containing command injection payload
            injection_prompt = f"$(touch {marker_file}) ; touch {marker_file} ; `touch {marker_file}`"

            # Execute via CLIProvider (which wraps 'cat')
            provider = CLIProvider(command=["cat"])
            out = provider.run(injection_prompt)

            # 1. Output must be the literal payload (unmodified data stream)
            self.assertEqual(out, injection_prompt)
            # 2. Marker file MUST NOT exist!
            self.assertFalse(
                os.path.exists(marker_file),
                "VULNERABILITY DETECTED: Shell command in prompt was executed!"
            )

    def test_cli_provider_timeout(self):
        """Verify CLIProvider raises LLMTimeoutError when process times out."""
        provider = CLIProvider(command=["sleep", "2"], timeout_seconds=1)
        with self.assertRaises(LLMTimeoutError):
            provider.run("test")

    def test_cli_provider_non_zero_exit(self):
        """Verify CLIProvider raises LLMExecutionError on non-zero exit code."""
        provider = CLIProvider(command=["false"])
        with self.assertRaises(LLMExecutionError) as cm:
            provider.run("test")
        self.assertEqual(cm.exception.exit_code, 1)


class TestOpenAICompatProvider(unittest.TestCase):
    """Test OpenAICompatProvider HTTP client."""

    def test_openai_compat_success(self):
        """Verify valid OpenAI /chat/completions payload and response parsing."""
        mock_response_data = {
            "id": "chatcmpl-test",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "Refined task title"
                    },
                    "finish_reason": "stop"
                }
            ]
        }

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.getcode.return_value = 200
            mock_resp.read.return_value = json.dumps(mock_response_data).encode("utf-8")
            mock_resp.__enter__.return_value = mock_resp
            mock_urlopen.return_value = mock_resp

            provider = OpenAICompatProvider(
                base_url="http://127.0.0.1:4000/v1",
                model="glm",
                api_key="test_secret_key",
                timeout_seconds=30,
            )

            result = provider.run("Hello LLM")
            self.assertEqual(result, "Refined task title")

            # Check request details
            req = mock_urlopen.call_args[0][0]
            self.assertEqual(req.full_url, "http://127.0.0.1:4000/v1/chat/completions")
            self.assertEqual(req.headers.get("Authorization"), "Bearer test_secret_key")
            req_body = json.loads(req.data.decode("utf-8"))
            self.assertEqual(req_body["model"], "glm")
            self.assertEqual(req_body["messages"], [{"role": "user", "content": "Hello LLM"}])

    def test_openai_compat_secret_redaction_on_error(self):
        """Verify API key is never leaked in exception messages."""
        secret_key = "sk-super-secret-leaked-token"
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError(
                url="http://127.0.0.1:4000/v1/chat/completions",
                code=401,
                msg="Unauthorized with key " + secret_key,
                hdrs={},
                fp=MagicMock(read=lambda: f"Invalid key {secret_key}".encode("utf-8"))
            )

            provider = OpenAICompatProvider(
                base_url="http://127.0.0.1:4000/v1",
                model="glm",
                api_key=secret_key,
            )

            with self.assertRaises(LLMExecutionError) as cm:
                provider.run("Test prompt")

            err_msg = str(cm.exception)
            self.assertNotIn(secret_key, err_msg)
            self.assertIn("[REDACTED]", err_msg)

    def test_openai_compat_timeout(self):
        """Verify network timeout raises LLMTimeoutError."""
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = socket.timeout("timed out")
            provider = OpenAICompatProvider(base_url="http://127.0.0.1:4000/v1")
            with self.assertRaises(LLMTimeoutError):
                provider.run("Prompt")


class TestLLMFactory(unittest.TestCase):
    """Test create_llm_provider factory and configuration mapping."""

    def test_create_openai_compat_default(self):
        """Default configuration should instantiate OpenAICompatProvider."""
        cfg = {
            "provider": "openai-compat",
            "base_url": "http://127.0.0.1:4000/v1",
            "model": "glm",
        }
        provider = create_llm_provider(cfg)
        self.assertIsInstance(provider, OpenAICompatProvider)
        self.assertEqual(provider.model, "glm")

    def test_create_cli_provider_explicit(self):
        """Explicit provider: cli should instantiate CLIProvider."""
        cfg = {
            "provider": "cli",
            "command": ["agy-run.sh"],
        }
        provider = create_llm_provider(cfg)
        self.assertIsInstance(provider, CLIProvider)
        self.assertEqual(provider.argv, ["agy-run.sh"])

    def test_create_legacy_auto_migration(self):
        """Old config with command containing {prompt} should auto-migrate to CLIProvider."""
        cfg = {
            "command": 'agy-run.sh "{prompt}"',
        }
        provider = create_llm_provider(cfg)
        self.assertIsInstance(provider, CLIProvider)
        self.assertEqual(provider.argv[0], "agy-run.sh")
        self.assertEqual(provider.argv, ["agy-run.sh", "{prompt}"])


class TestSecurityFixes(unittest.TestCase):
    """Test Matrix fail-closed and Backup 0600 permissions security fixes."""

    def test_matrix_empty_room_fail_closed(self):
        """Matrix delivery must fail-closed (not send) and warn when room_id or homeserver_url is empty."""
        cfg = {
            "morning": {
                "matrix": {
                    "homeserver_url": "",
                    "room_id": "",
                    "token": "valid_token_should_not_deliver",
                }
            },
            "notifier": {"type": "none"}
        }

        with patch("sys.stderr.write") as mock_stderr:
            with patch("urllib.request.urlopen") as mock_urlopen:
                # deliver_morning_report should not call urlopen for matrix
                # because room_id is empty, and fail-closed when email is also unavailable
                with self.assertRaises(RuntimeError):
                    deliver_morning_report("Morning Report", cfg)

                mock_urlopen.assert_not_called()
                stderr_text = "".join(call[0][0] for call in mock_stderr.call_args_list)
                self.assertIn("Matrix homeserver_url or room_id is not configured", stderr_text)

    def test_backup_report_tempfile_0600_permissions(self):
        """Backup report with default/unspecified path must create tempfile with 0600 permissions."""
        created_path = save_backup_report("Sample backup report content", backup_path=None)
        self.assertIsNotNone(created_path)
        try:
            self.assertTrue(os.path.exists(created_path))
            file_stat = os.stat(created_path)
            mode = stat.S_IMODE(file_stat.st_mode)
            # 0600: read/write for owner only
            self.assertEqual(oct(mode), oct(0o600))
        finally:
            if created_path and os.path.exists(created_path):
                os.remove(created_path)

    def test_backup_report_configured_path_0600_permissions(self):
        """Backup report with explicit path override must also enforce 0600 permissions."""
        with tempfile.TemporaryDirectory() as tmpdir:
            custom_path = os.path.join(tmpdir, "custom-backup.txt")
            saved_path = save_backup_report("Custom backup report", backup_path=custom_path)
            self.assertEqual(saved_path, custom_path)
            self.assertTrue(os.path.exists(custom_path))
            file_stat = os.stat(custom_path)
            mode = stat.S_IMODE(file_stat.st_mode)
            self.assertEqual(oct(mode), oct(0o600))


if __name__ == "__main__":
    unittest.main()
