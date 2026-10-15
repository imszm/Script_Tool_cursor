"""R3 MCB 基础寄存器与参数用例。由 R3McbTester 继承，顺序与旧脚本一致。"""

from __future__ import annotations

from ppx_testkit.apps.whitebox.r3_names import MOTOR_STOP_BIT, log
from ppx_testkit.core.protocol.ppx_types import ModeV2, RegV2


class R3BasicCases:
    # ------------------------------------------------------------ 基础功能用例
    def test_clear_error(self) -> bool:
        return self._record("清除故障码", self.clear_error_code(), "写入CLR_ERRCODE")

    def test_read_state(self) -> bool:
        return self._record("读取电机状态", self.read_motor_status(), "寄存器5~8")

    def test_brake_state(self) -> bool:
        state = self.read_brake_state()
        return self._record("读取电磁刹状态", state is not None, f"状态码:{state}")

    def test_mode_switch(self, mode: int, name: str) -> bool:
        log.info("=== 测试: 切换%s ===", name)
        self.set_run_mode(mode)
        self._pause(0.1)
        data = self._read(RegV2.RUN_MODE, 1, f"验证{name}")
        if data is not None and int(data.run_mode) == mode:
            return self._record(f"模式切换:{name}", True, f"回读={mode}")
        actual = data.run_mode if data is not None else "N/A"
        return self._record(f"模式切换:{name}", False, f"期望{mode},实际{actual}")

    def test_all_modes(self) -> bool:
        all_ok = True
        for step in self.cfg.basic.mode_steps:
            all_ok = self.test_mode_switch(step.mode, step.name) and all_ok
            self._pause(0.15)
        return all_ok

    def test_speed_control(self, speed: int) -> bool:
        b = self.cfg.basic
        log.info("=== 测试: 速度控制 %drpm ===", speed)
        self.set_run_mode(ModeV2.RUNNING)
        self._pause(0.1)
        self.set_target_speed(speed)
        self._pause(0.3)
        name = f"速度控制{speed}rpm"
        data = self._read(RegV2.TARGET_SPEED, 2, f"验证速度{speed}")
        if data is None:
            return self._record(name, False, "回读失败")
        actual = int(data.target_speed)
        if abs(actual - speed) <= max(b.speed_tol_abs, speed * b.speed_tol_ratio):
            return self._record(name, True, f"回读={actual}")
        return self._record(name, False, f"期望{speed},实际{actual}")

    def test_current_control(self, current_01a: int) -> bool:
        log.info("=== 测试: 电流控制 %.1fA ===", current_01a * 0.1)
        self.set_run_mode(ModeV2.RUNNING)
        self._pause(0.1)
        self.set_target_current(current_01a)
        self._pause(0.2)
        name = f"电流控制{current_01a * 0.1:.1f}A"
        data = self._read(RegV2.TARGET_CUR, 1, f"验证电流{current_01a * 0.1:.1f}A")
        if data is None:
            return self._record(name, False, "回读失败")
        actual = int(data.target_current)
        if abs(actual - current_01a) <= self.cfg.basic.current_tol:
            return self._record(name, True, f"回读={actual * 0.1:.1f}A")
        return self._record(name, False, f"期望{current_01a},实际{actual}")

    def test_ramp_profile(self) -> bool:
        r = self.cfg.basic.ramp
        ok = self.ramp_test(r.max_speed, r.step, r.dwell_s)
        return self._record("斜坡测试", ok, f"0->{r.max_speed}->0")

    def test_param_boundary(self) -> bool:
        b = self.cfg.basic
        all_ok = True
        name = f"边界:负速度{b.boundary_speed}"
        if self.set_target_speed(b.boundary_speed):
            self._record(name, True, "设置成功")
        else:
            all_ok = self._record(name, False, "拒绝或失败")
        self._pause(0.1)
        self.set_run_mode(ModeV2.RUNNING)
        self._pause(0.1)
        if self.set_target_current(0):
            self._record("边界:零电流", True, "设置成功")
        else:
            all_ok = self._record("边界:零电流", False, "设置失败")
        self._pause(0.1)
        self.set_run_mode(ModeV2.IDLE)
        return all_ok

    def test_read_invalid_register(self) -> bool:
        reg = self.cfg.basic.invalid_read_reg
        log.info("=== 测试: 读取非法寄存器 (0x%02X) ===", reg)
        passed = self._read(reg, 1, "非法读") is None
        return self._record("读取非法寄存器", passed, "预期异常" if passed else "意外返回数据")

    def test_write_invalid_register(self) -> bool:
        b = self.cfg.basic
        log.info("=== 测试: 写入非法寄存器 (0x%02X) ===", b.invalid_write_reg)
        passed, detail = self._write_expect_reject(b.invalid_write_reg, {"id_num": b.invalid_write_value}, "非法写")
        return self._record("写入非法寄存器", passed, detail)

    def test_write_readonly_reg(self) -> bool:
        log.info("=== 测试: 写入只读寄存器 (motor_speed) ===")
        passed, detail = self._write_expect_reject(
            RegV2.MOTOR_SPEED, {"motor_speed": self.cfg.basic.readonly_write_value}, "写只读")
        return self._record("写入只读寄存器", passed, detail)

    def test_invalid_mode_number(self) -> bool:
        mode = self.cfg.basic.invalid_mode
        log.info("=== 测试: 无效运行模式 (%d) ===", mode)
        passed, detail = self._write_expect_reject(RegV2.RUN_MODE, {"run_mode": mode}, f"设置模式(未知{mode})")
        detail = detail.replace("写入被拒绝", "模式被拒绝")
        return self._record("无效运行模式", passed, detail)

    def test_flag_motor_stop(self) -> bool:
        b = self.cfg.basic
        log.info("=== 测试: 电机停止标志位 ===")
        self.set_run_mode(ModeV2.RUNNING)
        self._pause(0.1)
        self.set_target_speed(b.stop_flag_speed)
        self._pause(0.3)
        before = self._read(RegV2.MOTOR_SPEED, 1, "停止前速度")
        if before is None:
            self.stop_motor()
            return self._record("电机停止标志位", False, "无法读取速度")
        speed_before = int(before.motor_speed)
        log.info("停止前速度: %d rpm", speed_before)
        self._write(RegV2.RT_SETTING, {"rt_setting": MOTOR_STOP_BIT}, label="MOTOR_STOP")
        self._pause(0.3)
        after = self._read(RegV2.MOTOR_SPEED, 1, "停止后速度")
        if after is None:
            self.stop_motor()
            return self._record("电机停止标志位", False, "无法读取速度")
        speed_after = int(after.motor_speed)
        log.info("停止后速度: %d rpm", speed_after)
        reduced = abs(speed_after) < max(b.stop_flag_abs, abs(speed_before) * b.stop_flag_ratio)
        self.stop_motor()
        return self._record("电机停止标志位", reduced, f"速度从{speed_before}->{speed_after}")

    def test_rapid_commands(self) -> bool:
        b = self.cfg.basic
        log.info("=== 测试: 指令洪泛稳定性 ===")
        self.set_run_mode(ModeV2.RUNNING)
        self._pause(0.1)
        all_ok = True
        for i in range(b.rapid_count):
            all_ok = self.set_target_speed(50 + (i * 30) % 500) and all_ok
            self._wait(b.rapid_interval_s)
        final_ok = self.read_motor_status()
        self.set_run_mode(ModeV2.IDLE)
        all_ok = all_ok and final_ok
        return self._record("指令洪泛稳定性", all_ok, f"连续{b.rapid_count}次快速写" if all_ok else "通信失败")

    def test_read_all_status_registers(self) -> bool:
        b = self.cfg.basic
        lim = b.status_limits
        log.info("=== 测试: 全状态寄存器扫描 ===")
        data = self._read(RegV2.MCU_ERRCODE, b.status_scan_nums, "全状态")
        if data is None:
            return self._record("全状态寄存器扫描", False, "读取失败")
        checks = {
            "bus_voltage": lim.bus_voltage_v[0] <= data.bus_voltage * 0.1 <= lim.bus_voltage_v[1],
            "bus_current": lim.bus_current_a[0] <= data.bus_current * 0.1 <= lim.bus_current_a[1],
            "target_speed": lim.target_speed[0] <= data.target_speed <= lim.target_speed[1],
            "target_accel": lim.target_accel[0] <= data.target_accel <= lim.target_accel[1],
            "motor_speed": lim.motor_speed[0] <= data.motor_speed <= lim.motor_speed[1],
        }
        bad = [k for k, ok in checks.items() if not ok]
        return self._record("全状态寄存器扫描", not bad, "字段合理" if not bad else f"字段异常:{bad}")

    def test_response_time(self) -> bool:
        log.info("=== 测试: 通信响应时间 ===")
        start = self._clock()
        data = self._read(RegV2.MCU_ERRCODE, 1, "计时读")
        elapsed_ms = (self._clock() - start) * 1000
        if data is None:
            return self._record("通信响应时间", False, "读失败")
        passed = elapsed_ms < self.cfg.basic.response_time_max_ms
        return self._record("通信响应时间", passed, f"{elapsed_ms:.1f} ms" + ("" if passed else "(超时)"))

    # ------------------------------------------------------------ 流程
    def run_basic(self) -> None:
        b = self.cfg.basic
        self.test_clear_error()
        self._pause(0.1)
        self.test_read_state()
        self.test_brake_state()
        self._pause(0.2)
        self.test_all_modes()
        self._pause(0.2)
        for sp in b.speed_points:
            self.test_speed_control(sp)
            self._pause(0.2)
        for cur in b.current_points:
            self.test_current_control(cur)
            self._pause(0.2)
        self.test_ramp_profile()
        self._pause(0.2)
        self.test_param_boundary()
        self._pause(0.2)
        self.test_read_invalid_register()
        self.test_write_invalid_register()
        self.test_write_readonly_reg()
        self.test_invalid_mode_number()
        self.test_flag_motor_stop()
        self.test_rapid_commands()
        self.test_read_all_status_registers()
        self.test_response_time()
