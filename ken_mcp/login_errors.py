"""Actionable login diagnostics built from fixed text, never exception messages."""
import asyncio
import builtins

from .store import UserError


_TELEGRAM_ADVICE = {
    "ApiIdInvalidError": "Telegram rejected the API ID/hash pair. Recheck both in my.telegram.org/apps.",
    "ApiIdPublishedFloodError": "Telegram restricted this API application. Use your own API application or contact Telegram support.",
    "PhoneNumberInvalidError": "Telegram rejected the phone number. Enter the full international number, starting with +.",
    "PhoneNumberBannedError": "Telegram reports this phone number is banned. Check the account in Telegram or contact Telegram support.",
    "PhoneNumberFloodError": "Telegram limited login attempts for this number. Wait before trying again.",
    "PhonePasswordFloodError": "Telegram limited password attempts. Wait before trying again.",
    "PhoneCodeInvalidError": "Telegram rejected the login code. Use the newest code delivered to your Telegram app.",
    "PhoneCodeExpiredError": "The login code expired. Start login again to request a new code.",
    "PhoneCodeEmptyError": "No login code was entered. Enter the code delivered by Telegram.",
    "PasswordHashInvalidError": "Telegram rejected the two-step password. Check your password in the local window.",
    "SessionPasswordNeededError": "This account requires its Telegram two-step password.",
    "AuthRestartError": "Telegram asked to restart login. Run the login command again.",
    "PhoneMigrateError": "Telegram requires a different data center. Report this error type so the login redirect can be checked.",
    "NetworkMigrateError": "Telegram requires a different data center. Report this error type so the login redirect can be checked.",
}


def login_failure(stage, error):
    """Expose trusted exception class and numeric codes, never str/repr(error)."""
    error_type = type(error)
    name = error_type.__name__
    trusted_type = getattr(builtins, name, None) is error_type
    telegram_error = False
    try:
        from telethon import errors
        telegram_error = isinstance(error, errors.RPCError)
        trusted_type = trusted_type or getattr(errors, name, None) is error_type
    except ImportError:
        pass
    kind = name if trusted_type else "UnexpectedError"
    codes = []
    for label, attribute in (("RPC", "code"), ("Win32", "winerror"), ("errno", "errno")):
        value = getattr(error, attribute, None)
        if type(value) is int and -2147483648 <= value <= 2147483647:
            codes.append(f"{label} {value}")

    if telegram_error and name in _TELEGRAM_ADVICE:
        advice = _TELEGRAM_ADVICE[name]
    elif telegram_error and name in ("FloodWaitError", "FloodPremiumWaitError"):
        seconds = getattr(error, "seconds", None)
        delay = f" {seconds} seconds" if type(seconds) is int and 0 <= seconds <= 315360000 else " before trying again"
        advice = f"Telegram limited login attempts. Wait{delay}. No automatic retry was made."
    elif telegram_error:
        advice = "Telegram rejected this login step. Report the stage, error type and RPC code shown here; no automatic retry was made."
    elif isinstance(error, (ImportError, ModuleNotFoundError)):
        advice = 'A login dependency could not load. Use the project .venv Python and run: python -m pip install -e ".[test]"'
    elif isinstance(error, (asyncio.TimeoutError, TimeoutError)):
        advice = "This step timed out. Check Telegram connectivity and try again when the connection is stable. No automatic retry was made."
    elif isinstance(error, ConnectionError):
        advice = "The Telegram connection failed. Check internet/VPN/firewall access to Telegram, then retry login."
    elif isinstance(error, PermissionError):
        advice = "Windows denied access. Run login in your normal local terminal and check access to the app profile."
    elif isinstance(error, OSError):
        advice = "A system or network operation failed. Check the numeric OS code above and connectivity/profile access for this step."
    elif isinstance(error, RuntimeError):
        advice = "The Python runtime could not complete this step. Restart login using the project .venv Python; report the stage and error type if it repeats."
    elif isinstance(error, EOFError):
        advice = "The input window closed. Run login again in a normal interactive terminal."
    else:
        advice = "Login could not complete this step. Report the stage and error type shown here so this specific failure can be fixed."
    details = ", ".join([kind, *codes])
    return UserError(f"Login failed while {stage} [{details}]. {advice}")
