# AI 自动反应器选型 + Aspen HYSYS V15 自动化建模系统

本项目提供一套可直接运行、可提交 GitHub 的工程化实现，覆盖以下 5 大模块：

- 自然语言反应器选型（输出 JSON）
- HYSYS COM 连接
- 三种反应器创建：Conversion / Equilibrium / Gibbs
- 参数自动配置
- 运行求解 + 结果读取

并内置 3 个考核场景，一键运行：

- 场景1：甲烷蒸汽重整 → Gibbs / Equilibrium（自动判断）
- 场景2：乙烷裂解，转化率 60% → Conversion
- 场景3：水煤浆气化 → Gibbs

## 环境要求

- Windows 10/11
- Python 3.10+（建议 3.11）
- Aspen HYSYS V15 已安装（可选：没有 HYSYS 时可用 `--dry-run` 演示全流程）

## 安装

在项目根目录执行：

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

如果要安装为可复用包（可选）：

```bash
pip install -e .
```

## 一键运行（三场景）

### 真实连接 HYSYS（需要已安装 HYSYS）

```bash
python -m ai_hysys_autobuilder --run-all --visible
```

### 演示模式（不依赖 HYSYS，可录屏）

```bash
python -m ai_hysys_autobuilder --run-all --dry-run
```

## 运行参数

- `--run-all`：顺序运行 3 个场景
- `--scenario {1,2,3}`：只运行指定场景
- `--dry-run`：不调用 COM，仅输出将要执行的建模步骤与 JSON（可用于 CI/演示）
- `--visible`：让 HYSYS 窗口可见（真实 COM 时有效）
- `--save-dir <path>`：保存 `.hsc` 的目录（真实 COM 时有效）

## 输出

每个场景会输出：

- `selection.json`：自然语言选型 JSON
- `actions.log`：建模步骤与异常日志
- `results.json`：求解后读取的关键结果（若真实 HYSYS 可用）

## 重要说明（工程边界）

HYSYS 的 COM API 在不同版本/安装环境中 ProgID、对象模型可能存在差异。本项目采取：

- **晚绑定（late-binding）**：尽量不依赖类型库
- **强异常信息**：失败时输出清晰的对象路径与建议
- **dry-run**：即使无 HYSYS 也可完整演示端到端流程

你可以在 `src/ai_hysys_autobuilder/hysys_com.py` 中调整 `prog_id_candidates` 以适配你的 HYSYS 安装。

