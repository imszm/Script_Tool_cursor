# ppx_testkit 使用说明书

这份说明讲怎么装环境、怎么选工位、怎么从命令行或网页跑起来。配置叠在哪一层、日志和退出码也在这里。测试通过/失败的判定在代码里，改配置不会改判定规则。

当前仓库里有 30 个工位，都在 `config/stations/*.yaml`。入口是 `ppx-test`，也就是 `python -m ppx_testkit`。

## 1. 先记住五件事

1. 子命令有五个：`list`、`ports`、`show`、`run`、`ui`。`Tool/`、`L5*`、`R3*`、`P3系列*` 这些旧脚本目录已经删除，历史版本还在 git 里。
2. 一个工位 = 一份 `config/stations/<工位名>.yaml`。文件里的 `app:` 决定调用哪段 Python。改次数、串口、关键字、坐标，先改配置。
3. 本机串口写在 `config/local.yaml`（不入库）。工位 yaml 里不写死 `COM` 口。
4. 网页运行台只监听 `127.0.0.1`，同一时间只能跑一场。页面上选的串口会写回 `local.yaml`；轮次只对这一次生效。关键字和坐标这个页面不改。
5. 断言失败、串口闪断、Ctrl+C、网页上的停止，都会走收尾。带电工位的复位顺序是：停心跳 → 电机/抱闸释能 → 继电器断开 → 释放串口。

`run` / `show` 的 `--port` 是串口，写成 `relay=COM5`。`ui` 的 `--port` 是网页端口，写成整数，默认 `8765`。两个不是一回事。

## 2. 第一次在 Windows 上跑

在项目根目录（能看到 `pyproject.toml` 和 `config/` 的那一层）打开终端。需要 Python 3.11 及以上。

```bat
scripts\setup_env.bat
```

这一步会：

- 用 Python 3.11 创建 `.venv`
- 执行 `pip install -e ".[win,gui,report,dev]"`，装上本包以及串口、界面、报告、开发依赖
- 如果还没有 `config\local.yaml`，就从 `config\local.yaml.example` 复制一份

然后改 `config\local.yaml` 里的串口。示例口是旧脚本当时的口，本机不一定一样。先看机器上实际有哪些口：

```bat
.venv\Scripts\python.exe -m ppx_testkit ports
```

安装成功后也可以直接打 `ppx-test`，它和 `python -m ppx_testkit` 是同一个入口（`pyproject.toml` 里的 `ppx-test = ppx_testkit.cli:main`）。

跑之前先看合并后的配置。这一步不会打开串口：

```bat
ppx-test show relay_power_cycle
ppx-test list
```

第一次接通硬件，把循环次数压到 2 轮。`relay_power_cycle` 默认是 200000 轮，失败时继电器保持上电：

```bat
scripts\run_station.bat relay_power_cycle --set test.cycles=2 --port relay=COM3 --port device=COM13
```

把 `COM3`、`COM13` 换成 `ports` 打出来的口。口写进 `local.yaml` 以后，就不用每次加 `--port`。

双击 `scripts\run_station.bat` 且不带参数时，窗口只列出工位，按键后关闭，不会开始测试。

不想记命令时，双击 `scripts\start_ui.bat`。

## 3. 网页运行台

```bat
scripts\start_ui.bat
```

等价命令：

```bat
ppx-test ui
python -m ppx_testkit ui
python -m ppx_testkit.web
```

浏览器打开 `http://127.0.0.1:8765`。只绑定本机，局域网里的其他电脑打不开。可选参数：

```bat
ppx-test ui --port 8765
ppx-test ui --no-browser
```

`--port` 这里是网页端口。端口已被占用时，向 stderr 写原因并退出，退出码 2。关掉这个窗口就停掉网页服务。正在跑的测试请先在页面上点「停止」。

页面左边是工位列表，右边对串口。用法：

1. 选一个工位。配置读失败的工位会显示错误，开始按钮不可用。
2. 每一路串口可以选本机已有的口，或留空「不指定，沿用配置」。
3. 有轮次字段的工位（`test.cycles`、`stress.cycles`、`lamp.cycles`）可以填「只跑这一次的轮次」。留空就用 yaml 里的默认值。合法范围是 1 到 10000000。
4. 点「开始」。同一时间只能有一场。已有测试在跑时再点开始，页面会拒绝。
5. 点「停止」会向测试进程发中断，让工位走原来的收尾。等几秒还不退，才会结束进程。
6. 下方黑底区域是这场的控制台输出。完整日志仍在 `logs/<工位>/<时间>/full.log`。

选了串口并开始之后，口会写进 `config/local.yaml` 的 `stations.<工位>.serial.<名字>.match.port`。其他键保留。标准库写 YAML 时不保留注释，所以网页保存过后，`local.yaml` 里原来的注释会丢掉。轮次不会写进文件，只出现在这一次的 `--set` 里。

下面这几个工位，页面上会单独提示：

| 工位 | 页面上的提示 |
| --- | --- |
| `relay_probe` | 默认每一步都等回车。网页已关掉键盘输入，开始后可能马上结束 |
| `r3_mcb_whitebox` | 默认是键盘菜单。网页不能替你按键 |
| `speed_calc` | 开始后会再弹出车速计算窗口 |
| `time_diff` | 开始后会再弹出时间差窗口 |
| `ppx_pctool_stress` | 会控制鼠标和键盘。先改好坐标，窗口露在最前面，输入法切到英文。鼠标移到屏幕左上角可紧急停止 |

L5 和 R3 的 DLL 带有进程级全局状态，两边不能混在一个进程里加载。网页进程本身不加载这些 DLL。每一场测试都新起一个 `python -m ppx_testkit run ...` 子进程，结束或停止时进程退出，串口和 DLL 一起释放。

## 4. 命令行

```bat
ppx-test list
ppx-test ports
ppx-test show relay_power_cycle
ppx-test run relay_power_cycle --set test.cycles=2 --port relay=COM3 --port device=COM13
ppx-test ui --no-browser
ppx-test --version
```

`show` 和 `run` 的工位参数可以是工位名，也可以是 yaml 文件路径。

| 参数 | 作用 |
| --- | --- |
| `--set a.b=value` | 覆盖任意配置项，可重复。例如 `--set test.cycles=2` |
| `--port 名字=COMx` | 覆盖该路串口，可重复。等价于 `--set serial.名字.match.port=COMx` |
| `--no-local` | 忽略 `config/local.yaml` |

`list`、`ports`、`show` 把结果打到标准输出。其余业务路径写日志，不使用 `print`。配置错误在打开硬件之前打印原因。

退出码：

| 码 | 含义 |
| --- | --- |
| 0 | 通过 |
| 1 | 测试未通过，或应用内部未捕获的异常 |
| 2 | 配置错误（缺字段、类型不对、应用模块加载失败）。网页端口被占用也是 2 |
| 3 | 硬件或通信故障（打开串口失败、继电器发送失败等） |
| 130 | Ctrl+C，或网页停止后的中断 |

日志目录 `logs/<工位>/<YYYYmmdd_HHMMSS>/`：

| 文件 | 谁写入 | 内容 |
| --- | --- | --- |
| `full.log` | `logger.py` | 全程，含 DEBUG 和串口收发十六进制 |
| `error.log` | `logger.py` | WARNING 及以上 |
| `device_raw.log` | `logger.py` 的 `ppx.raw` | 设备原始串口文本。有内容才创建，单文件 50MB 后轮转，不进控制台 |
| `config.yaml` | `cli.py` | 这次真正生效的配置 |
| `summary.json` | `StressRunner` 或具体应用 | 轮次、成败、是否保留上电 |
| `logs/console_<工位>_<时间>.log` | `run_station.bat` | 控制台原样副本。网页运行台不写这个文件，输出留在页面上 |

业务代码只调用 `get_logger(__name__)`，不要自己再加日志 handler。`PIL`、`pywinauto`、`comtypes` 在 DEBUG 下太吵，它们被提到 WARNING，业务日志仍是 DEBUG。

## 5. 一条 run 是怎么执行的

以 `ppx-test run relay_power_cycle --set test.cycles=2 --port relay=COM3` 为例。

```
ppx-test
  └─ src/ppx_testkit/__main__.py
       └─ cli.main()
            └─ load_settings("relay_power_cycle")
                 按第 6 节的顺序合并 YAML、环境变量、命令行
                 station_schema 校验类型和必填段
            └─ setup_logging()               建立 logs/<工位>/<时间>/
            └─ 把生效配置写成该目录下的 config.yaml
            └─ resolve_runner(app)
                 已用装饰器登记的工具优先
                 否则导入 ppx_testkit.apps.<app>，调用 run(settings, ctx)
            └─ shutdown_logging()            无论成败都会收尾
```

`app:` 的写法是包内相对路径，点号对应目录。`power_cycle.relay_power_on` 就是 `src/ppx_testkit/apps/power_cycle/relay_power_on.py`，里面必须有 `run(settings, ctx) -> int`。

网页点开始时，主进程不跑测试。它拼出 `python -m ppx_testkit run <工位> [--port ...] [--set ...]`，交给子进程。停止时发 Ctrl+Break（Windows）或 SIGINT，工位里变成 `KeyboardInterrupt`，`finally` 里的复位能够执行。

`settings` 是只读配置对象。`ctx` 是这次运行的目录和写文件方法（`ctx.write_text`、`ctx.write_json`、`ctx.artifact`）。应用返回整数退出码；抛出 `ConfigError` 得到 2，抛出其他 `ToolError` 得到 3。

## 6. 配置是怎么叠在一起的

后者覆盖前者：

1. 代码里 dataclass 的默认值
2. `config/default.yaml`（目前主要是日志目录和级别）
3. `config/stations/<工位>.yaml`，其中 `include:` 引入 `config/stations/_fragments/` 里的公共片段
4. `config/local.yaml`：顶层键对所有工位生效；`stations.<工位名>` 只对该工位生效
5. 环境变量 `PPX__A__B`，例如 `PPX__TEST__CYCLES=2` 相当于 `test.cycles: 2`
6. 命令行 `--set test.cycles=2`，以及 `--port relay=COM5`

`--no-local` 会跳过第 4 步。网页启动子进程时会带上当前环境变量，因此 `PPX__A__B` 对这场测试仍然有效。页面上填的轮次和串口另外通过 `--set` / `--port` 传入，不会改工位 yaml。

未知字段、缺必填项、类型不对，会在打开硬件之前失败，退出码 2。已注册的应用由 `src/ppx_testkit/station_schema.py` 规定必须有哪些段（`test`、`serial.relay`、`relay` 等）。没登记的应用不做业务段校验，已经出现的 `serial` / `relay` / `keywords` 仍然会校验。

工位 yaml 里继电器的 `on` / `off` 必须加引号，例如 `"on": 0x50`。不加引号时 YAML 会把 `on` 当成布尔值。

公共片段：

| 文件 | 内容 |
| --- | --- |
| `_fragments/keywords_assertion.yaml` | 公共异常关键字 |
| `_fragments/relay_ch340.yaml` | CH340 继电器口的匹配方式 |
| `_fragments/relay_cmd_50_4f.yaml` | `0x50` / `0x4F` 吸合与断开 |
| `_fragments/w3_pctool_clicks.yaml` | W3 点击坐标和默认轮次 |

`local.yaml` 的形状：

```yaml
stations:
  relay_power_cycle:
    serial:
      relay: { match: { port: COM3 } }
      device: { match: { port: COM13 } }
```

`serial` 下面的名字必须和工位 yaml 里的 `serial` 键一致，也必须和应用代码里 `open_serial(settings, "relay")` 用的名字一致。开关机是 `relay` 和 `device`，舵机是 `servo` 和 `device`，灯板是 `lcb`，L5 电机板是 `mcb`，R3 刹车是 `tested` 和 `load`，转向灯是 `relay_switch`、`relay_icse`、`relay_rtp`，前灯是 `relay_icse`。

点击类工位没有串口。程序路径、窗口标题写在 `local.yaml` 的 `stations.<工位>.stress.program_path`，不要写进将要提交的工位 yaml。`ppx-test show <工位>` 能看到合并之后的完整结果，包括 `local.yaml` 是否生效。

## 7. 目录：哪些要看，哪些先不用管

```
pyproject.toml              包名、依赖、ppx-test 入口。Python >= 3.11
config/default.yaml         全局默认（日志）
config/local.yaml.example   本机串口示例，复制成 local.yaml
config/stations/*.yaml      30 个工位，这是改行为的第一处
config/stations/_fragments/ 多个工位共用的继电器口、关键字、W3 点击坐标
src/ppx_testkit/cli.py      命令行
src/ppx_testkit/settings.py 读配置、合并、校验
src/ppx_testkit/logger.py   每次运行一个日志目录
src/ppx_testkit/station_schema.py  各应用允许的配置段
src/ppx_testkit/core/       串口、继电器、关键字、协议、循环、报告、窗口、硬件复位
src/ppx_testkit/profiles/   l5 / p3 / r3 的 DLL 路径、节点、寄存器、按键枚举
src/ppx_testkit/apps/       各工位流程。部分文件只重导出 tools
src/ppx_testkit/tools/      计算器、鼠标坐标、继电器探测、灯板调试、R3 菜单
src/ppx_testkit/web/        本机网页运行台
resources/dll/l5/           L5 的 5 个运行库
resources/dll/r3/           R3 的 ppx_ble.dll、ppx_region.dll
resources/include/          与协议结构体对齐的 C 头文件
resources/                  图片、用例表
scripts/setup_env.bat       装环境
scripts/start_ui.bat        打开网页运行台
scripts/run_station.bat     带控制台日志跑一个工位
tests/core  tests/apps      离线 Fake，默认就跑
tests/hardware              真实串口 / 继电器，默认跳过
logs/                       运行后才出现，不入库
```

`core` 里和跑工位最相关的几块：

| 目录或文件 | 作用 |
| --- | --- |
| `core/factory.py` | 按配置打开串口、建继电器、建关键字监控，并解析 `run` 入口 |
| `core/context.py` | `HardwareContext`：退出时先紧急复位，再释放串口 |
| `core/serial/` | 找口、打开、读写。异常变成 `PortNotFoundError`、`SerialTimeoutError`、`SerialDisconnectedError` |
| `core/relay/` | `command`（0x50/0x4F 或 ASCII）、`icse_a0`、舵机 |
| `core/monitor/` | 设备日志分行、成功/异常关键字、滑动窗口熔断、高速日志按块读 |
| `core/runner/` | `StressRunner`：循环、统计、Ctrl+C、结束时一定 teardown |
| `core/protocol/` | L5 用 region v1，R3 用 region v2。两套 `ppx_region.dll` 不同，分目录存放，不合并加载器 |
| `core/gui/` | 窗口点击、截图、结束进程。PC 工具和 FCT 用到 |
| `core/report/` | CSV、HTML、Excel 用例 |
| `profiles/` | 机型差异。工位代码不按 L5/R3/W3/P3 写分支，读 profile 或 yaml 注入的结果 |

`apps` 按工位种类分目录。同一个应用可以被多份 yaml 复用。例如 `power_cycle.relay_power_on` 同时服务继电器开关机、NFC 开关机和把手开关，差别全在各自的 yaml。

下面这些工位的流程在 `tools/`，`apps/` 里的同名文件只是重导出，改逻辑只改 `tools/`：

| 工位 | 事实来源 |
| --- | --- |
| `mouse_locate` | `tools/mouse_locate.py` |
| `speed_calc` | `tools/calculators/speed_calc.py` |
| `time_diff` | `tools/calculators/time_diff.py` |
| `relay_probe` | `tools/relay_probe.py` |
| `lrd_debug` | `tools/lrd_debug.py` |

`tools/r3_menu.py` 是 R3 电机白盒的交互菜单，没有单独的工位 yaml，由 `r3_mcb_whitebox` 调用。这些工具不进压测统计报告。

要在项目根目录执行命令，或者设置环境变量 `PPX_TESTKIT_ROOT` 指向能同时看到 `config/` 和 `pyproject.toml` 的目录。

## 8. 选哪个工位

`ppx-test list` 打印工位名和 yaml 里的 `description`。下面按用途分组。「旧脚本」只说明这份工位是从哪个文件迁来的，那些目录已经不在仓库里。

### 上下电与继电器

| 工位名 | 应用模块 | 说明 | 旧脚本 |
| --- | --- | --- | --- |
| `relay_power_cycle` | `power_cycle.relay_power_on` | 继电器开关机。失败默认保持上电。默认 200000 轮 | `Tool/继电器开关机压力测试.py` |
| `nfc_power_cycle` | 同上 | NFC 开关机，异常关键字熔断断电。默认 100000 轮 | `Tool/NFC开关机异常关键字检测.py` |
| `handle_power_cycle` | 同上 | 把手继电器循环，不读设备日志。默认 200000 轮 | `Tool/把手自动化开关.py` |
| `w3_power_button` | `power_cycle.power_button` | 电源键开关机，命中停止关键字保留现场。默认 500000 轮 | `Tool/W3继电器开关机压力测试.py` |
| `relay_probe` | `power_cycle.relay_probe` | 继电器极性人工探测。逻辑在 `tools/relay_probe.py` | `Tool/查看继电器状态.py` |

### 充电、灯光、舵机、电机

| 工位名 | 应用模块 | 说明 | 旧脚本 |
| --- | --- | --- | --- |
| `l5_charge_cycle` | `charge.charge_cycle` | L5 继电器充电压测，高速日志按块读取。默认 500000 轮 | `Tool/继电器充电压力测试_L5.py` |
| `r3_charge_cycle` | 同上 | R3 继电器充电压测。默认 500000 轮 | `Tool/继电器充电压力测试_R3.py` |
| `turn_signal` | `lamp.turn_signal` | 转向灯。`--set lamp.variant=formal` 等 | `Tool/左右转向灯自动化测试-正式版本.py` 和 `Tool/TestTurnSignals/` |
| `headlight` | `lamp.headlight` | 前灯。`--set lamp.variant=spec` 或 `no_spec` | `Tool/TestHeadlights/` |
| `servo_nfc` | `servo.servo_nfc` | 舵机刷 NFC。默认 10000 轮 | `Tool/舵机压力测试_V1_1.py` |
| `r3_brake_lock_unlock` | `motor.brake_lock_unlock` | 电磁刹车解锁 / 上锁，单线程顺序执行。默认 1000 轮 | 电磁刹车优化完整版 |

转向灯的 `lamp.variant` 可选 `formal`、`left_spec`、`right_spec`、`left_no_spec`、`right_no_spec`。`formal` 默认 10000 轮。前灯默认方案是 `spec`，`spec` 和 `no_spec` 在工位 yaml 里各写了 10 轮；顶层 `lamp.cycles` 为空时用所选方案自己的轮次。

### PC 工具与组装

| 工位名 | 应用模块 | 说明 | 旧脚本 |
| --- | --- | --- | --- |
| `upgrade_log_stress` | `pctool.upgrade_log_stress` | 升级日志关键字，窗口 `L5 PCTOOL V3.9.00`。默认 10 轮 | `Tool/升级工具压力自动化测试 - 可截图版本.py` |
| `l5_pctool_stress` | `pctool.l5_upgrade_stress` | 按钮压力，窗口 `W3 PCTOOL V5.5.00`。默认 100 轮 | L5/W3 PC 工具按钮压力 V1.2 |
| `l5_fixture_relay_stress` | 同上 | 治具左右灯，ICSE 继电器，无窗口。默认 50 轮 | 治具工具软件压力测试 V1.3 |
| `w3_pctool_stress` | `pctool.w3_pctool_stress` | 点击压力，窗口 `W3 PCTOOL V5.4.00`。默认 100 轮 | `Tool/组装生产工具压力测试.py` |
| `w3_assembly_stress` | 同上 | 与上一工位同一套点击坐标 | 同上 |
| `ppx_pctool_stress` | `pctool.ppx_scan_stress` | PPX PCTOOL 车架号与扫码弹窗。默认 100 轮 | 新工位，见下一节 |
| `r3_assembly_upgrade` | `assembly.r3_assembly_upgrade` | 总装 VIN / 部件 SN 录入与升级确认 | R3 总装升级 |
| `inspect_controls` | `pctool.inspect_controls` | 导出窗口控件名 | 识别读取应用窗口控件 |
| `mouse_locate` | `pctool.mouse_locate` | 鼠标坐标采样。逻辑在 `tools/mouse_locate.py` | `Tool/鼠标定位脚本.py` |

### 白盒、FCT、计算器

| 工位名 | 应用模块 | 说明 | 旧脚本 |
| --- | --- | --- | --- |
| `l5_lcb_ble` | `whitebox.lcb_ble` | 灯板 BLE，Excel/CSV 用例，HTML 报告 | L5 BLE 自动化工具 V1.6 |
| `lrd_debug` | `whitebox.lrd_debug` | 灯板单次点灯并回读。逻辑在 `tools/lrd_debug.py` | `Tool/LRD调试程序.py` |
| `mcb_whitebox` | `whitebox.mcb_region` | L5 MCB 白盒，region v1，6 项用例 | L5 MCB V1.4.6 |
| `l5_mcb_hall_diag` | `whitebox.mcb_hall_diag` | MCB 霍尔体检，人工转动车轮 | L5 MCB 霍尔诊断 |
| `r3_mcb_whitebox` | `whitebox.r3_mcb` | R3 MCB 电机白盒。默认 `interactive` 键盘菜单，也可切全自动或稳定性 | `R3_MCB白盒测试/PPX_MCB_V2.PY` |
| `p3_fct` | `fct.p3_fcb` | 多通道 SMT/FCT，SN 递增，绿灯状态机 | P3 FCT |
| `r3_leb_fct` | `fct.r3_leb` | LEB SMT，扫码加屏幕颜色判定 | R3 LEB SMT |
| `r3_mcb_fct` | `fct.r3_mcb` | MCB SMT，继电器上下电加屏幕颜色判定 | R3 MCB SMT |
| `speed_calc` | `calculators.speed_calc` | 车速计算。逻辑在 `tools/calculators/speed_calc.py` | `Tool/代步车车速计算器.py` |
| `time_diff` | `calculators.time_diff` | 时间差。逻辑在 `tools/calculators/time_diff.py` | `Tool/时间差计算.py` |

## 9. 容易踩到的默认值

第一次接硬件，凡是上表里轮次到了万、十万的工位，都加上 `--set test.cycles=2`（充电、灯光、舵机、刹车看自己的段名，用 `show` 确认是 `test.cycles`、`stress.cycles` 还是 `lamp.cycles`）。

- `upgrade_log_stress` 默认 10 轮，每轮仍等 170 秒，日志里出现「失败」或「超时」就停。要一直跑：`--set stress.cycles=1000000`。
- `w3_pctool_stress` / `w3_assembly_stress` 默认 100 轮。正式压测：`--set stress.cycles=30000`。窗口标题是 `W3 PCTOOL V5.4.00`。程序路径写在 `local.yaml` 的 `stations.<工位>.stress.program_path`。
- `l5_pctool_stress` 默认 100 轮，连的是窗口 `W3 PCTOOL V5.5.00`，控件按 `auto_id` 找，不是屏幕坐标。正式压测：`--set stress.cycles=100000`。对不上控件时先跑 `inspect_controls`。
- FCT 和组装点击的坐标是旧电脑上的绝对屏幕坐标。换机器后先跑 `mouse_locate`，窗口控件名用 `inspect_controls` 核对。坐标在 `config/stations/_fragments/w3_pctool_clicks.yaml`。

### `ppx_pctool_stress`

窗口标题是 `PPX PCTOOL V2.5.00`。开跑前先：

```bat
ppx-test run mouse_locate
```

把看到的屏幕坐标写进 `config/stations/ppx_pctool_stress.yaml` 的 `stress.vin_click` 和 `stress.confirm_click`。这两项仍是占位，不改会点偏。`torque_confirm_click` 是力矩校验「确认」。填坐标时窗口放在哪，开跑时就放在哪。`follow_window: true` 时，开跑之后如果把窗口拖走，点击会跟着窗口走。

一轮顺序：

1. 点 VIN 框，输入车架号，回车。车架号从剪贴板粘贴，避免长跑后输入法在开头多出一个空格。
2. 等 `popup_wait_s`（默认 2 秒），扫码窗弹出。
3. 五项条码从上到下各输入一次，每次回车。不要点输入框。弹窗出来后焦点在第一格。条码里的 `+` 按 Shift 组合键送出。不要在条码前按 Ctrl+A，否则回车不会被当成这一格结束。
4. 点扫码窗的「确认」。先按按钮标题找，点不到再用 `confirm_click`。
5. 等 `settle_wait_s`（默认 20 秒），弹出「力矩校验」。
6. 再点一次「确认」。先按按钮标题找，点不到再用 `torque_confirm_click`。
7. 等 `after_torque_click_s`（默认 10 秒），再开始下一轮。

停止：Ctrl+C，或把鼠标移到屏幕左上角。网页上跑这场时，上位机窗口要露在最前面，输入法切到英文。

改轮次：`ppx-test run ppx_pctool_stress --set stress.cycles=1000`。

## 10. 一份工位文件怎么读

打开 `config/stations/relay_power_cycle.yaml`，按这个顺序看：

| 字段 | 含义 |
| --- | --- |
| `include` | 先并入公共片段：CH340 继电器口、`0x50/0x4F` 指令、公共异常关键字 |
| `station` | 工位名，也是日志子目录名 |
| `app` | 要加载的 Python 模块 |
| `description` | `ppx-test list` 和网页列表上的一行字 |
| `serial.relay` / `serial.device` | 两路串口。`match.port` 一般由 `local.yaml` 或 `--port` 补上；也可以按描述匹配 CH340、CP210 |
| `relay` | 继电器类型、通道字节、写完后等多久 |
| `keywords` | 成功关键字、异常关键字、频率熔断（几秒内出现几次就停） |
| `test` | 轮次、上电听多久、断电等多久、失败是否保持上电 |

灯具工位的继电器可能在 `relays.<名字>`，串口名也不是 `relay`。以该工位 yaml 和 `ppx-test show` 的结果为准。

## 11. 停下来时硬件怎么收

供电、启动电机或打开心跳之后，工位会把复位动作登记进 `HardwareContext`。下面这些情况都会进到 `finally`：

- 断言失败
- 串口闪断
- Ctrl+C，以及 Windows 上的 Ctrl+Break
- 网页上的「停止」（子进程收到中断信号）
- 进程收到 SIGTERM

复位按登记的逆序执行，约定的顺序是：停心跳 → 电机/抱闸释能 → 继电器断开 → 释放串口。某一个复位动作失败只记入当次 `full.log`，不会挡住后面的断电和关口。

主动停掉整场测试时，代码抛 `TestAbort`。带 `keep_power=True` 表示这一次不要断电，用于失败后保留现场。`relay_power_cycle` 失败默认保持上电，所以试跑请把轮次改小，人在旁边。

网页停止若等了几秒进程还不退，会先 `terminate` 再 `kill`。强杀之后工位自己的 `finally` 可能来不及跑完。所以停测优先用页面上的「停止」或 Ctrl+C，不要直接拔串口或关网页窗口。

## 12. 必要文件对照

### `src/ppx_testkit/cli.py`

五个子命令：`list`、`ports`、`show`、`run`、`ui`。`run` 在加载配置之后登记 SIGINT、SIGTERM，Windows 上还有 SIGBREAK，把它们转成 `KeyboardInterrupt`，让 `finally` 里的复位能够执行。进程退出前 `main` 的 `finally` 调用 `shutdown_logging()`。

### `src/ppx_testkit/settings.py`

`load_settings(station)` 是读配置的唯一入口。`from_dict` 把 YAML 映射成冻结的 dataclass：多出来的字段、缺必填、类型不对都变成 `ConfigError`。`AppSettings.section("test", RelayPowerCycleConfig)` 是应用取自己那一段配置的方法。

### `src/ppx_testkit/station_schema.py`

`load_settings` 返回前会调用这里。每个 `app` 登记了必须出现的配置段和串口名。写错字段会在启动时暴露，而不是跑到一半。

### `src/ppx_testkit/logger.py`

`setup_logging(工位名, 日志配置)` 创建 `logs/<工位>/<时间>/`。同一进程里再调用一次会直接返回已有上下文，不会再开一个目录。目录创建失败时改写到系统临时目录。

### `src/ppx_testkit/core/factory.py`

应用和硬件之间的接口。常用函数：

- `open_serial(settings, "relay")`：读 `serial.relay`，按 `match` 找口并打开
- `relay_from_settings(settings, transport)`：读 `relay` 段，建成继电器驱动
- `keyword_evaluator(settings)`：读 `keywords` 段
- `resolve_runner(app)`：先查已登记的工具，没有再导入 `ppx_testkit.apps.<app>.run`

应用文件里不应出现 `import serial`。全仓库只有 `core/serial/` 直接使用 pyserial。

### `src/ppx_testkit/core/runner/stress_runner.py`

压测循环的骨架。应用实现三个方法后交给它：

- `setup`：打开串口、做预检。这里失败就不会进入循环
- `run_cycle(index)`：跑第 index 轮（从 1 开始），返回成败。要停掉整个测试时抛 `TestAbort`
- `teardown`：关串口、按结果决定是否断电。中断和异常之后仍会调用

`StressRunner.run()` 负责计数、连续失败熔断、Ctrl+C，并把 `summary.json` 写到当前日志目录。

### `src/ppx_testkit/web/server.py` 与 `web/session.py`

网页只提供工位列表、串口列表、开始、停止和日志轮询。`RunSession` 保证同一时间一个子进程。

### `scripts/run_station.bat`

必须用系统 ANSI 代码页能读的英文写，所以文件里没有中文注释。无参数：列出工位并暂停。有参数：`python -m ppx_testkit run <参数>`，标准输出和错误都追加到 `logs\console_<工位>_<时间>.log`，结束时用 `[result]` 行标出 PASS / FAIL / configuration error / hardware or communication error / interrupted。

### `scripts/setup_env.bat`

创建 `.venv`，执行 `pip install -e ".[win,gui,report,dev]"`，并在缺少时复制 `local.yaml`。装完后要自己改串口。

## 13. 想改行为时改哪里

| 想改的东西 | 改这里 |
| --- | --- |
| 本机 COM 口 | `config/local.yaml`，或命令行 `--port 名字=COMx`，或网页上选择（会写回 `local.yaml`） |
| 循环次数、等待秒数 | 工位 yaml 的 `test` / `stress` / `lamp`，或 `--set`，或网页上的「只跑这一次的轮次」 |
| 成功/失败关键字 | 工位 yaml 的 `keywords`，或 `_fragments/keywords_assertion.yaml` |
| 继电器吸合/断开字节 | 工位 yaml 的 `relay`，或 `_fragments/relay_cmd_50_4f.yaml` |
| 转向灯、前灯的亮灭顺序 | `config/stations/turn_signal.yaml`、`headlight.yaml` 里的 `lamp.variants` |
| W3 组装点击坐标、输入的 SN | `config/stations/_fragments/w3_pctool_clicks.yaml` |
| PPX 扫码点击坐标、条码、车架号 | `config/stations/ppx_pctool_stress.yaml` 的 `stress` |
| 本机程序路径 | `config/local.yaml` 的 `stations.<工位>.stress.program_path` |
| 日志放哪、控制台打到哪一级 | `config/default.yaml` 的 `logging`，或 `local.yaml` |
| 换一套测试步骤 | 改对应 `apps/...py` 的 `run`。已经迁到 `tools/` 的，只改 `tools/` 里那一份 |

改完配置先 `ppx-test show <工位>`。配置错误会直接打印原因，不会去动继电器。

新增一个工位时：

1. 在 `config/stations/` 新建 `<工位名>.yaml`。能复用的继电器口、关键字和点击坐标用 `include` 引用 `_fragments/`。
2. `app:` 指向已有模块，或在 `src/ppx_testkit/apps/<域>/` 实现 `run(settings, ctx) -> int`。辅助工具的流程写在 `tools/`，`apps/` 只做重导出，并用 `@register_tool` 登记，注册名与 `app:` 一致。
3. 在 `src/ppx_testkit/station_schema.py` 登记该应用的配置段和串口名。
4. 串口号只写 `config/local.yaml`，或通过 `--port` 传入。
5. 先 `ppx-test show <工位名>`，再限制轮次试跑。

不要在 `core/factory.py` 或 `web/catalog.py` 里按工位名写分支表。网页列表来自扫描 `config/stations/*.yaml`。

## 14. 开发时怎么确认没改坏

不接硬件。`pyproject.toml` 已默认跳过标记了 `hardware` 的用例，单测超时 120 秒：

```bash
ruff check .
pytest
```

显式跳过硬件目录也可以：

```bash
pytest --ignore=tests/hardware
```

接上设备后才跑：

```bash
pytest -m hardware
```

`tests/core/` 与 `tests/apps/` 用 Fake/Mock，不打开真实串口。协议结构体要和 `resources/include/` 下的 C 头文件逐字段对齐。L5 与 R3 的 `ppx_region.dll` 分目录存放，不要合并加载器，也不要在网页主进程里加载它们。
