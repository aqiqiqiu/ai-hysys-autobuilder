#!/usr/bin/env python3
"""
Interactive AI Reactor Selector
完全依赖大模型理解用户输入，自动提取温度、压力、转化率等参数。
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from models import ScenarioSpec
from reactor_selection import NaturalLanguageReactorSelector

def main():
    print("=" * 60)
    print("🤖 AI 反应器选型助手（自然语言 → 反应器类型 + 参数提取）")
    print("=" * 60)
    raw_text = input("请输入对反应过程的自然语言描述：\n").strip()
    if not raw_text:
        print("❌ 未输入任何内容，退出。")
        return

    scenario = ScenarioSpec(
        scenario_id="interactive",
        name="用户自定义场景",
        description="由用户输入的描述",
        input_text=raw_text,
        # 无需提供温度压力，让大模型从文本中提取
    )

    print("\n⏳ 正在调用 AI 模型进行分析，请稍候...")
    selector = NaturalLanguageReactorSelector()
    try:
        selection = selector.select(scenario)
    except Exception as e:
        print(f"❌ 选型失败: {e}")
        return

    print("\n" + "=" * 60)
    print("📋 AI 选型结果")
    print("=" * 60)
    print(f"反应器类型: {selection.reactor_type.value}")
    print(f"置信度: {selection.confidence:.2f}")
    print("理由:")
    for i, reason in enumerate(selection.rationale, 1):
        print(f"  {i}. {reason}")
    if selection.suggested_hysys.get("temperature_c") is not None:
        print(f"推断温度: {selection.suggested_hysys['temperature_c']} °C")
    if selection.suggested_hysys.get("pressure_kpa") is not None:
        print(f"推断压力: {selection.suggested_hysys['pressure_kpa']} kPa")
    if selection.suggested_hysys.get("conversion_fraction") is not None:
        print(f"推断转化率: {selection.suggested_hysys['conversion_fraction']:.2f}")

    output_path = Path("selection_result.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(selection.to_json_dict(), f, ensure_ascii=False, indent=2)
    print(f"\n✅ 结果已保存至: {output_path.resolve()}")

if __name__ == "__main__":
    main()