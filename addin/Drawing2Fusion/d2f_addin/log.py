"""Logging to Fusion's Text Commands window and ~/.d2f/logs/addin.log."""

import datetime
import os

_LOG_DIR = os.path.join(os.path.expanduser("~"), ".d2f", "logs")
_LOG_FILE = os.path.join(_LOG_DIR, "addin.log")


def log(msg):
    line = "[d2f] %s" % msg
    try:
        import adsk.core
        adsk.core.Application.get().log(line)
    except Exception:
        pass
    try:
        os.makedirs(_LOG_DIR, exist_ok=True)
        with open(_LOG_FILE, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (datetime.datetime.now().isoformat(timespec="seconds"), line))
    except Exception:
        pass
