# ppx_testkit — 嵌入式整机测试工具集

用于四轮电动代步车和电动轮椅的自动化测试：继电器开关机与充电压测、舵机刷卡、灯板与主控白盒、刹车专项、SMT/FCT、总装升级，以及车速、时间差等小工具。

旧脚本仍留在原目录，只作只读对照。实机核对通过后再归档，不从本仓库删除产品脚本。新入口统一为 `ppx-test`。目录怎么读、一条命令怎么执行，见 [使用说明书](docs/使用说明书.md)。

## 环境

- Python 3.11 及以上（开发与 CI 使用 3.11）
- Windows 工位需要串口、继电器，以及可选的图形界面依赖

```bat
scripts\setup_env.bat
```

或手动：

```bash
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[win,gui,report,dev]"
copy config\local.yaml.example config\local.yaml
```

`config/local.yaml` 只放本机串口号，不入库。工位 yaml 不写死 `COM` 口；用 `--port 名称=COM5` 或 `local.yaml` 覆盖。

## 运行

双击 `scripts\run_station.bat` 只列出工位，然后停在按键提示，不会开始测试。要跑工位，在项目根目录打开终端再输入命令。

第一次接通硬件时加上次数限制。`relay_power_cycle` 的默认次数是 20 万轮，失败时继电器保持上电：

```bat
scripts\run_station.bat relay_power_cycle --set test.cycles=2 --port relay=COM3 --port device=COM13
```

`COM3`、`COM13` 只是旧脚本示例。本机端口用 `ppx-test ports` 查看，并写进 `config\local.yaml`。

等价命令：

```bash
ppx-test list
ppx-test ports
ppx-test show relay_power_cycle
ppx-test run relay_power_cycle --set test.cycles=2
```

退出码：0 通过，1 未通过，2 配置错误，3 硬件或通信故障，130 用户中断。

每次运行在 `logs/<工位>/<YYYYmmdd_HHMMSS>/` 下保存：

| 文件 | 内容 |
| --- | --- |
| `full.log` | 全程日志，含 DEBUG 与串口收发十六进制 |
| `error.log` | WARNING 及以上 |
| `device_raw.log` | 被测设备原始串口输出（按大小轮转） |
| `config.yaml` | 本次生效配置 |
| `summary.json` | 运行统计 |

`scripts\run_station.bat` 另外把控制台输出保存到 `logs/console_<工位>_<时间戳>.log`。

## 配置优先级

后者覆盖前者：

1. `config/default.yaml`
2. `config/stations/<工位>.yaml`（可用 `include:` 引入 `config/stations/_fragments/` 公共片段）
3. `config/local.yaml`（顶层对所有工位生效，`stations.<工位名>` 只对该工位生效）
4. 环境变量 `PPX__A__B`
5. 命令行 `--set a.b=value` 或 `--port 名称=COM5`

已注册的配置段在启动时校验：未知字段、类型不符、缺少必填项都会失败，不会带着错误配置跑测试。

## 目录

```
config/stations/          工位配置
config/stations/_fragments/  继电器口、公共关键字等片段
src/ppx_testkit/
  cli.py                  ppx-test 入口
  settings.py             配置加载与启动校验
  logger.py               每次运行独立日志目录
  apps/                   各工位流程（只编排，不直接操作 pyserial / ctypes）
  core/                   串口、继电器、关键字监控、PPX v1/v2、压测执行器、报告、GUI
resources/                DLL、头文件、图片、用例模板
tests/                    无硬件单测；需要实机的用例标记 hardware
scripts/                  Windows 环境安装与工位启动
```

应用模块实现 `run(settings, ctx) -> int`。串口名在配置的 `serial.<名称>` 下，继电器用 `relay` 或 `relays.<名称>`，设备日志用 `device`。

L5 与 R3 的 `ppx_region.dll` 同名但接口不同，分别放在 `resources/dll/l5` 和 `resources/dll/r3`，不合并加载器。

## 工位一览

| 工位 | 应用 | 来源 |
| --- | --- | --- |
| `relay_power_cycle` / `nfc_power_cycle` / `handle_power_cycle` | 继电器开关机 | `Tool/继电器开关机压力测试.py`、`NFC开关机异常关键字检测.py`、`把手自动化开关.py` |
| `relay_probe` | 继电器通道手动点动 | `Tool/查看继电器状态.py` |
| `w3_power_button` | W3 开关机（精确匹配） | `Tool/W3继电器开关机压力测试.py` |
| `l5_charge_cycle` / `r3_charge_cycle` | 充电压测 | `Tool/继电器充电压力测试_L5.py`、`_R3.py` |
| `servo_nfc` | 舵机刷 NFC | `Tool/舵机压力测试_V1_1.py` |
| `l5_lcb_ble` | L5 灯板 BLE 用例 | BLE 自动化工具 V1.6 |
| `mcb_whitebox` / `l5_mcb_hall_diag` | L5 MCB 白盒 / 霍尔诊断 | mcb V1.4.6 / V1.4.7 |
| `r3_mcb_whitebox` | R3 MCB 白盒 | `PPX_MCB_V2.PY` |
| `lrd_debug` | 灯板单次点灯并回读 | `Tool/LRD调试程序.py` |
| `r3_brake_lock_unlock` | 电磁刹车解锁上锁 | 优化完整版，单线程顺序执行；超温保护默认关闭 |
| `r3_leb_fct` / `r3_mcb_fct` / `p3_fct` | SMT/FCT | 对应系列脚本 |
| `r3_assembly_upgrade` | R3 总装升级 | `R3组装升级.py` |
| `l5_pctool_stress` | W3 PC 工具按钮压力 | `PC_tool_工具软件压力测试-V1.2.py`（窗口 `W3 PCTOOL V5.5.00`） |
| `l5_fixture_relay_stress` | 治具左右灯（ICSE，无窗口） | `治具工具软件压力测试（带继电器版）-V1.3.py` |
| `w3_pctool_stress` / `w3_assembly_stress` | W3 点击压力 | 同一来源：`Tool/组装生产工具压力测试.py` |
| `upgrade_log_stress` | L5 升级日志关键字压力 | `Tool/升级工具压力自动化测试 - 可截图版本.py` |
| `inspect_controls` | 读取窗口控件 | `识别读取应用窗口控件.py` |
| `mouse_locate` | 鼠标坐标采样 | `Tool/鼠标定位脚本.py` |
| `turn_signal` / `headlight` | 转向灯、前灯 | `Tool/TestTurnSignals`、`TestHeadlights` |
| `speed_calc` / `time_diff` | 车速、时间差 | `Tool/` 下对应脚本 |

FCT 和 W3 点击压测里的坐标是旧脚本在原屏幕上的绝对坐标。换电脑或改窗口位置后，用 `mouse_locate` 重采，控件名用 `inspect_controls` 核对。

`r3_brake_lock_unlock` 相对最早的双线程加本地 TCP 版本，已改为单线程顺序执行。并发路径不恢复，差异留到实机核对。

## 旧脚本（只读对照）

下列文件不再作为入口，也不再移植。产品行为以 `ppx-test run <工位>` 为准。本轮不删除这些文件，也不从 git 历史抹掉。`libs/` 下已移除的副本不要加回来。

- `L5_CCB&MCB白盒测试` 中 MCB V1.1–V1.4.5、BLE V1.1–V1.5，以及 `libcs_mcb/libs/测试.py`。对照保留 MCB V1.4.6、V1.4.7 与 BLE V1.6。
- `R3电机专项测试/` 里与 `resources/dll/r3` 哈希不一致的 DLL 和头文件。工位只加载 `resources/dll/r3` 与 `resources/include/r3`。
- `R3电机专项测试/电磁刹车解锁和上锁测试.py`（非优化完整版）。对照保留优化完整版，工位为 `r3_brake_lock_unlock`。

## 开发

```bash
ruff check src tests
python -m pytest
python -m pytest -m hardware   # 需要连接真实设备，默认不跑
```

协议结构体与 `resources/include` 下的 C 头文件逐字段比对。
