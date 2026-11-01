#!/usr/bin/env python3
"""Local line-oriented Telnet client for MUDs.

The client intentionally treats Telnet as insecure transport. It adds guard
rails around plaintext sessions, but it does not make Telnet cryptographically
safe. For secure hops, use a SOCKS5 proxy or a profile proxy_command such as
OpenSSH's "-W host:port" mode.
"""

from __future__ import annotations

import argparse
import base64
import codecs
import ctypes
import getpass
import hashlib
import ipaddress
import json
import os
import queue
import re
import select
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
from unittest import mock
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Compatibility exports for historic imports; production CLI lives in
# aeon_mud.cli and never imports this wrapper.
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
