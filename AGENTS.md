# 编码规则

- 生成的代码完之后，请二次确认代码逻辑。
- 串口通信逻辑必须包含完善的异常捕获。
- 执行脚本过程中，需要单独保存一份全程完整日志（`logs/<工位>/<时间戳>/full.log`）。

# 目录

- 入口是 `ppx-test`（`src/ppx_testkit/cli.py`），不要新增根目录 `main.py`。
- 公共能力放 `src/ppx_testkit/core/`，工位流程放 `src/ppx_testkit/apps/`。
- 工位配置放 `config/stations/`，本机串口只放 `config/local.yaml`。
- 静态资源放 `resources/`。L5 与 R3 的 `ppx_region.dll` 同名不同接口，分别使用 `resources/dll/l5` 与 `resources/dll/r3`。

# 边界

- 应用层只编排：读配置、经 `core.factory` 组装对象、按步骤执行。不在应用里组 DLL 报文，不直接调用 pyserial。
- CLI 的 `print` 只用于 `list` / `ports` / `show` 和启动前的配置错误。其余路径使用 logging。
- 旧目录 `Tool/`、`L5*`、`R3*`、`P3*` 已删除。入口只有 `ppx-test`。DLL 和头文件只放 `resources/`。
