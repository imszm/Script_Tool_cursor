# 编码规则

- 生成代码后二次确认控制流、异常路径和硬件复位。
- 串口与硬件路径必须 try/except/finally，异常写入当次 `logs/<工位>/<时间戳>/full.log`。
- 业务路径使用 logging。`print` 只用于 `cli.py` 的 `list` / `ports` / `show` 和启动前配置错误。
- 全量 Type Hints，中文注释说明功能、入参和返回值。重构不改变通过/失败判定。
- 改完代码后通过 `pytest -q` 与 `ruff check .`。迁移期保留旧导入路径的 re-export。

# 目录

- 入口是 `ppx-test`（`src/ppx_testkit/cli.py`），不要新增根目录 `main.py`。
- `core/`：通用通信、硬件抽象、协议加载、执行引擎、报告。禁止机型常量、寄存器地址和专属业务。
- `profiles/`：各机型 DLL 映射、节点 ID、寄存器表、按键/菜单枚举、通道状态机。
- `stations/`：受控工位，按功能域划分，继承 `BaseStation`，用 `@register_station` 自注册。兼容期与 `apps/` 共存；新工位不要再写入 `apps/`。
- `tools/`：非压测小工具，不接入 `stress_runner` 与报告流水线。
- `web/` 与 `cli.py` 只做参数、配置、子进程会话和状态展示。
- 工位 YAML 在 `config/stations/`，公共片段在 `config/stations/_fragments/`。本机串口只放 `config/local.yaml`。
- L5 与 R3 的 `ppx_region.dll` 分放 `resources/dll/l5` 与 `resources/dll/r3`，不合并加载器。

# 边界

- 工位只编排：读配置、组装 core 对象、按步骤执行。不组 DLL 报文，不直接调用 pyserial / ctypes，不按机型写 if 分支。
- 串口、继电器、心跳注册到 `HardwareContext`。供电或启动后立刻登记逆序紧急复位：停心跳、释能、断继电器、释放串口。`finally`、Ctrl+C 和 Web 停止都必须走到复位。
- Web 启动工位必须用独立子进程，禁止在主进程多线程加载不同机型 DLL。
- 禁止在 `core/factory.py` 或 `web/catalog.py` 用大型 if-elif 注册工位。
- 旧目录 `Tool/`、`L5*`、`R3*`、`P3*` 已删除。`resources/dll/` 只保留运行所需 `.dll` 与 `manifest.json`。
