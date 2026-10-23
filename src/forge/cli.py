"""Dispatch the product's doctor and database migration commands."""

from __future__ import annotations

import sys

__all__ = ["main", "COMMANDS"]

#: The dispatch table: subcommand → (module, main). Every entry is an
#: EXISTING module main; adding a subcommand is adding a row here.
COMMANDS: dict[str, tuple[str, str]] = {
    "doctor": ("forge.doctor", "main"),
    "migrate": ("forge.migrate", "main"),
}

_USAGE = (
    "usage: forge <command> [args]\n"
    "\n"
    "The thin forge console dispatcher (each command is the module main):\n"
    "  doctor    environment verification (python -m forge.doctor)\n"
    "  migrate   apply database migrations (python -m forge.migrate)\n"
)


def main(argv: list[str] | None = None) -> int:
    """Dispatch *argv* (default: the process arguments) to a module main."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(_USAGE)
        return 0
    command, rest = args[0], args[1:]
    entry = COMMANDS.get(command)
    if entry is None:
        sys.stderr.write(
            f"forge: unknown command {command!r} — known commands: {sorted(COMMANDS)}\n{_USAGE}"
        )
        return 2
    module_name, main_name = entry
    import importlib

    module = importlib.import_module(module_name)
    module_main = getattr(module, main_name)
    return int(module_main(rest))


if __name__ == "__main__":
    sys.exit(main())
