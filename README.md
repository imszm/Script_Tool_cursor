# ppx_testkit — 嵌入式整机测试工具集

用于四轮电动代步车和电动轮椅的自动化测试：继电器开关机与充电压测、舵机刷卡、灯板与主控白盒、刹车专项、SMT/FCT、总装升级，以及车速、时间差等小工具。

旧脚本仍留在原目录，实机核对通过后再删除。新入口统一为 `ppx-test`。

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

`config/local.yaml` 只放本机串口号，不入库。

## 运行

```bat
scripts\run_station.bat relay_power_cycle --port relay=COM3 --port device=COM13
```

等价命令：

```bash
ppx-test list
ppx-test ports
ppx-test show relay_power_cycle
ppx-test run relay_power_cycle --set test.cycles=10
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
2. `config/stations/<工位>.yaml`（可用 `include:` 引入公共片段）
3. `config/local.yaml`（顶层对所有工位生效，`stations.<工位名>` 只对该工位生效）
4. 环境变量 `PPX__A__B`
5. 命令行 `--set a.b=value` 或 `--port 名称=COM5`

未知字段、类型不符、缺少必填项都会在启动时失败，不会带着错误配置跑测试。

## 目录

```
config/stations/          工位配置
src/ppx_testkit/
  cli.py                  ppx-test 入口
  settings.py             配置加载
  logger.py               每次运行独立日志目录
  apps/                   各工位流程（只编排，不直接操作 pyserial）
  core/                   串口、继电器、关键字监控、PPX v1/v2、压测执行器、报告、GUI
resources/                DLL、头文件、图片、用例模板
tests/                    无硬件单测；需要实机的用例标记 hardware
scripts/                  Windows 环境安装与工位启动
```

应用模块实现 `run(settings, ctx) -> int`。串口名在配置的 `serial.<名称>` 下，继电器用 `relay`，设备日志用 `device`。

## 工位一览

| 工位 | 应用 | 来源 |
| --- | --- | --- |
| `relay_power_cycle` / `nfc_power_cycle` / `handle_power_cycle` | 继电器开关机 | `Tool/继电器开关机压力测试.py` 等 |
| `w3_power_button` | W3 开关机（精确匹配） | `Tool/W3继电器开关机压力测试.py` |
| `l5_charge_cycle` / `r3_charge_cycle` | 充电压测 | `Tool/继电器充电压力测试_L5.py`、`_R3.py` |
| `servo_nfc` | 舵机刷 NFC | `Tool/舵机压力测试_V1_1.py` |
| `l5_lcb_ble` | L5 灯板 BLE 用例 | BLE 自动化工具 V1.6 |
| `mcb_whitebox` / `l5_mcb_hall_diag` | L5 MCB 白盒 / 霍尔诊断 | mcb V1.4.6 / V1.4.7 |
| `r3_mcb_whitebox` | R3 MCB 白盒 | `PPX_MCB_V2.PY` |
| `lrd_debug` | 灯板单次点灯并回读 | `Tool/LRD调试程序.py` |
| `r3_brake_lock_unlock` | 电磁刹车解锁上锁 | 优化完整版（超温保护默认关闭） |
| `r3_leb_fct` / `r3_mcb_fct` / `p3_fct` | SMT/FCT | 对应系列脚本 |
| `r3_assembly_upgrade` | R3 总装升级 | `R3组装升级.py` |
| `l5_pctool_stress` / `l5_fixture_relay_stress` | L5 PC 工具压力 | V1.2 / V1.3 |
| `w3_pctool_stress` / `w3_assembly_stress` | W3 点击压力 | 升级工具、组装生产工具 |
| `turn_signal` / `headlight` | 转向灯、前灯 | `Tool/TestTurnSignals`、`TestHeadlights` |
| `speed_calc` / `time_diff` | 车速、时间差 | `Tool/` 下对应脚本 |

FCT、点击压测里的坐标是占位值，上线前用 `mouse_locate` 和 `inspect_controls` 在本机核对。

## 开发

```bash
ruff check src tests
python -m pytest
python -m pytest -m hardware   # 需要连接真实设备，默认不跑
```

协议结构体与 `resources/include` 下的 C 头文件逐字段比对。L5 与 R3 的 `ppx_region.dll` 同名但接口不同，分别放在 `resources/dll/l5` 和 `resources/dll/r3`。
