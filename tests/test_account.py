import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telethon import errors
from ken_mcp.account import login, make_client, request_login_code
from ken_mcp.login_errors import login_failure
from ken_mcp.store import UserError


class LoginDiagnosticsTests(unittest.TestCase):
    def test_telegram_failures_have_actionable_safe_codes(self):
        for error, expected in (
            (errors.ApiIdInvalidError(None), "API ID/hash"),
            (errors.PhoneNumberInvalidError(None), "international"),
            (errors.PhoneNumberBannedError(None), "banned"),
            (errors.PhoneCodeInvalidError(None), "newest code"),
            (errors.PhoneCodeExpiredError(None), "expired"),
            (errors.PasswordHashInvalidError(None), "two-step password"),
            (errors.FloodWaitError(None, capture=42), "42 seconds"),
            (errors.ServerError(None, "PRIVATE PHONE HASH CODE SESSION PASSWORD", code=500), "RPC 500"),
        ):
            with self.subTest(error=type(error).__name__):
                message = str(login_failure("requesting the login code", error))
                self.assertIn(type(error).__name__, message)
                self.assertIn("requesting the login code", message)
                self.assertIn(expected, message)
                self.assertNotIn("PRIVATE", message)

    def test_dependencies_runtime_and_network_errors_do_not_echo_raw_text(self):
        for error, expected in (
            (ModuleNotFoundError("PRIVATE dependency path"), "pip install"),
            (RuntimeError("PRIVATE event loop data"), "Python runtime"),
            (TimeoutError("PRIVATE connection"), "timed out"),
            (ConnectionRefusedError(10061, "PRIVATE endpoint"), "errno 10061"),
            (PermissionError(13, "PRIVATE profile path"), "denied access"),
            (ValueError("PRIVATE API data"), "ValueError"),
        ):
            with self.subTest(error=type(error).__name__):
                message = str(login_failure("connecting to Telegram", error))
                self.assertIn(expected, message)
                self.assertNotIn("PRIVATE", message)

    def test_unknown_exception_class_and_request_are_not_exposed(self):
        error_type = type("PRIVATE_SECRET", (Exception,), {})
        message = str(login_failure("creating the Telegram client", error_type("PRIVATE")))
        self.assertIn("UnexpectedError", message)
        self.assertNotIn("PRIVATE", message)


class LoginFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.output = io.StringIO()
        self.stack.enter_context(contextlib.redirect_stdout(self.output))
        self.stack.enter_context(contextlib.redirect_stderr(self.output))
        self.stack.enter_context(patch("ken_mcp.account.sys.stdin.isatty", return_value=True))
        self.stack.enter_context(patch("ken_mcp.account.secure_directory"))
        self.saved = self.stack.enter_context(patch("ken_mcp.account.save_credentials"))
        self.client = SimpleNamespace(
            connect=AsyncMock(), send_code_request=AsyncMock(), sign_in=AsyncMock(),
            get_me=AsyncMock(return_value=SimpleNamespace(bot=False, first_name="Synthetic", last_name="Owner", id=9000)),
            session=SimpleNamespace(save=Mock(return_value="PRIVATE_SESSION")),
            disconnect=AsyncMock(), log_out=AsyncMock(),
        )
        self.factory = self.stack.enter_context(patch("ken_mcp.account.make_client", return_value=self.client))
        self.inputs = self.stack.enter_context(patch("builtins.input", side_effect=["12345", "a" * 32, "+15550001111", "12345"]))

    async def test_preapproved_login_needs_only_credentials_and_code(self):
        await login(self.directory, save_session=True)
        self.assertEqual(self.inputs.call_count, 4)
        self.saved.assert_called_once()
        self.assertEqual(self.saved.call_args.args[1]["account_id"], "9000")
        self.assertNotIn("AUTHORIZE", self.output.getvalue())
        self.assertNotIn("PRIVATE_SESSION", self.output.getvalue())
        self.client.log_out.assert_not_awaited()

    async def test_two_factor_password_is_visible_local_input(self):
        self.client.sign_in.side_effect = [errors.SessionPasswordNeededError(None), None]
        self.inputs.side_effect = ["12345", "a" * 32, "+15550001111", "12345", "PRIVATE_PASSWORD"]
        await login(self.directory, save_session=True)
        self.assertEqual(self.inputs.call_count, 5)
        self.client.sign_in.assert_awaited_with(password="PRIVATE_PASSWORD")
        self.assertNotIn("PRIVATE_PASSWORD", self.output.getvalue())

    async def test_default_login_can_decline_persistence(self):
        self.inputs.side_effect = ["12345", "a" * 32, "+15550001111", "12345", "no"]
        with self.assertRaisesRegex(UserError, "declined"):
            await login(self.directory)
        self.saved.assert_not_called()
        self.client.log_out.assert_awaited_once()

    async def test_client_creation_error_identifies_stage(self):
        self.factory.side_effect = ModuleNotFoundError("PRIVATE_HASH")
        with self.assertRaisesRegex(UserError, "creating the Telegram client.*ModuleNotFoundError") as caught:
            await login(self.directory, save_session=True)
        self.assertNotIn("PRIVATE_HASH", str(caught.exception))
        self.saved.assert_not_called()

    async def test_connect_error_identifies_stage_without_requesting_code(self):
        self.client.connect.side_effect = RuntimeError("PRIVATE event loop data")
        with self.assertRaisesRegex(UserError, "connecting to Telegram.*RuntimeError"):
            await login(self.directory, save_session=True)
        self.client.send_code_request.assert_not_awaited()
        self.client.disconnect.assert_awaited_once()

    async def test_code_request_failure_is_not_retried(self):
        for error in (errors.ApiIdInvalidError(None), errors.FloodWaitError(None, capture=42), TimeoutError()):
            with self.subTest(error=type(error).__name__):
                self.client.send_code_request.reset_mock()
                self.client.send_code_request.side_effect = error
                self.inputs.side_effect = ["12345", "a" * 32, "+15550001111"]
                with self.assertRaisesRegex(UserError, "requesting the login code"):
                    await login(self.directory, save_session=True)
                self.client.send_code_request.assert_awaited_once()
                self.saved.assert_not_called()

    async def test_one_explicit_data_center_redirect_is_followed(self):
        self.client.send_code_request.side_effect = [errors.PhoneMigrateError(None, capture=4), "sent"]
        self.assertEqual(await request_login_code(self.client, "+15550001111"), "sent")
        self.assertEqual(self.client.send_code_request.await_count, 2)

    async def test_repeated_redirect_stops(self):
        self.client.send_code_request.side_effect = errors.PhoneMigrateError(None, capture=4)
        with self.assertRaises(errors.PhoneMigrateError):
            await request_login_code(self.client, "+15550001111")
        self.assertEqual(self.client.send_code_request.await_count, 2)

    async def test_cleanup_error_does_not_hide_original_failure(self):
        self.client.send_code_request.side_effect = errors.ApiIdInvalidError(None)
        self.client.disconnect.side_effect = RuntimeError("PRIVATE cleanup details")
        with self.assertRaisesRegex(UserError, "requesting the login code.*ApiIdInvalidError"):
            await login(self.directory, save_session=True)
        self.assertIn("closing the Telegram connection", self.output.getvalue())
        self.assertNotIn("PRIVATE", self.output.getvalue())

    async def test_invalid_api_id_never_reaches_transport(self):
        self.inputs.side_effect = ["9999999999999999999999", "a" * 32, "+15550001111"]
        with self.assertRaisesRegex(UserError, "Invalid API ID"):
            await login(self.directory, save_session=True)
        self.factory.assert_not_called()

    async def test_real_telethon_zero_retry_redirect_path(self):
        # Exercise the installed client's actual request handling, without networking.
        from telethon import functions, types
        client = make_client({"api_id": 12345, "api_hash": "0" * 32})
        client.is_user_authorized = AsyncMock(return_value=False)
        client._switch_dc = AsyncMock()
        async def redirected(*args, **kwargs):
            raise errors.PhoneMigrateError(None, capture=4)
        client._sender.send = Mock(side_effect=redirected)
        with self.assertRaises(errors.PhoneMigrateError):
            await client(functions.auth.SendCodeRequest("+15550001111", 12345, "0" * 32, types.CodeSettings()))
        client._switch_dc.assert_awaited_once_with(4)
        self.assertEqual(client._sender.send.call_count, 1)


if __name__ == "__main__":
    unittest.main()
