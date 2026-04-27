"""
AI 反应器助手 - 自动设置 Feed 参数 + 读取两个产品物流 + 保存完整结果
用户需手动：添加 Feed 组分、创建反应器、连接物流、按 F5 求解
"""

import json
import re
import time
import win32com.client as win32
import pythoncom
import sys
from pathlib import Path

# 将当前目录加入模块搜索路径，以便导入项目模块
sys.path.insert(0, str(Path(__file__).parent))
from models import ScenarioSpec
from reactor_selection import NaturalLanguageReactorSelector


def ensure_stream(flowsheet, name):
    """如果物流不存在则创建，返回物流对象"""
    try:
        stream = flowsheet.MaterialStreams.Item(name)
        print(f"✅ 物流 '{name}' 已存在")
    except:
        stream = flowsheet.MaterialStreams.Add(name)
        print(f"✅ 已创建物流 '{name}'")
    return stream


def is_stream_composition_ready(stream):
    """检查物流是否已设置组分（摩尔分数总和接近1）"""
    try:
        comp_mf = stream.ComponentMolarFraction
        values = comp_mf.Values
        total = sum(values)
        return 0.99 < total < 1.01
    except:
        return False


def wait_for_feed_composition(feed):
    """等待用户手动设置 Feed 的组分"""
    print("\n⚠️ 请手动在 HYSYS 中为物流 'Feed' 添加组分及摩尔分数。")
    print("   操作：双击 Feed → Composition 页面 → 输入各组分摩尔分数（总和为1）。")
    print("   组分名称必须与物性包中的名称完全一致。")
    while True:
        if is_stream_composition_ready(feed):
            print("✅ 检测到 Feed 组分已设置！")
            break
        print("尚未检测到有效组分，请继续设置...")
        time.sleep(2)
    input("按 Enter 继续，脚本将自动设置 Feed 的温度、压力和流量...")


def extract_temperature(text):
    """从文本中提取温度（摄氏度），返回浮点数或 None"""
    # 匹配 450°C, 450 C, 450℃, 450摄氏度
    m = re.search(r'(\d+(?:\.\d+)?)\s*°?\s*[Cc]', text)
    if m:
        return float(m.group(1))
    return None


def extract_pressure(text):
    """从文本中提取压力，转换为 kPa，返回浮点数或 None"""
    # 匹配 2 atm, 2atm, 2 bar, 2bar, 2 barg
    m = re.search(r'(\d+(?:\.\d+)?)\s*(atm|bar|barg)', text, re.IGNORECASE)
    if m:
        val = float(m.group(1))
        unit = m.group(2).lower()
        if unit == 'atm':
            return val * 101.325
        elif 'bar' in unit:
            return val * 100.0
    return None


def extract_molar_flow(text):
    """从文本中提取摩尔流量（kmol/h），返回浮点数或 None"""
    # 匹配 200 kmol/h, 200 kmol/h
    m = re.search(r'(\d+(?:\.\d+)?)\s*kmol/h', text, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return None


def set_feed_conditions(feed, suggested, user_input):
    """设置进料物流的温度、压力、流量（优先使用 AI 建议值，否则从文本提取，最后用默认值）"""
    # 温度
    temp = suggested.get('temperature_c')
    if temp is None:
        temp = extract_temperature(user_input)
        if temp is not None:
            print(f"🔍 从描述中提取到温度: {temp} °C")
    if temp is not None:
        feed.Temperature.Value = temp
        print(f"🌡️ 已设置进料温度: {temp} °C")
    else:
        print("⚠️ 未提供温度值，请手动设置")

    # 压力
    press = suggested.get('pressure_kpa')
    if press is None:
        press = extract_pressure(user_input)
        if press is not None:
            print(f"🔍 从描述中提取到压力: {press:.2f} kPa")
    if press is not None:
        feed.Pressure.Value = press
        print(f"⚙️ 已设置进料压力: {press:.2f} kPa")
    else:
        print("⚠️ 未提供压力值，请手动设置")

    # 流量
    flow = suggested.get('molar_flow')
    if flow is None:
        flow = extract_molar_flow(user_input)
        if flow is not None:
            print(f"🔍 从描述中提取到摩尔流量: {flow} kmol/h")
    if flow is not None:
        feed.MolarFlow.Value = flow
        print(f"📊 已设置进料摩尔流量: {flow} kmol/h")
    else:
        # 默认值
        feed.MolarFlow.Value = 200.0
        print(f"📊 已设置进料摩尔流量（默认）: 200 kmol/h")


def read_product_results(prod, name=""):
    """读取产品物流的结果，返回字典（温度、压力、流量、组成）"""
    try:
        temp = prod.Temperature.Value
        press = prod.Pressure.Value
        flow = prod.MolarFlow.Value

        # 读取组成
        comp_mf = prod.ComponentMolarFraction
        values = comp_mf.Values
        fp = prod.FluidPackage
        if fp is None:
            case = prod.Parent.Parent
            fp = case.BasisManager.FluidPackages.Item(0)
        comps = fp.Components
        comp_names = [comps.Item(i).Name for i in range(comps.Count)]
        comp_dict = dict(zip(comp_names, values))

        result = {
            "temperature_c": temp,
            "pressure_kpa": press,
            "molar_flow_kmolh": flow,
            "composition": comp_dict
        }
        print(f"{name}出口温度: {temp:.2f} °C")
        print(f"{name}出口压力: {press:.2f} kPa")
        print(f"{name}总摩尔流量: {flow:.4f} kmol/h")
        if comp_dict:
            print(f"{name}出口摩尔分数:")
            for k, v in comp_dict.items():
                print(f"  {k}: {v:.6f}")
        return result
    except Exception as e:
        print(f"读取{name}结果失败: {e}")
        return None


def main():
    print("=" * 60)
    print("🤖 AI 反应器助手（自动设置 Feed 参数 + 读取两个产品物流）")
    print("用户需手动：添加 Feed 组分、创建反应器、连接物流、按 F5 求解")
    print("=" * 60)

    user_input = input("请输入对反应过程的自然语言描述：\n").strip()
    if not user_input:
        print("未输入内容，退出。")
        return

    # 1. AI 选型（调用大模型或规则版）
    scenario = ScenarioSpec(
        scenario_id="interactive",
        name="用户场景",
        description="",
        input_text=user_input,
    )
    selector = NaturalLanguageReactorSelector()
    try:
        selection = selector.select(scenario)
    except Exception as e:
        print(f"❌ AI 选型失败: {e}")
        return

    print("\n" + "=" * 60)
    print("📋 AI 选型结果（供您参考）")
    print("=" * 60)
    print(f"反应器类型: {selection.reactor_type.value}")
    print(f"置信度: {selection.confidence:.2f}")
    print("理由:")
    for r in selection.rationale:
        print(f"  - {r}")

    suggested = selection.suggested_hysys

    # 2. 连接 HYSYS
    pythoncom.CoInitialize()
    try:
        app = win32.GetActiveObject("HYSYS.Application")
        case = app.ActiveDocument
        fs = case.Flowsheet
        print("\n✅ 已连接到 HYSYS，当前案例:", case.Name)
    except Exception as e:
        print(f"❌ 连接 HYSYS 失败: {e}")
        pythoncom.CoUninitialize()
        return

    # 3. 创建物流
    feed = ensure_stream(fs, "Feed")
    prod_vap = ensure_stream(fs, "Prod_Vap")
    prod_liq = ensure_stream(fs, "Prod_Liq")
    print("✅ 已创建进料物流 'Feed' 和产品物流 'Prod_Vap', 'Prod_Liq'")

    # 4. 等待用户手动设置 Feed 组分
    wait_for_feed_composition(feed)

    # 5. 自动设置 Feed 的温度、压力、流量
    set_feed_conditions(feed, suggested, user_input)

    # 6. 提示用户手动创建反应器并连接
    print("\n🔧 请确保您已完成以下手动操作：")
    print("   - 在流程图中创建反应器（类型参照 AI 建议）")
    print("   - 将 Feed 连接到反应器进口")
    print("   - 将反应器的气相出口连接到 Prod_Vap，液相出口连接到 Prod_Liq")
    if selection.reactor_type.value == "Conversion":
        print("   - 如果使用 Conversion 反应器，请手动在反应器中设置转化率")
    input("完成上述操作后，按 Enter 继续...")

    # 7. 等待用户按 F5 求解
    input("\n⏳ 请在 HYSYS 中按 F5 运行求解，完成后按 Enter 继续...")

    # 8. 读取产品物流结果
    print("\n📊 产品物流结果：")
    result_vap = read_product_results(prod_vap, "气相 ")
    result_liq = read_product_results(prod_liq, "液相 ")

    pythoncom.CoUninitialize()

    # 9. 保存完整结果到 JSON 文件
    output = {
        "ai_selection": selection.to_json_dict(),
        "product_vapor": result_vap,
        "product_liquid": result_liq,
        "user_input": user_input
    }
    out_file = Path("selection_result.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    print(f"\n✅ 选型结果及产品结果已保存至 {out_file.resolve()}")


if __name__ == "__main__":
    main()