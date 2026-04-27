import win32com.client

# 连接到已打开的 HYSYS（确保已打开并有一个活动案例）
app = win32com.client.GetActiveObject("HYSYS.Application")
case = app.ActiveDocument
fs = case.Flowsheet

# 创建三个物流
feed = fs.MaterialStreams.Add("Feed")
product1 = fs.MaterialStreams.Add("Product1")
product2 = fs.MaterialStreams.Add("Product2")

print("三个物流创建成功：")
print(f"  - {feed.Name}")
print(f"  - {product1.Name}")
print(f"  - {product2.Name}")