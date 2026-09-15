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
_HELP_FLAGS = frozenset({"-h", "--help"})


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args:
        first = args[0]
        if first in _HELP_FLAGS:
            from .cli import main as cli_main

            return cli_main(args)

        from .cli import get_subcommand_names

        if first in get_subcommand_names():
            from .cli import main as cli_main

            return cli_main(args)

    from .app import main as app_main

    return app_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
