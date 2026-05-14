"""Data models for trading bot state management."""

from dataclasses import dataclass, field
from typing import Literal
import asyncio


@dataclass
class SymbolState:
    """State management for a single trading symbol."""
    
    symbol: str
    phase: Literal["idle", "phase1", "phase2"] = "idle"
    tasks: dict[str, asyncio.Task] = field(default_factory=dict)
    stop_flags: dict[str, asyncio.Event] = field(default_factory=dict)
    last_trigger_time: float = 0.0
    proximity_notified: dict[str, float] = field(default_factory=dict)
    analyzed_levels: set[str] = field(default_factory=set)
    level_strengths: dict[str, int] = field(default_factory=dict)  # task_key -> strength
    
    def make_task_key(self, level: float) -> str:
        """Generate unique task key for symbol-level pair."""
        from analysis.level_builder import _round_level
        return f"{self.symbol}_{_round_level(level)}"
    
    def add_task(self, level: float, task: asyncio.Task, strength: int = 0) -> str:
        """Add monitoring task for a level. Cancels existing tasks on nearby levels."""
        key = self.make_task_key(level)

        # Cancel existing tasks on levels within 0.5% (duplicates)
        if level > 0:
            for existing_key in list(self.tasks.keys()):
                parts = existing_key.rsplit("_", 1)
                if len(parts) == 2:
                    try:
                        existing_level = float(parts[1])
                        if existing_level > 0 and abs(existing_level - level) / level < 0.005:
                            self.tasks[existing_key].cancel()
                            stop = self.stop_flags.get(existing_key)
                            if stop:
                                stop.set()
                            del self.tasks[existing_key]
                            self.stop_flags.pop(existing_key, None)
                            self.level_strengths.pop(existing_key, None)
                    except ValueError:
                        pass

        self.tasks[key] = task
        self.stop_flags[key] = asyncio.Event()
        self.level_strengths[key] = strength
        return key

    def remove_task(self, task_key: str):
        """Remove monitoring task."""
        self.tasks.pop(task_key, None)
        self.stop_flags.pop(task_key, None)
        self.proximity_notified.pop(task_key, None)
        self.level_strengths.pop(task_key, None)
    
    def cancel_all_tasks(self):
        """Cancel all monitoring tasks for this symbol."""
        for task in self.tasks.values():
            task.cancel()
        for event in self.stop_flags.values():
            event.set()
    
    def has_active_tasks(self) -> bool:
        """Check if symbol has any active monitoring tasks."""
        return len(self.tasks) > 0
    
    def mark_level_analyzed(self, level: float):
        """Mark level as analyzed to prevent re-analysis."""
        self.analyzed_levels.add(f"{self.symbol}:{level}")
    
    def is_level_analyzed(self, level: float) -> bool:
        """Check if level was already analyzed."""
        return f"{self.symbol}:{level}" in self.analyzed_levels
    
    def clear_analyzed_levels(self):
        """Clear analyzed levels cache."""
        self.analyzed_levels.clear()


@dataclass
class LevelData:
    """Data structure for a support/resistance level."""
    
    level: float
    type: str  # pump_base, body_level, wick_level, etc.
    symbol: str
    level_side: Literal["support", "resistance"]
    strength: int = 0
    verdict: Literal["hold", "exit", "exit_fast"] = "hold"
    reason: str = ""
    
    # Technical indicators
    approach: int = 0
    vol_ratio: float = 1.0
    atr_pct: float = 0.0
    zone_approaches: int = 0
    
    # Level characteristics
    position: str = "mid_move"  # origin or mid_move
    cluster: bool = False
    pump_volume_ratio: float = 1.5
    
    # History
    was_broken: bool = False
    sweep_reclaimed: bool = False
    price_min_since_level: float = 0.0
    max_vol_on_approach: float = 0.0
    engulf_15m: bool = False
    
    def to_dict(self) -> dict:
        """Convert to dictionary for API calls."""
        return {
            "level": self.level,
            "type": self.type,
            "symbol": self.symbol,
            "level_side": self.level_side,
            "strength": self.strength,
            "verdict": self.verdict,
            "reason": self.reason,
            "approach": self.approach,
            "vol_ratio": self.vol_ratio,
            "atr_pct": self.atr_pct,
            "zone_approaches": self.zone_approaches,
            "position": self.position,
            "cluster": self.cluster,
            "pump_volume_ratio": self.pump_volume_ratio,
            "was_broken": self.was_broken,
            "sweep_reclaimed": self.sweep_reclaimed,
            "max_vol_on_approach": self.max_vol_on_approach,
            "engulf_15m": self.engulf_15m,
        }


class StateManager:
    """Global state manager for all symbols."""
    
    def __init__(self):
        self._states: dict[str, SymbolState] = {}
    
    def get_state(self, symbol: str) -> SymbolState:
        """Get or create state for symbol."""
        if symbol not in self._states:
            self._states[symbol] = SymbolState(symbol=symbol)
        return self._states[symbol]
    
    def remove_state(self, symbol: str):
        """Remove state for symbol."""
        if symbol in self._states:
            self._states[symbol].cancel_all_tasks()
            del self._states[symbol]
    
    def get_all_active_tasks(self) -> dict[str, asyncio.Task]:
        """Get all active tasks across all symbols."""
        tasks = {}
        for state in self._states.values():
            tasks.update(state.tasks)
        return tasks
    
    def cancel_all_tasks(self):
        """Cancel all tasks for all symbols."""
        for state in self._states.values():
            state.cancel_all_tasks()
    
    def get_active_monitors_count(self) -> int:
        """Get total number of active monitors."""
        return sum(len(state.tasks) for state in self._states.values())


# Global state manager instance
state_manager = StateManager()
