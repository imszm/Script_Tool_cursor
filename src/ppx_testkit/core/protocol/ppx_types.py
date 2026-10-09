"""PPX 协议 ctypes 类型（唯一一份）。

两个协议版本，DLL 同名但不兼容：

* **v1（L5 系列）**：``resources/dll/l5/ppx_region.dll`` / ``ppx_ble.dll``，
  头文件 ``resources/include/l5``；寄存器数据保存在 DLL 全局变量
  ``g_ppx_region_data`` / ``g_ppx_ble_data`` 中，format/parse 只传 msg。
* **v2（R3 系列）**：``resources/dll/r3/ppx_region.dll``，头文件 ``resources/include/r3``；
  无全局变量，format/parse 传 ``ppx_region_ctrl_t``（msg + data），
  响应需先 ``ppx_com_packet_parse`` 再 ``ppx_com_region_parse``。

字段顺序与类型必须与头文件逐一对应，``tests/unit/test_ppx_types.py`` 会校验 sizeof。
"""

from __future__ import annotations

import ctypes
from ctypes import c_int16, c_int32, c_uint8, c_uint16, c_uint32
from enum import IntEnum, IntFlag

# ------------------------------------------------------------------ 公共常量
PPX_FRAME_HEAD = 0xA5
PPX_FRAME_END = 0x55
PPX_PACKET_MIN_SIZE = 9
PPX_PARSE_OK = 1

PPX_MODEL_SIZE = 8
PPX_SN_SIZE = 26
PPX_SW_VER_SIZE_V1 = 20
PPX_SW_VER_SIZE_V2 = 32
PPX_DATA_BUF_SIZE_V1 = 192
PPX_DATA_BUF_SIZE_V2 = 416


class DevId(IntEnum):
    RSVD = 0x00
    CCB = 0x10
    MCB = 0x20
    FCB = 0x30
    BMS = 0x40
    GPRS = 0x50
    BLE = 0x60
    ALARM = 0x70
    VOICE = 0x80


class CmdType(IntEnum):
    REQ = 0x00
    RSP = 0x80
    EXCP = 0xC0


class Msg(IntEnum):
    READ = 0x01
    MULTREAD = 0x02
    WRITE = 0x03
    MULTWRITE = 0x04
    COMPARE = 0x05
    UPGRADE = 0x06
    NOTIFY = 0x07


def is_exception_cmd(cmd: int) -> bool:
    return (cmd & CmdType.EXCP) == CmdType.EXCP


def looks_like_frame(buf: bytes) -> bool:
    return len(buf) >= PPX_PACKET_MIN_SIZE and buf[0] == PPX_FRAME_HEAD and buf[-1] == PPX_FRAME_END


# ------------------------------------------------------------------ region 公共结构
class RegionExcp(ctypes.Structure):
    _fields_ = [("parse_status", c_uint8), ("cmd_status", c_uint8), ("data_status", c_uint8)]


class RegionMsg(ctypes.Structure):
    _fields_ = [
        ("id", c_uint8),
        ("cmd", c_uint8),
        ("msg_type", c_uint8),
        ("reg_addr", c_uint8),
        ("reg_nums", c_uint8),
        ("reg_excp", RegionExcp),
    ]


class RtSetting(IntFlag):
    BRAKE_LED_ON = 1 << 0
    TAIL_LED_ON = 1 << 1
    RIGHT_LED_ON = 1 << 2
    LEFT_LED_ON = 1 << 3
    CLR_ERRCODE = 1 << 15


# ------------------------------------------------------------------ v1（L5）
class RegionDataV1(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("id_num", c_uint8),
        ("model", c_uint8 * PPX_MODEL_SIZE),
        ("serial_num", c_uint8 * PPX_SN_SIZE),
        ("hw_version", c_uint16),
        ("sw_version", c_uint8 * PPX_SW_VER_SIZE_V1),
        ("rim_state", c_uint8),
        ("mcu_errcode", c_uint32),
        ("ctrl_model", c_uint8),
        ("speed_ref", c_int16),
        ("motor_speed", c_int16),
        ("bus_voltage", c_uint16),
        ("bus_current", c_uint16),
        ("phase_current_a", c_int16),
        ("phase_current_b", c_int16),
        ("phase_current_c", c_int16),
        ("hall_state", c_uint8),
        ("pi_vq", c_int16),
        ("pi_iq", c_int16),
        ("brake_state", c_uint8),
        ("imu_pitch", c_int16),
        ("imu_roll", c_int16),
        ("imu_acc", c_uint8),
        ("brake_mileage", c_uint8),
        ("motor_angle", c_int32),
        ("single_mileage", c_uint32),
        ("angular_speed", c_int16),
        ("rt_setting", c_uint16),
        ("run_mode", c_uint8),
        ("gear", c_uint8),
        ("target_speed", c_int16),
        ("rated_voltage", c_uint16),
        ("rated_current", c_uint16),
        ("max_voltage", c_uint16),
        ("min_voltage", c_uint16),
        ("acceration", c_uint32),
        ("dat_setting", c_uint32),
        ("rsvd_data", c_uint32),
    ]


class RegV1(IntEnum):
    ID_NUM = 0
    MODEL = 1
    SERIAL_NUM = 2
    HW_VERSION = 3
    SW_VERSION = 4
    RIM_STATE = 5
    MCU_ERRCODE = 6
    CTRL_MODEL = 7
    SPEED_REF = 8
    MOTOR_SPEED = 9
    BUS_VOLTAGE = 10
    BUS_CURRENT = 11
    PHASE_CUR_A = 12
    PHASE_CUR_B = 13
    PHASE_CUR_C = 14
    HALL_STATE = 15
    PI_VQ = 16
    PI_IQ = 17
    BRAKE_STATE = 18
    IMU_PITCH = 19
    IMU_ROLL = 20
    BOARD_TEMP = 21
    BRAKE_MILEAGE = 22
    MOTOR_ANGLE = 23
    SINGLE_MILEAGE = 24
    ANGULAR_SPEED = 25
    RT_SETTING = 26
    RUN_MODE = 27
    GEARS = 28
    TARGET_SPEED = 29
    RATED_VOLT = 30
    RATED_CUR = 31
    MAX_VOLTAGE = 32
    MIN_VOLTAGE = 33
    ACCERATION = 34
    DAT_SETTING = 35
    RVSD_DATA = 36


#: v1 寄存器 -> RegionDataV1 字段名（读写寄存器时用于取值/赋值）
REG_FIELD_V1: dict[int, str] = {
    RegV1.ID_NUM: "id_num",
    RegV1.HW_VERSION: "hw_version",
    RegV1.RIM_STATE: "rim_state",
    RegV1.MCU_ERRCODE: "mcu_errcode",
    RegV1.CTRL_MODEL: "ctrl_model",
    RegV1.SPEED_REF: "speed_ref",
    RegV1.MOTOR_SPEED: "motor_speed",
    RegV1.BUS_VOLTAGE: "bus_voltage",
    RegV1.BUS_CURRENT: "bus_current",
    RegV1.PHASE_CUR_A: "phase_current_a",
    RegV1.PHASE_CUR_B: "phase_current_b",
    RegV1.PHASE_CUR_C: "phase_current_c",
    RegV1.HALL_STATE: "hall_state",
    RegV1.PI_VQ: "pi_vq",
    RegV1.PI_IQ: "pi_iq",
    RegV1.BRAKE_STATE: "brake_state",
    RegV1.IMU_PITCH: "imu_pitch",
    RegV1.IMU_ROLL: "imu_roll",
    RegV1.BRAKE_MILEAGE: "brake_mileage",
    RegV1.MOTOR_ANGLE: "motor_angle",
    RegV1.SINGLE_MILEAGE: "single_mileage",
    RegV1.ANGULAR_SPEED: "angular_speed",
    RegV1.RT_SETTING: "rt_setting",
    RegV1.RUN_MODE: "run_mode",
    RegV1.GEARS: "gear",
    RegV1.TARGET_SPEED: "target_speed",
    RegV1.RATED_VOLT: "rated_voltage",
    RegV1.RATED_CUR: "rated_current",
    RegV1.MAX_VOLTAGE: "max_voltage",
    RegV1.MIN_VOLTAGE: "min_voltage",
    RegV1.ACCERATION: "acceration",
    RegV1.DAT_SETTING: "dat_setting",
    RegV1.RVSD_DATA: "rsvd_data",
}


class ModeV1(IntEnum):
    IDLE = 0
    SET = 1
    RUN = 2
    LOCK = 3
    AID = 4
    BRAKE = 5
    IAP = 6
    TEST = 7


# ------------------------------------------------------------------ v2（R3）
class RegionDataV2(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("id_num", c_uint8),
        ("model", c_uint8 * PPX_MODEL_SIZE),
        ("serial_num", c_uint8 * PPX_SN_SIZE),
        ("hw_version", c_uint16),
        ("sw_version", c_uint8 * PPX_SW_VER_SIZE_V2),
        ("mcu_errcode", c_uint32),
        ("motor_speed", c_int16),
        ("bus_voltage", c_uint16),
        ("bus_current", c_uint16),
        ("rim_state", c_uint8),
        ("ctrl_model", c_uint8),
        ("speed_ref", c_int16),
        ("mosfet_temp", c_int16),
        ("motor_temp", c_int16),
        ("motor_limit_flg", c_uint8),
        ("motor_vq", c_int32),
        ("motor_iq", c_int16),
        ("motor_cali_state", c_uint8),
        ("motor_cali_res", c_uint16),
        ("motor_cali_ld", c_uint16),
        ("motor_cali_lq", c_uint16),
        ("motor_cali_bemf", c_uint16),
        ("brake_state", c_uint8),
        ("single_mileage", c_uint32),
        ("rt_setting", c_uint16),
        ("run_mode", c_uint8),
        ("road_cond", c_uint8),
        ("target_speed", c_int16),
        ("target_accel", c_uint16),
        ("target_current", c_int16),
        ("rated_voltage", c_uint16),
        ("rated_current", c_uint16),
        ("dat_setting", c_uint32),
        ("reserved_data", c_uint32),
    ]


class RegionCtrlV2(ctypes.Structure):
    _fields_ = [("msg", RegionMsg), ("data", RegionDataV2)]


class PacketDataV2(ctypes.Structure):
    _fields_ = [
        ("id", c_uint8),
        ("cmd", c_uint8),
        ("data_len", c_uint16),
        ("data", c_uint8 * PPX_DATA_BUF_SIZE_V2),
    ]


class RegV2(IntEnum):
    ID_NUM = 0
    MODEL = 1
    SERIAL_NUM = 2
    HW_VERSION = 3
    SW_VERSION = 4
    MCU_ERRCODE = 5
    MOTOR_SPEED = 6
    BUS_VOLTAGE = 7
    BUS_CURRENT = 8
    RIM_STATE = 9
    CTRL_MODEL = 10
    SPEED_REF = 11
    MOSFET_TEMP = 12
    MOTOR_TEMP = 13
    MOTOR_LIMIT_FLG = 14
    MOTOR_VQ = 15
    MOTOR_IQ = 16
    MOTOR_CALI_STATE = 17
    MOTOR_CALI_RES = 18
    MOTOR_CALI_LD = 19
    MOTOR_CALI_LQ = 20
    MOTOR_CALI_BEMF = 21
    BRAKE_STATE = 22
    SINGLE_MILEAGE = 23
    RT_SETTING = 24
    RUN_MODE = 25
    ROAD_COND = 26
    TARGET_SPEED = 27
    TARGET_ACCEL = 28
    TARGET_CUR = 29
    RATED_VOLT = 30
    RATED_CUR = 31
    DAT_SETTING = 32
    RVSD_DATA = 33


REG_FIELD_V2: dict[int, str] = {
    RegV2.ID_NUM: "id_num",
    RegV2.HW_VERSION: "hw_version",
    RegV2.MCU_ERRCODE: "mcu_errcode",
    RegV2.MOTOR_SPEED: "motor_speed",
    RegV2.BUS_VOLTAGE: "bus_voltage",
    RegV2.BUS_CURRENT: "bus_current",
    RegV2.RIM_STATE: "rim_state",
    RegV2.CTRL_MODEL: "ctrl_model",
    RegV2.SPEED_REF: "speed_ref",
    RegV2.MOSFET_TEMP: "mosfet_temp",
    RegV2.MOTOR_TEMP: "motor_temp",
    RegV2.MOTOR_LIMIT_FLG: "motor_limit_flg",
    RegV2.MOTOR_VQ: "motor_vq",
    RegV2.MOTOR_IQ: "motor_iq",
    RegV2.MOTOR_CALI_STATE: "motor_cali_state",
    RegV2.MOTOR_CALI_RES: "motor_cali_res",
    RegV2.MOTOR_CALI_LD: "motor_cali_ld",
    RegV2.MOTOR_CALI_LQ: "motor_cali_lq",
    RegV2.MOTOR_CALI_BEMF: "motor_cali_bemf",
    RegV2.BRAKE_STATE: "brake_state",
    RegV2.SINGLE_MILEAGE: "single_mileage",
    RegV2.RT_SETTING: "rt_setting",
    RegV2.RUN_MODE: "run_mode",
    RegV2.ROAD_COND: "road_cond",
    RegV2.TARGET_SPEED: "target_speed",
    RegV2.TARGET_ACCEL: "target_accel",
    RegV2.TARGET_CUR: "target_current",
    RegV2.RATED_VOLT: "rated_voltage",
    RegV2.RATED_CUR: "rated_current",
    RegV2.DAT_SETTING: "dat_setting",
    RegV2.RVSD_DATA: "reserved_data",
}


class ModeV2(IntEnum):
    IDLE = 0
    SETTING = 1
    RUNNING = 2
    LOCK = 3
    PWR_PUSH = 4
    BRK_EMER = 5
    IAP = 6
    TESTING = 7
    REHAB_WALK = 8
    REHAB_TRAIN = 9
    BRAKE = 10
    BRK_ECND = 11
    CLUTCH_OPEN = 12


class BrakeStateV2(IntEnum):
    CLOSED = 0
    OPENING = 1
    OPENED = 2


class DatSettingV2(IntFlag):
    CHR_CHECK = 1 << 0
    IMU_OPEN = 1 << 1
    IMU_CALI = 1 << 2
    IAP_MODE = 1 << 3
    SN_WRITE = 1 << 4
    TST_MOTO = 1 << 5
    ACC_CALI = 1 << 6
    MOTOR_STOP = 1 << 10
    PARA_LEARN = 1 << 11


# ------------------------------------------------------------------ BLE v1（L5 CCB/灯板）
class BleMsg(ctypes.Structure):
    _fields_ = [("id", c_uint8), ("cmd", c_uint8), ("reg_addr", c_uint8), ("reg_nums", c_uint8)]


class LedMsgV1(ctypes.Structure):
    _fields_ = [
        ("screen_on", c_uint32, 1),
        ("brightness", c_uint32, 3),
        ("blink_period", c_uint32, 4),
        ("blink_duty", c_uint32, 4),
        ("blink_en", c_uint32, 8),
        ("err_flag", c_uint32, 2),
        ("err_code", c_uint32, 4),
        ("digital", c_uint32, 7),
        ("logo", c_uint32, 2),
        ("rim_state", c_uint32, 2),
        ("rdygo", c_uint32, 2),
        ("turn_left", c_uint32, 2),
        ("turn_right", c_uint32, 2),
        ("ring", c_uint32, 2),
        ("rsvd_data", c_uint32, 19),
    ]


LED_FIELDS_V1 = ("screen_on", "brightness", "digital", "logo", "rim_state", "rdygo", "turn_left", "turn_right", "ring")


class BleDataV1(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("id_num", c_uint8),
        ("model", c_uint8 * PPX_MODEL_SIZE),
        ("serial_num", c_uint8 * PPX_SN_SIZE),
        ("hw_version", c_uint8),
        ("sw_version", c_uint8 * PPX_SW_VER_SIZE_V1),
        ("status", c_uint32),
        ("ldr_value", c_uint16),
        ("io_status", c_uint16),
        ("led_msg", LedMsgV1),
        ("card_id", c_uint32),
        ("dat_setting", c_uint32),
    ]


class BleRegV1(IntEnum):
    ID_NUM = 0
    MODEL = 1
    SERIAL_NUM = 2
    HW_VERSION = 3
    SW_VERSION = 4
    STATUS = 5
    LDR_VALUE = 6
    IO_STATUS = 7
    LED_MSG = 8
    CARD_ID = 9
    DAT_SETTING = 10


def c_string(arr: ctypes.Array) -> str:
    """把 ctypes uint8 数组（\\0 结尾）转换为字符串。"""
    raw = bytes(arr).split(b"\x00", 1)[0]
    return raw.decode("ascii", errors="replace")


def struct_to_dict(obj: ctypes.Structure) -> dict[str, object]:
    out: dict[str, object] = {}
    for name, *_ in obj._fields_:
        value = getattr(obj, name)
        if isinstance(value, ctypes.Array):
            value = c_string(value)
        elif isinstance(value, ctypes.Structure):
            value = struct_to_dict(value)
        out[name] = value
    return out
