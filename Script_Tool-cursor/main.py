from __future__ import annotations

"""
兼容入口：保留原有交互菜单，但实际执行统一交给 `script_tool` CLI。
这样不会出现 `src/` 与 `script_tool/` 两套实现各自漂移的问题。
"""

from script_tool.cli import main as cli_main


def main():
    print("\n请选择要执行的测试：")
    print("1. W3 继电器开关机测试 (需连设备串口)")
    print("2. 继电器充电测试 (需连设备串口)")
    print("3. PC 升级工具测试 (Pywinauto)")
    print("4. CCB SMT 自动化测试 (COM12 + 像素识别)")

    choice = input("请输入数字: ").strip()
    loops = int(input("请输入循环次数: ").strip() or 10)

    if choice == '1':
        return cli_main(["w3-power", "--loops", str(loops)])
    elif choice == '2':
        return cli_main(["charging", "--loops", str(loops)])
    elif choice == '3':
        return cli_main(["pc-upgrade", "--loops", str(loops)])
    elif choice == '4':
        return cli_main(["ccb-smt", "--loops", str(loops)])
    else:
        print("无效选择")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())