# Script Tool (工程化版)

## 安装

建议使用虚拟环境：

```bash
python -m venv .venv
.\.venv\Scripts\activate
pip install -r requirements.txt
```

## 运行（统一入口）

查看帮助：

```bash
python -m script_tool --help
```

列出串口：

```bash
python -m script_tool w3-power --list-ports
```

## 站点配置（推荐）

你可以把本机的 COM 口、窗口标题、坐标等放到一个 JSON 配置文件里，然后运行时通过 `--config` 加载。

示例（`configs/station.json`）：

```json
{
  "serial": {
    "relay_port": "COM12",
    "device_port": "COM14",
    "relay_ccb_port": "COM12"
  },
  "pc_tool": {
    "upgrade_app_title": "L5 PCTOOL V3.9.00",
    "ccb_title_regex": "CCB 测试 V3.2.00.*"
  }
}
```

使用方式：

```bash
python -m script_tool --config configs/station.json charging --loops 100
```

### W3 继电器开关机压力测试

```bash
python -m script_tool w3-power --loops 1000 --relay-keyword COM4 --device-keyword cp210x
```

### 继电器充电压力测试

```bash
python -m script_tool charging --loops 1000 --relay-keyword COM4 --device-keyword cp210x
```

### PC 升级工具自动化

```bash
python -m script_tool pc-upgrade --loops 100
```

### CCB SMT 自动化

```bash
python -m script_tool ccb-smt --loops 100 --relay-ccb-keyword COM12
```

### 总装：W3 PCTOOL 压力测试（工程化入口）

```bash
python -m script_tool w3-pc-tool-stress --loops 1000
```

### 治具&上位机：转向灯继电器压力测试（工程化入口）

```bash
python -m script_tool fixture-turn-signal --loops 50 --relay-port COM4
```

### 治具CCB SMT：像素识别+继电器版本（工程化入口）

```bash
python -m script_tool ccb-smt-fuzzy --loops 100 --relay-ccb-port COM12
```

## 输出与日志

每次运行会生成一个目录（默认：`runs/YYYYMMDD_HHMMSS_<task>/`），包括：

- `logs/full_*.log`：全程完整日志
- `logs/exception_*.log`：异常/错误日志（ERROR 及以上）
- `summary.json`：结构化汇总（方便统计与追溯）
- `artifacts/`：截图等产物

## 兼容旧脚本

`Tool/` 下的关键脚本已改为“薄封装”，保留原文件名与运行方式，但内部优先调用 `python -m script_tool ...` 的实现；若新入口异常，会自动回退到旧实现（保障现场可用）。

同样，以下目录内的部分入口脚本也已改为薄封装（保留文件名与运行方式）：

- `总装测试工具/`
- `治具&上位机压力测试工具/`
- `治具CCB SMT测试V3.0.0/`

## 旧框架说明（src/）

仓库中仍保留 `src/` 目录（历史实现）。现在推荐统一使用 `script_tool/` 作为唯一可维护入口；`main.py` 也已改为调用 `script_tool` 的交互式兼容入口，避免两套框架同时演进导致漂移。

## 添加新脚本（工程化规范）

1. 在 `script_tool/tasks/` 新建一个任务模块，实现 `setup/run/teardown`（参考 `w3_power.py`）。
2. 在 `script_tool/tasks/registry.py` 注册子命令名。
3. 在 `script_tool/cli.py` 增加子命令参数（如需要）。
