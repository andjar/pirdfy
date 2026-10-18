"""Command line entry point: ``python -m pirdfy [serve|passwd|check-config|version]``."""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import sys

from . import __version__
from .paths import Paths


def setup_logging(level: str) -> None:
    # Under systemd, journald adds timestamps; avoid duplicating them.
    fmt = "%(levelname)s %(name)s: %(message)s" if os.environ.get("JOURNAL_STREAM") else \
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=level, format=fmt, stream=sys.stderr, force=True)
    for noisy in ("picamera2", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def cmd_serve(args) -> int:
    import uvicorn

    from .app import Pirdfy
    from .web.app import create_app

    setup_logging("INFO")
    paths = Paths.from_env()
    try:
        p = Pirdfy(paths)
    except Exception as e:
        logging.getLogger("pirdfy").error("Startup failed: %s", e)
        return 1
    s = p.store.settings
    logging.getLogger().setLevel(s.log_level)
    web = s.web
    config = uvicorn.Config(
        create_app(p), host=args.host or web.host, port=args.port or web.port, log_config=None,
        access_log=False, proxy_headers=bool(web.trusted_proxies),
        forwarded_allow_ips=",".join(web.trusted_proxies) or None,
        ssl_certfile=web.tls_cert, ssl_keyfile=web.tls_key, timeout_graceful_shutdown=5,
    )
    logging.getLogger("pirdfy").info("Pirdfy %s, data in %s, config %s", __version__, paths.data, paths.config)
    uvicorn.Server(config).run()
    return 0


def cmd_passwd(args) -> int:
    from .auth import AuthStore

    paths = Paths.from_env()
    store = AuthStore(paths.auth)
    if args.stdin:
        pw = sys.stdin.readline().rstrip("\n")
    else:
        pw = getpass.getpass("New dashboard password: ")
        if pw != getpass.getpass("Repeat: "):
            print("Passwords do not match", file=sys.stderr)
            return 1
    try:
        store.set_password(pw)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    print("Password updated; existing sessions were signed out.")
    return 0


def cmd_check_config(args) -> int:
    from .config import ConfigStore

    paths = Paths.from_env()
    try:
        ConfigStore(paths.config)
    except Exception as e:
        print(f"Invalid configuration {paths.config}:\n{e}", file=sys.stderr)
        return 1
    print(f"{paths.config}: OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["bench"]:
        from .bench import main as bench

        setup_logging("WARNING")
        return bench(argv[1:])
    parser = argparse.ArgumentParser(prog="pirdfy", description="Bird feeder camera detector")
    sub = parser.add_subparsers(dest="cmd")
    serve = sub.add_parser("serve", help="run the service (default)")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    pw = sub.add_parser("passwd", help="set the dashboard password")
    pw.add_argument("--stdin", action="store_true", help="read the password from stdin")
    sub.add_parser("check-config", help="validate the configuration file")
    sub.add_parser("version")
    sub.add_parser("bench", help="measure CPU/temperature cost of pre-roll (stop the service first)")
    get = sub.add_parser("get", help="print a config value, e.g. web.port")
    get.add_argument("key")
    args = parser.parse_args(argv)
    if args.cmd == "get":
        from .config import ConfigStore

        value = ConfigStore(Paths.from_env().config).settings.model_dump(mode="json")
        for part in args.key.split("."):
            value = value[int(part)] if isinstance(value, list) else value[part]
        print(value)
        return 0
    if args.cmd in (None, "serve"):
        if args.cmd is None:
            args.host = args.port = None
        return cmd_serve(args)
    if args.cmd == "passwd":
        return cmd_passwd(args)
    if args.cmd == "check-config":
        return cmd_check_config(args)
    print(__version__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
