#!/usr/bin/env python3
"""Compatibility entry point for Aeon MUD.

The canonical CLI and all implementation modules live in the aeon_mud
package. This module retains historic imports and script launchers.
"""

from __future__ import annotations

# Backward-compatible exports. Package modules never import this entry point.
from aeon_mud.config import *
from aeon_mud.profiles import *
from aeon_mud.security import *
from aeon_mud.terminal_text import *
from aeon_mud.telnet import *
from aeon_mud.transport import *
from aeon_mud.transcript import *
from aeon_mud.input import *
from aeon_mud.terminal import *
from aeon_mud.commands import *
from aeon_mud.session import *


from aeon_mud.cli import *


if __name__ == "__main__":
    raise SystemExit(main())
