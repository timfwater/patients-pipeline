"""Small CLI shim exposing the pipeline `main()` entrypoint.

This file keeps the current behavior unchanged while allowing imports
from `src.cli` in tooling or entrypoint configuration.
"""
from __future__ import annotations

from src.pipeline import main

__all__ = ["main"]


if __name__ == "__main__":
    main()
