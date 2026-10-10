#!/usr/bin/env python3
"""通用米家（MiJia）设备 → Prometheus exporter。

和「一台设备一个脚本」相比，通用性体现在四点：

  1. **指标名由规格推导，不写死**：从 miot-spec.org 取规格里的服务与属性，
     按属性的**单位**归到不同指标族（`mijia_watts` / `mijia_volts` / `mijia_kilowatt_hours_total`……）。
     不同单位不会混进同一个指标，换设备不用改代码。
  2. **找不到规格也能用**：发现按「规格 → MIoT siid.piid 盲扫 → 旧版 get_prop 盲扫」
     三级回退，纯 MIoT、旧版固件、规格缺失三种情况都能接。
  3. **一台 exporter 接多台设备**：cloud 传输可以用米家云批量读属性，
     连没有本地 token、只上报云端的设备也能轮询。
  4. **不碰设备控制**：只读。exporter 只做观测。

设计取舍：
  - 后台线程轮询 + HTTP 只暴露最后一次结果：设备慢或掉线不会把 Prometheus 的抓取拖垮。
  - 读失败**不清空**旧值，而是让 `mijia_device_ready` 变 0 并推进 `mijia_poll_errors_total`，
    避免「看起来正常其实是陈数据」。
  - 累计量用「绝对值 gauge + `_total` 后缀」：`increase()` / `rate()` 会自动处理设备重启归零。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import signal
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable

import yaml
from prometheus_client import CONTENT_TYPE_LATEST, REGISTRY, Counter, Gauge, generate_latest

from mijia_spec import Property, SpecStore, metric_family, sanitize, slug_label
from miio_client import MiioDevice, MiioError

VERSION = "1.0.0"
LOG = logging.getLogger("mijia-exporter")

# 旧版（MIoT 之前）属性名 → 单位/格式/是否累计量。没有规格可用时靠这张表给出像样的指标名。
LEGACY_HINTS: dict[str, tuple[str, str, bool]] = {
    "power": ("watt", "float", False),
    "load_power": ("watt", "float", False),
    "active_power": ("watt", "float", False),
    "electric_power": ("watt", "float", False),
    "voltage": ("volt", "float", False),
    "current": ("ampere", "float", False),
    "power_consumed": ("kwh", "float", True),
    "energy_consumed": ("kwh", "float", True),
    "power_factor": ("", "float", False),
    "frequency": ("hertz", "float", False),
    "temperature": ("celsius", "float", False),
    "humidity": ("percent", "float", False),
    "brightness": ("percent", "uint8", False),
    "bright": ("percent", "uint8", False),
    "ct": ("kelvin", "uint16", False),
    "color_temperature": ("kelvin", "uint16", False),
    "on": ("", "bool", False),
    "power_state": ("", "bool", False),
    "led": ("", "bool", False),
}
LEGACY_CANDIDATES = list(LEGACY_HINTS) + ["electric_current", "aqi", "pm2.5", "mode", "fan_level"]

MIOT_SCAN_SERVICES = range(1, 10)
MIOT_SCAN_PROPERTIES = range(1, 13)
BATCH_SIZE = 8

# ------------------------------------------------------------------ 指标定义
PROPERTY_LABELS = ["device", "model", "property", "siid", "piid"]
DEVICE_INFO = Gauge("mijia_device_info", "设备身份信息，值恒为 1",
                    ["device", "model", "firmware", "hardware", "mac", "transport"])
DEVICE_READY = Gauge("mijia_device_ready", "该设备最近一次读取是否新鲜（1 新鲜 / 0 过期）", ["device"])
DEVICE_CONFIGURED = Gauge("mijia_device_configured",
                          "该设备是否成功完成初始化/属性发现（1 成功 / 0 失败，失败原因见日志）", ["device"])
DEVICE_PROPERTIES = Gauge("mijia_device_properties", "该设备发现到的可读属性个数", ["device"])
LAST_SUCCESS = Gauge("mijia_last_success_timestamp_seconds",
                     "最近一次成功读取的 Unix 时间戳，用 time() 减它得到数据陈旧度", ["device"])
POLL_DURATION = Gauge("mijia_poll_duration_seconds", "最近一次读取耗时", ["device"])
POLL_ERRORS = Counter("mijia_poll_errors_total", "读取失败次数", ["device", "reason"])
BUILD_INFO = Gauge("mijia_build_info", "exporter 版本", ["version"])
# 心跳：默认不开启。开启后定期 ping 一个**外部**服务，用于「监控自己死了」这类
# Prometheus 自身发不出告警的场景。
HEARTBEAT_SENT = Counter("mijia_heartbeat_sent_total", "心跳推送次数（按结果）", ["result"])
HEARTBEAT_LAST_SUCCESS = Gauge("mijia_heartbeat_last_success_timestamp_seconds",
                               "最近一次心跳推送成功的 Unix 时间戳（未启用时不出现）")

ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


class ConfigError(RuntimeError):
    pass


# ------------------------------------------------------------------ 配置
def expand_env(value: str) -> str:
    def repl(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        return os.environ[name] if name in os.environ else (default or "")
    return ENV_RE.sub(repl, value)


def walk_expand(node: Any) -> Any:
    if isinstance(node, str):
        return expand_env(node)
    if isinstance(node, dict):
        return {k: walk_expand(v) for k, v in node.items()}
    if isinstance(node, list):
        return [walk_expand(v) for v in node]
    return node


def load_config(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    if not config_path.is_file():
        raise ConfigError(f"配置文件不存在：{config_path}（cp exporter/config.example.yaml exporter/config.yaml）")
    data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是映射：{config_path}")
    data = walk_expand(data)
    if not data.get("devices"):
        raise ConfigError("配置里至少要有 devices: 一项")
    return data


@dataclass
class DeviceConfig:
    """单台设备的配置。"""

    name: str
    transport: str = "local"
    ip: str = ""
    token: str = ""
    did: str = ""
    model: str = ""
    discovery: str = "auto"
    timeout: float = 8.0
    poll_interval: float = 60.0
    stale_after: float = 300.0
    overrides: dict[str, Any] = field(default_factory=dict)
    ignore: list[str] = field(default_factory=list)
    miot_props: dict[str, Any] = field(default_factory=dict)
    legacy_props: dict[str, Any] = field(default_factory=dict)
    batch_size: int = BATCH_SIZE
    batch_delay: float = 0.2

    @classmethod
    def parse(cls, raw: dict[str, Any], defaults: dict[str, Any]) -> "DeviceConfig":
        if not isinstance(raw, dict):
            raise ConfigError(f"devices 的每一项都必须是映射，收到 {type(raw).__name__}")
        name = str(raw.get("name") or "").strip()
        if not name:
            raise ConfigError("每个设备都要有 name（会作为 device 标签）")
        cfg = cls(
            name=name,
            transport=str(raw.get("transport") or defaults.get("transport") or "local").lower(),
            ip=str(raw.get("ip") or "").strip(),
            token=str(raw.get("token") or "").strip(),
            did=str(raw.get("did") or "").strip(),
            model=str(raw.get("model") or "").strip(),
            discovery=str(raw.get("discovery") or "auto").lower(),
            timeout=float(raw.get("timeout_seconds") or defaults.get("timeout_seconds") or 8),
            poll_interval=float(raw.get("poll_interval_seconds") or defaults.get("poll_interval_seconds") or 60),
            stale_after=float(raw.get("stale_after_seconds") or defaults.get("stale_after_seconds") or 300),
            overrides=dict(raw.get("overrides") or {}),
            ignore=list(raw.get("ignore") or []),
            miot_props=dict(raw.get("miot_props") or {}),
            legacy_props=dict(raw.get("legacy_props") or {}),
            batch_size=int(raw.get("batch_size") or defaults.get("batch_size") or BATCH_SIZE),
            batch_delay=float(raw.get("batch_delay_seconds") or defaults.get("batch_delay_seconds") or 0.2),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.transport not in ("local", "cloud", "mock"):
            raise ConfigError(f"设备 {self.name}：transport 只能是 local / cloud / mock，收到 {self.transport!r}")
        if self.discovery not in ("auto", "spec", "scan", "manual"):
            raise ConfigError(f"设备 {self.name}：discovery 只能是 auto / spec / scan / manual")
        if self.transport == "local":
            if not self.ip:
                raise ConfigError(f"设备 {self.name}：local 传输需要 ip")
            if not re.fullmatch(r"[0-9a-fA-F]{32}", self.token):
                raise ConfigError(f"设备 {self.name}：local 传输需要 32 位十六进制 token（make token 获取）")
        if self.transport == "cloud" and not self.did and not self.model:
            raise ConfigError(f"设备 {self.name}：cloud 传输至少要给 did 或 model（用来在账号里定位设备）")
        if self.discovery == "manual" and not (self.miot_props or self.legacy_props):
            raise ConfigError(f"设备 {self.name}：discovery=manual 时必须给 miot_props 或 legacy_props")
        if self.poll_interval < 5:
            raise ConfigError(f"设备 {self.name}：poll_interval_seconds 不要小于 5 秒")


@dataclass
class HeartbeatConfig:
    """外部心跳（死人开关）。默认关闭 —— 外联与否由使用者自己决定。"""

    enabled: bool = False
    url: str = ""
    fail_url: str = ""
    interval_seconds: float = 300.0
    timeout_seconds: float = 10.0
    method: str = "GET"
    require_all_devices_ready: bool = False

    @classmethod
    def parse(cls, raw: Any) -> "HeartbeatConfig":
        if raw in (None, False):
            return cls()
        if raw is True:  # heartbeat: true —— 允许，但没有 url 会被 validate 拦下
            return cls(enabled=True)
        if not isinstance(raw, dict):
            raise ConfigError("heartbeat 必须是映射（见 config.example.yaml）")
        cfg = cls(
            enabled=bool(raw.get("enabled", True)),
            url=str(raw.get("url") or "").strip(),
            fail_url=str(raw.get("fail_url") or "").strip(),
            interval_seconds=float(raw.get("interval_seconds") or 300),
            timeout_seconds=float(raw.get("timeout_seconds") or 10),
            method=str(raw.get("method") or "GET").upper(),
            require_all_devices_ready=bool(raw.get("require_all_devices_ready", False)),
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if not self.enabled:
            return
        if not self.url:
            raise ConfigError("heartbeat.enabled=true 时必须给 heartbeat.url")
        if not re.match(r"^https?://", self.url):
            raise ConfigError(f"heartbeat.url 必须以 http:// 或 https:// 开头：{self.url!r}")
        if self.fail_url and not re.match(r"^https?://", self.fail_url):
            raise ConfigError(f"heartbeat.fail_url 必须以 http:// 或 https:// 开头：{self.fail_url!r}")
        if self.method not in ("GET", "POST"):
            raise ConfigError(f"heartbeat.method 只能是 GET 或 POST，收到 {self.method!r}")
        if self.interval_seconds < 10:
            raise ConfigError("heartbeat.interval_seconds 不要小于 10 秒（外部服务一般也有最小间隔）")


# ------------------------------------------------------------------ 传输层
class Transport:
    kind = "base"

    def __init__(self, cfg: DeviceConfig) -> None:
        self.cfg = cfg
        self.model = cfg.model
        self.firmware = ""
        self.hardware = ""
        self.mac = ""
        self.did = cfg.did

    def connect(self) -> None:  # pragma: no cover - 依赖真机
        raise NotImplementedError

    def read_properties(self, props: list[Property], delay: float = 0.0) -> dict[str, Any]:
        """返回 {property.key: 原始值}，读不到的属性不出现。

        delay 覆盖默认的 batch_delay，用于盲扫这类密集请求（原因见 miio_client.get_properties）。
        """
        raise NotImplementedError

    def read_legacy(self, names: list[str]) -> dict[str, Any]:
        raise NotImplementedError


class LocalTransport(Transport):
    """局域网 miIO 直连。

    用的是内置的极简客户端（见 miio_client.py），只做「按 IP 单播握手 + 加密收发」，
    不引入 python-miio 那一堆用不上的依赖（清单见 exporter/requirements.txt）。
    """

    kind = "local"

    def __init__(self, cfg: DeviceConfig) -> None:
        super().__init__(cfg)
        self.device: MiioDevice | None = None

    def connect(self) -> None:
        if self.device is None:
            self.device = MiioDevice(self.cfg.ip, self.cfg.token, timeout=self.cfg.timeout)
        self.device.handshake()
        try:
            info = self.device.info()
        except MiioError as exc:  # 个别设备不实现 miIO.info，不影响读属性
            LOG.warning("[%s] 握手成功但读设备信息失败（不影响取数）：%s", self.cfg.name, exc)
            return
        self.model = str(info.get("model") or self.model)
        self.firmware = str(info.get("fw_ver") or "")
        self.hardware = str(info.get("hw_ver") or "")
        self.mac = str(info.get("mac") or "")
        self.did = str(info.get("did") or self.did)

    def close(self) -> None:
        if self.device is not None:
            self.device.close()

    def read_properties(self, props: list[Property], delay: float = 0.0) -> dict[str, Any]:
        if self.device is None:
            raise MiioError("尚未连接设备")
        size = max(1, int(self.cfg.batch_size))
        # delay 用于盲扫阶段放慢节奏（见 scan_miot）
        pause = self.cfg.batch_delay if delay <= 0 else delay
        raw = self.device.get_properties(
            [(p.siid, p.piid) for p in props], batch_size=size, batch_delay=pause
        )
        return {p.key: raw[(p.siid, p.piid)] for p in props if (p.siid, p.piid) in raw}

    def read_legacy(self, names: list[str]) -> dict[str, Any]:
        if self.device is None:
            return {}
        try:
            values = self.device.send("get_prop", list(names))
        except MiioError:  # 纯 MIoT 设备不支持 get_prop 是常态
            return {}
        if not isinstance(values, (list, tuple)):
            return {}
        return {
            name: value
            for name, value in zip(names, values)
            if value not in (None, "", "unknown", "error")
        }


class CloudTransport(Transport):
    """米家云（mijiaAPI）。适合没有本地 token、或不在同一网段的设备。"""

    kind = "cloud"
    _api: Any = None
    _lock = threading.Lock()

    def __init__(self, cfg: DeviceConfig, auth_file: str) -> None:
        super().__init__(cfg)
        self.auth_file = auth_file

    @classmethod
    def _client(cls, auth_file: str) -> Any:
        """拿到（并缓存）米家云客户端。

        刻意**不用 `api.login()`**：mijiaAPI 的 `login()` → `QRlogin()`，在刷新失败时会
        退化成二维码登录 —— 在无头容器里这会空等约 2 分钟长轮询、还往日志里打二维码。
        这里只做「用 passToken 刷新」，失败就直接抛错让人去重新扫码。

        auth 的自动刷新本身是可靠的：`_request()` 每次请求前都会调 `_refresh_token()`，
        token 还有效就跳过，失效就用 passToken 换新的并**回写 auth 文件** —— 所以只要
        passToken 没过期（官方说约一个月，实际更长），云端设备可以长期无人值守。
        """
        with cls._lock:
            if cls._api is None:
                try:
                    from mijiaAPI import mijiaAPI as _Api  # type: ignore
                except ImportError as exc:  # pragma: no cover
                    raise ConfigError("缺少 mijiaAPI：容器镜像已自带；本机运行请 pip install mijiaAPI") from exc
                if not Path(auth_file).is_file():
                    raise ConfigError(
                        f"云端传输需要米家登录态，但 {auth_file} 不存在：先在能扫码的环境执行 make token"
                    )
                try:
                    api = _Api(auth_file)
                    api._refresh_token()   # 私有方法，但它是「只刷新、不交互」的那一个
                except Exception as exc:  # noqa: BLE001
                    cls._api = None
                    raise ConfigError(
                        f"米家登录态无法刷新（{type(exc).__name__}: {exc}）：passToken 多半已过期，"
                        f"请重新执行 make token 扫码登录"
                    ) from exc
                cls._api = api
            return cls._api

    def connect(self) -> None:
        api = self._client(self.auth_file)
        devices = api.get_devices_list() or []
        target = None
        for dev in devices:
            if self.cfg.did and str(dev.get("did")) == self.cfg.did:
                target = dev
                break
            if not self.cfg.did and self.cfg.model and dev.get("model") == self.cfg.model:
                target = dev
                break
        if target is None:
            raise ConfigError(
                f"[{self.cfg.name}] 云端设备列表里找不到 did={self.cfg.did or '-'} model={self.cfg.model or '-'}："
                f"核对配置里的 did 或 model"
            )
        self.did = str(target.get("did") or "")
        self.model = str(target.get("model") or self.model)
        self.firmware = str(target.get("fwVersion") or "")
        self.hardware = str(target.get("hwVersion") or "")
        self.mac = str(target.get("mac") or "")

    def read_properties(self, props: list[Property], delay: float = 0.0) -> dict[str, Any]:
        api = self._client(self.auth_file)
        out: dict[str, Any] = {}
        size = max(1, min(int(self.cfg.batch_size), 20))
        pause = self.cfg.batch_delay if delay <= 0 else delay
        batches = [props[i:i + size] for i in range(0, len(props), size)]
        for index, batch in enumerate(batches):
            if index and pause:
                time.sleep(pause)   # 云端有频控，慢慢来
            request = [{"did": self.did, "siid": p.siid, "piid": p.piid} for p in batch]
            try:
                response = api.get_devices_prop(request)
            except Exception as exc:  # noqa: BLE001
                # 登录态类错误不能吞成 DEBUG：否则日志里只剩「没读到值」，
                # 而真正原因是 auth 过期，人根本查不出来。
                if type(exc).__name__ in ("LoginError", "APIError"):
                    LOG.warning("[%s] 云端读取失败，疑似米家登录态失效（重新执行 make token）：%s",
                                self.cfg.name, exc)
                else:
                    LOG.debug("[%s] 云端批量读失败：%s", self.cfg.name, exc)
                continue
            items = response if isinstance(response, list) else [response]
            index_map = {(int(r.get("siid", 0)), int(r.get("piid", 0))): r for r in items if isinstance(r, dict)}
            for prop in batch:
                item = index_map.get((prop.siid, prop.piid))
                if item and item.get("code", 0) == 0 and item.get("value") is not None:
                    out[prop.key] = item["value"]
        return out

    def read_legacy(self, names: list[str]) -> dict[str, Any]:
        LOG.info("[%s] 云端传输不支持旧版 get_prop，跳过", self.cfg.name)
        return {}


MOCK_SPEC: list[tuple[int, int, str, str, str]] = [
    (2, 1, "Switch", "bool", ""),
    (3, 1, "Power Consumption", "uint32", "kwh"),
    (3, 2, "Electric Current", "uint16", "ampere"),
    (3, 3, "Voltage", "uint16", "volt"),
    (3, 6, "Electric Power", "uint32", "watt"),
    (4, 1, "Temperature", "float", "celsius"),
]


class MockTransport(Transport):
    """不接真机也能验证整条链路（指标命名、抓取、面板）。"""

    kind = "mock"

    def __init__(self, cfg: DeviceConfig) -> None:
        super().__init__(cfg)
        self.energy = 1200.0
        self.base = 320.0
        self.power = self.base
        self.voltage = 220.0

    def connect(self) -> None:
        self.model = self.cfg.model or "mock.meter.v1"
        self.firmware, self.hardware, self.mac = "1.0.0", "mock", "aa:bb:cc:dd:ee:ff"
        self.did = self.did or "mock0001"

    @staticmethod
    def properties() -> list[Property]:
        return [
            Property(siid=s, piid=p, name=name, fmt=fmt, unit=unit, access=["read"])
            for s, p, name, fmt, unit in MOCK_SPEC
        ]

    def read_properties(self, props: list[Property], delay: float = 0.0) -> dict[str, Any]:
        self.power = max(0.0, self.power + (self.base - self.power) * 0.25 + random.gauss(0, self.base * 0.08))
        self.voltage = min(242.0, max(205.0, self.voltage + random.gauss(0, 0.7)))
        self.energy += self.power * self.cfg.poll_interval / 3600.0 / 1000.0
        # 注意：mock 直接给「已经是规格单位」的值（V / A / W / kWh），
        # 不像某些真机那样带内部系数 —— 这样 mock 模式的数字一眼就是合理的。
        values = {
            "Switch": True,
            "Power Consumption": round(self.energy, 2),
            "Electric Current": round(self.power / max(self.voltage, 1.0), 2),
            "Voltage": round(self.voltage, 1),
            "Electric Power": round(self.power, 1),
            "Temperature": round(34.0 + random.gauss(0, 0.6), 2),
        }
        return {p.key: values[p.key] for p in props if p.key in values}


# ------------------------------------------------------------------ 发现
def to_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = NUMBER_RE.search(value)
        if match:
            return float(match.group(0))
    return None


def scan_miot(transport: Transport) -> list[Property]:
    """盲扫 siid.piid：规格缺失时兜底。返回能读到的属性。

    108 个候选点分批发出，批次之间必须停顿。
    """
    candidates = [Property(siid=s, piid=p, name="", access=["read"])
                  for s in MIOT_SCAN_SERVICES for p in MIOT_SCAN_PROPERTIES]
    raw = transport.read_properties(candidates, delay=max(0.3, transport.cfg.batch_delay))
    found: list[Property] = []
    for prop in candidates:
        if prop.key not in raw:
            continue
        value = raw[prop.key]
        prop.fmt = "bool" if isinstance(value, bool) else ("string" if isinstance(value, str) else "float")
        found.append(prop)
    return found


def scan_legacy(transport: Transport) -> list[Property]:
    """盲扫旧版 get_prop 属性名：老固件设备兜底。"""
    raw = transport.read_legacy(LEGACY_CANDIDATES)
    found: list[Property] = []
    for name, value in raw.items():
        unit, fmt, _ = LEGACY_HINTS.get(name, ("", "", False))
        if not fmt:
            fmt = "bool" if isinstance(value, bool) else ("string" if isinstance(value, str) else "float")
        found.append(Property(siid=0, piid=0, name=name, fmt=fmt, unit=unit, access=["read"]))
    return found


def manual_properties(cfg: DeviceConfig, legacy: bool) -> list[Property]:
    """discovery: manual —— 用户显式给的属性表。

    属性表里可以带 unit/format/metric/cumulative/scale，这些都会被采纳，
    契约与 overrides 一致（metric 是不含 mijia_ 前缀的指标族名）。
    """
    out: list[Property] = []
    source = cfg.legacy_props if legacy else cfg.miot_props
    for key, spec in source.items():
        spec = spec or {}
        name = str(spec.get("prop") or key) if legacy else str(key)
        siid = 0 if legacy else int(spec["siid"])
        piid = 0 if legacy else int(spec["piid"])
        out.append(Property(
            siid=siid, piid=piid, name=name,
            fmt=str(spec.get("format") or ""), unit=str(spec.get("unit") or ""),
            access=["read"],
            metric_override=sanitize(str(spec["metric"])) if spec.get("metric") else "",
            cumulative_override=bool(spec["cumulative"]) if spec.get("cumulative") is not None else None,
            scale_override=float(spec["scale"]) if spec.get("scale") is not None else None,
        ))
    return out


@dataclass
class Discovery:
    properties: list[Property]
    source: str
    legacy: bool = False


class Discoverer:
    """按「规格 → MIoT 盲扫 → 旧版盲扫」三级回退发现可读属性。

    前置条件：调用前 transport 必须已经 connect()（否则拿不到 model，规格就查不了）。
    """

    def __init__(self, store: SpecStore) -> None:
        self.store = store

    def discover(self, transport: Transport, cfg: DeviceConfig) -> Discovery:
        if cfg.discovery == "manual":
            if cfg.miot_props:
                props = manual_properties(cfg, legacy=False)
                return Discovery(self.finalize(transport, cfg, props, legacy=False), "manual-miot", False)
            props = manual_properties(cfg, legacy=True)
            return Discovery(self.finalize(transport, cfg, props, legacy=True), "manual-legacy", True)

        if cfg.discovery in ("auto", "spec"):
            props = self.store.spec_for_model(cfg.model or transport.model)
            if props:
                props = self.finalize(transport, cfg, props, legacy=False)
                if props:
                    return Discovery(props, "spec", False)
            elif cfg.discovery == "spec":
                raise ConfigError(
                    f"[{cfg.name}] 规格里查不到 model={cfg.model or transport.model}："
                    f"改用 discovery: scan 或 discovery: manual"
                )

        # 退化到盲扫意味着拿不到属性名和单位：指标名会变成 mijia_value / mijia_state
        # （property 标签退化成 siid.piid），与规格模式下的名字不同 —— 面板会静默变空，
        # 所以这里必须显式告警，而不是悄悄降级。
        LOG.warning("[%s] 没有可用的规格（model=%s），退化为盲扫："
                    "指标名会变成 mijia_value/mijia_state，建议补 overrides，"
                    "或把 exporter/spec-cache 一起部署，避免依赖首次联网",
                    cfg.name, cfg.model or transport.model or "?")
        props = self.finalize(transport, cfg, scan_miot(transport), legacy=False)
        if props:
            return Discovery(props, "scan-miot", False)

        props = self.finalize(transport, cfg, scan_legacy(transport), legacy=True)
        if props:
            return Discovery(props, "scan-legacy", True)

        raise ConfigError(
            f"[{cfg.name}] 三种发现方式都没找到可读属性（规格 / MIoT 盲扫 / 旧版盲扫）："
            f"用 discovery: manual + miot_props 手工指定可读属性"
        )

    # ------------------------------------------------------------------ 内部
    def _overrides_for(self, cfg: DeviceConfig, prop: Property) -> dict[str, Any]:
        """overrides 的键可以是 siid.piid、规格名、或名字的 slug，大小写和空格不敏感。"""
        candidates = [prop.address, prop.label, slug_label(prop.key), prop.key]
        for key, value in cfg.overrides.items():
            if key in candidates or slug_label(str(key)) in candidates:
                return value or {}
        return {}

    def _apply_overrides(self, cfg: DeviceConfig, prop: Property) -> Property | None:
        spec = self._overrides_for(cfg, prop)
        if spec.get("ignore"):
            return None
        if spec.get("name"):
            prop.name = str(spec["name"])
        if spec.get("unit") is not None:
            prop.unit = str(spec["unit"])
        if spec.get("format"):
            prop.fmt = str(spec["format"])
        if spec.get("metric"):
            # 直接指定指标族名（不含 mijia_ 前缀），用来纠正规格里单位缺失/错误的情况
            prop.metric_override = sanitize(str(spec["metric"]))
        if spec.get("cumulative") is not None:
            prop.cumulative_override = bool(spec["cumulative"])
        if spec.get("scale") is not None:
            prop.scale_override = float(spec["scale"])
        return prop

    def finalize(self, transport: Transport, cfg: DeviceConfig, props: Iterable[Property],
                  legacy: bool) -> list[Property]:
        kept: list[Property] = []
        ignored = {slug_label(str(i)) for i in cfg.ignore}
        for prop in props:
            if slug_label(prop.key) in ignored or slug_label(prop.label) in ignored:
                continue
            prop = self._apply_overrides(cfg, prop)
            if prop is None:
                continue
            if not legacy and not prop.numeric:
                continue
            kept.append(prop)
        if not kept:
            return []
        # 实测一遍：规格里列了但设备不实现的属性会被剔掉（例如某些规格里的只读占位项）
        if legacy:
            raw = transport.read_legacy([p.name for p in kept])
        else:
            raw = transport.read_properties(kept)
        return [p for p in kept if p.key in raw]


# ------------------------------------------------------------------ 指标写入
# 指标族缓存必须是模块级的：prometheus_client 不允许同名指标注册两次，
# 而同一进程里可能创建多个 MetricSink（例如 --dump 之后再起服务、或单测里反复构造）。
_FAMILY_GAUGES: dict[str, Gauge] = {}
_FAMILY_LOCK = threading.Lock()


class MetricSink:
    """按需创建指标族。一个单位一个族，族内用标签区分设备与属性。"""

    def gauge(self, family: str) -> Gauge:
        with _FAMILY_LOCK:
            gauge = _FAMILY_GAUGES.get(family)
            if gauge is None:
                name = family if family.startswith("mijia_") else f"mijia_{family}"
                gauge = Gauge(name, f"米家设备属性（{family}）", PROPERTY_LABELS)
                _FAMILY_GAUGES[family] = gauge
            return gauge

    def set(self, prop: Property, labels: dict[str, str], value: float) -> str:
        name = prop.metric_name
        self.gauge(name).labels(
            device=labels["device"], model=labels["model"], property=prop.label,
            siid=str(prop.siid), piid=str(prop.piid),
        ).set(value)
        return name


# ------------------------------------------------------------------ 轮询
class DevicePoller(threading.Thread):
    def __init__(self, cfg: DeviceConfig, transport: Transport, discoverer: "Discoverer",
                 sink: MetricSink, stop_event: threading.Event) -> None:
        super().__init__(name=f"poller-{cfg.name}", daemon=True)
        self.cfg = cfg
        self.transport = transport
        self.discoverer = discoverer
        self.sink = sink
        self.stop_event = stop_event
        self.discovery: Discovery | None = None
        self.properties: list[Property] = []
        self.legacy = False
        self.discovered = False
        self.lock = threading.Lock()
        self.last_success: float | None = None
        self.last_error: str = ""
        self.values: dict[str, float] = {}

    # -------------------------------------------------------- 惰性初始化
    def ensure_ready(self) -> bool:
        """连接 + 发现属性；只做一次，失败则下次轮询再试。

        故意不做成「启动时一次性初始化」：家里的设备开机时可能不在线，
        一次性初始化失败会让它永久掉队，而重试让它自己恢复。
        """
        if self.discovered:
            return True
        try:
            self.transport.connect()
            if self.cfg.transport == "mock":
                props = MockTransport.properties()
                discovery = Discovery(
                    self.discoverer.finalize(self.transport, self.cfg, props, legacy=False), "mock", False
                )
            else:
                discovery = self.discoverer.discover(self.transport, self.cfg)
        except Exception as exc:  # noqa: BLE001
            POLL_ERRORS.labels(device=self.cfg.name, reason=f"discovery-{type(exc).__name__}").inc()
            with self.lock:
                self.last_error = f"{type(exc).__name__}: {exc}"
            DEVICE_CONFIGURED.labels(device=self.cfg.name).set(0)
            DEVICE_READY.labels(device=self.cfg.name).set(0)
            LOG.warning("[%s] 初始化/属性发现失败，下个周期重试：%s", self.cfg.name, exc)
            return False

        self.discovery = discovery
        self.properties = discovery.properties
        self.legacy = discovery.legacy
        self.discovered = True
        DEVICE_CONFIGURED.labels(device=self.cfg.name).set(1)
        DEVICE_INFO.labels(
            device=self.cfg.name, model=self.transport.model or self.cfg.model or "unknown",
            firmware=self.transport.firmware, hardware=self.transport.hardware,
            mac=self.transport.mac, transport=self.transport.kind,
        ).set(1)
        DEVICE_PROPERTIES.labels(device=self.cfg.name).set(len(self.properties))
        LOG.info("[%s] 发现 %d 个属性（来源 %s，传输 %s）",
                 self.cfg.name, len(self.properties), discovery.source, self.transport.kind)
        return True

    # ------------------------------------------------------------ 一次轮询
    def poll_once(self) -> bool:
        if not self.ensure_ready():
            return False
        started = time.monotonic()
        try:
            if self.legacy:
                raw = self.transport.read_legacy([p.name for p in self.properties])
            else:
                raw = self.transport.read_properties(self.properties)
        except Exception as exc:  # noqa: BLE001
            POLL_ERRORS.labels(device=self.cfg.name, reason=type(exc).__name__).inc()
            with self.lock:
                self.last_error = f"{type(exc).__name__}: {exc}"
            LOG.warning("[%s] 读取失败：%s", self.cfg.name, exc)
            POLL_DURATION.labels(device=self.cfg.name).set(time.monotonic() - started)
            return False
        finally:
            POLL_DURATION.labels(device=self.cfg.name).set(time.monotonic() - started)

        if not raw:
            POLL_ERRORS.labels(device=self.cfg.name, reason="EmptyResult").inc()
            with self.lock:
                self.last_error = "设备没有返回任何属性值"
            LOG.warning("[%s] 设备没有返回任何属性值", self.cfg.name)
            return False

        scaled: dict[str, float] = {}
        labels = {"device": self.cfg.name, "model": self.transport.model or self.cfg.model or "unknown"}
        for prop in self.properties:
            if prop.key not in raw:
                continue
            number = to_float(raw[prop.key])
            if number is None:
                continue
            override = self._scale_for(prop)
            value = number * override
            scaled[prop.key] = value
            self.sink.set(prop, labels, value)

        if not scaled:
            POLL_ERRORS.labels(device=self.cfg.name, reason="NoNumericValue").inc()
            with self.lock:
                self.last_error = "读到的属性都不是数值"
            return False

        with self.lock:
            self.values = scaled
            self.last_success = time.time()
            self.last_error = ""
        LAST_SUCCESS.labels(device=self.cfg.name).set(self.last_success)
        DEVICE_READY.labels(device=self.cfg.name).set(1)
        # 每次成功都走 DEBUG：60s 轮询下，一台设备一天就是 1400 行，长期跑没必要刷屏。
        # 可观测性由 mijia_last_success_timestamp_seconds / poll_duration_seconds 承担。
        LOG.debug("[%s] 读取成功：%s", self.cfg.name,
                 ", ".join(f"{k}={v:g}" for k, v in sorted(scaled.items())))
        return True

    def _scale_for(self, prop: Property) -> float:
        if prop.scale_override is not None:
            return prop.scale_override
        spec = self._override(prop)
        try:
            return float(spec.get("scale", 1) or 1)
        except (TypeError, ValueError):
            return 1.0

    def _override(self, prop: Property) -> dict[str, Any]:
        candidates = [prop.address, prop.label, slug_label(prop.key), prop.key]
        for key, value in self.cfg.overrides.items():
            if key in candidates or slug_label(str(key)) in candidates:
                return value or {}
        return {}

    # ------------------------------------------------------------ 派生指标
    def staleness(self) -> float | None:
        with self.lock:
            return None if self.last_success is None else time.time() - self.last_success

    def ready(self) -> bool:
        age = self.staleness()
        return age is not None and age <= self.cfg.stale_after

    def refresh_derived(self) -> None:
        """在 /metrics 时刷新「是否新鲜」，这样不依赖额外定时器。"""
        DEVICE_READY.labels(device=self.cfg.name).set(1 if self.ready() else 0)

    def status(self) -> dict[str, Any]:
        age = self.staleness()
        return {
            "device": self.cfg.name,
            "transport": self.transport.kind,
            "model": self.transport.model or self.cfg.model,
            "discovery": self.discovery.source if self.discovery else "pending",
            "properties": len(self.properties),
            "ready": self.ready(),
            "last_success_seconds_ago": None if age is None else round(age, 1),
            "last_error": self.last_error,
        }

    def run(self) -> None:
        while not self.stop_event.wait(self.cfg.poll_interval):
            self.poll_once()


# ------------------------------------------------------------------ 外部心跳
class Heartbeat(threading.Thread):
    """死人开关：定期 ping 一个**外部**服务。

    Prometheus 就跑在这台机器上，「这台机器断电 / Docker 挂了 / 磁盘满了」这类故障，
    它自己一条告警都发不出去，外部服务发现心跳停了才能来通知你。默认**不开启**，
    外联出口也可以用 Alertmanager 或 Grafana 的 Contact points 代替。

    require_all_devices_ready=true 时，只要有一台设备不健康就不打点（或改推 fail_url），
    把「设备挂了」也交给外部服务兜底；false 时只关心 exporter 进程本身是否活着。
    """

    def __init__(self, cfg: HeartbeatConfig, pollers: list["DevicePoller"],
                 stop_event: threading.Event, sender: Any = None) -> None:
        super().__init__(name="heartbeat", daemon=True)
        self.cfg = cfg
        self.pollers = pollers
        self.stop_event = stop_event
        self._sender = sender          # 测试注入用；None = 真发 HTTP
        self.beats = 0
        self.last_success_at: float | None = None

    def target(self) -> str | None:
        """这一拍该打到哪个 URL；None 表示这一拍不打。"""
        if self.cfg.require_all_devices_ready and not all(p.ready() for p in self.pollers):
            return self.cfg.fail_url or None
        return self.cfg.url

    def beat(self) -> bool:
        url = self.target()
        if url is None:
            HEARTBEAT_SENT.labels(result="skipped").inc()
            LOG.debug("心跳跳过：有设备未就绪且未配置 fail_url")
            return False
        # 结果标签区分「推的是健康心跳」还是「推的是失败告知」，看指标就知道当前状态
        result = "fail" if url == self.cfg.fail_url else "ok"
        self.beats += 1
        try:
            self._send(url)
        except Exception as exc:  # noqa: BLE001 - 心跳失败绝不能让 exporter 挂掉
            HEARTBEAT_SENT.labels(result="error").inc()
            LOG.warning("心跳推送失败（%s）：%s", url, exc)
            return False
        HEARTBEAT_SENT.labels(result=result).inc()
        if result == "ok":
            HEARTBEAT_LAST_SUCCESS.set(time.time())
            self.last_success_at = time.time()
        LOG.debug("心跳已推送（%s）：%s", result, url)
        return True

    def _send(self, url: str) -> None:
        if self._sender is not None:
            self._sender(url)
            return
        request = urllib.request.Request(url, method=self.cfg.method)
        with urllib.request.urlopen(request, timeout=self.cfg.timeout_seconds) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}")

    def run(self) -> None:
        while True:
            self.beat()   # 先打一拍，让外部服务立刻知道 exporter 已启动
            if self.stop_event.wait(self.cfg.interval_seconds):
                return


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = f"mijia-exporter/{VERSION}"
    # 这两个由 Exporter.run() 注入（Handler 是独立类，不能直接引用 Exporter 的实例属性）
    pollers: list[DevicePoller] = []
    heartbeat: Heartbeat | None = None

    def _respond(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path == "/metrics":
            for poller in self.pollers:
                poller.refresh_derived()
            self._respond(200, CONTENT_TYPE_LATEST, generate_latest(REGISTRY))
            return
        if path in ("/ready", "/health"):
            strict = "strict=1" in self.path
            statuses = [p.status() for p in self.pollers]
            ready = all(s["ready"] for s in statuses) if strict else any(s["ready"] for s in statuses)
            payload = {"ready": ready, "strict": strict, "devices": statuses}
            code = 200 if (ready or path == "/health") else 503
            self._respond(code, "application/json", json.dumps(payload, ensure_ascii=False).encode())
            return
        if path == "/":
            lines = [f"mijia-exporter {VERSION}", ""]
            for poller in self.pollers:
                status = poller.status()
                lines.append(
                    f"  {status['device']:<16} {status['model'] or '?':<28} {status['transport']:<6} "
                    f"{status['discovery']:<12} props={status['properties']:<3} ready={status['ready']}"
                    + (f"  上次错误：{status['last_error']}" if status["last_error"] else "")
                )
            lines += ["", "/metrics  Prometheus 指标", "/ready    就绪状态（?strict=1 要求全部设备就绪）",
                      "/health   进程存活"]
            if self.heartbeat is None:
                lines.append("外部心跳：未启用（默认关闭）")
            else:
                age = None if self.heartbeat.last_success_at is None else time.time() - self.heartbeat.last_success_at
                lines.append(
                    f"外部心跳：每 {self.heartbeat.cfg.interval_seconds:g}s → {self.heartbeat.cfg.url}"
                    f"  已推送 {self.heartbeat.beats} 次"
                    + (f"  上次成功 {age:.0f}s 前" if age is not None else "  （还没有成功过）")
                )
            self._respond(200, "text/plain; charset=utf-8", ("\n".join(lines) + "\n").encode())
            return
        self._respond(404, "text/plain; charset=utf-8", b"not found\n")

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.debug("%s - %s", self.address_string(), fmt % args)


# ------------------------------------------------------------------ 组装
class Exporter:
    def __init__(self, config: dict[str, Any], auth_file: str) -> None:
        self.config = config
        self.defaults = {
            "transport": config.get("transport"),
            "timeout_seconds": config.get("timeout_seconds"),
            "poll_interval_seconds": config.get("poll_interval_seconds"),
            "stale_after_seconds": config.get("stale_after_seconds"),
        }
        self.cache_dir = Path(str(config.get("spec_cache_dir") or "spec-cache"))
        self.store = SpecStore(self.cache_dir, ttl_hours=float(config.get("spec_cache_ttl_hours") or 720))
        self.auth_file = auth_file
        self.sink = MetricSink()
        # 心跳默认关闭；配置写错在启动时就报出来，而不是等运行到一半才发现
        self.heartbeat_cfg = HeartbeatConfig.parse(config.get("heartbeat"))
        self.heartbeat: Heartbeat | None = None
        BUILD_INFO.labels(version=VERSION).set(1)   # 任何使用方式（含 --dump/--once）都能看到版本
        self.stop_event = threading.Event()
        self.pollers: list[DevicePoller] = []

    def build_transport(self, cfg: DeviceConfig) -> Transport:
        if cfg.transport == "local":
            return LocalTransport(cfg)
        if cfg.transport == "cloud":
            return CloudTransport(cfg, self.auth_file)
        return MockTransport(cfg)

    def setup(self) -> list[DevicePoller]:
        """只组装对象，不碰设备 —— 真正的连接与属性发现在第一次轮询时惰性完成，
        失败还会自愈（见 DevicePoller.ensure_ready）。"""
        discoverer = Discoverer(self.store)
        for raw in self.config["devices"]:
            try:
                cfg = DeviceConfig.parse(raw, self.defaults)
            except ConfigError as exc:
                LOG.error("配置有误，跳过这台设备：%s", exc)
                continue
            DEVICE_CONFIGURED.labels(device=cfg.name).set(0)
            self.pollers.append(
                DevicePoller(cfg, self.build_transport(cfg), discoverer, self.sink, self.stop_event)
            )
        return self.pollers

    def run(self, port: int) -> int:
        Handler.pollers = self.pollers
        httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        httpd.daemon_threads = True

        def shutdown(signum: int, _frame: Any) -> None:
            LOG.info("收到信号 %s，正在退出……", signum)
            self.stop_event.set()
            threading.Thread(target=httpd.shutdown, daemon=True).start()

        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, shutdown)

        for poller in self.pollers:
            poller.poll_once()          # 先同步轮询一次，避免刚起来时 /metrics 是空的
            poller.start()
        if self.heartbeat_cfg.enabled:
            self.heartbeat = Heartbeat(self.heartbeat_cfg, self.pollers, self.stop_event)
            self.heartbeat.start()
            LOG.info("外部心跳已启用：每 %gs 推送一次 → %s（require_all_devices_ready=%s）",
                     self.heartbeat_cfg.interval_seconds, self.heartbeat_cfg.url,
                     self.heartbeat_cfg.require_all_devices_ready)
        else:
            LOG.info("外部心跳未启用（默认关闭）；告警出口请自备 Alertmanager 或 Grafana Contact points")
        Handler.heartbeat = self.heartbeat   # 索引页要用；漏了这行索引页会 500
        LOG.info("已启动：%d 台设备，监听 0.0.0.0:%s", len(self.pollers), port)
        try:
            httpd.serve_forever(poll_interval=0.5)
        finally:
            self.stop_event.set()
            LOG.info("已退出")
        return 0


# ------------------------------------------------------------------ CLI
def cmd_check(config: dict[str, Any], auth_file: str) -> int:
    defaults = {
        "transport": config.get("transport"),
        "timeout_seconds": config.get("timeout_seconds"),
        "poll_interval_seconds": config.get("poll_interval_seconds"),
        "stale_after_seconds": config.get("stale_after_seconds"),
    }
    for raw in config["devices"]:
        cfg = DeviceConfig.parse(raw, defaults)
        LOG.info("✓ %s（transport=%s discovery=%s poll=%ss）", cfg.name, cfg.transport, cfg.discovery, cfg.poll_interval)
    hb = HeartbeatConfig.parse(config.get("heartbeat"))
    if hb.enabled:
        LOG.info("✓ 外部心跳：每 %gs → %s（require_all_devices_ready=%s，fail_url=%s）",
                 hb.interval_seconds, hb.url, hb.require_all_devices_ready, hb.fail_url or "未设置")
    else:
        LOG.info("- 外部心跳：未启用（默认）；告警出口请自备 Alertmanager 或 Grafana Contact points")
    LOG.info("配置校验通过：%d 台设备", len(config["devices"]))
    return 0


def cmd_dump(config: dict[str, Any], auth_file: str) -> int:
    """打印每台设备发现到的属性 → 指标映射，便于写 overrides。"""
    exporter = Exporter(config, auth_file)
    for poller in exporter.setup():
        if not poller.ensure_ready():
            print(f"\n=== 设备 {poller.cfg.name} 初始化失败：{poller.last_error} ===")
            continue
        print(f"\n=== 设备 {poller.cfg.name}（{poller.transport.model}，来源 {poller.discovery.source}）===")
        print(f"  {'属性(property)':<28} {'siid.piid':<10} {'单位':<8} {'格式':<8} 指标名")
        for prop in poller.properties:
            print(f"  {prop.label[:26]:<28} {prop.address:<10} {(prop.unit or '-'):<8} "
                  f"{(prop.fmt or '-'):<8} {prop.metric_name}")
        print("\n  可粘贴的 overrides 骨架（只列需要改的）：")
        print("    overrides:")
        for prop in poller.properties:
            print(f"      {prop.label}: {{scale: 1}}   # {prop.metric_name}")
    return 0


def cmd_once(config: dict[str, Any], auth_file: str) -> int:
    exporter = Exporter(config, auth_file)
    failed = 0
    for poller in exporter.setup():
        ok = poller.poll_once()
        print(f"\n=== 设备 {poller.cfg.name}（{poller.transport.model}）{'✓' if ok else '✗'} ===")
        if not ok:
            failed += 1
            print(f"  读取失败：{poller.last_error}")
            continue
        raw_dump = None
        for prop in poller.properties:
            value = poller.values.get(prop.key)
            if value is None:
                continue
            scale = poller._scale_for(prop)
            raw = value / scale if scale else value
            print(f"  {prop.label[:26]:<28} {prop.address:<10} 原始={raw:>12g} 系数={scale:<7g} → {value:>12g} {(prop.unit or ''):<8} {prop.metric_name}")
    return 1 if failed else 0


def cmd_watch(config: dict[str, Any], auth_file: str, interval: float) -> int:
    """反复采样并打印**原始值**：校准内部系数唯一可靠的办法。

    单次读数定不了系数，得看一段时间的变化。拿到原始值后用两条物理关系反推：
      - P = U × I（同一瞬间三个原始值的比例关系）
      - 累计电量增量 ÷ 时间 ≈ 平均功率
    """
    exporter = Exporter(config, auth_file)
    pollers = exporter.setup()
    print(f"每 {interval:g} 秒采样一次，Ctrl-C 退出。下表都是**原始值**（未乘系数）。")
    try:
        while True:
            stamp = time.strftime("%H:%M:%S")
            for poller in pollers:
                if not poller.poll_once():
                    print(f"[{stamp}] {poller.cfg.name}: 读取失败：{poller.last_error}")
                    continue
                parts = []
                for prop in poller.properties:
                    scaled = poller.values.get(prop.key)
                    if scaled is None:
                        continue
                    scale = poller._scale_for(prop) or 1
                    parts.append(f"{prop.label}={scaled / scale:g}")
                print(f"[{stamp}] {poller.cfg.name}: " + "  ".join(parts))
            time.sleep(interval)
    except KeyboardInterrupt:
        print()
        return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="通用米家设备 Prometheus exporter")
    parser.add_argument("--config", default=os.environ.get("MIJIA_CONFIG", "/app/config.yaml"))
    parser.add_argument("--auth-file", default=os.environ.get("MIJIA_AUTH", ".mijia-auth.json"))
    parser.add_argument("--log-level", default=os.environ.get("LOG_LEVEL", "INFO"))
    parser.add_argument("--check", action="store_true", help="只校验配置")
    parser.add_argument("--once", action="store_true", help="每台设备真读一次并打印（含原始值与系数）")
    parser.add_argument("--watch", type=float, default=0.0, metavar="SECONDS",
                        help="反复采样并打印原始值，用于校准系数（Ctrl-C 退出）")
    parser.add_argument("--dump", action="store_true", help="打印属性 → 指标映射，便于写 overrides")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, str(args.log_level).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        LOG.error("%s", exc)
        return 2

    BUILD_INFO.labels(version=VERSION).set(1)
    port = int(config.get("http_port") or os.environ.get("HTTP_PORT") or 9101)

    if args.check:
        return cmd_check(config, args.auth_file)
    if args.dump:
        return cmd_dump(config, args.auth_file)
    if args.once:
        return cmd_once(config, args.auth_file)
    if args.watch:
        return cmd_watch(config, args.auth_file, args.watch)

    exporter = Exporter(config, args.auth_file)
    exporter.setup()
    if not exporter.pollers:
        LOG.error("配置里的设备全部无效，退出（检查 devices 的 name/transport/ip/token）")
        return 2
    return exporter.run(port)


if __name__ == "__main__":
    raise SystemExit(main())
