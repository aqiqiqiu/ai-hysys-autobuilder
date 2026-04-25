from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from hysys_com import HysysComError, _com_call
from models import ReactorType


def _safe_get(obj: Any, attr: str) -> Any:
    try:
        return getattr(obj, attr)
    except Exception as e:
        raise HysysComError(f"无法读取 COM 属性 `{attr}`: {obj}") from e


@dataclass
class ReactorHandles:
    reactor: Any
    feed_stream: Any
    product_stream: Any


class ReactorFactory:
    """
    三种反应器创建函数：Conversion / Equilibrium / Gibbs
    """

    def __init__(self, logger=None):
        self.logger = logger

    def create_reactor(self, flowsheet: Any, reactor_type: ReactorType, name: str = "R-100") -> ReactorHandles:
        if reactor_type == ReactorType.CONVERSION:
            return self.create_conversion_reactor(flowsheet, name=name)
        if reactor_type == ReactorType.EQUILIBRIUM:
            return self.create_equilibrium_reactor(flowsheet, name=name)
        if reactor_type == ReactorType.GIBBS:
            return self.create_gibbs_reactor(flowsheet, name=name)
        raise ValueError(f"Unsupported reactor type: {reactor_type}")

    def create_conversion_reactor(self, flowsheet: Any, name: str = "R-Conv") -> ReactorHandles:
        reactor = self._add_unit_operation(
            flowsheet,
            unit_type_candidates=[
                "Conversion Reactor",
                "Conversion",
                "RConversion",
            ],
            name=name,
        )
        feed, prod = self._ensure_streams(flowsheet, base=name)
        self._connect_streams(reactor, feed, prod)
        return ReactorHandles(reactor=reactor, feed_stream=feed, product_stream=prod)

    def create_equilibrium_reactor(self, flowsheet: Any, name: str = "R-Equil") -> ReactorHandles:
        reactor = self._add_unit_operation(
            flowsheet,
            unit_type_candidates=[
                "Equilibrium Reactor",
                "Equilibrium",
                "REquilibrium",
            ],
            name=name,
        )
        feed, prod = self._ensure_streams(flowsheet, base=name)
        self._connect_streams(reactor, feed, prod)
        return ReactorHandles(reactor=reactor, feed_stream=feed, product_stream=prod)

    def create_gibbs_reactor(self, flowsheet: Any, name: str = "R-Gibbs") -> ReactorHandles:
        reactor = self._add_unit_operation(
            flowsheet,
            unit_type_candidates=[
                "Gibbs Reactor",
                "Gibbs",
                "RGibbs",
            ],
            name=name,
        )
        feed, prod = self._ensure_streams(flowsheet, base=name)
        self._connect_streams(reactor, feed, prod)
        return ReactorHandles(reactor=reactor, feed_stream=feed, product_stream=prod)

    # -----------------------------
    # Internals
    # -----------------------------

    def _add_unit_operation(self, flowsheet: Any, unit_type_candidates: list[str], name: str) -> Any:
        ops = None
        for attr in ("Operations", "UnitOperations", "OperationsCollection"):
            if hasattr(flowsheet, attr):
                try:
                    ops = getattr(flowsheet, attr)
                    break
                except Exception:
                    continue
        if ops is None:
            raise HysysComError("Flowsheet 未暴露 Operations/UnitOperations 集合，无法添加单元。")

        last_err: Optional[Exception] = None
        attempt_errors: list[str] = []

        for unit_type in unit_type_candidates:
            for args in (
                (unit_type, name),
                (unit_type, name, ""),
                (name, unit_type),
                (name, unit_type, ""),
                (unit_type,),
            ):
                try:
                    reactor = _com_call(ops, "Add", *args)
                    try:
                        if hasattr(reactor, "Name"):
                            reactor.Name = name
                    except Exception:
                        pass
                    if self.logger:
                        self.logger.info(f"Created unit `{name}` as `{unit_type}` via `Add{args}`.")
                    return reactor
                except Exception as e:
                    last_err = e
                    attempt_errors.append(f"Add{args} -> {type(e).__name__}: {e}")

            for method in ("Create", "New"):
                if hasattr(ops, method):
                    for args in ((unit_type, name), (unit_type, name, ""), (unit_type,)):
                        try:
                            reactor = _com_call(ops, method, *args)
                            try:
                                if hasattr(reactor, "Name"):
                                    reactor.Name = name
                            except Exception:
                                pass
                            if self.logger:
                                self.logger.info(f"Created unit `{name}` as `{unit_type}` via `{method}{args}`.")
                            return reactor
                        except Exception as e:
                            last_err = e
                            attempt_errors.append(f"{method}{args} -> {type(e).__name__}: {e}")

        if self.logger and attempt_errors:
            # Keep log concise; details are also bubbled up in the exception message.
            self.logger.error(
                "Failed to create unit operation. Last attempts:\n- " + "\n- ".join(attempt_errors[-5:])
            )

        details = ""
        if attempt_errors:
            tail = attempt_errors[-5:]
            details = "\n失败尝试摘要（最近 5 条）:\n- " + "\n- ".join(tail)

        raise HysysComError(
            f"无法创建反应器 `{name}`。已尝试类型: {unit_type_candidates}。{details}"
        ) from last_err

    def _ensure_streams(self, flowsheet: Any, base: str) -> tuple[Any, Any]:
        streams = None
        for attr in ("MaterialStreams", "Streams", "MaterialStreamCollection"):
            if hasattr(flowsheet, attr):
                try:
                    streams = getattr(flowsheet, attr)
                    break
                except Exception:
                    continue
        if streams is None:
            raise HysysComError("Flowsheet 未暴露 MaterialStreams/Streams 集合，无法创建物流。")

        feed_name = f"{base}-Feed"
        prod_name = f"{base}-Prod"

        feed = self._add_stream(streams, feed_name)
        prod = self._add_stream(streams, prod_name)
        return feed, prod

    def _add_stream(self, streams: Any, name: str) -> Any:
        last_err: Optional[Exception] = None
        for method in ("Add", "Create", "New"):
            try:
                stream = _com_call(streams, method, name)
                if self.logger:
                    self.logger.info(f"Created material stream `{name}`.")
                return stream
            except Exception as e:
                last_err = e
                continue
        raise HysysComError(f"无法创建物流 `{name}`。") from last_err

    def _connect_streams(self, reactor: Any, feed: Any, prod: Any) -> None:
        for inlet_attr, outlet_attr in (
            ("InletStreams", "OutletStreams"),
            ("Inlet", "Outlet"),
            ("Inlets", "Outlets"),
        ):
            if hasattr(reactor, inlet_attr) and hasattr(reactor, outlet_attr):
                try:
                    inlet_coll = _safe_get(reactor, inlet_attr)
                    outlet_coll = _safe_get(reactor, outlet_attr)
                    try:
                        _com_call(inlet_coll, "Add", feed)
                        _com_call(outlet_coll, "Add", prod)
                        if self.logger:
                            self.logger.info("Connected streams via inlet/outlet collections.")
                        return
                    except Exception:
                        pass
                except Exception:
                    pass

        for feed_attr, prod_attr in (
            ("FeedStream", "ProductStream"),
            ("InletStream", "OutletStream"),
        ):
            if hasattr(reactor, feed_attr) and hasattr(reactor, prod_attr):
                try:
                    setattr(reactor, feed_attr, feed)
                    setattr(reactor, prod_attr, prod)
                    if self.logger:
                        self.logger.info("Connected streams via Feed/Product properties.")
                    return
                except Exception:
                    continue

        raise HysysComError("无法将物流连接到反应器（端口 API 未匹配）。请根据你的 HYSYS COM 对象模型调整连接逻辑。")