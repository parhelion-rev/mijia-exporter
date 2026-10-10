#!/usr/bin/env python3
"""米家设备接入方式探测工具。

回答三个问题：
  1. 设备是否响应本地 miIO 协议（决定能不能不走云端）。
  2. 设备的 token / IP 是多少（本地协议必需）。
  3. 功率、电压、电流、累计电量这些量挂在哪个属性（siid.piid）上，
     以及原始值需要乘什么系数。

只依赖标准库即可完成局域网发现和端口探测；
握手、属性扫描、token 提取需要额外依赖（见 probe-requirements.txt）。
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import time
from pathlib import Path
from typing import Any

# 本机直接跑时（仓库根目录）把 exporter/ 加进路径；容器里 /app 已在 PYTHONPATH 上
sys.path.insert(0, str(Path(__file__).resolve().parent / "exporter"))

from miio_client import MiioDevice, MiioError  # noqa: E402

MI_PORT = 54321
# miIO 发现包，与 miio_client.HELLO 相同：正好 32 字节，少 4 字节设备会直接丢弃。
HELLO = bytes.fromhex("21310020" + "ff" * 28)
SUBNET_BROADCAST = "255.255.255.255"

LEGACY_CANDIDATES = [
    "power",
    "voltage",
    "current",
    "power_consumed",
    "electric_power",
    "energy_consumed",
    "load_power",
    "active_power",
    "power_factor",
    "frequency",
    "temperature",
    "humidity",
    "on",
    "led",
]

# miot-spec 属性名 / 描述关键词 → exporter 里的指标名
SPEC_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("energy_kwh_total", ("kwh", "kilowatt", "energy", "consumed", "电量", "用电")),
    ("power_watts", ("power", "watt", "功率")),
    ("voltage_volts", ("voltage", "volt", "电压")),
    ("current_amps", ("current", "ampere", "amp", "电流")),
    ("power_factor", ("power-factor", "power factor", "功率因数")),
    ("frequency_hertz", ("frequency", "频率")),
    ("temperature_celsius", ("temperature", "温度")),
]

DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"


def title(text: str) -> None:
    print(f"\n{BOLD}== {text} =={RESET}")


def ok(text: str) -> None:
    print(f"  {GREEN}✓{RESET} {text}")


def warn(text: str) -> None:
    print(f"  {YELLOW}!{RESET} {text}")


def bad(text: str) -> None:
    print(f"  {RED}✗{RESET} {text}")


def info(text: str) -> None:
    print(f"  {DIM}{text}{RESET}")


# --------------------------------------------------------------------------- #
# 1. 局域网发现
# --------------------------------------------------------------------------- #
def _parse_hello(data: bytes) -> dict[str, Any] | None:
    if len(data) < 32 or data[:4] != HELLO[:4]:
        return None
    device_id = int.from_bytes(data[8:12], "big")
    stamp = int.from_bytes(data[12:16], "big")
    token = data[16:32].hex()
    token_valid = token not in ("0" * 32, "f" * 32)
    return {"device_id": device_id, "stamp": stamp, "token": token if token_valid else None}


def discover(broadcasts: list[str], timeout: float) -> dict[str, dict[str, Any]]:
    """向广播地址发送 miIO 握手包，收集应答。"""
    found: dict[str, dict[str, Any]] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(0.4)

    sent_to: set[str] = set()
    for target in broadcasts + [SUBNET_BROADCAST]:
        if target in sent_to:
            continue
        sent_to.add(target)
        try:
            sock.sendto(HELLO, (target, MI_PORT))
        except OSError as exc:
            warn(f"向 {target} 发送握手包失败：{exc}")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data, addr = sock.recvfrom(2048)
        except socket.timeout:
            continue
        except OSError:
            break
        parsed = _parse_hello(data)
        if parsed is None:
            continue
        parsed["ip"] = addr[0]
        parsed["raw_len"] = len(data)
        found[addr[0]] = parsed
    sock.close()
    return found


# --------------------------------------------------------------------------- #
# 2. 端口探测
# --------------------------------------------------------------------------- #
def port_scan(ip: str, ports: tuple[int, ...] = (MI_PORT, 80, 443, 1883), timeout: float = 1.5) -> dict[int, bool]:
    result: dict[int, bool] = {}
    for port in ports:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            result[port] = sock.connect_ex((ip, port)) == 0
        except OSError:
            result[port] = False
        finally:
            sock.close()
    return result


# --------------------------------------------------------------------------- #
# 3. 本地协议握手 + 属性扫描
# --------------------------------------------------------------------------- #
def probe_local(ip: str, token: str, timeout: float) -> dict[str, Any] | None:
    """返回 {model, legacy: {...}, miot: [...], info: {...}}，失败返回 None。"""
    dev = MiioDevice(ip, token, timeout=timeout)
    result: dict[str, Any] = {"legacy": {}, "miot": [], "info": {}}

    try:
        dev.handshake()
    except MiioError as exc:
        bad(f"握手失败：{exc}")
        info("常见原因：token 不对、设备只支持云端、设备不在同一网段、UDP 54321 被防火墙挡住。")
        return None

    try:
        dev_info = dev.info()
        for field in ("model", "fw_ver", "hw_ver", "mac", "did"):
            value = dev_info.get(field)
            if value:
                result["info"][field] = value
        ok(f"本地协议握手成功：model={result['info'].get('model')} fw={result['info'].get('fw_ver')}")
    except MiioError as exc:
        warn(f"握手成功但 miIO.info 不可用（{exc}），继续扫属性")

    # ---- 旧版（MIoT 之前）get_prop 扫描 ----
    try:
        values = dev.send("get_prop", LEGACY_CANDIDATES)
        if isinstance(values, str):
            warn(f"get_prop 返回错误信息：{values}")
        elif isinstance(values, list):
            for name, value in zip(LEGACY_CANDIDATES, values):
                if value in (None, "", "unknown", "error"):
                    continue
                result["legacy"][name] = value
            if result["legacy"]:
                ok(f"get_prop 可用，命中 {len(result['legacy'])} 个属性")
            else:
                info("get_prop 全部返回 unknown，该设备大概率是纯 MIoT 设备，请看下面的 siid.piid 扫描。")
    except MiioError as exc:
        info(f"get_prop 不可用（{type(exc).__name__}: {exc}），继续尝试 MIoT 属性。")

    # ---- MIoT siid.piid 扫描（放慢节奏，否则便宜模组会不响应）----
    combos = [(siid, piid) for siid in range(2, 7) for piid in range(1, 9)]
    try:
        found = dev.get_properties(combos, batch_size=8, batch_delay=0.3)
    except MiioError as exc:
        info(f"MIoT 属性读取中断（{type(exc).__name__}: {exc}）")
        found = {}
    for (siid, piid), value in sorted(found.items()):
        result["miot"].append({"siid": siid, "piid": piid, "value": value})

    if result["miot"]:
        ok(f"MIoT 属性扫描命中 {len(result['miot'])} 个可读属性（下方表格）")
    else:
        warn("MIoT 属性扫描没有命中任何可读值。")

    dev.close()
    return result


def print_scan_result(result: dict[str, Any]) -> None:
    if result["legacy"]:
        title("旧版属性（get_prop）")
        for name, value in result["legacy"].items():
            print(f"    {name:<18} = {value!r}")

    if result["miot"]:
        title("MIoT 属性（siid.piid）")
        print(f"    {'siid.piid':<12} 原始值")
        for item in result["miot"]:
            print(f"    {str(item['siid']) + '.' + str(item['piid']):<12} {item['value']!r}")
        info("对照设备本身显示的实时数值，判断某个 siid.piid 是功率还是电压，以及需要什么系数。")


# --------------------------------------------------------------------------- #
# 4. miot-spec 官方规格查询
# --------------------------------------------------------------------------- #
def fetch_json(url: str, timeout: float = 20.0) -> Any:
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "xiaomi-meter-probe/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def guess_metric(name: str, description: str) -> str | None:
    haystack = f"{name} {description}".lower()
    for metric, keywords in SPEC_KEYWORDS:
        if any(k in haystack for k in keywords):
            return metric
    return None


def spec_lookup(model: str, timeout: float = 20.0) -> dict[str, Any] | None:
    """按设备 model 查 miot-spec，返回 {type, rows:[...]}。"""
    try:
        instances = fetch_json("https://miot-spec.org/miot-spec-v2/instances?status=all", timeout)
    except Exception as exc:
        warn(f"无法获取 miot-spec 实例列表（{exc}），跳过规格查询。")
        return None

    matches = [i for i in instances.get("instances", []) if i.get("model") == model]
    if not matches:
        warn(f"miot-spec 里没有 model={model} 的公开规格，只能靠上面的实测属性值人工判断。")
        return None
    matches.sort(key=lambda i: (i.get("status") != "released", -(i.get("version") or 0)))
    chosen = matches[0]

    try:
        spec = fetch_json(f"https://miot-spec.org/miot-spec-v2/instance?type={chosen['type']}", timeout)
    except Exception as exc:
        warn(f"获取规格详情失败：{exc}")
        return None

    rows: list[dict[str, Any]] = []
    for service in spec.get("services", []):
        for prop in service.get("properties", []):
            if not prop.get("access") or "read" not in prop["access"]:
                continue
            rows.append(
                {
                    "siid": service.get("iid"),
                    "piid": prop.get("iid"),
                    "service": service.get("description"),
                    "name": prop.get("name"),
                    "description": prop.get("description"),
                    "format": prop.get("format"),
                    "unit": prop.get("unit"),
                    "access": ",".join(prop.get("access", [])),
                }
            )
    return {"type": chosen["type"], "version": chosen.get("version"), "rows": rows}


def print_spec(spec: dict[str, Any]) -> None:
    title(f"miot-spec 规格：{spec['type']}")
    print(f"    {'siid.piid':<12} {'服务':<20} {'属性':<28} {'格式':<10} {'单位':<10} 对应指标")
    for row in spec["rows"]:
        label = row.get("name") or row.get("description") or ""
        metric = guess_metric(label, row.get("description") or "")
        print(
            f"    {str(row['siid']) + '.' + str(row['piid']):<12} "
            f"{str(row.get('service') or '')[:18]:<20} "
            f"{str(label)[:26]:<28} "
            f"{str(row.get('format') or '')[:8]:<10} "
            f"{str(row.get('unit') or '-')[:8]:<10} "
            f"{GREEN if metric else DIM}{metric or '—'}{RESET}"
        )


def suggest_config(model: str, scan: dict[str, Any] | None, spec: dict[str, Any] | None) -> None:
    title("建议的 exporter 配置片段（务必人工核对系数）")
    lines = [
        "mode: local",
        "local:",
        "  ip: <设备 IP>",
        "  token: ${XIAOMI_TOKEN}",
    ]
    if spec and spec["rows"]:
        lines.append("  method: miot")
        lines.append("  miot_props:")
        seen: set[str] = set()
        for row in spec["rows"]:
            label = row.get("name") or row.get("description") or ""
            metric = guess_metric(label, row.get("description") or "")
            if not metric or metric in seen:
                continue
            seen.add(metric)
            lines.append(
                f"    {metric}: {{siid: {row['siid']}, piid: {row['piid']}, scale: 1}}"
                f"  # {label} ({row.get('unit') or '无单位'})"
            )
        if len(seen) == 1:
            info("规格里只认出 1 个指标，说明该设备的规格描述较少，请结合下面的实测扫描结果手工映射。")
    if scan and scan.get("legacy"):
        lines.append("  # 或者用旧版属性（该设备实测 get_prop 可用）：")
        lines.append("  method: legacy")
        lines.append("  legacy_props:")
        reverse = {
            "power": "power_watts",
            "voltage": "voltage_volts",
            "current": "current_amps",
            "power_consumed": "energy_kwh_total",
            "temperature": "temperature_celsius",
        }
        for prop, metric in reverse.items():
            if prop in scan["legacy"]:
                lines.append(f"    {metric}: {{prop: {prop}, scale: 1}}  # 实测原始值 {scan['legacy'][prop]!r}")

    print()
    for line in lines:
        print(f"    {line}")
    print()
    warn("scale 一律先填 1，然后对照 /metrics 输出与设备本身显示的实际数值再修正。")
    info("注意：有的设备电压返回 mV（需 0.001），有的累计电量返回 0.01 kWh 单位（需 0.01）。")


# --------------------------------------------------------------------------- #
# 5. 说明：取 token 请用 make token（mijiaAPI 扫码登录）
# --------------------------------------------------------------------------- #
# 取 token 用 make token（见 mijia_token.py）；本脚本只负责找设备和摸清属性。


# --------------------------------------------------------------------------- #
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="米家设备接入方式探测（局域网发现 / 握手 / 属性扫描 / miot-spec 查询）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python probe.py --scan-only\n"
            "  python probe.py --ip 192.168.1.50 --token <32 位 hex>\n"
            "\n取 token 用 `make token`（米家 App 扫码）；本脚本只负责找设备和摸清属性。\n"
        ),
    )
    parser.add_argument("--ip", help="设备局域网 IP（不给则先广播发现）")
    parser.add_argument("--token", help="32 位十六进制 token")
    parser.add_argument("--broadcast", action="append", default=[], help="额外的广播地址，可重复")
    parser.add_argument("--timeout", type=float, default=3.0, help="等待设备应答的秒数（默认 3）")
    parser.add_argument("--scan-only", action="store_true", help="只做局域网发现和端口探测，不握手")
    parser.add_argument("--no-spec", action="store_true", help="跳过 miot-spec 在线规格查询")
    args = parser.parse_args(argv)

    print(f"{BOLD}米家设备接入探测{RESET}  {DIM}(miIO UDP {MI_PORT}){RESET}")

    # ---- 局域网发现 ----
    title("局域网发现")
    found = discover(args.broadcast, args.timeout)
    if found:
        for ip, item in sorted(found.items()):
            token_text = f"token={item['token']}" if item["token"] else "token=（设备未在发现包里返回）"
            ok(f"{ip}  device_id={item['device_id']}  {token_text}")
    else:
        warn("没有设备应答握手包，但这不代表无法接入：")
        info("- 新版固件和纯 MIoT 设备常常不响应 UDP 广播，但仍可用 token 直连（用 --ip/--token 单独测试）；")
        info("- 如果本机在 Docker/VLAN 里，需要 --network host 才能收到广播；")
        info("- token 和设备的局域网 IP 都用 `make token` 从米家云一次拿到，再回来精确测试。")

    targets: list[tuple[str, str | None]] = []
    if args.ip:
        targets.append((args.ip, args.token))
    else:
        for ip, item in sorted(found.items()):
            targets.append((ip, item.get("token") or args.token))

    if not targets:
        title("下一步")
        print("    1) 先执行 make token：扫码登录后它会列出所有设备的 model / 局域网 IP / token，并写进 .env；")
        print("    2) 再执行：python probe.py --ip <IP> --token <TOKEN> 做握手与属性扫描；")
        print("    3) 扫描结果对应 exporter/config.yaml 里的 overrides（scale）。")
        return 0

    # ---- 端口 ----
    title("端口探测")
    for ip, _ in targets:
        ports = port_scan(ip, timeout=min(args.timeout, 2.0))
        text = "  ".join(f"{port}={'开放' if open_ else '关闭'}" for port, open_ in ports.items())
        print(f"    {ip}: {text}")
        if not ports.get(MI_PORT):
            info("TCP 54321 关闭属正常（miIO 主用 UDP），关键是下一步的握手是否成功。")

    if args.scan_only:
        return 0

    # ---- 握手 + 属性扫描 ----
    any_success = False
    for ip, token in targets:
        title(f"设备 {ip}")
        if not token:
            warn("没有 token，跳过握手。先执行 make token 扫码取 token（它会列出设备 IP 与 token）。")
            continue
        if len(token) != 32:
            warn("token 长度不是 32，仍然尝试。")
        scan = probe_local(ip, token, args.timeout)
        if not scan:
            continue
        any_success = True
        print_scan_result(scan)

        spec = None
        model = scan.get("info", {}).get("model")
        if model and not args.no_spec:
            spec = spec_lookup(model)
            if spec:
                print_spec(spec)
        suggest_config(model or "<model>", scan, spec)

    title("结论")
    if any_success:
        ok("该设备支持本地 miIO 协议 —— exporter 用 mode: local，不需要走云端。")
        info("把上面的配置片段填进 exporter/config.yaml，然后 make up 即可。")
    else:
        warn("本次没有成功建立本地连接。若确认 token/IP 无误仍失败，说明设备只上报到云端，")
        info("推荐方案：Home Assistant + xiaomi_miot 集成，让 Prometheus 直接抓 HA 的 /api/prometheus")
        info("（prometheus/prometheus.yml 里有现成的注释配置，取消注释即可）。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        sys.exit(130)
