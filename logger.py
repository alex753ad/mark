"""Logging configuration with structured logging support."""

import sys
from loguru import logger

# Remove default handler
logger.remove()

# Console handler - simple format for readability
logger.add(
    sys.stderr,
    format="{time:HH:mm:ss} {level.icon} {message}",
    level="INFO",
    colorize=True,
)

# File handler - detailed format with structured data
logger.add(
    "logs/bot_{time:YYYY-MM-DD}.log",
    rotation="1 day",
    retention="7 days",
    level="DEBUG",
    encoding="utf-8",
    format="{time:YYYY-MM-DD HH:mm:ss} | {level} | {name}:{function}:{line} | {message} | {extra}",
    serialize=False,  # Keep human-readable format
)


def log_with_context(level: str, message: str, **kwargs):
    """
    Log with structured context data.
    
    Example:
        log_with_context("info", "Level triggered", symbol="BTCUSDT", level=50000, strength=5)
    """
    logger.bind(**kwargs).log(level.upper(), message)

