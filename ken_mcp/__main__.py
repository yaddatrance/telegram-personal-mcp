import argparse
import asyncio
import logging
import secrets
import sys
from pathlib import Path

from .security import ProfileLock, default_data_dir
from .service import Messenger
from .store import Store, UserError
from .transport import FakeTransport, TransportError


async def serve(directory, live, enable_sends, require_local_approval):
    from .server import create_server
    from .account import connect_existing
    with ProfileLock(directory):
        transport = await connect_existing(directory) if live else FakeTransport()
        store = Store(directory / "outbox.sqlite3", "live" if live else "demo")
        try:
            service = Messenger(store, transport, enable_sends, require_local_approval)
            await service.start()
            await create_server(service).run_stdio_async()
        finally:
            await transport.close()
            store.close()


def parser():
    root = argparse.ArgumentParser(description="Ken's local personal-account Telegram MCP. Defaults to offline demo.")
    subs = root.add_subparsers(dest="command", required=True)
    for name in ("serve", "serve-http", "review", "approve", "login", "demo"):
        command = subs.add_parser(name)
        command.add_argument("--data-dir", type=Path)
        if name != "login":
            command.add_argument("--live", action="store_true")
        if name in ("serve", "serve-http"):
            command.add_argument("--enable-sends", action="store_true", help="Explicitly enable live sends for specifically user-authorized MCP requests")
            command.add_argument("--require-local-approval", action="store_true", help="Optional strict mode: require a separate owner review click per message")
        if name == "serve-http":
            command.add_argument("--config", type=Path, required=True, help="Verified non-secret OAuth resource configuration")
            command.add_argument("--port", type=int, default=8766)
        if name in ("review", "demo"):
            command.add_argument("--port", type=int, default=8765)
            command.add_argument("--open", action="store_true", help="Open the private review URL in your browser")
        if name == "approve":
            command.add_argument("draft_id")
    return root


def main():
    logging.disable(logging.CRITICAL)  # No library request, account, or session logging.
    args = parser().parse_args()
    live = args.command == "login" or args.live
    directory = (args.data_dir or default_data_dir(live)).resolve()
    try:
        if args.command == "serve":
            asyncio.run(serve(directory, live, args.enable_sends, args.require_local_approval))
        elif args.command == "serve-http":
            from .remote import RemoteConfig, serve_http
            asyncio.run(serve_http(directory, live, args.enable_sends, args.require_local_approval,
                                   RemoteConfig.read(args.config), args.port))
        elif args.command == "login":
            from .account import login
            with ProfileLock(directory):
                asyncio.run(login(directory))
        elif args.command in ("review", "demo"):
            from .review import run_review
            if args.command == "demo":
                if live:
                    raise UserError("The demo command cannot run live.")
                store = Store(directory / "outbox.sqlite3", "demo")
                try:
                    store.bind_account("9000")
                    if not store.recent(1):
                        store.prepare(secrets.token_hex(16), "9000", FakeTransport().peers["1001"],
                                      "Hey Alex! Coffee on Saturday? I found a place I think you'd like.", None)
                finally:
                    store.close()
            run_review(directory, live, args.port, args.open)
        elif args.command == "approve":
            if not sys.stdin.isatty():
                raise UserError("Owner approval requires a local interactive terminal.")
            store = Store(directory / "outbox.sqlite3", "live" if live else "demo")
            try:
                row = store.item(args.draft_id)
                print("Account: {} | To: {} ({}) | Reply: {}".format(row["account_id"], repr(row["peer_name"]), row["peer_id"], row["reply_to"]))
                # repr escapes terminal control sequences in untrusted message content.
                print("Exact message (escaped representation):\n" + repr(row["text"]))
                print("Preview SHA-256: " + row["digest"])
                if input("Type APPROVE to allow this exact send for five minutes: ") == "APPROVE":
                    store.approve_local(row["id"], row["digest"])
                    print("Approved locally. MCP may now dispatch this request once.")
                else:
                    print("Not approved.")
            finally:
                store.close()
    except (UserError, TransportError) as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception:
        print("Operation failed. No sensitive exception details were logged. Check local setup and account status.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
