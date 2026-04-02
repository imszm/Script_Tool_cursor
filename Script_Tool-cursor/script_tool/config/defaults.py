from __future__ import annotations

from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "serial": {
        "relay_port": "COM4",
        "relay_ccb_port": "COM12",
        "device_port": "COM25",
        "baudrate_relay": 9600,
        "baudrate_device": 115200,
        "timeout_s": 0.1,
    },
    "commands": {
        "hex_press_on": [0x50],
        "hex_release_off": [0x4F],
        "hex_enable": [0x51],
        "ascii_off": "P",
        "ascii_on": "O",
    },
    "keywords": {
        "w3_stop": "voice_msg num: 6",
        "w3_error": "communication loss",
        "charge_success": ["voice_msgnum:9", "voice_msgnum:10"],
        "charge_error": ["assertionfailedatfunction"],
    },
    "pc_tool": {
        "upgrade_app_title": "L5 PCTOOL V3.9.00",
        "upgrade_btn_id": "Widget.buttonUpgrade",
        "upgrade_log_id": "Widget.textEditLog",
        "upgrade_wait_time_s": 170,
        "ccb_title_regex": "CCB 测试 V3.2.00.*",
        "ccb_serial_prefix": "2010007005R615GD00590",
        "ccb_coords": {
            "pass_light": [1165, 224],
            "pass_horn": [1171, 274],
        },
        "ccb_check_points": [[1701, 820], [1846, 812]],
    },
}

