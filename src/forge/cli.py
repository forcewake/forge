"""``forge`` — the thin console dispatcher (R38-17 / issue #318, ADR-0032 §5).

The module mains already existed (``python -m forge.doctor`` and
siblings); the v0.37.0 wheel simply shipped no console_scripts for
them. This dispatcher adds NO logic: each subcommand delegates to the
existing module main with the remaining arguments, so the module's own
argparse keeps owning its surface (flags, exit codes, usage text):

    forge doctor [--json | --capabilities | --support-matrix | ...]
                 ≡ python -m forge.doctor ...
    forge gate --version V --image-ref R --digest sha256:...
                 ≡ python -m forge.release_promotion gate ...
    forge migrate [--database-url ...]
                 ≡ python -m forge.migrate ...

The imports are DELIBERATELY lazy (one command, one module import):
``forge doctor`` must not pay for the release-promotion module, and a
misconfigured environment still gets the usage message below.
"""

from __future__ import annotations

import sys

__all__ = ["main", "COMMANDS"]

#: The dispatch table: subcommand → (module, main). Every entry is an
#: EXISTING module main; adding a subcommand is adding a row here.
COMMANDS: dict[str, tuple[str, str]] = {
    "doctor": ("forge.doctor", "main"),
    "gate": ("forge.release_promotion", "main"),
    "migrate": ("forge.migrate", "main"),
}

#: The one word ``gate`` expands to before the module main sees the
#: arguments (``forge gate --version ...`` ≡ ``python -m
#: forge.release_promotion gate --version ...``).
_GATE_SUBCOMMAND = "gate"

_USAGE = (
    "usage: forge <command> [args]\n"
    "\n"
    "The thin forge console dispatcher (each command is the module main):\n"
    "  doctor    environment verification (python -m forge.doctor)\n"
    "  gate      release promotion gate (python -m forge.release_promotion gate)\n"
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
    forwarded = [*rest] if command != "gate" else [_GATE_SUBCOMMAND, *rest]
    return int(module_main(forwarded))


if __name__ == "__main__":
    sys.exit(main())
