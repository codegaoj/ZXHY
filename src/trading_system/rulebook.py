from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List


@dataclass
class Rulebook:
    raw: Dict[str, Any]

    @classmethod
    def load(cls, path: Path) -> "Rulebook":
        return cls(json.loads(path.read_text(encoding="utf-8")))

    @property
    def market_timing(self) -> Dict[str, Any]:
        return self.raw["market_timing"]

    @property
    def position_policy(self) -> Dict[str, Any]:
        return self.raw["position_policy"]

    @property
    def risk_priority(self) -> List[str]:
        return self.raw["risk_priority"]

    @property
    def tag_signal_map(self) -> Dict[str, Dict[str, Any]]:
        return self.raw["tag_signal_map"]

    def tag_score(self, tag: str) -> int:
        return int(self.tag_signal_map.get(tag, {}).get("base_score", 0))

    def tag_direction(self, tag: str) -> str:
        return str(self.tag_signal_map.get(tag, {}).get("direction", "context"))
