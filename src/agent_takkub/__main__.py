from __future__ import annotations

import sys

# Standard Qt application options that may legitimately be passed when launching GUI
_QT_APP_FLAGS = frozenset(
    {
        "-platform",
        "-platformpluginpath",
        "-style",
        "-stylesheet",
        "-widgetcount",
        "-reverse",
        "-qmljsdebugger",
        "-geometry",
        "-session",
    }
)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or (args and args[0] in _QT_APP_FLAGS):
        from .app import main as app_main

        return app_main(argv)
    from .cli import main as cli_main

    return cli_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
