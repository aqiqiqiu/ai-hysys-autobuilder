import win32com.client as win32

# 连接 HYSYS
hysys = win32.GetActiveObject("HYSYS.Application")
case = hysys.ActiveDocument
fs = case.Flowsheet

stream = fs.MaterialStreams.Item("4")
print(f"流股: {stream.Name}")
print(f"温度: {stream.Temperature.Value:.2f} °C")
print(f"压力: {stream.Pressure.Value:.2f} kPa")
print(f"总摩尔流量: {stream.MolarFlow.Value:.4f} kmol/h")

# 获取组分数组
comp_mf = stream.ComponentMolarFraction
# 方式1: 直接取 Values 属性（返回数组）
try:
    values = comp_mf.Values  # 这是一个元组/列表
    print("Values 类型:", type(values))
    print("Values 长度:", len(values) if hasattr(values, '__len__') else '?')
except Exception as e:
    print(f"读取 Values 失败: {e}")
    values = None

# 获取组分名称（从流体包中读取）
try:
    fp = stream.FluidPackage
    if fp is None:
        # 从案例获取默认物性包
        fp = case.BasisManager.FluidPackages.Item(0)
    comps = fp.Components
    comp_names = []
    for i in range(comps.Count):
        comp_names.append(comps.Item(i).Name)
    print("组分列表:", comp_names)
except Exception as e:
    print(f"读取组分列表失败: {e}")
    comp_names = []

# 如果 values 可用且长度与组分数量一致，打印
if values is not None and len(values) == len(comp_names):
    print("\n出口摩尔分数:")
    for name, val in zip(comp_names, values):
        print(f"  {name}: {val:.6f}")
else:
    # 备用方案：尝试 GetValues 方法返回数组
    try:
        arr = comp_mf.GetValues()
        # 可能需要转换为列表
        print("GetValues 结果:", arr)
    except:
        print("无法通过 GetValues 获取组成")