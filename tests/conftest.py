"""Test-run setup shared by every test module."""

from __future__ import annotations

import asyncio
import sys

if sys.platform == "win32":  # pragma: no cover - CI and production are Linux
    # psycopg's async connections cannot run on the Proactor loop Python picks by
    # default on Windows. The agent itself only ever runs in the Linux container,
    # so this belongs to the test run rather than to the app.
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
