"""`python -m src.app` (or `make app`): serve the dashboard on 127.0.0.1.

    python -m src.app                 # http://127.0.0.1:8000, opens your browser
    python -m src.app --port 8001     # another port
    python -m src.app --no-browser
"""

import argparse
import sys
import threading
import webbrowser

from src.config import ConfigError
from src.logging_config import setup_logging

HOST = "127.0.0.1"  # never all interfaces: the job data is yours alone


def main(argv=None) -> None:
    setup_logging()
    parser = argparse.ArgumentParser(prog="python -m src.app", description="Serve the dashboard.")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = parser.parse_args(argv)

    from src.app import create_app

    try:
        app = create_app()
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        raise SystemExit(1)

    url = f"http://{HOST}:{args.port}/"
    print(f"Dashboard: {url}  (Ctrl+C to stop)")
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    app.run(host=HOST, port=args.port, debug=False)


if __name__ == "__main__":
    main()
