"""Configuration management for trading bot."""

import os
import json
from dotenv import load_dotenv
from logger import logger

load_dotenv()

# API Configuration
CLAUDE_API_KEY = os.getenv("CLAUDE_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))

# File paths
TOKENS_FILE = "tokens.json"
TRIGGER_TIMES_FILE = "trigger_times.json"
HISTORY_DB_FILE = "history.db"


class TokenRegistry:
    """Registry for managing active trading symbols."""
    
    def __init__(self):
        self._tokens: list[str] = []
        self._load()

    def _load(self):
        """Load tokens from file."""
        if os.path.exists(TOKENS_FILE):
            try:
                with open(TOKENS_FILE) as f:
                    self._tokens = json.load(f)
                logger.info("Loaded tokens", count=len(self._tokens), tokens=self._tokens)
            except Exception as e:
                logger.error("Failed to load tokens", error=str(e))
                self._tokens = []

    def _save(self):
        """Save tokens to file."""
        try:
            with open(TOKENS_FILE, "w") as f:
                json.dump(self._tokens, f, indent=2)
            logger.debug("Saved tokens", count=len(self._tokens))
        except Exception as e:
            logger.error("Failed to save tokens", error=str(e))

    def get_all(self) -> list[str]:
        """Get all registered tokens."""
        return list(self._tokens)

    def add(self, symbol: str):
        """Add symbol to registry."""
        if symbol not in self._tokens:
            self._tokens.append(symbol)
            self._save()
            logger.info("Token added", symbol=symbol)

    def remove(self, symbol: str):
        """Remove symbol from registry."""
        if symbol in self._tokens:
            self._tokens.remove(symbol)
            self._save()
            logger.info("Token removed", symbol=symbol)

    def contains(self, symbol: str) -> bool:
        """Check if symbol is registered."""
        return symbol in self._tokens


# Global token registry instance
token_registry = TokenRegistry()


def validate_config() -> bool:
    """Validate that all required configuration is present."""
    if not CLAUDE_API_KEY:
        logger.error("Missing CLAUDE_API_KEY in environment")
        return False
    if not TELEGRAM_TOKEN:
        logger.error("Missing TELEGRAM_TOKEN in environment")
        return False
    if TELEGRAM_CHAT_ID == 0:
        logger.error("Missing or invalid TELEGRAM_CHAT_ID in environment")
        return False
    return True
