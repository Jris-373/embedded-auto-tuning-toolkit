"""
sinks.py — Decision output destinations.

UNVERIFIED: All reserved sinks (MQTT, Webhook, SQLite) are named but not implemented.

Concrete implementations:
    FileSink — Write decision JSON to tools/logs/

Reserved:
    MQTTSink       — Publish to MQTT broker
    WebhookSink    — HTTP POST to monitoring platform
    SQLiteSink     — Write to local database for long-term analysis
"""

from abc import ABC, abstractmethod
import json
from pathlib import Path


class DecisionSink(ABC):
    """Abstract decision output."""

    @abstractmethod
    def write(self, decision: dict, round_num: int) -> str:
        """Persist decision.  Returns the path or URI written to."""


class FileSink(DecisionSink):
    """Write decision JSON to tools/logs/decision_r{N}.json."""

    def __init__(self, output_dir: str = "tools/logs"):
        self._dir = Path(output_dir)

    def write(self, decision: dict, round_num: int) -> str:
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"decision_r{round_num:02d}.json"
        path.write_text(json.dumps(decision, indent=2) + "\n")
        return str(path)
