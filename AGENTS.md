# 编码规则

- 生成代码后二次确认控制流、异常路径和硬件复位。
- 串口与硬件路径必须 try/except/finally，异常写入当次 `logs/<工位>/<时间戳>/full.log`。
- 业务路径使用 logging。`print` 只用于 `cli.py` 的 `list` / `ports` / `show` 和启动前配置错误。
- 全量 Type Hints，中文注释说明功能、入参和返回值。重构不改变通过/失败判定、时序和关键字规则。
- 改完代码后运行 `pytest --ignore=tests/hardware` 与 `ruff check .`。`pyproject.toml` 已默认跳过 `hardware` 标记，单测超时 120 秒。

# 目录职责

入口是 `ppx-test`（`ppx_testkit.cli:main`）和 `python -m ppx_testkit`。不要新增根目录 `main.py`，不要恢复已删除的 `Tool/`、`L5*`、`R3*`、`P3*`。仓库里没有 `src/ppx_testkit/stations/` 包，不要另起一套并行工位目录。

| 目录 | 职责 |
| --- | --- |
| `core/` | 串口、继电器、GUI、监控、C DLL 协议、报告、Runner。禁止 L5/R3/W3/P3 常量、寄存器地址和专属业务。 |
| `profiles/` | `l5` / `p3` / `r3` 的机型差异：DLL 路径、节点 ID、寄存器表、按键/菜单枚举、通道状态机。 |
| `apps/` | 各工站业务流程，按 `assembly/` `charge/` `fct/` `lamp/` `motor/` `pctool/` `power_cycle/` `servo/` `whitebox/` `calculators/` 划分。只读配置、组装 core 对象、按步骤执行。 |
| `tools/` | 独立辅助工具：`calculators/`、`mouse_locate`、`relay_probe`、`lrd_debug`、`r3_menu`。不接入压测统计和测试报告流水线。 |
| `web/`、`cli.py` | 参数、配置、子进程会话、状态展示。不做硬件操作。 |

`config/stations/<工位>.yaml` 的 `app:` 决定加载 `ppx_testkit.apps.<app>.run`。`core/factory.py` 的 `resolve_runner` 先查注册表，未命中再导入该模块。禁止在 `core/factory.py` 或 `web/catalog.py` 堆 if-elif 工位表，禁止在工位代码里按机型写分支。

下面这些 `apps/` 文件只是重导出。改核心逻辑只改事实来源，禁止两处复制：

| `apps/` 包装 | 事实来源 |
| --- | --- |
| `apps/calculators/speed_calc.py` | `tools/calculators/speed_calc.py` |
| `apps/calculators/time_diff.py` | `tools/calculators/time_diff.py` |
| `apps/pctool/mouse_locate.py` | `tools/mouse_locate.py` |
| `apps/power_cycle/relay_probe.py` | `tools/relay_probe.py` |
| `apps/whitebox/lrd_debug.py` | `tools/lrd_debug.py` |
| `apps/whitebox/r3_menu.py` | `tools/r3_menu.py` |
| `apps/whitebox/r3_names.py` | `profiles/r3.py` |

工具可以用 `@register_tool` 登记，注册名与 YAML 的 `app:` 一致。`@register_station` 与 `BaseStation` 已存在，当前 YAML 工位仍走 `apps` 的 `run(settings, ctx)`。新增工位沿用这条路径，并在 `station_schema.py` 登记配置段。

# 配置驱动

新增或修改 `config/stations/*.yaml` 时，优先 `include` 这些公共片段：

- `config/stations/_fragments/keywords_assertion.yaml`
- `config/stations/_fragments/relay_ch340.yaml`
- `config/stations/_fragments/relay_cmd_50_4f.yaml`
- `config/stations/_fragments/w3_pctool_clicks.yaml`

合并后的配置必须通过 `src/ppx_testkit/station_schema.py`：未知字段、类型不符、缺少必填段在打开硬件前失败。配置模型用 dataclass。

本机串口号、程序路径等环境差异只写 `config/local.yaml`（不入库）。禁止写进工位代码、`config/default.yaml` 或将要提交的工位 yaml。

文件优先级（后者覆盖前者）：`config/default.yaml` → `config/stations/<工位>.yaml`（含片段）→ `config/local.yaml`。其后还有环境变量 `PPX__A__B` 和命令行 `--set` / `--port`。

# C DLL 协议

`resources/dll/l5/` 有 5 个运行库：`ppx_ble.dll`、`ppx_factory.dll`、`ppx_iap.dll`、`ppx_log.dll`、`ppx_region.dll`。`resources/dll/r3/` 有 2 个：`ppx_ble.dll`、`ppx_region.dll`。两边的 `ppx_region.dll` 同名但接口不同，含 C 全局/静态状态，禁止合并加载器。

`resources/dll/manifest.json` 按机型记录文件名、大小和 sha256，是运行库清单。进程内加载不读这份清单：`profiles/l5.py` 与 `profiles/r3.py` 给出路径，`core/protocol/dll_loader.py` 的 `PpxDll` 用 `ctypes.CDLL` 加载。`resources/dll/` 只保留运行所需 `.dll` 与 `manifest.json`，不要提交 `.a` / `.o`。

修改 `core/protocol/`（`ppx_types.py`、`ble_client.py`、`region_client.py` 等）时，字段顺序和类型必须对齐 `resources/include/l5/`、`resources/include/r3/` 的头文件，并同步更新 `tests/core/test_ppx_types.py` 与 `tests/core/test_protocol_clients.py`。

Web 启动工位走独立子进程。禁止在 Web 主进程用多线程加载不同机型 DLL。

# 测试隔离

`tests/core/` 与 `tests/apps/` 必须用 Fake/Mock 脱离物理硬件，写法参考 `tests/apps/_whitebox_fakes.py`。依赖真实串口、继电器或下位机的用例放在 `tests/hardware/`，并标记 `hardware`。

# 硬件复位

串口、继电器、心跳注册到 `core/context.py` 的 `HardwareContext`。供电或启动后立刻登记逆序紧急复位：停心跳 → 电机/抱闸释能 → 继电器断开 → 释放串口。断言失败、串口闪断、`SIGINT` / `SIGTERM`、Web 停止，都要在 `finally` 里走到该复位。

工位只编排：不组 DLL 报文，不直接调用 pyserial / ctypes。
