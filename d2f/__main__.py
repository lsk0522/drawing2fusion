import sys

from .cli import main

if __name__ == "__main__":
    # Windows consoles default to cp949; force UTF-8 so Korean text and icons print.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())
