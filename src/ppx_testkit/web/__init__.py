"""本机网页运行台。只监听 127.0.0.1，用来选工位、填串口、看日志。

工位在独立子进程里运行，网页进程不加载 L5 / R3 的 DLL。
"""

from ppx_testkit.web.server import serve

__all__ = ["serve"]
