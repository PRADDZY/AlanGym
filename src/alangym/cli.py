"""Command line entry points for the local AlanGym dashboard and run controls."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_SERVER = "http://127.0.0.1:8765"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(prog="alangym")
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="Start the local dashboard API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)

    run = commands.add_parser("run", help="Start a benchmark on a public GitHub repository")
    run.add_argument("--repo", required=True, help="GitHub owner/name")
    run.add_argument("--seed", type=int, default=42)
    run.add_argument("--server", default=DEFAULT_SERVER)

    cancel = commands.add_parser("cancel", help="Cancel an active run")
    cancel.add_argument("run_id")
    cancel.add_argument("--server", default=DEFAULT_SERVER)

    status = commands.add_parser("status", help="Show the saved results for a run")
    status.add_argument("run_id")
    status.add_argument("--server", default=DEFAULT_SERVER)

    export = commands.add_parser("export", help="Export a completed run for review")
    export.add_argument("run_id")
    export.add_argument("--output", type=Path, help="Destination JSON file")

    arguments = parser.parse_args()
    if arguments.command == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(), host=arguments.host, port=arguments.port, log_level="info")
        return
    if arguments.command == "run":
        _request(
            arguments.server,
            "/api/runs",
            method="POST",
            payload={"repo": arguments.repo, "seed": arguments.seed},
        )
        return
    if arguments.command == "cancel":
        _request(arguments.server, f"/api/runs/{arguments.run_id}/cancel", method="POST")
        return
    if arguments.command == "status":
        _request(arguments.server, f"/api/runs/{arguments.run_id}")
        return
    if arguments.command == "export":
        from .database import RunDatabase
        from .replay import export_replay

        database_path = PROJECT_ROOT / ".alangym" / "runs.sqlite3"
        if not database_path.is_file():
            print(f"Local run database not found: {database_path}", file=sys.stderr)
            raise SystemExit(1)
        try:
            database = RunDatabase(database_path)
            # Resolve the run before deriving the default filename from its ID.
            run = database.get_run(arguments.run_id)
            destination = arguments.output or (
                PROJECT_ROOT / ".alangym" / "exports" / f"{run['id']}.json"
            )
            output = export_replay(database, arguments.run_id, destination)
        except (KeyError, ValueError, OSError) as error:
            print(f"Could not export run: {error}", file=sys.stderr)
            raise SystemExit(1) from error
        print(output)


def _request(
    server: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict[str, object] | None = None,
) -> None:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        server.rstrip("/") + path,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=10) as response:
            result = json.loads(response.read())
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        print(f"AlanGym returned HTTP {error.code}: {detail}", file=sys.stderr)
        raise SystemExit(1) from error
    except URLError as error:
        print(
            f"Could not reach {server}. Start it with alangym serve. ({error.reason})",
            file=sys.stderr,
        )
        raise SystemExit(1) from error
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
