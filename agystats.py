#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "rich>=13.0.0",
# ]
# ///
"""Standalone uv-runnable entrypoint for agystats."""

from __future__ import annotations

from agystats.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
