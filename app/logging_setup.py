"""Lightweight structured logging setup for observability.

Idempotent — safe to call multiple times. Level via CUE_LOG_LEVEL (default INFO).
"""
from __future__ import annotations

import logging
import os

_CONFIGURED = False


def setup() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    level = os.getenv("CUE_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
        datefmt="%H:%M:%S",
    )
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    setup()
    return logging.getLogger(name)
