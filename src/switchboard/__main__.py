"""Enable ``python -m switchboard <command>`` (used by the PM2 ecosystem on the
ShellAgent VM). Delegates to the same entrypoint as the ``switchboard`` console
script."""

from .cli import main

raise SystemExit(main())
