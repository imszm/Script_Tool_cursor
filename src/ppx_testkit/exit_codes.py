"""进程退出码。命令行、网页和压测工位都用这一组数字。

通过 0，用例失败 1，配置错误 2，硬件或协议故障 3，用户中断 130。
"""

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_CONFIG = 2
EXIT_HARDWARE = 3
EXIT_INTERRUPTED = 130
