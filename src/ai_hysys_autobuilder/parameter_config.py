from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from hysys_com import HysysComError
from models import ReactorSelection, ReactorType, ScenarioSpec


@dataclass
class ConfigReport:
    ok: bool
    details: Dict[str, Any]


class ParameterAutoConfigurator:
    """
    参数自动配置模块：尽可能设置
    - 物性包 / 组分
    - 物流 T/P 与基础流量（示例）
    - 反应器 T/P 与关键特定参数（Conversion 的转化率等）
    """

    def __init__(self, logger=None):
        self.logger = logger

    def configure(
        self,
        *,
        case: Any,
        flowsheet: Any,
        reactor_handles: Any,
        scenario: ScenarioSpec,
        selection: ReactorSelection,
    ) -> ConfigReport:
        details: Dict[str, Any] = {}

        # Property package / components live in "FluidPackage"/"BasisManager" depending on HYSYS version.
        details["property_package"] = self._try_set_property_package(case, selection.suggested_hysys.get("property_package"))
        details["components"] = self._try_set_components(case, scenario.components)

        feed = reactor_handles.feed_stream
        reactor = reactor_handles.reactor

        # Stream conditions
        t_c = selection.suggested_hysys.get("temperature_c", scenario.temperature_c)
        p_kpa = selection.suggested_hysys.get("pressure_kpa", scenario.pressure_kpa)
        details["feed_TP"] = self._try_set_stream_tp(feed, t_c=t_c, p_kpa=p_kpa)

        # Reactor conditions
        details["reactor_TP"] = self._try_set_reactor_tp(reactor, t_c=t_c, p_kpa=p_kpa)

        # Type-specific
        if selection.reactor_type == ReactorType.CONVERSION:
            conv = selection.suggested_hysys.get("conversion_fraction", scenario.conversion_fraction)
            details["conversion_fraction"] = self._try_set_conversion(reactor, conv)
        elif selection.reactor_type == ReactorType.EQUILIBRIUM:
            details["equilibrium_setup"] = self._try_setup_equilibrium(reactor)
        elif selection.reactor_type == ReactorType.GIBBS:
            details["gibbs_setup"] = self._try_setup_gibbs(reactor)

        ok = all(v.get("ok", True) for v in details.values() if isinstance(v, dict))
        return ConfigReport(ok=ok, details=details)

    # -----------------------------
    # Basis / package / components (best effort)
    # -----------------------------

    def _try_set_property_package(self, case: Any, package_name: Optional[str]) -> dict:
        if not package_name:
            return {"ok": True, "message": "No property package specified."}

        # Many installs require interacting with BasisManager; we do best-effort and log guidance.
        for path in (
            ("BasisManager", "FluidPackages"),
            ("BasisManager", "PropertyPackages"),
            ("SimulationBasis", "FluidPackages"),
        ):
            try:
                obj = case
                for attr in path:
                    obj = getattr(obj, attr)

                # Try: obj.Add(package_name) or obj.Item(package_name).Activate()
                for method in ("Add", "Create"):
                    try:
                        fp = getattr(obj, method)(package_name)
                        return {"ok": True, "message": f"Property package set via {'.'.join(path)}.{method}({package_name})."}
                    except Exception:
                        pass

                for method in ("Item", "GetItem"):
                    try:
                        fp = getattr(obj, method)(package_name)
                        if hasattr(fp, "Activate"):
                            fp.Activate()
                        return {"ok": True, "message": f"Property package activated: {package_name}."}
                    except Exception:
                        pass
            except Exception:
                continue

        msg = (
            f"未能通过通用路径设置物性包 `{package_name}`。"
            "不同 HYSYS COM 对象模型差异较大，请在你的环境中确认 BasisManager/FluidPackage API。"
        )
        if self.logger:
            self.logger.warning(msg)
        return {"ok": False, "message": msg}

    def _try_set_components(self, case: Any, components: list[str]) -> dict:
        if not components:
            return {"ok": True, "message": "No components specified."}

        for path in (
            ("BasisManager", "Components"),
            ("SimulationBasis", "Components"),
        ):
            try:
                obj = case
                for attr in path:
                    obj = getattr(obj, attr)
                added = []
                for comp in components:
                    try:
                        for method in ("Add", "AddComponent", "Insert"):
                            try:
                                getattr(obj, method)(comp)
                                added.append(comp)
                                break
                            except Exception:
                                continue
                    except Exception:
                        continue
                if added:
                    return {"ok": True, "message": f"Added components: {added}"}
            except Exception:
                continue

        msg = (
            f"未能通过通用路径添加组分: {components}。"
            "请在你的 HYSYS 环境确认 components/basis API。"
        )
        if self.logger:
            self.logger.warning(msg)
        return {"ok": False, "message": msg}

    # -----------------------------
    # Stream / reactor conditions
    # -----------------------------

    def _try_set_stream_tp(self, stream: Any, *, t_c: Optional[float], p_kpa: Optional[float]) -> dict:
        ok = True
        msgs = []
        if t_c is not None:
            ok_t, msg = self._set_value(stream, ["Temperature", "T"], t_c, unit_hint="C")
            ok &= ok_t
            msgs.append(msg)
        if p_kpa is not None:
            ok_p, msg = self._set_value(stream, ["Pressure", "P"], p_kpa, unit_hint="kPa")
            ok &= ok_p
            msgs.append(msg)
        return {"ok": ok, "message": "; ".join(m for m in msgs if m)}

    def _try_set_reactor_tp(self, reactor: Any, *, t_c: Optional[float], p_kpa: Optional[float]) -> dict:
        ok = True
        msgs = []
        if t_c is not None:
            ok_t, msg = self._set_value(reactor, ["Temperature", "T", "ReactorTemperature"], t_c, unit_hint="C")
            ok &= ok_t
            msgs.append(msg)
        if p_kpa is not None:
            ok_p, msg = self._set_value(reactor, ["PressureDrop", "DP", "DeltaP"], 0.0, unit_hint="kPa")
            msgs.append(msg)
            # Reactor absolute pressure often defined by inlet stream; keep DP=0 by default.
            ok &= ok_p
        return {"ok": ok, "message": "; ".join(m for m in msgs if m)}

    # -----------------------------
    # Type-specific setups
    # -----------------------------

    def _try_set_conversion(self, reactor: Any, conversion_fraction: Optional[float]) -> dict:
        if conversion_fraction is None:
            return {"ok": False, "message": "Conversion reactor requires conversion_fraction."}
        if not (0.0 <= float(conversion_fraction) <= 1.0):
            raise ValueError(f"conversion_fraction out of range: {conversion_fraction}")

        # HYSYS typically uses reaction sets; exact API varies. We try a few common patterns.
        for attr in ("Conversion", "OverallConversion", "ConversionFraction"):
            if hasattr(reactor, attr):
                try:
                    setattr(reactor, attr, float(conversion_fraction))
                    return {"ok": True, "message": f"Set {attr}={conversion_fraction}."}
                except Exception:
                    pass

        msg = (
            f"未能在 COM 反应器对象上找到可写的 conversion 属性（目标 {conversion_fraction}）。"
            "请在你的 HYSYS 环境中将 Conversion 反应器关联 Reaction Set 并设置反应转化率。"
        )
        if self.logger:
            self.logger.warning(msg)
        return {"ok": False, "message": msg}

    def _try_setup_equilibrium(self, reactor: Any) -> dict:
        # Usually equilibrium reactor requires reaction set; placeholder best-effort.
        for attr in ("ReactionSet", "Reactions", "EquilibriumReactions"):
            if hasattr(reactor, attr):
                return {"ok": True, "message": f"Equilibrium setup placeholder: found `{attr}` (please bind reaction set)."}
        msg = "Equilibrium 反应器通常需要绑定 Reaction Set；当前 COM 对象未暴露常见入口（占位提示）。"
        if self.logger:
            self.logger.warning(msg)
        return {"ok": False, "message": msg}

    def _try_setup_gibbs(self, reactor: Any) -> dict:
        # Gibbs reactor often needs "Calculate Equilibrium" flags, phase settings, etc.
        for attr in ("MinimizeGibbsEnergy", "DoGibbs", "Equilibrium"):
            if hasattr(reactor, attr):
                try:
                    setattr(reactor, attr, True)
                    return {"ok": True, "message": f"Enabled `{attr}` for Gibbs reactor."}
                except Exception:
                    pass
        return {"ok": True, "message": "Gibbs setup: no explicit flags set (inlet composition will drive equilibrium)."}

    # -----------------------------
    # Generic setter with unit hints (best effort)
    # -----------------------------

    def _set_value(self, obj: Any, candidates: list[str], value: float, *, unit_hint: str) -> tuple[bool, str]:
        last_err: Optional[Exception] = None
        for attr in candidates:
            if not hasattr(obj, attr):
                continue
            try:
                target = getattr(obj, attr)
                # Some HYSYS properties are "Variable" objects with .Value or .SetValue(value, unit)
                if hasattr(target, "SetValue"):
                    try:
                        target.SetValue(float(value), unit_hint)
                        return True, f"Set {attr}.SetValue({value}, {unit_hint})"
                    except Exception as e:
                        last_err = e
                if hasattr(target, "Value"):
                    try:
                        target.Value = float(value)
                        return True, f"Set {attr}.Value={value}"
                    except Exception as e:
                        last_err = e
                try:
                    setattr(obj, attr, float(value))
                    return True, f"Set {attr}={value}"
                except Exception as e:
                    last_err = e
            except Exception as e:
                last_err = e
                continue
        return False, f"Failed set {candidates}={value} ({unit_hint}). LastErr={last_err}"

