#!/usr/bin/env python3
"""MIoT 规格（miot-spec.org）的获取/缓存/解析，以及「属性 → Prometheus 指标族」命名规则。

通用 exporter 的关键在这两件事：
  1. 按设备 model 拿到官方规格（服务/属性/siid/piid/单位/格式/读写权限），
     这样才能不写死任何 siid.piid —— 换设备不用改代码。
  2. 把「属性」映射成符合 Prometheus 习惯的指标名：单位相同的属性进同一个指标族，
     单位进名字（`mijia_watts` / `mijia_volts`），而不是把不同单位塞进一个指标再打标签。

规格是设备固件的一部分，基本不变，所以缓存到磁盘：首次联网取一次（索引约 7MB，
本地只留 model→type 的映射），之后离线也能启动。
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from prometheus_client import Gauge

LOG = logging.getLogger("mijia-exporter.spec")

SPEC_CACHE_WRITABLE = Gauge(
    "mijia_spec_cache_writable",
    "规格缓存目录是否可写。为 0 时规格无法落盘 → 退化为盲扫 → 指标名降级为 "
    "mijia_value/mijia_state，面板会查不到数据",
)

INSTANCES_URL = "https://miot-spec.org/miot-spec-v2/instances?status=all"
INSTANCE_URL = "https://miot-spec.org/miot-spec-v2/instance?type={type}"
DEFAULT_CACHE_DIR = Path("spec-cache")

# 单位 → 指标族后缀。Prometheus 习惯：单位放最后、用复数。
UNIT_FAMILIES: dict[str, str] = {
    "watt": "watts", "watts": "watts", "w": "watts",
    "volt": "volts", "volts": "volts", "v": "volts",
    "ampere": "amperes", "amperes": "amperes", "amp": "amperes", "a": "amperes",
    "kwh": "kilowatt_hours", "kilowatt_hour": "kilowatt_hours", "kilowatt_hours": "kilowatt_hours",
    "wh": "watt_hours", "watt_hour": "watt_hours",
    "celsius": "celsius", "°c": "celsius",
    "kelvin": "kelvin", "k": "kelvin",
    "hertz": "hertz", "hz": "hertz",
    "percentage": "percent", "percent": "percent", "%": "percent",
    "second": "seconds", "seconds": "seconds", "s": "seconds",
    "minute": "minutes", "minutes": "minutes", "min": "minutes",
    "hour": "hours", "hours": "hours", "h": "hours",
    "day": "days", "days": "days",
    "lux": "lux", "ppm": "ppm",
    "pa": "pascals", "kpa": "kilopascals",
    "mg/m3": "milligrams_per_cubic_meter",
    "ug/m3": "micrograms_per_cubic_meter",
    "m3": "cubic_meters",
    "m3/h": "cubic_meters_per_hour",
    "l": "liters", "ml": "milliliters",
    "db": "decibels",
}

# 这些单位字符串其实是「没有单位」，规格里很常见（实测同一台设备既有 `none` 也有 `-`）
NO_UNIT = {"", "none", "null", "-", "n/a", "na", "unknown", "无", "0"}

# 规格没给单位时，靠属性名猜单位。现实里规格质量参差：
# 同一台 chuangmi 插座的 power-consumption 单位是 `none`、voltage 单位是 `-`，
# 不兜底就会得到 mijia_none_total / mijia_value 这种没法用的指标名。
# 按顺序匹配子串，所以更具体的要排在前面（power-consumption 必须排在 power 之前）。
NAME_UNIT_HINTS: list[tuple[str, str, bool]] = [
    ("power-consumption", "kwh", True),
    ("power-consumed", "kwh", True),
    ("power_consumed", "kwh", True),
    ("energy", "kwh", True),
    ("electric-power", "watt", False),
    ("surge-power", "watt", False),
    ("power", "watt", False),
    ("electric-current", "ampere", False),
    ("current", "ampere", False),
    ("voltage", "volt", False),
    ("temperature", "celsius", False),
    ("humidity", "percent", False),
    ("battery", "percent", False),
    ("frequency", "hertz", False),
    ("brightness", "percent", False),
    ("duration", "seconds", False),
]

# 这些单位天然是「累计量」，指标名要加 _total，查询时用 increase()/rate()
CUMULATIVE_UNITS = {"kwh", "kilowatt_hours", "wh", "watt_hours"}
# 名字里带这些词的也算累计量（有些规格不给单位）
CUMULATIVE_HINTS = re.compile(r"(consum|energy|total|累计|电量|用电)", re.I)

_SLUG_RE = re.compile(r"[^0-9a-zA-Z]+")


def sanitize(text: str) -> str:
    """把任意文本变成能放进指标名/标签值的 slug。"""
    out = text.replace("/", " per ").replace("%", " percent ")
    out = _SLUG_RE.sub("_", out).strip("_").lower()
    return out or "unknown"


def slug_label(text: str) -> str:
    """属性名做成标签值：Electric Current → electric-current。"""
    out = re.sub(r"[^0-9a-zA-Z]+", "-", text or "").strip("-").lower()
    return out or "unknown"


def hint_unit(name: str) -> tuple[str, bool]:
    """按属性名猜 (单位, 是否累计量)。

    先 slug 再匹配：规格里的名字是 `Power Consumption`（带空格、首字母大写），
    而关键词表写的是 `power-consumption`，不归一化就只会命中更短的 `power`，
    结果把累计电量错认成功率。
    """
    key = slug_label(name)
    for token, unit, cumulative in NAME_UNIT_HINTS:
        if token in key:
            return unit, cumulative
    return "", False


def metric_family(unit: str | None, fmt: str | None, name: str) -> tuple[str, bool]:
    """返回 (指标族名, 是否累计量)。

    指标族名不含 `mijia_` 前缀，也不含累计量的 `_total` 后缀 —— 由调用方拼。
    单位来源优先级：规格里的单位 > 按属性名推断 > 按格式兜底（bool→state、数值→value）。
    bool 属性直接进 state、不参与按名推断（否则 power-enable 会被当成瓦特）。
    """
    u = (unit or "").strip().lower()
    if u in NO_UNIT:
        u = ""
    f = (fmt or "").lower()
    is_bool = "bool" in f

    hinted_cumulative = False
    if not u and not is_bool:
        u, hinted_cumulative = hint_unit(name)

    family = UNIT_FAMILIES.get(u)
    cumulative = u in CUMULATIVE_UNITS or hinted_cumulative

    if family is None and u:
        family = sanitize(u)          # 没见过的单位：把它本身变成族名，保证不会混单位
    if family is None:
        if is_bool:
            family = "state"
        elif "string" in f:
            family = "text"           # 文本属性不会被写成数值指标（见 exporter 的过滤）
        else:
            family = "value"          # 无单位数值：阈值、档位、故障码等

    if not cumulative and family not in ("state", "text") and CUMULATIVE_HINTS.search(name or ""):
        cumulative = True
    return family, cumulative


@dataclass
class Property:
    """一个可读属性。

    metric_override / cumulative_override 是给配置里的 overrides 用的逃生口：
    规格里单位缺失或标错时（现实中很常见），用户可以直接指定指标族名。
    """

    siid: int
    piid: int
    name: str
    fmt: str = ""
    unit: str = ""
    access: list[str] = field(default_factory=list)
    value_list: list[dict[str, Any]] = field(default_factory=list)
    service: str = ""
    metric_override: str = ""
    cumulative_override: bool | None = None
    scale_override: float | None = None

    @property
    def key(self) -> str:
        """配置里 overrides / ignore 用的键：优先用规格里的名字，其次 siid.piid。"""
        return self.name or f"{self.siid}.{self.piid}"

    @property
    def address(self) -> str:
        return f"{self.siid}.{self.piid}"

    @property
    def label(self) -> str:
        """property 标签值。规格没给名字的（常见于布尔开关）退回 siid.piid。"""
        return slug_label(self.name) if self.name else self.address

    @property
    def family(self) -> str:
        if self.metric_override:
            return sanitize(self.metric_override)
        return metric_family(self.unit, self.fmt, self.name or self.address)[0]

    @property
    def cumulative(self) -> bool:
        if self.cumulative_override is not None:
            return self.cumulative_override
        return metric_family(self.unit, self.fmt, self.name or self.address)[1]

    @property
    def metric_name(self) -> str:
        return f"mijia_{self.family}" + ("_total" if self.cumulative else "")

    @property
    def numeric(self) -> bool:
        """bool / 数值属性可以写成指标；字符串属性不行。"""
        return "string" not in (self.fmt or "").lower()

    def to_dict(self) -> dict[str, Any]:
        return {
            "siid": self.siid, "piid": self.piid, "name": self.name, "format": self.fmt,
            "unit": self.unit, "access": self.access, "service": self.service,
            "metric": self.metric_name, "label": self.label, "cumulative": self.cumulative,
        }


def parse_instance(spec: dict[str, Any]) -> list[Property]:
    """把规格的 instance 文档解析成可读属性列表。"""
    props: list[Property] = []
    for service in spec.get("services", []) or []:
        service_name = service.get("description") or service.get("type") or ""
        for item in service.get("properties", []) or []:
            access = [str(a).lower() for a in (item.get("access") or [])]
            if "read" not in access:
                continue
            # 规格里 name 经常是空的，真正的名字在 description
            name = (item.get("name") or item.get("description") or "").strip()
            props.append(
                Property(
                    siid=int(service.get("iid", 0)),
                    piid=int(item.get("iid", 0)),
                    name=name,
                    fmt=str(item.get("format") or ""),
                    unit=str(item.get("unit") or ""),
                    access=access,
                    value_list=item.get("value-list") or [],
                    service=str(service_name),
                )
            )
    return props


class SpecStore:
    """规格索引 + 单设备规格的磁盘缓存。"""

    def __init__(self, cache_dir: Path | str = DEFAULT_CACHE_DIR, ttl_hours: float = 720.0,
                 timeout: float = 20.0) -> None:
        self.cache_dir = Path(cache_dir)
        self.ttl_seconds = max(0.0, ttl_hours) * 3600.0
        self.timeout = timeout
        self._index: dict[str, str] | None = None
        self.check_writable()

    # ------------------------------------------------------- 缓存目录可写性
    def check_writable(self) -> bool:
        """探测缓存目录是否可写，并把它暴露成指标。

        这是踩过的真实故障：宿主上的挂载目录属主是 root、容器以非 root 运行，
        规格文件写不进去 → 退化成盲扫 → 指标名从 `mijia_watts` 变成 `mijia_value`，
        面板全空而容器健康检查依然是 healthy。所以要让它可告警。
        """
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            probe = self.cache_dir / ".write-probe"
            probe.write_text("", encoding="utf-8")
            probe.unlink()
        except Exception as exc:  # noqa: BLE001
            SPEC_CACHE_WRITABLE.set(0)
            LOG.warning("规格缓存目录不可写（%s）：%s。"
                        "规格将无法落盘缓存：一旦 miot-spec.org 不可达就会退化为盲扫、指标名降级为 mijia_value/mijia_state；"
                        "检查挂载目录属主是否与容器运行 uid 一致", self.cache_dir, exc)
            return False
        SPEC_CACHE_WRITABLE.set(1)
        return True

    def _write(self, path: Path, text: str) -> bool:
        try:
            path.write_text(text, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            LOG.warning("写入规格缓存失败（%s）：%s", path, exc)
            self.check_writable()
            return False
        SPEC_CACHE_WRITABLE.set(1)
        return True

    # ---------------------------------------------------------------- 索引
    @property
    def index_path(self) -> Path:
        return self.cache_dir / "model-index.json"

    def _fetch_json(self, url: str) -> Any:
        req = urllib.request.Request(url, headers={"User-Agent": "mijia-prometheus-exporter/1.0"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def load_index(self, refresh: bool = False) -> dict[str, str]:
        """model → 规格 type 的映射（只留 released 里的最高版本）。"""
        if self._index is not None and not refresh:
            return self._index

        cached: dict[str, str] = {}
        fresh = False
        if self.index_path.is_file():
            try:
                blob = json.loads(self.index_path.read_text(encoding="utf-8"))
                cached = blob.get("models") or {}
                fresh = (time.time() - float(blob.get("fetched_at") or 0)) < self.ttl_seconds
            except Exception as exc:  # noqa: BLE001
                LOG.warning("规格索引缓存损坏，将重新获取：%s", exc)

        if cached and fresh and not refresh:
            self._index = cached
            return cached

        try:
            raw = self._fetch_json(INSTANCES_URL)
            best: dict[str, tuple[bool, int, str]] = {}
            for item in raw.get("instances", []) or []:
                model, type_ = item.get("model"), item.get("type")
                if not model or not type_:
                    continue
                released = item.get("status") == "released"
                version = int(item.get("version") or 0)
                current = best.get(model)
                if current is None or (released, version) > (current[0], current[1]):
                    best[model] = (released, version, type_)
            models = {m: v[2] for m, v in best.items()}
            self._write(
                self.index_path,
                json.dumps({"fetched_at": time.time(), "models": models}, ensure_ascii=False),
            )
            LOG.info("已更新规格索引：%d 个型号（缓存 %s）", len(models), self.index_path)
            self._index = models
            return models
        except Exception as exc:  # noqa: BLE001
            if cached:
                LOG.warning("获取规格索引失败（%s），改用本地缓存（可能已过期）", exc)
                self._index = cached
                return cached
            LOG.warning("获取规格索引失败且无本地缓存：%s", exc)
            self._index = {}
            return {}

    # ------------------------------------------------------------ 单设备规格
    def spec_path(self, spec_type: str) -> Path:
        return self.cache_dir / f"spec-{sanitize(spec_type)}.json"

    def spec_for_model(self, model: str, refresh: bool = False) -> list[Property] | None:
        """按 model 拿可读属性；规格里查不到这个型号时返回 None。"""
        if not model:
            return None
        spec_type = self.load_index(refresh=refresh).get(model)
        if not spec_type:
            LOG.info("规格库里没有 model=%s 的公开规格", model)
            return None

        path = self.spec_path(spec_type)
        if path.is_file() and not refresh:
            try:
                return parse_instance(json.loads(path.read_text(encoding="utf-8")))
            except Exception as exc:  # noqa: BLE001
                LOG.warning("规格缓存损坏（%s），重新获取：%s", path, exc)

        try:
            spec = self._fetch_json(INSTANCE_URL.format(type=spec_type))
            self._write(path, json.dumps(spec, ensure_ascii=False))
            LOG.info("已缓存 %s 的规格 → %s", model, path)
            return parse_instance(spec)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("获取 %s 的规格失败：%s", model, exc)
            return None
