#!/usr/bin/env python3
"""用 mijiaAPI 扫码登录米家，取出设备的本地 token。

为什么用它：micloud 0.6 只支持「账号密码」登录，而小米现在对这类登录强制图形验证码
（实测返回 code=87001 验证码输入错误）。mijiaAPI 走米家 App 扫码，绕开这条风控。

用法：
  # 扫码登录（首次），成功后会把 token 写进 .env
  make token
  # 只列设备、不改 .env
  make token ARGS="--list-only"
  # 指定目标设备（默认取 .env 里的 XIAOMI_IP，匹配不到就按 model/名称）
  make token ARGS="--target-ip 192.168.1.50"
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time

from mijiaAPI import mijiaAPI

AUTH_FILE = pathlib.Path(os.environ.get("MIJIA_AUTH", ".mijia-auth.json"))
ENV_FILE = pathlib.Path(os.environ.get("METER_ENV", ".env"))


def mask(token: str | None) -> str:
    if not token:
        return "（无）"
    return f"{token[:4]}……{token[-4:]}" if len(token) > 8 else "（过短）"


def request_device_list_with_token(api: mijiaAPI) -> list[dict]:
    """mijiaAPI 的 get_devices_list() 走的是较新的接口，不一定带 token/localip。

    老接口 /home/device_list 是各家取 token 工具一直用的那个，返回的信息更全，作为兜底。
    """
    try:
        ret = api._request("/home/device_list", {"getVirtualModel": False, "getHuamiDevices": 0})
    except Exception as exc:  # noqa: BLE001 - 私有接口，失败就退回
        print(f"  - /home/device_list 兜底失败：{type(exc).__name__}: {exc}")
        return []
    result = (ret or {}).get("result") or {}
    return result.get("list") or []


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-ip", default=os.environ.get("XIAOMI_IP", "").strip())
    parser.add_argument("--target-name", default="", help="按设备名匹配（IP 匹配不到时用）")
    parser.add_argument("--list-only", action="store_true", help="只打印设备列表，不写 .env")
    parser.add_argument("--qr-image", default="", metavar="PATH",
                        help="把二维码渲染成 PNG 存到该路径（给不方便看终端 ASCII 码的场景）")
    parser.add_argument("--retry", type=int, default=1, metavar="N",
                        help="二维码超时后自动换新码的次数（配合 --qr-image 时始终覆盖同一个文件）")
    args = parser.parse_args()

    print(f"登录态文件：{AUTH_FILE}（不存在则需要扫码登录）")
    api = mijiaAPI(str(AUTH_FILE))

    print("\n== 登录 ==")
    if args.qr_image:
        import qrcode
        from qrcode.image.pure import PyPNGImage

        path = pathlib.Path(args.qr_image)
        for attempt in range(1, max(1, args.retry) + 1):
            try:
                # 走私有方法只为拿到 loginUrl 自己渲染 PNG；其余流程与 api.login() 一致。
                data = api._get_qr_login_data()
                if data.get("refreshed"):
                    print("✓ 已有有效的认证，无需扫码")
                    break
                tmp = path.with_suffix(path.suffix + ".tmp")
                # 用纯 Python 的 PNG 写入器，避免引入图像库（取舍见 exporter/requirements.txt）。
                qrcode.make(data["loginUrl"], image_factory=PyPNGImage).save(tmp)
                tmp.replace(path)  # 原子替换：读图的人不会看到半张二维码
                os.chmod(path, 0o600)
                print(f"[{time.strftime('%H:%M:%S')}] 第 {attempt} 张二维码已写入 {path}（约 2 分钟内有效）")
                api._complete_qr_login(data)
                print("✓ 扫码成功")
                break
            except Exception as exc:  # noqa: BLE001
                print(f"[{time.strftime('%H:%M:%S')}] 第 {attempt} 次没成功：{type(exc).__name__}: {exc}")
                if attempt >= max(1, args.retry):
                    print("✗ 重试次数用尽，请重新执行。")
                    return 1
    else:
        try:
            api.login()  # 认证有效会自动跳过；否则在终端打印二维码并等待扫码
        except Exception as exc:  # noqa: BLE001
            print(f"✗ 登录失败：{type(exc).__name__}: {exc}")
            print("  如果是二维码超时，重新执行一次即可（会生成新码）。")
            return 1
    print("✓ 已登录")

    print("\n== 设备列表 ==")
    devices = api.get_devices_list() or []
    has_token = any(d.get("token") for d in devices)
    if not has_token:
        print("  - 新接口没返回 token，改用 /home/device_list 兜底")
        legacy = request_device_list_with_token(api)
        if legacy:
            devices = legacy

    if not devices:
        print("  没有取到任何设备。确认这个米家账号下确实有设备（且未被解绑）。")
        return 1

    print(f"  {'名称':<22} {'model':<30} {'IP':<16} {'token':<20} 在线")
    for d in devices:
        ip = str(d.get("localip") or d.get("local_ip") or "-")
        tok = d.get("token")
        online = d.get("isOnline", d.get("online", "?"))
        print(f"  {str(d.get('name') or '')[:20]:<22} {str(d.get('model') or '')[:28]:<30} "
              f"{ip[:15]:<16} {mask(tok):<20} {online}")

    json.dump(devices, open(".mijia-devices.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2, default=str)
    print("  （完整响应已存到 .mijia-devices.json）")

    if args.list_only:
        return 0

    hit = None
    for d in devices:
        ip = str(d.get("localip") or d.get("local_ip") or "")
        if args.target_ip and ip == args.target_ip:
            hit = d
            break
    if hit is None and args.target_name:
        for d in devices:
            if args.target_name in str(d.get("name") or ""):
                hit = d
                break
    if hit is None or not hit.get("token"):
        print(f"\n✗ 没找到匹配 {args.target_ip or args.target_name} 且带 token 的设备。")
        print("  请用上面的 IP 列找到你的设备，然后：make token ARGS=\"--target-ip <IP>\"")
        return 1

    token = hit["token"]
    if not ENV_FILE.exists():
        print(f"✗ 找不到 {ENV_FILE}，先执行 make init")
        return 1
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    out = []
    for line in lines:
        if line.startswith("XIAOMI_TOKEN="):
            out.append(f"XIAOMI_TOKEN={token}")
        elif line.startswith("XIAOMI_IP="):
            out.append(f"XIAOMI_IP={hit.get('localip') or args.target_ip}")
        else:
            out.append(line)
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(ENV_FILE, 0o600)
    print(f"\n✓ 已把「{hit.get('name')}」（{hit.get('model')}）的 token 写入 {ENV_FILE}")
    print(f"  token = {mask(token)}（完整值在 .env 里，{len(token)} 字符）")
    print(f"  下一步：make try-token IP={hit.get('localip') or args.target_ip}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
