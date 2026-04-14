import argparse
import json
import sys

from .curator_api import run_curator_server
from .core.curator import PresentationConfig, rebuild_and_trigger
from .dav import DavConfig, run_dav_server


def main():
    parser = argparse.ArgumentParser(prog="buzz", description="Buzz CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # dav
    subparsers.add_parser("dav", help="Start WebDAV API server")

    # curator
    curator_parser = subparsers.add_parser(
        "curator", help="Curator (Presentation Layer)"
    )
    curator_sub = curator_parser.add_subparsers(dest="subcommand", required=True)

    curator_sub.add_parser("server", help="Start curator API server")
    curator_sub.add_parser("sync", help="Run a one-time presentation build")

    args = parser.parse_args()

    if args.command == "dav":
        config = DavConfig.load()
        run_dav_server(config)
    elif args.command == "curator":
        config = PresentationConfig()
        if args.subcommand == "server":
            run_curator_server(config)
        elif args.subcommand == "sync":
            report = rebuild_and_trigger(config)
            print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
