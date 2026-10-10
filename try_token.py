#!/usr/bin/env python3
"""用给定 token 直接握手取设备信息（不依赖 python-miio）。

用法：
  make try-token IP=192.168.1.50          # token 从 .env 的 XIAOMI_TOKEN 读
  make try-token IP=192.168.1.50 TOKEN=<32 位 hex>

不带 token 时先读环境变量，再退回全 ff（少数设备 token 确实是全 ff）。
成功后会打印 model / 固件 / mac，并各扫一遍旧版 get_prop 与 MIoT siid.piid。
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

# 本机直接跑时（仓库根目录），把 exporter/ 加进路径；容器里 /app 已在 PYTHONPATH 上
sys.path.insert(0, str(Path(__file__).resolve().parent / "exporter"))

from miio_client import MiioDevice, MiioDeviceError, MiioError  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    ip = sys.argv[1]
    token = (sys.argv[2] if len(sys.argv) > 2 else "") or os.environ.get("XIAOMI_TOKEN", "") or "f" * 32
    print(f"目标 {ip}  token={token[:8]}……{token[-4:]}（长度 {len(token)}）")

    dev = MiioDevice(ip, token, timeout=8)
    try:
        dev.handshake()
        info = dev.info()
    except MiioError as exc:
        print(f"  ✗ 握手/取信息失败：{type(exc).__name__}: {exc}")
        return 1
    print(f"  ✓ 握手成功  did={dev.device_id.hex()}  stamp={dev.stamp}")
    print(f"    model={info.get('model')}  固件版本={info.get('fw_ver')}  硬件版本={info.get('hw_ver')}  mac={info.get('mac')}")

    # ---- 旧版属性 ----
    legacy_props = ["power", "voltage", "current", "power_consumed", "electric_power",
                    "energy_consumed", "load_power", "active_power", "power_factor",
                    "frequency", "temperature"]
    print("\n  get_prop(legacy):")
    try:
        values = dev.send("get_prop", legacy_props)
        if isinstance(values, (list, tuple)):
            hit = 0
            for name, value in zip(legacy_props, values):
                if value not in (None, "", "unknown", "error"):
                    print(f"    {name:<18} = {value!r}")
                    hit += 1
            if not hit:
                print("    （全部 unknown —— 该设备是纯 MIoT，看下面的 siid.piid 扫描）")
        else:
            print(f"    {values!r}")
    except MiioError as exc:
        print(f"    ✗ {type(exc).__name__}: {exc}")

    # ---- MIoT 属性扫描（放慢节奏：便宜模组连续快发会不响应）----
    print("\n  MIoT 扫描 siid 2~8 × piid 1~10：")
    combos = [(siid, piid) for siid in range(2, 9) for piid in range(1, 11)]
    t0 = time.time()
    try:
        found = dev.get_properties(combos, batch_size=8, batch_delay=0.3)
    except MiioDeviceError as exc:
        print(f"    ✗ 设备拒绝：{exc}")
        found = {}
    for (siid, piid), value in sorted(found.items()):
        print(f"    siid={siid} piid={piid}  = {value!r}")
    print(f"    （命中 {len(found)} 个，耗时 {time.time() - t0:.1f}s）")
    dev.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
