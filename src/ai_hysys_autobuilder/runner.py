from __future__ import annotations

import argparse
import json
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, Optional

from hysys_com import DryRunHysysClient, HysysComClient, HysysComError
from logging_utils import setup_logging
from models import HysysRunResult, ReactorSelection, ScenarioSpec
from parameter_config import ParameterAutoConfigurator
from reactor_builders import ReactorFactory
from reactor_selection import NaturalLanguageReactorSelector


def _configure_windows_utf8_console() -> None:
    """
    Make Windows console behave with UTF-8 as much as possible.
    This reduces UnicodeDecodeError/UnicodeEncodeError noise when printing Chinese logs.
    """
    try:
        import sys

        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        # Never fail the run because of console config.
        pass


class AutoHysysBuilder:
    """
    端到端系统封装：
    - 自然语言选型（JSON）
    - HYSYS COM 连接
    - 创建 Conversion/Equilibrium/Gibbs 反应器
    - 参数自动配置
    - 求解 + 结果读取
    """

    def __init__(
        self,
        *,
        output_root: Path,
        visible: bool = False,
        dry_run: bool = False,
        prog_id_candidates: Optional[list[str]] = None,
    ):
        self.output_root = Path(output_root)
        self.visible = bool(visible)
        self.dry_run = bool(dry_run)
        self.prog_id_candidates = prog_id_candidates

        self.output_root.mkdir(parents=True, exist_ok=True)
        self.logger = setup_logging(self.output_root)

        self.selector = NaturalLanguageReactorSelector()
        self.reactor_factory = ReactorFactory(logger=self.logger)
        self.configurator = ParameterAutoConfigurator(logger=self.logger)

        self.hysys = (
            DryRunHysysClient(logger=self.logger)
            if self.dry_run
            else HysysComClient(visible=self.visible, prog_id_candidates=self.prog_id_candidates, logger=self.logger)
        )

    # -----------------------------
    # Scenarios
    # -----------------------------

    @staticmethod
    def exam_scenarios() -> Dict[str, ScenarioSpec]:
        return {
            "1": ScenarioSpec(
                scenario_id="1",
                name="甲烷蒸汽重整",
                description="Steam reforming of methane; equilibrium/multi-reaction system.",
                input_text="场景1：甲烷蒸汽重整 → Gibbs / Equilibrium（自动判断）",
                temperature_c=850.0,
                pressure_kpa=3000.0,
                components=["CH4", "H2O", "CO", "CO2", "H2"],
                property_package="Peng-Robinson",
            ),
            "2": ScenarioSpec(
                scenario_id="2",
                name="乙烷裂解(转化率60%)",
                description="Ethane cracking with specified conversion.",
                input_text="场景2：乙烷裂解，转化率60% → Conversion",
                temperature_c=820.0,
                pressure_kpa=150.0,
                conversion_fraction=0.60,
                components=["C2H6", "C2H4", "H2", "CH4"],
                property_package="Peng-Robinson",
            ),
            "3": ScenarioSpec(
                scenario_id="3",
                name="水煤浆气化",
                description="Coal-water slurry gasification; complex multi-reaction equilibrium.",
                input_text="场景3：水煤浆气化 → Gibbs",
                temperature_c=1200.0,
                pressure_kpa=4000.0,
                components=["H2O", "CO", "CO2", "H2", "CH4", "N2"],
                property_package="Peng-Robinson",
            ),
        }

    # -----------------------------
    # Run pipeline
    # -----------------------------

    def run_scenario(self, scenario: ScenarioSpec) -> HysysRunResult:
        out_dir = self.output_root / f"scenario_{scenario.scenario_id}"
        out_dir.mkdir(parents=True, exist_ok=True)

        # Re-point log file per scenario (keep same logger name but new file handler is avoided by setup_logging)
        self.logger.info(f"=== Running scenario {scenario.scenario_id}: {scenario.name} ===")

        try:
            selection = self.selector.select(scenario)
            self._write_json(out_dir / "selection.json", selection.to_json_dict())

            app = self.hysys.connect()
            case = self.hysys.new_case(app)
            flowsheet = self.hysys.get_flowsheet(case)

            handles = self.reactor_factory.create_reactor(
                flowsheet, reactor_type=selection.reactor_type, name=f"R-{scenario.scenario_id}"
            )

            cfg_report = self.configurator.configure(
                case=case,
                flowsheet=flowsheet,
                reactor_handles=handles,
                scenario=scenario,
                selection=selection,
            )
            self._write_json(out_dir / "config_report.json", asdict(cfg_report))

            # Solve
            self.hysys.solve(case, timeout_s=120.0)

            # Read results (best effort)
            results = self._read_results(selection=selection, handles=handles)
            self._write_json(out_dir / "results.json", results.outputs)

            # Save case
            case_path = out_dir / f"scenario_{scenario.scenario_id}.hsc"
            saved = self.hysys.save_case(case, case_path)

            results.raw_paths["case_path"] = str(saved)
            results.ok = True
            results.message = "Success"
            return results

        except Exception as e:
            self.logger.error(f"Scenario {scenario.scenario_id} failed: {e}")
            self.logger.error(traceback.format_exc())
            return HysysRunResult(
                ok=False,
                message=str(e),
                outputs={
                    "error": str(e),
                    "traceback": traceback.format_exc(),
                },
            )

    def run_all(self) -> Dict[str, HysysRunResult]:
        results: Dict[str, HysysRunResult] = {}
        for sid, scenario in self.exam_scenarios().items():
            results[sid] = self.run_scenario(scenario)
        return results

    # -----------------------------
    # Results reader (best effort)
    # -----------------------------

    def _read_results(self, *, selection: ReactorSelection, handles: Any) -> HysysRunResult:
        prod = handles.product_stream
        out: Dict[str, Any] = {
            "scenario_id": selection.scenario_id,
            "scenario_name": selection.scenario_name,
            "reactor_type": selection.reactor_type.value,
            "confidence": selection.confidence,
        }

        # Attempt common stream properties
        for key, attrs in (
            ("temperature_c", ("Temperature", "T")),
            ("pressure_kpa", ("Pressure", "P")),
            ("mass_flow", ("MassFlow", "W", "Mass Flow")),
            ("molar_flow", ("MolarFlow", "Molar Flow")),
        ):
            val = self._try_read_scalar(prod, attrs)
            if val is not None:
                out[key] = val

        # Attempt composition
        comp = self._try_read_composition(prod)
        if comp:
            out["composition"] = comp

        return HysysRunResult(ok=True, message="Solved", outputs=out)

    def _try_read_scalar(self, obj: Any, attrs: tuple[str, ...]) -> Optional[float]:
        for attr in attrs:
            if hasattr(obj, attr):
                try:
                    v = getattr(obj, attr)
                    if hasattr(v, "Value"):
                        return float(v.Value)
                    if isinstance(v, (int, float)):
                        return float(v)
                except Exception:
                    continue
        return None

    def _try_read_composition(self, stream: Any) -> Dict[str, float]:
        """
        Best-effort composition extraction.
        Typical HYSYS stream provides component fractions under stream.ComponentMolarFraction or similar.
        """
        candidates = (
            "ComponentMolarFraction",
            "MolarComposition",
            "Composition",
            "CompMoleFrac",
        )
        for attr in candidates:
            if hasattr(stream, attr):
                try:
                    comp_obj = getattr(stream, attr)
                    # comp_obj may be dict-like or a collection with Item()
                    if isinstance(comp_obj, dict):
                        return {str(k): float(v) for k, v in comp_obj.items()}
                    if hasattr(comp_obj, "Count") and hasattr(comp_obj, "Item"):
                        out = {}
                        for i in range(int(comp_obj.Count)):
                            item = comp_obj.Item(i)
                            name = getattr(item, "Name", f"Comp{i}")
                            val = getattr(item, "Value", None)
                            out[str(name)] = float(val) if val is not None else 0.0
                        return out
                except Exception:
                    continue
        return {}

    # -----------------------------
    # IO helpers
    # -----------------------------

    def _write_json(self, path: Path, data: Any) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="AI reactor selection + HYSYS V15 COM auto builder")
    p.add_argument("--run-all", action="store_true", help="Run all 3 exam scenarios")
    p.add_argument("--scenario", choices=["1", "2", "3"], help="Run one scenario")
    p.add_argument("--dry-run", action="store_true", help="Do not call HYSYS COM; only generate outputs")
    p.add_argument("--visible", action="store_true", help="Show HYSYS window (real COM only)")
    p.add_argument("--out", default="outputs", help="Output directory")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    _configure_windows_utf8_console()

    p = build_arg_parser()
    args = p.parse_args(argv)
    out_root = Path(args.out).resolve()

    print(f"[runner] started  out={out_root}  dry_run={bool(args.dry_run)}  visible={bool(args.visible)}")

    builder = AutoHysysBuilder(output_root=out_root, visible=args.visible, dry_run=args.dry_run)

    if args.run_all:
        print("[runner] running all scenarios...")
        all_results = builder.run_all()
        # also write summary
        builder._write_json(out_root / "summary.json", {k: v.to_json_dict() for k, v in all_results.items()})
        print(f"[runner] done. summary={out_root / 'summary.json'}")
        return 0 if all(r.ok for r in all_results.values()) else 2

    if args.scenario:
        print(f"[runner] running scenario {args.scenario}...")
        scenario = builder.exam_scenarios()[args.scenario]
        r = builder.run_scenario(scenario)
        builder._write_json(out_root / "summary.json", {args.scenario: r.to_json_dict()})
        print(f"[runner] done. summary={out_root / 'summary.json'}")
        return 0 if r.ok else 2

    # argparse-friendly error: prints usage + message and exits with code 2
    p.error("Please specify --run-all or --scenario {1,2,3}.")
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as e:
        # If someone runs this file directly, always print a visible traceback.
        print("[runner] FATAL:", e)
        print(traceback.format_exc())
        raise SystemExit(1)

