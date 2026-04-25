from __future__ import annotations

import ctypes
import ctypes.wintypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Optional


class HysysComError(RuntimeError):
    pass


def _try_import_win32():
    try:
        import win32com.client  # type: ignore
        import pythoncom  # type: ignore

        return win32com.client, pythoncom
    except Exception as e:  # pragma: no cover
        raise HysysComError(
            "pywin32 未安装或不可用，无法进行 HYSYS COM 自动化。请先 `pip install -r requirements.txt`。"
        ) from e


def _hresult_hex(hr: int) -> str:
    # HRESULT is a signed 32-bit integer in pywintypes; normalize to unsigned for display.
    return f"0x{(hr & 0xFFFFFFFF):08X}"


def _format_win32_message(code: int) -> str | None:
    """
    Best-effort FormatMessage for Win32 error codes / HRESULT low word.
    Uses Win32 API directly (no extra dependencies).
    """
    try:
        # https://learn.microsoft.com/windows/win32/api/winbase/nf-winbase-formatmessagew
        FORMAT_MESSAGE_FROM_SYSTEM = 0x00001000
        FORMAT_MESSAGE_IGNORE_INSERTS = 0x00000200
        flags = FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS

        buf = ctypes.wintypes.LPWSTR()
        n = ctypes.windll.kernel32.FormatMessageW(  # type: ignore[attr-defined]
            flags,
            None,
            ctypes.wintypes.DWORD(code),
            0,
            ctypes.cast(ctypes.byref(buf), ctypes.wintypes.LPWSTR),
            0,
            None,
        )
        if not n:
            return None
        try:
            msg = buf.value
            if msg:
                return " ".join(msg.split())
        finally:
            ctypes.windll.kernel32.LocalFree(buf)  # type: ignore[attr-defined]
    except Exception:
        return None
    return None


def _describe_com_exception(e: BaseException) -> str | None:
    """
    Extract useful details from pywintypes.com_error without importing pywintypes directly.
    Typical shape: (hresult, text, excepinfo, argerr)
    """
    args = getattr(e, "args", None)
    if not args or not isinstance(args, tuple):
        return None

    hresult = args[0] if len(args) >= 1 and isinstance(args[0], int) else None
    text = args[1] if len(args) >= 2 else None
    excepinfo = args[2] if len(args) >= 3 else None

    details: list[str] = []
    if hresult is not None:
        details.append(f"HRESULT={hresult} ({_hresult_hex(hresult)})")

        # Common HRESULTs seen from late-binding COM.
        if (hresult & 0xFFFFFFFF) == 0x80070005:
            details.append("Win32=E_ACCESSDENIED(权限被拒绝)")
        elif (hresult & 0xFFFFFFFF) == 0x80004005:
            details.append("E_FAIL(未指定失败)")
        elif (hresult & 0xFFFFFFFF) == 0x80020009:
            details.append("DISP_E_EXCEPTION(被调用端抛异常)")
        elif (hresult & 0xFFFFFFFF) == 0x80020003:
            details.append("DISP_E_MEMBERNOTFOUND(成员不存在)")
        elif (hresult & 0xFFFFFFFF) == 0x80020005:
            details.append("DISP_E_TYPEMISMATCH(参数类型不匹配)")

    if text:
        details.append(f"Text={text!r}")

    # excepinfo sometimes embeds an additional Win32 error code in the 6th slot
    # e.g. (0, None, None, None, 0, -2147024891)
    if isinstance(excepinfo, tuple) and len(excepinfo) >= 6 and isinstance(excepinfo[5], int):
        embedded = excepinfo[5]
        details.append(f"ExcepInfoCode={embedded} ({_hresult_hex(embedded)})")
        # If it looks like an HRESULT, try to decode low word.
        low_word = embedded & 0xFFFF
        msg = _format_win32_message(low_word) or _format_win32_message(embedded & 0xFFFFFFFF)
        if msg:
            details.append(f"Message={msg}")

        if (embedded & 0xFFFFFFFF) == 0x80070005:
            details.append(
                "Hint=可能是权限/UAC/会话隔离导致 HYSYS 拒绝自动化调用；可尝试让 HYSYS 与脚本处于同一权限级别（都以管理员或都非管理员）"
            )

    if not details:
        return None
    return " | ".join(details)


def _com_call(obj: Any, attr: str, *args: Any, **kwargs: Any) -> Any:
    """
    COM late-binding safe call helper.
    """
    try:
        fn = getattr(obj, attr)
    except Exception as e:
        raise HysysComError(f"COM 对象不包含属性/方法 `{attr}`: {obj}") from e
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        com_detail = _describe_com_exception(e)
        detail_suffix = f"\n[COM detail] {com_detail}" if com_detail else ""
        raise HysysComError(
            f"调用 COM 方法失败: {attr}(*{args}, **{kwargs})\n"
            f"[COM object] type={type(obj)!r} repr={obj!r}\n"
            f"[Python exception] {type(e).__name__}: {e}{detail_suffix}"
        ) from e


@dataclass
class HysysSession:
    app: Any
    case: Any
    flowsheet: Any


class HysysComClient:
    """
    Aspen HYSYS V15 COM 连接封装（晚绑定）。
    """

    def __init__(self, *, visible: bool = False, prog_id_candidates: Optional[Iterable[str]] = None, logger=None):
        self.visible = bool(visible)
        self.prog_id_candidates = list(prog_id_candidates or self._default_prog_ids())
        self.logger = logger
        self._win32 = None
        self._pythoncom = None

    def connect(self) -> Any:
        win32, pythoncom = _try_import_win32()
        self._win32 = win32
        self._pythoncom = pythoncom

        pythoncom.CoInitialize()

        last_err: Exception | None = None
        for prog_id in self.prog_id_candidates:
            try:
                # Prefer DispatchEx to force a NEW instance in the current user/session.
                # This avoids accidentally attaching to an existing instance created under
                # a different user / privilege level (common in RDP environments).
                dispatch_mode = "DispatchEx"
                try:
                    app = win32.DispatchEx(prog_id)
                except Exception:
                    dispatch_mode = "Dispatch"
                    app = win32.Dispatch(prog_id)
                try:
                    app.Visible = self.visible
                except Exception:
                    pass
                if self.logger:
                    self.logger.info(f"Connected to HYSYS via {dispatch_mode} ProgID: {prog_id}")
                return app
            except Exception as e:
                last_err = e
                if self.logger:
                    self.logger.warning(f"Failed ProgID `{prog_id}`: {e}")

        raise HysysComError(
            "无法连接到 HYSYS COM。请确认已安装 Aspen HYSYS V15 且可通过 COM 调用。\n"
            f"已尝试 ProgID: {self.prog_id_candidates}"
        ) from last_err

    def new_case(self, app: Any) -> Any:
        if hasattr(app, "SimulationCases"):
            cases = getattr(app, "SimulationCases")
            try:
                case = _com_call(cases, "Add")
                return case
            except Exception:
                pass

        for method in ("NewCase", "New", "Add"):
            try:
                return _com_call(app, method)
            except Exception:
                continue

        raise HysysComError("无法通过 COM 创建新 Case。请检查 HYSYS 版本 COM API。")

    def open_case(self, app: Any, case_path: Path) -> Any:
        case_path = Path(case_path)
        if not case_path.exists():
            raise FileNotFoundError(str(case_path))

        for attr in ("SimulationCases", "Cases", "Documents"):
            if hasattr(app, attr):
                collection = getattr(app, attr)
                for method in ("Open", "OpenCase", "Load"):
                    try:
                        return _com_call(collection, method, str(case_path))
                    except Exception:
                        continue

        for method in ("Open", "OpenCase", "Load"):
            try:
                return _com_call(app, method, str(case_path))
            except Exception:
                continue

        raise HysysComError(f"无法打开 Case: {case_path}")

    def get_flowsheet(self, case: Any) -> Any:
        for attr in ("Flowsheet", "MainFlowsheet", "ActiveFlowsheet"):
            if hasattr(case, attr):
                try:
                    return getattr(case, attr)
                except Exception:
                    continue
        if hasattr(case, "Flowsheets"):
            flowsheets = getattr(case, "Flowsheets")
            for method in ("Item", "GetItem"):
                try:
                    return _com_call(flowsheets, method, 0)
                except Exception:
                    continue
        raise HysysComError("无法获取 Flowsheet。")

    def save_case(self, case: Any, target_path: Path) -> Path:
        target_path = Path(target_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)

        for method in ("SaveAs", "Save"):
            try:
                if method == "SaveAs":
                    _com_call(case, method, str(target_path))
                else:
                    _com_call(case, method)
                return target_path
            except Exception:
                continue
        raise HysysComError(f"无法保存 Case 到: {target_path}")

    def solve(self, case: Any, timeout_s: float = 60.0, poll_s: float = 0.5) -> None:
        for obj in (case, getattr(case, "Solver", None)):
            if obj is None:
                continue
            for method in ("Solve", "Run", "Calculate", "Start"):
                try:
                    _com_call(obj, method)
                    return
                except Exception:
                    continue

        start = time.time()
        while time.time() - start < timeout_s:
            for flag in ("IsSolving", "Solving"):
                if hasattr(case, flag):
                    try:
                        if not bool(getattr(case, flag)):
                            return
                    except Exception:
                        pass
            time.sleep(poll_s)

        raise HysysComError("求解超时或无法触发求解（COM API 未暴露求解入口）。")

    def close(self, app: Any, case: Any | None = None) -> None:
        if case is not None:
            for method in ("Close", "Quit"):
                try:
                    _com_call(case, method)
                    break
                except Exception:
                    continue

        if self._pythoncom is not None:
            try:
                self._pythoncom.CoUninitialize()
            except Exception:
                pass

    @staticmethod
    def _default_prog_ids() -> list[str]:
        return [
            "HYSYS.Application",
            "AspenHYSYS.Application",
            "HYSYS.Application.11",
            "HYSYS.Application.12",
            "HYSYS.Application.13",
            "HYSYS.Application.14",
            "HYSYS.Application.15",
        ]


class DryRunHysysClient:
    """
    用于没有安装 HYSYS 时的演示客户端：返回 COM-like mock 对象，保证流程可跑通。
    """

    def __init__(self, *, logger=None):
        self.logger = logger

    class _Var:
        def __init__(self, value: float | None = None):
            self.Value = value

        def SetValue(self, value: float, unit_hint: str | None = None) -> None:  # noqa: ARG002
            self.Value = float(value)

    class _StreamPortCollection:
        def __init__(self):
            self._streams: list[Any] = []

        def Add(self, stream: Any) -> Any:
            self._streams.append(stream)
            return stream

    class _DryRunMaterialStream:
        def __init__(self, *, name: str):
            self.Name = name
            self.Temperature = DryRunHysysClient._Var(None)
            self.Pressure = DryRunHysysClient._Var(None)
            self.MassFlow = DryRunHysysClient._Var(None)
            self.MolarFlow = DryRunHysysClient._Var(None)
            self.ComponentMolarFraction: dict[str, float] = {}

    class _DryRunUnitOperation:
        def __init__(self, *, unit_type: str, name: str):
            self.Name = name
            self.UnitType = unit_type

            self.InletStreams = DryRunHysysClient._StreamPortCollection()
            self.OutletStreams = DryRunHysysClient._StreamPortCollection()

            self.Temperature = DryRunHysysClient._Var(None)
            self.PressureDrop = DryRunHysysClient._Var(0.0)

            # Conversion reactor candidates
            self.Conversion = None
            self.OverallConversion = None
            self.ConversionFraction = None

            # Gibbs candidates
            self.MinimizeGibbsEnergy = False
            self.DoGibbs = False
            self.Equilibrium = False

            # Equilibrium placeholders
            self.ReactionSet = None
            self.Reactions = []
            self.EquilibriumReactions = []

    class _NamedCollection:
        def __init__(self, kind: str):
            self._kind = kind
            self._items: dict[str, Any] = {}

        def Add(self, *args: Any) -> Any:
            if self._kind == "streams":
                name = str(args[0])
                obj = DryRunHysysClient._DryRunMaterialStream(name=name)
                self._items[name] = obj
                return obj

            # operations: Add(unit_type, name, *extra)
            unit_type = str(args[0])
            name = str(args[1])
            obj = DryRunHysysClient._DryRunUnitOperation(unit_type=unit_type, name=name)
            self._items[name] = obj
            return obj

        def Create(self, *args: Any) -> Any:
            return self.Add(*args)

        def New(self, *args: Any) -> Any:
            return self.Add(*args)

        def Item(self, key: Any) -> Any:
            if isinstance(key, int):
                return list(self._items.values())[key]
            return self._items[str(key)]

        def GetItem(self, key: Any) -> Any:
            return self.Item(key)

    class _DryRunFluidPackages:
        def __init__(self):
            self._active: str | None = None

        def Add(self, name: str) -> Any:
            self._active = str(name)
            return {"Name": self._active}

        def Create(self, name: str) -> Any:
            return self.Add(name)

        def Item(self, name: str) -> Any:
            outer = self

            class _Pkg:
                def __init__(self, n: str):
                    self.Name = n

                def Activate(self) -> None:
                    outer._active = self.Name

            return _Pkg(str(name))

        def GetItem(self, name: str) -> Any:
            return self.Item(name)

    class _DryRunComponents:
        def __init__(self):
            self._components: list[str] = []

        def Add(self, name: str) -> None:
            self._components.append(str(name))

        def AddComponent(self, name: str) -> None:
            self.Add(name)

        def Insert(self, name: str) -> None:
            self.Add(name)

    class _DryRunBasisManager:
        def __init__(self):
            self.FluidPackages = DryRunHysysClient._DryRunFluidPackages()
            self.PropertyPackages = self.FluidPackages
            self.Components = DryRunHysysClient._DryRunComponents()

    class _DryRunSimulationBasis:
        def __init__(self, basis_mgr: Any):
            self.FluidPackages = basis_mgr.FluidPackages
            self.Components = basis_mgr.Components

    class _DryRunFlowsheet:
        def __init__(self):
            ops = DryRunHysysClient._NamedCollection(kind="ops")
            streams = DryRunHysysClient._NamedCollection(kind="streams")

            self.Operations = ops
            self.UnitOperations = ops
            self.OperationsCollection = ops

            self.MaterialStreams = streams
            self.Streams = streams
            self.MaterialStreamCollection = streams

    class _DryRunCase:
        def __init__(self):
            self.BasisManager = DryRunHysysClient._DryRunBasisManager()
            self.SimulationBasis = DryRunHysysClient._DryRunSimulationBasis(self.BasisManager)

            self.Flowsheet = DryRunHysysClient._DryRunFlowsheet()
            self.MainFlowsheet = self.Flowsheet
            self.ActiveFlowsheet = self.Flowsheet

        def SaveAs(self, path: str) -> None:
            Path(path).write_text("DRY_RUN_PLACEHOLDER", encoding="utf-8")

        def Save(self) -> None:
            return None

    class _DryRunApp:
        def __init__(self):
            self.Visible = False

    def connect(self) -> Any:
        if self.logger:
            self.logger.info("[dry-run] connect()")
        return DryRunHysysClient._DryRunApp()

    def new_case(self, app: Any) -> Any:  # noqa: ARG002
        if self.logger:
            self.logger.info("[dry-run] new_case()")
        return DryRunHysysClient._DryRunCase()

    def get_flowsheet(self, case: Any) -> Any:
        if self.logger:
            self.logger.info("[dry-run] get_flowsheet()")
        for attr in ("Flowsheet", "MainFlowsheet", "ActiveFlowsheet"):
            if hasattr(case, attr):
                return getattr(case, attr)
        raise HysysComError("[dry-run] case does not expose Flowsheet.")

    def save_case(self, case: Any, target_path: Path) -> Path:
        if self.logger:
            self.logger.info(f"[dry-run] save_case({target_path})")
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text("DRY_RUN_PLACEHOLDER", encoding="utf-8")
        return target_path

    def solve(self, case: Any, timeout_s: float = 60.0, poll_s: float = 0.5) -> None:  # noqa: ARG002
        if self.logger:
            self.logger.info("[dry-run] solve()")

    def close(self, app: Any, case: Any | None = None) -> None:  # noqa: ARG002
        if self.logger:
            self.logger.info("[dry-run] close()")