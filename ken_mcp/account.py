"""Explicit local login, completely separate from the MCP tool surface."""
import asyncio
import re
import sys

from .security import load_credentials, save_credentials, secure_directory
from .login_errors import login_failure
from .store import UserError
from .transport import TelethonTransport


def make_client(values):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    return TelegramClient(StringSession(values.get("session", "")), values["api_id"], values["api_hash"],
                          request_retries=0, connection_retries=0, auto_reconnect=False,
                          flood_sleep_threshold=0, raise_last_call_error=True, receive_updates=False,
                          device_model="Ken Telegram MCP", app_version="0.1.0")


async def connect_existing(directory):
    values = load_credentials(directory)
    client = make_client(values)
    try:
        await asyncio.wait_for(client.connect(), 30)
        if not await asyncio.wait_for(client.is_user_authorized(), 30):
            raise UserError("Telegram authorization expired. Local login is required.")
        transport = TelethonTransport(client)
        identity = await transport.identity()
        if identity["account_id"] != values["account_id"]:
            raise UserError("The authorized account does not match the saved owner.")
        return transport
    except BaseException:
        await client.disconnect()
        raise


async def request_login_code(client, phone):
    from telethon import errors
    try:
        return await asyncio.wait_for(client.send_code_request(phone), 30)
    except (errors.PhoneMigrateError, errors.NetworkMigrateError, errors.UserMigrateError):
        # Telethon 1.45 switches DC then re-raises with request_retries=0.
        # This explicit rejection did not send a code. Follow one redirect only;
        # never repeat a timeout, flood wait or an uncertain/successful request.
        print("Following Telegram's login data-center redirect...")
        return await asyncio.wait_for(client.send_code_request(phone), 30)


async def login(directory, save_session=False):
    if not sys.stdin.isatty():
        raise UserError("Login requires an interactive local terminal. Never pass credentials as command arguments.")
    print("Enter your Telegram app credentials below. Input is visible. No messages will be sent.")
    stage = "loading Telegram dependencies"
    client = None
    authenticated = False
    saved = False
    try:
        from telethon import errors
        stage = "preparing the local profile"
        secure_directory(directory)
        if (directory / "credentials.dpapi").exists():
            raise UserError("This profile is already configured. Use a new profile or revoke the old session in Telegram before replacing it.")
        stage = "reading local input"
        api_id = input("API ID from my.telegram.org: ").strip()
        api_hash = input("API hash: ").strip()
        phone = input("Your phone number with country code: ").strip()
        if not re.fullmatch(r"[0-9]{1,10}", api_id) or not 0 < int(api_id) <= 2147483647:
            raise UserError("Invalid API ID. Enter the numeric API ID from my.telegram.org/apps. No login request made.")
        if not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash):
            raise UserError("Invalid API hash. Enter the 32-character API hash from my.telegram.org/apps. No login request made.")
        if not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
            raise UserError("Invalid phone format. Enter the international number starting with +. No login request made.")
        values = {"api_id": int(api_id), "api_hash": api_hash}
        stage = "creating the Telegram client"
        client = make_client(values)
        stage = "connecting to Telegram"
        print("Connecting to Telegram...")
        await asyncio.wait_for(client.connect(), 30)
        stage = "requesting the login code"
        print("Requesting your login code...")
        await request_login_code(client, phone)
        code = input("Telegram login code: ").strip()
        stage = "checking the login code"
        try:
            await asyncio.wait_for(client.sign_in(phone=phone, code=code), 30)
        except errors.SessionPasswordNeededError:
            stage = "checking the two-step password"
            await asyncio.wait_for(client.sign_in(password=input("Telegram two-step password: ")), 30)
        authenticated = True
        stage = "reading the account identity"
        me = await asyncio.wait_for(client.get_me(), 30)
        if me.bot:
            raise UserError("Use a personal account, not a bot.")
        print("Account: {} {} | ID {}".format(me.first_name or "", me.last_name or "", me.id))
        print("The saved session allows future local read access. Live sending stays disabled.")
        if not save_session and input("Save this account session encrypted on this PC? [y/N]: ").strip().lower() not in ("y", "yes"):
            raise UserError("Persistent authorization declined.")
        stage = "saving the encrypted session"
        values.update({"session": client.session.save(), "account_id": str(me.id)})
        save_credentials(directory, values)
        saved = True
        print("Encrypted local account session saved. No messages sent. No MCP client configuration changed.")
    except UserError:
        raise
    except Exception as error:
        raise login_failure(stage, error) from None
    finally:
        if authenticated and not saved:
            try:
                await asyncio.wait_for(client.log_out(), 15)
            except Exception:
                print("Could not revoke the temporary login. Remove 'Ken Telegram MCP' in Telegram Settings > Devices.", file=sys.stderr)
        if client is not None:
            try:
                await asyncio.wait_for(client.disconnect(), 15)
            except Exception as error:
                # Cleanup must never replace the original failure or a saved-session success.
                print(login_failure("closing the Telegram connection", error), file=sys.stderr)
