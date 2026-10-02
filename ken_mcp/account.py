"""Explicit local login, completely separate from the MCP tool surface."""
import asyncio
import getpass
import re
import sys
import warnings

from .security import load_credentials, save_credentials, secure_directory
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


def hidden(prompt):
    # Refuse getpass's plaintext fallback when there is no secure console.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise UserError("Run login directly in a local Windows terminal with hidden input support.") from None


async def login(directory):
    from telethon import errors
    if not sys.stdin.isatty():
        raise UserError("Login requires an interactive local terminal. Never pass credentials as command arguments.")
    print("This authorizes a personal Telegram account and can save persistent access on this Windows user profile.")
    print("No messages will be sent. Never paste API secrets, login codes or passwords into an assistant chat.")
    if input("Type AUTHORIZE to request a Telegram login code: ").strip() != "AUTHORIZE":
        raise UserError("Login cancelled before contacting Telegram.")
    secure_directory(directory)
    if (directory / "credentials.dpapi").exists():
        raise UserError("This profile is already configured. Use a new profile or revoke the old session in Telegram before replacing it.")
    api_id = hidden("API ID from my.telegram.org: ").strip()
    api_hash = hidden("API hash (hidden): ").strip()
    phone = hidden("Your phone number with country code (hidden): ").strip()
    if not api_id.isdigit() or not re.fullmatch(r"[a-fA-F0-9]{32}", api_hash) or not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
        raise UserError("Invalid API ID, hash, or international phone format. No login request made.")
    values = {"api_id": int(api_id), "api_hash": api_hash}
    client = make_client(values)
    authenticated = False
    saved = False
    try:
        await asyncio.wait_for(client.connect(), 30)
        await asyncio.wait_for(client.send_code_request(phone), 30)
        code = hidden("Telegram login code (hidden): ").strip()
        try:
            await asyncio.wait_for(client.sign_in(phone=phone, code=code), 30)
        except errors.SessionPasswordNeededError:
            await asyncio.wait_for(client.sign_in(password=hidden("Telegram two-step password (hidden): ")), 30)
        authenticated = True
        me = await asyncio.wait_for(client.get_me(), 30)
        if me.bot:
            raise UserError("Use a personal account, not a bot.")
        print("Account: {} {} | ID {}".format(me.first_name or "", me.last_name or "", me.id))
        print("Saving permits future local read access; live sends still need --enable-sends and specific user authorization.")
        if input("Type SAVE to persist this account session encrypted on this PC: ").strip() != "SAVE":
            raise UserError("Persistent authorization declined.")
        values.update({"session": client.session.save(), "account_id": str(me.id)})
        save_credentials(directory, values)
        saved = True
        print("Encrypted local account session saved. No messages sent. No MCP client configuration changed.")
    finally:
        if authenticated and not saved:
            try:
                await asyncio.wait_for(client.log_out(), 15)
            except Exception:
                print("Could not revoke the temporary login. Remove 'Ken Telegram MCP' in Telegram Settings > Devices.", file=sys.stderr)
        await client.disconnect()
