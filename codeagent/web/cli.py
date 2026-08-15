"""Command-line launcher for the local CodeAgent workbench."""

from __future__ import annotations

import argparse
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the local CodeAgent web UI.")
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    workspace = args.workspace.resolve()
    if not workspace.is_dir():
        parser.error(f"Workspace is not a directory: {workspace}")
    try:
        import uvicorn
    except (ImportError, OSError) as exc:
        raise RuntimeError(
            "Unable to load the web server runtime. Install or repair it with: "
            "pip install -e .[web]"
        ) from exc

    from codeagent.web.api import create_app

    app = create_app(workspace=workspace)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
