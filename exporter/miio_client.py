#!/usr/bin/env python3
"""极简 miIO 客户端：只做「按 IP 点对点通信」必需的那点事。

刻意不用 python-miio：它只被用来做 miIO 协议收发，却带来一堆与此无关的依赖
（清单见 exporter/requirements.txt）。本文件约 200 行替代它们。

协议要点（逐条对照 python-miio 的 protocol.py / miioprotocol.py）：

    报文 = 16 字节头 + 16 字节 token/校验区 + AES-128-CBC 加密的 JSON

    头:   magic(2)=0x2131 | length(2)=整包长度 | unknown(4)=0 | device_id(4) | stamp(4)
    校验区: hello 包（整包 32 字节）是全 0xff；普通包是 md5(头 + token + 密文)
    加解密: key = md5(token)，iv = md5(key + token)，PKCS7 填充
    流程:   先发一次 32 字节单播 hello 拿 device_id 与 stamp（此后每收一包都刷新 stamp），
            再用「stamp + 1 秒」作为下一个报文的时间戳 —— 与 python-miio 的
            `send_ts = self._device_ts + timedelta(seconds=1)` 一致。

注意：这里刻意**不做**网段广播扫描和 mDNS —— 那是「设备 IP 未知」时的找设备手段，
属于 probe.py 的活；exporter 拿到的是配置里的 IP，只需要单播。
"""

from __future__ import annotations

import hashlib
import json
import socket
import time
from typing import Any

from Crypto.Cipher import AES  # pycryptodome：mijiaAPI 已依赖，不必额外引入 cryptography

MAGIC = b"\x21\x31"
PORT = 54321
# 发现/握手包必须正好 32 字节：magic(4) + 0x0020(2) + unknown(4) + did(4) + stamp(4) + token(16)
# 少 4 字节真实设备会直接丢弃，表现为「怎么都握不上手」（踩过）。
HELLO = bytes.fromhex("21310020" + "ff" * 28)


class MiioError(RuntimeError):
    """通信层面失败（超时、校验不过、token 不对）。"""


class MiioDeviceError(MiioError):
    """设备明确回了 error（例如方法不存在、入参不合法）—— 重试没意义，立即失败。"""


def _md5(data: bytes) -> bytes:
    return hashlib.md5(data).digest()  # noqa: S324 - miIO 协议规定，非安全用途


def _key_iv(token: bytes) -> tuple[bytes, bytes]:
    key = _md5(token)
    return key, _md5(key + token)


def encrypt(plaintext: bytes, token: bytes) -> bytes:
    key, iv = _key_iv(token)
    pad = 16 - (len(plaintext) % 16)
    padded = plaintext + bytes([pad]) * pad          # PKCS7
    return AES.new(key, AES.MODE_CBC, iv).encrypt(padded)


def decrypt(ciphertext: bytes, token: bytes) -> bytes:
    key, iv = _key_iv(token)
    data = AES.new(key, AES.MODE_CBC, iv).decrypt(ciphertext)
    pad = data[-1] if data else 0
    if not 0 < pad <= 16:
        raise MiioError("解密后填充不合法（token 可能不对）")
    return data[:-pad]


class MiioDevice:
    """一个设备一个实例；内部复用一个 UDP socket 并维护 stamp / 消息 id。"""

    def __init__(self, ip: str, token: str | bytes, timeout: float = 8.0, port: int = PORT) -> None:
        self.ip = ip
        self.port = port
        self.timeout = timeout
        if isinstance(token, str):
            if len(token) != 32:
                raise ValueError("token 必须是 32 位十六进制字符串")
            token = bytes.fromhex(token)
        if len(token) != 16:
            raise ValueError("token 必须是 16 字节")
        self.token = token
        self.device_id = b"\xff\xff\xff\xff"
        self.stamp = 0
        self.info_dict: dict[str, Any] = {}
        self._id = 0        # 第一个报文 id 为 1，与 python-miio 一致
        self._sock: socket.socket | None = None

    # ------------------------------------------------------------------ 底层
    def _socket(self) -> socket.socket:
        if self._sock is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(self.timeout)
            self._sock = sock
        return self._sock

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def _recv(self, want_id: int | None = None) -> tuple[bytes, dict[str, Any] | None]:
        """收一包。按消息 id 过滤掉上一次超时后迟到的旧响应。"""
        deadline = time.monotonic() + self.timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise socket.timeout("等待设备响应超时")
            self._socket().settimeout(remaining)
            data, addr = self._socket().recvfrom(4096)
            if addr[0] != self.ip:
                raise MiioError(f"收到来自 {addr[0]} 的包，不是目标设备 {self.ip}")
            if len(data) < 32 or data[:2] != MAGIC:
                raise MiioError(f"响应格式不对：{data[:16].hex()}")
            if want_id is None:
                return data, None
            try:
                payload = json.loads(decrypt(data[32:], self.token).rstrip(b"\x00").decode())
            except Exception:  # noqa: BLE001 - 解不开就当作不是我们要的那包
                continue
            if payload.get("id") != want_id:
                continue          # 旧响应，继续等
            return data, payload

    # ------------------------------------------------------------------ 协议
    def handshake(self, retries: int = 3) -> None:
        """一次**单播** hello，拿 device_id 和 stamp。不做广播扫描、不做 mDNS。"""
        last: Exception | None = None
        for attempt in range(retries):
            try:
                self._socket().sendto(HELLO, (self.ip, self.port))
                data, _ = self._recv()
            except (socket.timeout, OSError, MiioError) as exc:
                last = exc
                time.sleep(0.3 * (attempt + 1))
                continue
            self.device_id = data[8:12]
            self.stamp = int.from_bytes(data[12:16], "big")
            return
        raise MiioError(
            f"设备 {self.ip} 握手无响应（{last}）：常见原因 token 不对、设备只支持云端、"
            f"不在同一网段、或 UDP {self.port} 被防火墙拦住"
        )

    def send(self, method: str, params: Any = None, retries: int = 2) -> Any:
        """发命令并返回 result。

        重试策略与 python-miio 一致（失败后重新握手），但**设备明确报错不重试**——
        像 {'code': -32602, 'message': 'need did'} 这种重试多少次都一样。
        """
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                return self._send_once(method, params)
            except MiioDeviceError:
                raise
            except (socket.timeout, MiioError, OSError) as exc:
                last = exc
                time.sleep(0.3 * (attempt + 1))
                try:
                    self.handshake(retries=1)
                except MiioError:
                    pass
        raise MiioError(f"调用 {method} 失败：{last}")

    def _send_once(self, method: str, params: Any) -> Any:
        self._id += 1
        # params 一定要带上（python-miio 连空的 params 都发 []）。方法名的大小写也别改：
        # 设备对未知方法的回包是 {'code': -9999, 'message': 'user ack timeout'} ——
        # 看起来像「设备忙」，实际是「没这个方法」，很容易误判成瞬时错误。
        payload: dict[str, Any] = {"id": self._id, "method": method, "params": params if params is not None else []}
        body = json.dumps(payload, separators=(",", ":")).encode() + b"\x00"
        encrypted = encrypt(body, self.token)

        header = (
            MAGIC
            + (len(encrypted) + 32).to_bytes(2, "big")
            + b"\x00" * 4
            + self.device_id
            + (self.stamp + 1).to_bytes(4, "big")
        )
        packet = header + _md5(header + self.token + encrypted) + encrypted
        self._socket().sendto(packet, (self.ip, self.port))

        data, reply = self._recv(want_id=self._id)
        self.stamp = int.from_bytes(data[12:16], "big")     # 刷新设备时间戳
        if reply is None:  # pragma: no cover - want_id 一定给了
            raise MiioError("响应为空")
        if "error" in reply:
            raise MiioDeviceError(f"设备返回错误：{reply['error']}")
        return reply.get("result")

    # ------------------------------------------------------------------ 便捷
    def info(self) -> dict[str, Any]:
        """`miIO.info`：model / 固件版本 / 硬件版本 / mac / did。

        注意方法名是 **miIO.info**（大写 IO），不是 miio.info —— 写错会得到
        `{'code': -9999, 'message': 'user ack timeout'}`，极易误判成设备忙。
        """
        result = self.send("miIO.info")
        self.info_dict = result if isinstance(result, dict) else {}
        return self.info_dict

    def get_properties(self, props: list[tuple[int, int]], batch_size: int = 8,
                       batch_delay: float = 0.0) -> dict[tuple[int, int], Any]:
        """按批读 MIoT 属性，返回 {(siid, piid): value}（读不到的键不出现）。

        `did` 必须带：部分固件（实测 chuangmi.plug.212a01）不带就直接回
        {'code': -32602, 'message': 'need did'}。did 是客户端自定的标识，设备会原样回传。
        批次之间可以停顿 —— 便宜 Wi-Fi 模组（ESP32 之类）被连续快发会直接不给 ACK。
        """
        out: dict[tuple[int, int], Any] = {}
        for index in range(0, len(props), max(1, batch_size)):
            if index and batch_delay:
                time.sleep(batch_delay)
            batch = props[index:index + max(1, batch_size)]
            request = [{"did": f"{siid}.{piid}", "siid": siid, "piid": piid} for siid, piid in batch]
            result = self.send("get_properties", request)
            if not isinstance(result, list):
                continue
            for item in result:
                if isinstance(item, dict) and item.get("code") == 0 and item.get("value") is not None:
                    out[(item.get("siid", 0), item.get("piid", 0))] = item["value"]
        return out
