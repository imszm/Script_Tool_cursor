# Script_Tool_cursor - 测试工具集（AI 重构实验分支）

本项目为 `Script_Tool` 的重构实验分支，主要用于探索借助 Cursor 及 AI Agent 辅助编写、优化测试脚本与重构代码。

**实验目标与规范**

* 代码规范化：全量补充 Python 类型提示（Type Hints），使用 `logging` 模块替换 `print`，完善异常捕获机制。
* 智能体协作：结合 `AGENTS.md` 提示词配置，验证 AI 在自动化测试脚本重构中的可行性。
* 脚本优化：对现有的 SMT 及整机测试逻辑进行模块化与解耦优化。

**目录结构**

结构与主项目 `Script_Tool` 保持一致，实验性功能在验证稳定后再同步回主仓库。

**环境准备**

pip install -r requirements.txt