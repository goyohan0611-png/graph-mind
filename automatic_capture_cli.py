"""Command line control for the Graph-MIND automatic capture service."""
from __future__ import annotations

import argparse
import json
import os

from automatic_capture import (AutomaticCaptureService, default_config_path,
                               default_service_state_dir, load_config, make_config,
                               _atomic_json)


def _emit(value):
    print(json.dumps(value, ensure_ascii=False, indent=2))


def main(argv=None):
    parser = argparse.ArgumentParser(description="Graph-MIND automatic capture v0.2")
    parser.add_argument("--config", default=str(default_config_path()))
    parser.add_argument("--state-dir", default=str(default_service_state_dir()))
    commands = parser.add_subparsers(dest="command", required=True)
    configure = commands.add_parser("configure")
    configure.add_argument("--workspace", required=True)
    configure.add_argument("--project", required=True)
    configure.add_argument("--session", required=True)
    configure.add_argument("--sessions-root", required=True)
    configure.add_argument("--db")
    configure.add_argument("--poll-seconds", type=int, default=5)
    configure.add_argument("--code-poll-seconds", type=int, default=120)
    configure.add_argument("--enable-semantic-llm", action="store_true")
    configure.add_argument("--semantic-model")
    commands.add_parser("baseline")
    commands.add_parser("once")
    commands.add_parser("run")
    commands.add_parser("status")
    args = parser.parse_args(argv)
    if args.command == "configure":
        config = make_config(workspace=args.workspace, project_id=args.project,
            session_id=args.session, sessions_root=args.sessions_root,
            poll_seconds=args.poll_seconds, code_poll_seconds=args.code_poll_seconds,
            db_path=args.db)
        if args.enable_semantic_llm:
            if not args.semantic_model:
                parser.error("--semantic-model is required with --enable-semantic-llm")
            config["semantic_llm"].update({"enabled": True,
                                           "model": args.semantic_model})
        _atomic_json(args.config, config)
        _emit({"status": "CONFIGURED", "config": os.path.abspath(args.config),
               "value": config})
        return 0
    if args.command == "status":
        path = os.path.join(args.state_dir, "status.json")
        if not os.path.isfile(path):
            _emit({"status": "NOT_STARTED", "state": path})
        else:
            with open(path, encoding="utf-8") as handle:
                _emit(json.load(handle))
        return 0
    service = AutomaticCaptureService(load_config(args.config), state_dir=args.state_dir)
    if args.command == "baseline":
        result = service.baseline()
    elif args.command == "once":
        result = service.run_once()
    else:
        service.run_forever()
        return 0
    _emit(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
