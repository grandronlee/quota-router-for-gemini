"""Run the proxy: `python -m quota_router` (one uvicorn worker, DESIGN §7).

Startup problems (settings, config, state database) print one clear line and exit
with status 2, without a traceback.
"""

from __future__ import annotations

import sqlite3
import sys

from .config import ConfigError, load_config
from .db import StateDBError
from .logs import configure_logging
from .scrub import Scrubber
from .settings import SettingsError, load_settings


def main() -> int:
    try:
        settings = load_settings()
        configure_logging(Scrubber([settings.gemini_api_key, settings.proxy_token]))
        config = load_config(settings.config_path)
        from .app import Proxy, create_app  # after logging is set up
        proxy = Proxy(settings, config)
    except (SettingsError, ConfigError, StateDBError) as e:
        print(f"quota-router: startup error: {e}", file=sys.stderr)
        return 2
    except sqlite3.Error as e:  # e.g. the startup prune on a read-only or locked database
        print(f"quota-router: startup error: state database {config.db_path}: {e}. Check its "
              "permissions and free disk space, and that no other proxy is using it.", file=sys.stderr)
        return 2

    import uvicorn
    uvicorn.run(create_app(proxy), host=settings.host, port=settings.port, workers=1,
                log_config=None, access_log=False, server_header=False, date_header=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
