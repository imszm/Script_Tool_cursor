"""R3 MCB 稳定性用例。由 R3McbTester 继承，顺序与旧脚本一致。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ppx_testkit.apps.whitebox.r3_names import log
from ppx_testkit.core.protocol.ppx_types import ModeV2, RegV2
from ppx_testkit.exceptions import (
    HardwareError,
    SerialDisconnectedError,
)


class R3StabilityCases:
    # ------------------------------------------------------------ 稳定性辅助
    def _health_check(self) -> bool:
        """读故障码寄存器；连续失败达到阈值时标记疑似死机。"""
        if self._read(RegV2.MCU_ERRCODE, 1, "健康检查") is None:
            self.consecutive_failures += 1
            log.warning("健康检查失败，连续失败次数: %d", self.consecutive_failures)
            if self.consecutive_failures >= self.cfg.stability.crash_threshold:
                if not self.crash_detected:
                    log.critical("!!! 疑似 MCB 死机/无响应（连续%d次健康检查失败）!!!", self.consecutive_failures)
                self.crash_detected = True
            return False
        self.consecutive_failures = 0
        return True

    def _reset_stability_stats(self) -> None:
        self.crash_detected = False
        self.consecutive_failures = 0
        self.stress_total = 0
        self.stress_failures = 0

    def _stress_write(self, label: str, reg: int, fields: Mapping[str, Any]) -> bool:
        self.stress_total += 1
        ok = self._write(reg, fields, label=label)
        if not ok:
            self.stress_failures += 1
        return ok

    def _stress_read(self, label: str, reg: int) -> Any | None:
        self.stress_total += 1
        data = self._read(reg, 1, label)
        if data is None:
            self.stress_failures += 1
        return data

    def _prepare_stability(self, *, clear: bool = True) -> None:
        self.stop_motor()
        self._pause(0.3)
        if clear:
            self.clear_error_code()
            self._pause(0.1)

    def _finish_stability(self) -> None:
        self.stop_motor()
        self._pause(0.2)

    def _crash_suffix(crashed: bool, text: str = " | 死机") -> str:
        return text if crashed else ""

    # ------------------------------------------------------------ 稳定性用例
    def stability_long_run(self) -> bool:
        st = self.cfg.stability
        cycles = st.long_run_cycles
        log.info("=== 稳定性测试: 长时间运行 %d 个周期 ===", cycles)
        self._prepare_stability()
        self._reset_stability_stats()
        self._speed_block(st.long_run_speed, 0, "长跑预设")
        self._pause(0.1)
        if not self.set_run_mode(ModeV2.RUNNING):
            self._record("长时间运行稳定性", False, "无法进入骑行模式")
            self.stop_motor()
            return False
        failures = 0
        for cycle in range(1, cycles + 1):
            if self.crash_detected:
                log.critical("第 %d/%d 周期检测到死机，终止测试。", cycle, cycles)
                break
            if not self._health_check():
                failures += 1
            if cycle % 20 == 0:
                log.info("长时间运行进度: %d/%d 周期, 当前累计失败: %d", cycle, cycles, failures)
            self._wait(st.heartbeat_interval_s)
        self._finish_stability()
        crashed = self.crash_detected
        passed = (not crashed) and failures <= cycles * st.long_run_max_fail_ratio
        detail = f"{cycles}周期, 失败{failures}次" + self._crash_suffix(crashed, " | 检测到死机")
        return self._record("长时间运行稳定性", passed, detail)

    def stability_burst(self) -> bool:
        st = self.cfg.stability
        log.info("=== 稳定性测试: 指令洪泛压力 %d 次 ===", st.burst_count)
        self._prepare_stability()
        self._reset_stability_stats()
        self.set_run_mode(ModeV2.RUNNING)
        self._pause(0.1)
        for i in range(st.burst_count):
            if self.crash_detected:
                break
            if i % 3 == 0:
                speed = (i % 600) + 50
                self._stress_write(f"洪泛速度{speed}", RegV2.TARGET_SPEED, {"target_speed": speed})
            elif i % 3 == 1:
                self._stress_read("洪泛读状态", RegV2.MCU_ERRCODE)
            else:
                self._health_check()
            self._wait(st.burst_interval_s)
        self._finish_stability()
        crashed = self.crash_detected
        rate = self.stress_failures / max(1, self.stress_total) * 100
        passed = (not crashed) and rate <= st.burst_max_fail_pct
        detail = f"{self.stress_total}次, 失败{self.stress_failures}({rate:.1f}%)" + self._crash_suffix(crashed)
        return self._record("指令洪泛压力", passed, detail)

    def stability_idle_recovery(self) -> bool:
        st = self.cfg.stability
        log.info("=== 稳定性测试: 空闲恢复 (等待 %ss) ===", st.idle_wait_s)
        self._prepare_stability()
        log.info("进入空闲状态，等待 %s 秒...", st.idle_wait_s)
        self._wait(st.idle_wait_s)
        self._reset_stability_stats()
        ok1 = self._health_check()
        ok2 = self._health_check()
        ok3 = self._stress_read("空闲后读全状态", RegV2.MCU_ERRCODE) is not None
        passed = ok1 and ok2 and ok3
        detail = "正常" if passed else f"健康检查失败(ok1={ok1},ok2={ok2},ok3={ok3})"
        return self._record("长时间空闲后恢复", passed, detail)

    def stability_mode_switch(self) -> bool:
        st = self.cfg.stability
        rounds = st.mode_switch_rounds
        log.info("=== 稳定性测试: 模式反复切换 %d 轮 ===", rounds)
        self._prepare_stability()
        self._reset_stability_stats()
        failures = 0
        for rnd in range(rounds):
            if self.crash_detected:
                break
            for mode in st.mode_switch_modes:
                if not self.set_run_mode(mode):
                    failures += 1
                self._pause(0.08)
                if not self._health_check():
                    failures += 1
                self._pause(0.05)
            if (rnd + 1) % 5 == 0:
                log.info("模式切换进度: %d/%d 轮, 失败: %d", rnd + 1, rounds, failures)
        self._finish_stability()
        crashed = self.crash_detected
        # 与旧脚本一致：阈值按轮数（而非总切换次数）计算
        passed = (not crashed) and failures <= rounds * st.mode_switch_max_fail_ratio
        total = rounds * len(st.mode_switch_modes)
        detail = f"{rounds}轮共{total}次切换, 失败{failures}次" + self._crash_suffix(crashed)
        return self._record("模式反复切换压力", passed, detail)

    def stability_ramp_repeat(self) -> bool:
        st = self.cfg.stability
        cycles = st.ramp_cycles
        log.info("=== 稳定性测试: 斜坡往复 %d 次 ===", cycles)
        self._prepare_stability()
        self._reset_stability_stats()
        failures = 0
        for cyc in range(1, cycles + 1):
            if self.crash_detected:
                break
            log.info("斜坡往复 第 %d/%d 次", cyc, cycles)
            if not self.ramp_test(st.ramp.max_speed, st.ramp.step, st.ramp.dwell_s):
                failures += 1
            self._pause(0.2)
            if not self._health_check():
                failures += 1
            self._pause(0.1)
        self._finish_stability()
        crashed = self.crash_detected
        passed = (not crashed) and failures <= cycles * st.ramp_max_fail_ratio
        detail = f"{cycles}次往复, 失败{failures}次" + self._crash_suffix(crashed)
        return self._record("斜坡往复循环", passed, detail)

    def stability_speed_jump(self) -> bool:
        st = self.cfg.stability
        log.info("=== 稳定性测试: 边界速度跳变 ===")
        self._prepare_stability()
        self._reset_stability_stats()
        if not self.set_run_mode(ModeV2.RUNNING):
            return self._record("边界速度跳变", False, "无法进入骑行")
        self._pause(0.1)
        failures = 0
        for low, high in st.jump_pairs:
            if self.crash_detected:
                break
            for _ in range(st.jump_repeat):
                if not self._stress_write(f"跳变{high}", RegV2.TARGET_SPEED, {"target_speed": high}):
                    failures += 1
                self._wait(st.jump_interval_s)
                if not self._stress_write(f"跳变{low}", RegV2.TARGET_SPEED, {"target_speed": low}):
                    failures += 1
                self._wait(st.jump_interval_s)
            self._health_check()
        self._finish_stability()
        crashed = self.crash_detected
        total_ops = len(st.jump_pairs) * st.jump_repeat * 2
        passed = (not crashed) and failures <= total_ops * st.jump_max_fail_ratio
        detail = f"{total_ops}次跳变, 失败{failures}次" + self._crash_suffix(crashed)
        return self._record("边界速度跳变", passed, detail)

    def _send_raw(self, data: bytes) -> None:
        if self._raw_write is not None:
            self._raw_write(data)
            return
        self.client.transport.write(data)

    def stability_noise(self) -> bool:
        st = self.cfg.stability
        log.info("=== 稳定性测试: 通信噪声容限 ===")
        self._prepare_stability()
        transport = getattr(self.client, "transport", None)
        if self._raw_write is None and (transport is None or not transport.is_open):
            return self._record("通信噪声容限", False, "串口未打开")
        all_recovered = True
        for idx, garbage in enumerate(st.noise_packets):
            try:
                log.info("发送噪声包 #%d: %s", idx, garbage.hex(" ").upper())
                self._send_raw(garbage)
                self._wait(st.noise_settle_s)
            except SerialDisconnectedError as exc:
                self._reconnect(exc)
            except HardwareError as exc:
                log.error("发送噪声包失败: %s", exc)
            if not self._health_check():
                log.warning("噪声包 #%d 后通信异常", idx)
                all_recovered = False
            self._wait(st.noise_interval_s)
        final_ok = self.read_motor_status()
        passed = all_recovered and final_ok
        return self._record("通信噪声容限", passed, "MCB正常恢复" if passed else "MCB通信异常/死机")

    def stability_health_monitor(self) -> bool:
        st = self.cfg.stability
        log.info("=== 稳定性测试: 持续心跳监控 %ss ===", st.monitor_duration_s)
        self.stop_motor()
        self._pause(0.3)
        self._reset_stability_stats()
        self.set_run_mode(ModeV2.RUNNING)
        self._pause(0.1)
        self._stress_write(f"心跳速度{st.monitor_speed}", RegV2.TARGET_SPEED, {"target_speed": st.monitor_speed})
        start = self._clock()
        sent = failed = 0
        while self._clock() - start < st.monitor_duration_s:
            if self.crash_detected:
                break
            sent += 1
            if not self._health_check():
                failed += 1
            self._wait(st.heartbeat_interval_s)
        self._finish_stability()
        crashed = self.crash_detected
        rate = failed / max(1, sent) * 100
        passed = (not crashed) and rate <= st.monitor_max_fail_pct
        detail = f"{st.monitor_duration_s:g}s内{sent}次心跳, 失败{failed}({rate:.1f}%)" + self._crash_suffix(crashed)
        return self._record("持续心跳监控", passed, detail)

    def run_stability(self) -> None:
        st = self.cfg.stability
        log.info(">>> 阶段二: 深度稳定性压力测试 <<<")
        log.info("配置: 长跑%d周期 | 洪泛%d次 | 空闲%ss | 斜坡%d轮 | 模式%d轮 | 用例 %s",
                 st.long_run_cycles, st.burst_count, st.idle_wait_s, st.ramp_cycles, st.mode_switch_rounds, st.cases)
        cases: dict[str, Callable[[], bool]] = {
            "long_run": self.stability_long_run,
            "burst": self.stability_burst,
            "idle_recovery": self.stability_idle_recovery,
            "mode_switch": self.stability_mode_switch,
            "ramp_repeat": self.stability_ramp_repeat,
            "speed_jump": self.stability_speed_jump,
            "noise": self.stability_noise,
            "health_monitor": self.stability_health_monitor,
        }
        for name in st.cases:
            cases[name]()
            self._pause(0.3)
