"""Logging configuration with structured logging support."""

import os
import sys
from loguru import logger

# Remove default handler
logger.remove()

os.makedirs("logs", exist_ok=True)

# Console handler - simple format for readability
logger.add(
    sys.stderr,
    format="{time:HH:mm:ss} {level.icon} {message}",
    level="INFO",
    colorize=True,
)

# File handler - INFO only, compact format, gzip compression
logger.add(
    "logs/bot_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="7 days",
    level="INFO",
    encoding="utf-8",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level:<5} | {name}:{line} | {message}",
    serialize=False,
    compression="gz",
)


def log_with_context(level: str, message: str, **kwargs):
    logger.bind(**kwargs).log(level.upper(), message)
