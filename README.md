# ppx_testkit — 嵌入式整机测试工具集

用于四轮电动代步车和电动轮椅的自动化测试：FCT、白盒协议、继电器上下电、PC 工具点击、灯光、电机、充电和舵机，以及坐标、车速等辅助工具。

环境：Python 3.11 及以上。Windows 工位需要串口、继电器，以及可选的图形界面依赖。详细字段说明、日志和退出码见 [使用说明书](docs/user_guide.md)。

## 快速开始

### 1. Windows 批处理

在项目根目录双击或执行：

```bat
scripts\setup_env.bat
```

脚本会创建 `.venv`，执行 `pip install -e ".[win,gui,report,dev]"`，并在缺少时从 `config\local.yaml.example` 复制 `config\local.yaml`。接着把本机串口改进 `config\local.yaml`。

```bat
scripts\start_ui.bat
scripts\run_station.bat
scripts\run_station.bat relay_power_cycle --set test.cycles=2 --port relay=COM3 --port device=COM13
```

`start_ui.bat` 打开本机网页控制台。`run_station.bat` 不带参数时只列出工位，不会开始测试。带工位名时运行测试，控制台输出另存 `logs\console_<工位>_<时间>.log`。`COM3`、`COM13` 是示例，先用 `ports` 看本机端口。`relay_power_cycle` 默认轮次很大，第一次接通硬件请加上 `--set test.cycles=2`。

### 2. Web 控制台

```bat
python -m ppx_testkit.web
```

等价命令是 `python -m ppx_testkit ui` 或 `ppx-test ui`。浏览器打开 `http://127.0.0.1:8765`。页面只监听本机，同一时间只能跑一场。可选 `--port 8765` 改网页端口，`--no-browser` 不自动打开浏览器。网页写入 `config/local.yaml` 的串口，轮次只对这一次生效。

### 3. 命令行

安装后 `ppx-test` 与 `python -m ppx_testkit` 是同一个入口。

```bat
python -m ppx_testkit list
python -m ppx_testkit ports
python -m ppx_testkit show relay_power_cycle
python -m ppx_testkit run relay_power_cycle --set test.cycles=2 --port relay=COM3 --port device=COM13
python -m ppx_testkit ui --port 8765
```

`--port 名称=COMx` 覆盖该路串口。`--set a.b=value` 覆盖任意配置项，可重复。`--no-local` 忽略 `config/local.yaml`。

退出码：0 通过，1 未通过，2 配置错误，3 硬件或通信故障，130 用户中断。

每次运行在 `logs/<工位>/<YYYYmmdd_HHMMSS>/` 下保存 `full.log`、`error.log`、`device_raw.log`、`config.yaml`、`summary.json`。

## 工站全景

`config/stations/` 下 30 个工位。应用入口是 yaml 里的 `app:`，对应 `src/ppx_testkit/apps/<app>.py` 的 `run(settings, ctx)`。命令行写成 `ppx-test run <工位>`。

### FCT 产测

| 工位 | 机型 | 应用入口 | 说明 |
| --- | --- | --- | --- |
| `p3_fct` | P3 | `fct.p3_fcb` | 多通道 SMT/FCT，SN 递增，绿灯状态机 |
| `r3_leb_fct` | R3 | `fct.r3_leb` | LEB SMT，扫码加屏幕颜色判定 |
| `r3_mcb_fct` | R3 | `fct.r3_mcb` | MCB SMT，继电器上下电加屏幕颜色判定 |

### 白盒与协议诊断

| 工位 | 机型 | 应用入口 | 说明 |
| --- | --- | --- | --- |
| `l5_lcb_ble` | L5 | `whitebox.lcb_ble` | 灯板 BLE，Excel/CSV 用例 |
| `mcb_whitebox` | L5 | `whitebox.mcb_region` | MCB 白盒，region v1 |
| `l5_mcb_hall_diag` | L5 | `whitebox.mcb_hall_diag` | MCB 霍尔体检 |
| `r3_mcb_whitebox` | R3 | `whitebox.r3_mcb` | MCB 电机白盒，菜单 / 自动 / 稳定性 |
| `lrd_debug` | L5 | `whitebox.lrd_debug` | 灯板单次点灯并回读 |

### 上下电与继电器压测

| 工位 | 机型 | 应用入口 | 说明 |
| --- | --- | --- | --- |
| `relay_power_cycle` | 通用 | `power_cycle.relay_power_on` | 继电器开关机。失败默认保持上电 |
| `nfc_power_cycle` | 通用 | `power_cycle.relay_power_on` | NFC 开关机，异常关键字熔断断电 |
| `handle_power_cycle` | 通用 | `power_cycle.relay_power_on` | 把手继电器循环，不读设备日志 |
| `w3_power_button` | W3 | `power_cycle.power_button` | 电源键开关机，命中停止关键字保留现场 |
| `relay_probe` | 通用 | `power_cycle.relay_probe` | 继电器极性人工探测 |

### PCTool 与整机 GUI 压测

| 工位 | 机型 | 应用入口 | 说明 |
| --- | --- | --- | --- |
| `l5_pctool_stress` | W3 | `pctool.l5_upgrade_stress` | 按钮压力，窗口 `W3 PCTOOL V5.5.00` |
| `l5_fixture_relay_stress` | L5 | `pctool.l5_upgrade_stress` | 治具左右灯，ICSE 继电器，无窗口 |
| `w3_pctool_stress` | W3 | `pctool.w3_pctool_stress` | 点击压力，坐标见 `w3_pctool_clicks.yaml` |
| `w3_assembly_stress` | W3 | `pctool.w3_pctool_stress` | 与上一工位同一套点击序列 |
| `ppx_pctool_stress` | 通用 | `pctool.ppx_scan_stress` | PPX PCTOOL 车架号与扫码弹窗 |
| `upgrade_log_stress` | L5 | `pctool.upgrade_log_stress` | 升级日志关键字，窗口 `L5 PCTOOL V3.9.00` |
| `r3_assembly_upgrade` | R3 | `assembly.r3_assembly_upgrade` | 总装 VIN / 部件 SN 录入与升级确认 |

### 灯光 / 电机 / 充电 / 舵机

| 工位 | 机型 | 应用入口 | 说明 |
| --- | --- | --- | --- |
| `turn_signal` | 通用 | `lamp.turn_signal` | 转向灯。`--set lamp.variant=formal` 等 |
| `headlight` | 通用 | `lamp.headlight` | 前灯。`--set lamp.variant=spec` 或 `no_spec` |
| `l5_charge_cycle` | L5 | `charge.charge_cycle` | 继电器充电压测 |
| `r3_charge_cycle` | R3 | `charge.charge_cycle` | 继电器充电压测 |
| `r3_brake_lock_unlock` | R3 | `motor.brake_lock_unlock` | 电磁刹车解锁 / 上锁，单线程顺序执行 |
| `servo_nfc` | 通用 | `servo.servo_nfc` | 舵机刷 NFC |

### 辅助工具

| 工位 | 机型 | 应用入口 | 说明 |
| --- | --- | --- | --- |
| `mouse_locate` | 通用 | `pctool.mouse_locate` | 鼠标坐标采样 |
| `inspect_controls` | 通用 | `pctool.inspect_controls` | 导出窗口控件名 |
| `speed_calc` | 通用 | `calculators.speed_calc` | 车速计算 |
| `time_diff` | 通用 | `calculators.time_diff` | 时间差 |

FCT 和 W3 点击压测的坐标是旧屏幕上的绝对坐标。换电脑后用 `mouse_locate` 重采，控件名用 `inspect_controls` 核对。

## 架构概览与扩展

```
config/default.yaml
config/local.yaml                 本机串口，不入库
config/stations/*.yaml            30 个工位
config/stations/_fragments/       关键字、继电器口、W3 点击坐标
src/ppx_testkit/
  cli.py  __main__.py             python -m ppx_testkit
  settings.py  station_schema.py  配置合并与校验
  logger.py
  core/     serial  relay  protocol  monitor  runner  report  gui
  profiles/ l5.py  p3.py  r3.py  w3.py
  apps/     工站流程，含计算器、鼠标坐标、继电器探测、灯板调试、R3 菜单
  web/      python -m ppx_testkit.web
resources/dll/l5/   resources/dll/r3/   resources/dll/manifest.json
resources/include/  头文件
scripts/            setup_env.bat  start_ui.bat  run_station.bat
tests/core  tests/apps            离线 Fake
tests/hardware                    真实串口 / 继电器
```

配置文件优先级（后者覆盖前者）：

1. `config/default.yaml`
2. `config/stations/<工位>.yaml`（`include:` 引入 `_fragments/`）
3. `config/local.yaml`（顶层对所有工位生效，`stations.<工位名>` 只对该工位生效）

其后还有环境变量 `PPX__A__B` 和命令行 `--set` / `--port`。已注册的配置段在启动时由 `station_schema.py` 校验。

新增一个工位：

1. 在 `config/stations/` 新建 `<工位名>.yaml`，能复用的继电器口、关键字和点击坐标用 `include` 引用 `_fragments/`。
2. `app:` 指向已有模块，或在 `src/ppx_testkit/apps/<域>/` 实现 `run(settings, ctx) -> int`。机型差异写在 `src/ppx_testkit/profiles/`。辅助工具同样写在 `apps/`，用 `@register_tool` 登记。不要使用已删除的 `src/ppx_testkit/tools/`。
3. 在 `src/ppx_testkit/station_schema.py` 登记该应用的配置段和串口名。
4. 串口号只写 `config/local.yaml` 或通过 `--port` 传入。
5. 先 `ppx-test show <工位名>`，再限制轮次试跑。

字段含义、旧脚本对照和改哪一份配置，见 [docs/user_guide.md](docs/user_guide.md)。

## 开发

```bash
ruff check .
pytest --ignore=tests/hardware
pytest -m hardware
```

协议结构体与 `resources/include` 下的 C 头文件逐字段比对。L5 与 R3 的 `ppx_region.dll` 分目录存放，不合并加载器。
