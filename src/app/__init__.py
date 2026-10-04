"""The dashboard: a small Flask app over the job database.

Server-rendered pages and plain forms, no JavaScript framework and no build
step. It reads through the query helpers in src/storage/database.py and writes
status changes, jobs you add by hand, and a cover letter or cold email you
ask for from a job's page (src/app/generate.py). Scraping and the batch
generation stay in their own CLI steps (`make scrape`, `make process`).

Run it with `make app` (`python -m src.app`): it serves on 127.0.0.1 only.
"""

import os
from typing import Optional

from flask import Flask

from src.app.generate import Generator
from src.config import Config, load_config
from src.storage.database import init_db, set_database_url

LOCAL_HOSTS = ("localhost", "127.0.0.1")


def create_app(config: Optional[Config] = None) -> Flask:
    """Build the app against `config`'s database (config.yaml if not given).

    Raises ConfigError if config.yaml is missing or invalid.
    """
    config = config or load_config()
    set_database_url(config.database.url)
    init_db()

    app = Flask(__name__)
    # Only signs the flash-message cookie. A fresh random key per start means
    # there's no secret to manage; flash messages just don't survive a restart.
    app.secret_key = os.urandom(32)
    app.config["ALLOWED_HOSTS"] = LOCAL_HOSTS
    app.extensions["generator"] = Generator(config.llm.model_dump(), config.resume_summary)

    from src.app import web

    app.register_blueprint(web.bp)
    return app
