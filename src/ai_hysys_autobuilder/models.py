from __future__ import annotations

from dataclasses import dataclass, asdict, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ReactorType(str, Enum):
    CONVERSION = "Conversion"
    EQUILIBRIUM = "Equilibrium"
    GIBBS = "Gibbs"


@dataclass(frozen=True)
class ReactorSelection:
    scenario_id: str
    scenario_name: str
    input_text: str
    reactor_type: ReactorType
    confidence: float
    rationale: List[str]
    suggested_hysys: Dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["reactor_type"] = self.reactor_type.value
        return d


@dataclass(frozen=True)
class ScenarioSpec:
    scenario_id: str
    name: str
    description: str
    input_text: str

    # Minimal engineering defaults (can be extended per plant requirements)
    temperature_c: Optional[float] = None
    pressure_kpa: Optional[float] = None
    conversion_fraction: Optional[float] = None  # for Conversion reactor

    # Optional chemical system hints for building (components, etc.)
    components: List[str] = field(default_factory=list)
    property_package: Optional[str] = None


@dataclass
class HysysRunResult:
    ok: bool
    message: str
    outputs: Dict[str, Any] = field(default_factory=dict)
    raw_paths: Dict[str, str] = field(default_factory=dict)

    def to_json_dict(self) -> Dict[str, Any]:
        return asdict(self)

