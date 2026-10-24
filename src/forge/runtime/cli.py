"""Install, verify, serve and work against the one configured engine."""

import argparse
import asyncio
import json
from pathlib import Path
import sys

from forge.runtime.app import create_app
from forge.runtime.assembly import database, open_runtime
from forge.runtime.settings import environment, load, secret
from forge.runtime.worker import serve, signals
from forge.store.schema import check_schema, initialize


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(prog="forge")
    result.add_argument("--config", type=Path, default=Path("forge.toml"))
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("install", help="explicitly initialize a completely empty database")
    commands.add_parser(
        "doctor", help="read-only schema, configuration and native guard verification"
    )
    onboard = commands.add_parser(
        "onboard", help="verify a configured native repository without writes"
    )
    onboard.add_argument("repository")
    commands.add_parser("worker")
    server = commands.add_parser("serve")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8420)
    return result


async def execute(args: argparse.Namespace) -> int:
    config = load(args.config)
    values = environment()
    if args.command == "install":
        engine = database(secret(config.database_url_env, values).get_secret_value())
        try:
            await initialize(engine)
        finally:
            await engine.dispose()
        print(json.dumps({"schema": "installed", "version": "1.0.0"}))
        return 0
    async with open_runtime(config, environ=values) as runtime:
        if args.command == "worker":
            stop = asyncio.Event()
            signals(stop)
            await serve(runtime, stop)
        elif args.command == "doctor":
            await check_schema(runtime.engine)
            model = config.connections[0].repositories[0].policy.model
            guard = await runtime.budget.admission.readiness(model)
            print(
                json.dumps(
                    {
                        "schema": "checked",
                        "native_budget": "verified" if guard is None else "unavailable",
                        "repositories": len(runtime.connections.registry.entries),
                    }
                )
            )
            return 0 if guard is None else 1
        elif args.command == "onboard":
            native = runtime.connections.registry.named(args.repository)
            _, repository = config.repositories()[args.repository]
            head = await native.repository.head(native.ref, repository.policy.target_branch)
            if head is None:
                raise ValueError("configured branch unavailable")
            print(
                json.dumps(
                    {
                        "repository": args.repository,
                        "provider": native.ref.identity.provider,
                        "native_id": native.ref.identity.repository_id,
                        "head": head,
                        "writes": 0,
                    }
                )
            )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "serve":
            import uvicorn

            if not 1 <= args.port <= 65535:
                raise ValueError("invalid server port")
            uvicorn.run(
                create_app(load(args.config)), host=args.host, port=args.port, access_log=False
            )
            return 0
        return asyncio.run(execute(args))
    except KeyboardInterrupt:
        return 130
    except Exception:
        print(
            "forge: operation refused; check the selected configuration, schema and native service availability",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
