"""Public CLI entry point.

The existing aeon_mud_client module remains the implementation and patching
surface for compatibility during the incremental package extraction. Importing
it here is intentional until the CLI itself migrates.
"""

from __future__ import annotations

from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    from aeon_mud_client import main as legacy_main

    return legacy_main(list(argv) if argv is not None else None)
