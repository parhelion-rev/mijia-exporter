#!/usr/bin/env python3
"""离线自测：完全不需要真机，覆盖规格解析、指标命名、三级发现、多设备、云端传输与轮询语义。

运行：
    make test
    # 或者
    docker run --rm -v "$PWD":/src -w /src --entrypoint python mijia-exporter:1.0.3 /src/tests/test_offline.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "exporter"))

import mijia_exporter as mx  # noqa: E402
import mijia_spec as spec  # noqa: E402
from mijia_spec import Property  # noqa: E402


# --------------------------------------------------------------------- 假件
class FakeTransport(mx.Transport):
    """脚本化传输：按预设返回属性值，可指定前 N 次 connect 失败。"""

    kind = "fake"

    def __init__(self, cfg, miot=None, legacy=None, fail_connect_times=0):
        super().__init__(cfg)
        self._miot = miot if miot is not None else {}
        self._legacy = legacy or {}
        self._fail = fail_connect_times
        self.connect_calls = 0
        self.read_calls = 0

    def connect(self):
        self.connect_calls += 1
        if self.connect_calls <= self._fail:
            raise RuntimeError("device offline")
        self.model = self.cfg.model or "fake.model.v1"
        self.firmware, self.hardware, self.mac = "9.9.9", "hw", "aa:bb:cc:dd:ee:ff"

    def read_properties(self, props, delay=0.0):
        self.read_calls += 1
        if isinstance(self._miot, Exception):
            raise self._miot
        return {p.key: self._miot[p.key] for p in props if p.key in self._miot}

    def read_legacy(self, names):
        return {n: self._legacy[n] for n in names if n in self._legacy}


class FakeStore:
    def __init__(self, props, model="fake.model.v1"):
        self.props = props
        self.model = model
        self.calls = 0

    def spec_for_model(self, model, refresh=False):
        self.calls += 1
        return list(self.props) if model == self.model else None

    def load_index(self, refresh=False):
        return {self.model: "urn:fake"}


def device_cfg(**kwargs):
    raw = {"name": "dev", "transport": "mock", "discovery": "auto"}
    raw.update(kwargs)
    return mx.DeviceConfig.parse(raw, {})


SPEC_PROPS = [
    Property(3, 3, "Voltage", "uint16", "volt"),
    Property(3, 2, "Electric Current", "uint16", "ampere"),
    Property(3, 1, "Power Consumption", "uint32", "kwh"),
    Property(3, 6, "Electric Power", "uint32", "watt"),
    Property(2, 1, "Valve Switch", "bool", ""),
    Property(4, 1, "Secret", "string", ""),
    Property(4, 2, "Write Only", "uint8", "watt", access=["write"]),
]


def series(text: str) -> set[str]:
    """把 /metrics 文本解析成 {"指标名{标签=……}": 值}。

    prometheus_client 输出的标签是按字母序排的，不要按声明顺序去断言。
    """
    out: set[str] = set()
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name, brace, rest = line.partition("{")
        if brace:
            labels, _, _value = rest.partition("}")
            out.add(f"{name}{{{labels}}}")
        else:
            out.add(line.rsplit(" ", 1)[0])
    return out


# ------------------------------------------------------------------ 规格命名
class TestNaming(unittest.TestCase):
    def test_unit_to_family(self):
        cases = {
            ("volt", "uint16", "Voltage"): "mijia_volts",
            ("ampere", "uint16", "Electric Current"): "mijia_amperes",
            ("watt", "uint32", "Electric Power"): "mijia_watts",
            ("kwh", "uint32", "Power Consumption"): "mijia_kilowatt_hours_total",
            ("celsius", "float", "Temperature"): "mijia_celsius",
            ("hertz", "float", "Frequency"): "mijia_hertz",
            ("percent", "uint8", "Humidity"): "mijia_percent",
            ("", "bool", "Switch"): "mijia_state",
            ("", "uint8", "Fault"): "mijia_value",
            ("ug/m3", "float", "PM2.5"): "mijia_micrograms_per_cubic_meter",
            ("furlong", "float", "Odd"): "mijia_furlong",
        }
        for (unit, fmt, name), expected in cases.items():
            with self.subTest(unit=unit, name=name):
                self.assertEqual(Property(1, 1, name, fmt, unit).metric_name, expected)

    def test_non_ascii_property_names_are_kept_verbatim(self):
        """规格里确实有中文描述（小米空调的 耗电量 等）。标签值允许任意 UTF-8，
        只有指标名限定字符集；早期实现套用了指标名的规则，导致 `耗电量` 与
        `耗电量本次计数` 一起塌缩成 property="unknown"，同设备上无法区分。"""
        a = Property(5, 1, "耗电量", "uint32", "kwh")
        b = Property(5, 2, "耗电量本次计数", "uint32", "kwh")
        self.assertEqual(a.label, "耗电量")
        self.assertEqual(b.label, "耗电量本次计数")
        self.assertNotEqual(a.label, b.label)
        # 指标族仍由单位决定，中文名不影响
        self.assertEqual(a.metric_name, "mijia_kilowatt_hours_total")

    def test_ascii_property_labels_unchanged(self):
        """ASCII 名字的标签值必须与历史一致，否则现有面板与告警会静默失效。"""
        cases = {
            "Electric Power": "electric-power",
            "Electric Current": "electric-current",
            "Power Consumption": "power-consumption",
            "  Temperature  ": "temperature",
            "iot-temp": "iot-temp",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(Property(1, 1, name, "uint32", "watt").label, expected)

    def test_unnamed_property_falls_back_to_address(self):
        self.assertEqual(Property(4, 6, "", "bool", "").label, "4.6")
        self.assertEqual(Property(4, 8, "---", "bool", "").label, "4.8")

    def test_overrides_key_can_be_chinese(self):
        cfg = device_cfg(overrides={"耗电量": {"scale": 0.01}})
        prop = mx.Discoverer(FakeStore([]))._apply_overrides(cfg, Property(5, 1, "耗电量", "uint32", "kwh"))
        self.assertAlmostEqual(prop.scale_override, 0.01)

    def test_limit_properties_get_their_own_family(self):
        """阈值/门限不能和测量值同族，否则面板会把「过压门限 265 V」画进电压曲线。

        实测正泰 ZTY2002：规格对 overcurrent-alarm / overvoltage-alarm / undervoltage-alarm
        都没给单位，只有名字里含 current/voltage，于是曾被推断成 ampere/volt 混进测量族。
        """
        self.assertEqual(Property(4, 7, "overvoltage-alarm", "uint16", "").metric_name, "mijia_volts_limit")
        self.assertEqual(Property(4, 9, "undervoltage-alarm", "uint16", "").metric_name, "mijia_volts_limit")
        self.assertEqual(Property(4, 5, "overcurrent-alarm", "uint8", "").metric_name, "mijia_amperes_limit")
        self.assertEqual(Property(5, 2, "power-threshold", "uint32", "watt").metric_name, "mijia_watts_limit")
        # 带单位的阈值同样分族
        self.assertEqual(Property(3, 3, "voltage-limit", "uint16", "volt").metric_name, "mijia_volts_limit")
        # 正常测量值不受影响
        self.assertEqual(Property(4, 3, "Voltage", "uint16", "V").metric_name, "mijia_volts")
        self.assertEqual(Property(5, 3, "Max Temperature", "float", "celsius").metric_name, "mijia_celsius_limit")

    def test_limit_can_be_overridden_both_ways(self):
        # 显式 limit=false：确属测量值、但名字里带 max/limit 这类词时用
        p = Property(1, 1, "overvoltage-alarm", "uint16", "", limit_override=False)
        self.assertEqual(p.metric_name, "mijia_volts")
        # 显式 limit=true：名字看不出是阈值时用
        p = Property(1, 2, "setting-a", "uint16", "volt", limit_override=True)
        self.assertEqual(p.metric_name, "mijia_volts_limit")
        # metric 覆盖优先于一切：连 _limit 后缀也不加，完全由配置决定
        p = Property(1, 3, "overvoltage-alarm", "uint16", "", metric_override="volts")
        self.assertEqual(p.metric_name, "mijia_volts")

    def test_missing_unit_falls_back_to_name(self):
        """实测：同一台 chuangmi 插座的规格里，power-consumption 单位写成 `none`、voltage 写成 `-`。"""
        self.assertEqual(Property(5, 1, "power-consumption", "uint32", "none").metric_name,
                         "mijia_kilowatt_hours_total")
        self.assertEqual(Property(5, 3, "voltage", "uint16", "-").metric_name, "mijia_volts")
        self.assertEqual(Property(5, 2, "electric-current", "uint16", "none").metric_name, "mijia_amperes")
        self.assertEqual(Property(5, 7, "surge-power", "uint32", "").metric_name, "mijia_watts")
        # 规格里名字带空格/大写，按名推断前必须先归一化，否则只会命中更短的 `power`
        self.assertEqual(Property(5, 1, "Power Consumption", "uint32", "none").metric_name,
                         "mijia_kilowatt_hours_total")
        self.assertEqual(Property(5, 3, "Voltage", "uint16", "-").metric_name, "mijia_volts")
        # bool 不参与按名推断，否则 power-enable 会被当成瓦特
        self.assertEqual(Property(7, 1, "power-enable", "bool", "none").metric_name, "mijia_state")
        self.assertEqual(Property(4, 4, "task-switch", "bool", "none").metric_name, "mijia_state")
        # 没有任何线索时退回 value，而不是编一个单位出来
        self.assertEqual(Property(7, 2, "", "uint32", "none").metric_name, "mijia_value")
        # 真的给了单位就以单位为准（哪怕是按名会猜成别的）
        self.assertEqual(Property(2, 7, "working-time", "uint32", "minutes").metric_name, "mijia_minutes")

    def test_cumulative_by_unit_and_by_name(self):
        self.assertTrue(Property(3, 1, "Power Consumption", "uint32", "kwh").cumulative)
        self.assertTrue(Property(3, 1, "Energy Total", "uint32", "").cumulative)
        self.assertFalse(Property(3, 1, "Overcurrent Alarm", "uint16", "").cumulative)
        # 名字里有 energy，按名推断会给它 kWh（比塞进无单位的 value 更有用）
        self.assertEqual(Property(3, 1, "Energy Total", "uint32", "").metric_name, "mijia_kilowatt_hours_total")

    def test_label_falls_back_to_address(self):
        self.assertEqual(Property(4, 6, "", "bool", "").label, "4.6")
        self.assertEqual(Property(4, 6, "", "bool", "").metric_name, "mijia_state")
        self.assertEqual(Property(3, 2, "Electric Current", "uint16", "ampere").label, "electric-current")

    def test_slug_and_sanitize(self):
        self.assertEqual(spec.slug_label("Power Consumption"), "power-consumption")
        self.assertEqual(spec.slug_label("PM2.5 Density"), "pm2-5-density")
        self.assertEqual(spec.sanitize("mg/m3"), "mg_per_m3")
        self.assertEqual(spec.sanitize("  "), "unknown")

    def test_overrides_win(self):
        prop = Property(3, 3, "Voltage", "uint16", "volt", metric_override="my_volts", cumulative_override=True)
        self.assertEqual(prop.metric_name, "mijia_my_volts_total")

    def test_parse_instance(self):
        raw = {
            "services": [
                {"iid": 3, "description": "Power Consumption", "properties": [
                    {"iid": 1, "description": "Power Consumption", "format": "uint32", "unit": "kwh", "access": ["read"]},
                    {"iid": 2, "name": "Voltage", "format": "uint16", "unit": "volt", "access": ["read", "notify"]},
                    {"iid": 3, "description": "secret", "format": "string", "access": ["write"]},
                ]},
            ]
        }
        props = spec.parse_instance(raw)
        self.assertEqual([(p.siid, p.piid) for p in props], [(3, 1), (3, 2)])  # write-only 被剔除
        self.assertEqual(props[0].name, "Power Consumption")   # name 空时用 description
        self.assertEqual(props[1].service, "Power Consumption")


# ------------------------------------------------------------------ 配置
class TestConfig(unittest.TestCase):
    def test_env_expansion(self):
        os.environ["MIJIA_TEST_TOKEN"] = "a" * 32
        self.assertEqual(mx.expand_env("${MIJIA_TEST_TOKEN}"), "a" * 32)
        self.assertEqual(mx.expand_env("${MIJIA_TEST_MISSING:-d}"), "d")
        self.assertEqual(mx.expand_env("${MIJIA_TEST_MISSING}"), "")

    def test_validation(self):
        with self.assertRaises(mx.ConfigError):   # 没有 name
            mx.DeviceConfig.parse({"transport": "mock"}, {})
        with self.assertRaises(mx.ConfigError):   # local 缺 ip
            mx.DeviceConfig.parse({"name": "x", "transport": "local", "token": "f" * 32}, {})
        with self.assertRaises(mx.ConfigError):   # token 长度不对
            mx.DeviceConfig.parse({"name": "x", "transport": "local", "ip": "1.2.3.4", "token": "zz"}, {})
        with self.assertRaises(mx.ConfigError):   # manual 但没有属性表
            mx.DeviceConfig.parse({"name": "x", "transport": "mock", "discovery": "manual"}, {})
        with self.assertRaises(mx.ConfigError):   # cloud 没有 did/model
            mx.DeviceConfig.parse({"name": "x", "transport": "cloud"}, {})
        with self.assertRaises(mx.ConfigError):   # 轮询间隔过小
            mx.DeviceConfig.parse({"name": "x", "transport": "mock", "poll_interval_seconds": 1}, {})

    def test_defaults_and_override(self):
        defaults = {"poll_interval_seconds": 60, "timeout_seconds": 5, "stale_after_seconds": 300}
        cfg = mx.DeviceConfig.parse({"name": "a", "transport": "mock", "poll_interval_seconds": 15}, defaults)
        self.assertEqual(cfg.poll_interval, 15)
        self.assertEqual(cfg.timeout, 5)
        cfg2 = mx.DeviceConfig.parse({"name": "b", "transport": "mock"}, defaults)
        self.assertEqual(cfg2.poll_interval, 60)

    def test_load_config_requires_devices(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.yaml"
            path.write_text("http_port: 9101\n", encoding="utf-8")
            with self.assertRaises(mx.ConfigError):
                mx.load_config(path)
            path.write_text("devices:\n  - {name: a, transport: mock}\n", encoding="utf-8")
            self.assertEqual(len(mx.load_config(path)["devices"]), 1)

    def test_example_config_valid(self):
        # 模板里用的是 ${XIAOMI_IP}/${XIAOMI_TOKEN}，模拟 .env 已填写
        os.environ["XIAOMI_IP"] = "192.168.1.50"
        os.environ["XIAOMI_TOKEN"] = "a" * 32
        config = mx.load_config(ROOT / "exporter" / "config.example.yaml")
        self.assertTrue(config["devices"])
        for raw in config["devices"]:
            mx.DeviceConfig.parse(raw, config)   # 不应抛异常


# ------------------------------------------------------------------ 发现
class TestDiscovery(unittest.TestCase):
    def test_spec_path(self):
        cfg = device_cfg()
        transport = FakeTransport(cfg, miot={"Voltage": 2238, "Electric Power": 1747, "Valve Switch": True})
        transport.connect()          # 发现前必须先连接（否则拿不到 model）
        discovery = mx.Discoverer(FakeStore(SPEC_PROPS)).discover(transport, cfg)
        self.assertEqual(discovery.source, "spec")
        keys = sorted(p.label for p in discovery.properties)
        # 字符串属性被过滤（写不进数值指标），规格里列了但设备没返回的也被过滤
        self.assertEqual(keys, ["electric-power", "valve-switch", "voltage"])

    def test_scan_fallback_when_no_spec(self):
        cfg = device_cfg()
        transport = FakeTransport(cfg, miot={"3.3": 2238, "2.1": True})
        discovery = mx.Discoverer(FakeStore([])).discover(transport, cfg)
        self.assertEqual(discovery.source, "scan-miot")
        self.assertEqual(sorted(p.address for p in discovery.properties), ["2.1", "3.3"])
        # 扫描出来的属性不知道单位：bool → state，数值 → value
        self.assertEqual({p.metric_name for p in discovery.properties}, {"mijia_state", "mijia_value"})

    def test_legacy_fallback(self):
        cfg = device_cfg()
        transport = FakeTransport(cfg, miot={}, legacy={"power": 312, "bright": 60})
        discovery = mx.Discoverer(FakeStore([])).discover(transport, cfg)
        self.assertEqual(discovery.source, "scan-legacy")
        self.assertTrue(discovery.legacy)
        metrics = {p.label: p.metric_name for p in discovery.properties}
        self.assertEqual(metrics["power"], "mijia_watts")       # 旧版属性名有单位提示表
        self.assertEqual(metrics["bright"], "mijia_percent")

    def test_manual_miot(self):
        cfg = device_cfg(discovery="manual", miot_props={"electric-power": {"siid": 3, "piid": 6, "unit": "watt"}})
        transport = FakeTransport(cfg, miot={"electric-power": 100})
        discovery = mx.Discoverer(FakeStore([])).discover(transport, cfg)
        self.assertEqual(discovery.source, "manual-miot")
        self.assertEqual(discovery.properties[0].metric_name, "mijia_watts")

    def test_manual_legacy(self):
        cfg = device_cfg(discovery="manual", legacy_props={
            "power": {"prop": "power", "metric": "watts", "scale": 0.01},
        })
        transport = FakeTransport(cfg, legacy={"power": 100})
        transport.connect()
        discovery = mx.Discoverer(FakeStore([])).discover(transport, cfg)
        self.assertEqual(discovery.source, "manual-legacy")
        prop = discovery.properties[0]
        self.assertEqual(prop.metric_name, "mijia_watts")     # 属性表里的 metric 必须被采纳
        self.assertEqual(prop.label, "power")
        self.assertEqual(prop.scale_override, 0.01)           # 属性表里的 scale 也必须被采纳

    def test_overrides_ignore_metric_and_address_key(self):
        cfg = device_cfg(overrides={
            "voltage": {"scale": 0.1},
            "electric-power": {"metric": "my_watts", "scale": 0.01},
            "2.1": {"ignore": True},
        })
        transport = FakeTransport(cfg, miot={"Voltage": 2238, "Electric Power": 1747, "Valve Switch": True})
        transport.connect()
        discovery = mx.Discoverer(FakeStore(SPEC_PROPS)).discover(transport, cfg)
        names = {p.label: p.metric_name for p in discovery.properties}
        self.assertEqual(names, {"voltage": "mijia_volts", "electric-power": "mijia_my_watts"})
        scales = {p.label: p.scale_override for p in discovery.properties}
        self.assertEqual(scales, {"voltage": 0.1, "electric-power": 0.01})

    def test_no_properties_raises(self):
        cfg = device_cfg()
        with self.assertRaises(mx.ConfigError):
            mx.Discoverer(FakeStore([])).discover(FakeTransport(cfg), cfg)


# ------------------------------------------------------------------ 轮询
class TestPoller(unittest.TestCase):
    def make_poller(self, cfg=None, **transport_kwargs):
        cfg = cfg or device_cfg(overrides={"voltage": {"scale": 0.1}})
        transport = FakeTransport(cfg, miot={"Voltage": 2238, "Electric Power": 1747}, **transport_kwargs)
        discoverer = mx.Discoverer(FakeStore(SPEC_PROPS))
        poller = mx.DevicePoller(cfg, transport, discoverer, mx.MetricSink(), threading.Event())
        return poller, transport

    def test_scale_applied_and_labels(self):
        poller, _ = self.make_poller()
        self.assertTrue(poller.poll_once())
        self.assertAlmostEqual(poller.values["Voltage"], 223.8)
        self.assertEqual(poller.values["Electric Power"], 1747)
        self.assertTrue(poller.ready())
        from prometheus_client import REGISTRY, generate_latest
        lines = series(generate_latest(REGISTRY).decode())
        self.assertIn('mijia_volts{device="dev",model="fake.model.v1",piid="3",property="voltage",siid="3"}', lines)
        self.assertIn('mijia_watts{device="dev",model="fake.model.v1",piid="6",property="electric-power",siid="3"}', lines)

    def test_failure_keeps_values_and_marks_not_ready(self):
        poller, transport = self.make_poller()
        poller.poll_once()
        good = poller.values["Voltage"]
        transport._miot = RuntimeError("boom")            # 之后每次都失败
        self.assertFalse(poller.poll_once())
        self.assertEqual(poller.values["Voltage"], good)   # 旧值保留，靠 ready 暴露陈旧
        poller.cfg.stale_after = 0
        self.assertFalse(poller.ready())
        poller.refresh_derived()
        from prometheus_client import REGISTRY, generate_latest
        self.assertIn('mijia_device_ready{device="dev"} 0.0', generate_latest(REGISTRY).decode())

    def test_lazy_discovery_retries_after_failure(self):
        poller, transport = self.make_poller(fail_connect_times=1)
        self.assertFalse(poller.poll_once())               # 第一轮：connect 失败
        self.assertFalse(poller.discovered)
        self.assertIn("RuntimeError", poller.last_error)
        self.assertTrue(poller.poll_once())                # 第二轮：自动恢复，无需重启
        self.assertTrue(poller.discovered)
        self.assertEqual(transport.connect_calls, 2)
        from prometheus_client import REGISTRY, generate_latest
        lines = series(generate_latest(REGISTRY).decode())
        self.assertIn('mijia_poll_errors_total{device="dev",reason="discovery-RuntimeError"}', lines)

    def test_degraded_discovery_upgrades_when_spec_appears(self):
        """真实故障的回归用例：spec-cache 不可写 → 退化为盲扫 → 指标名降级、面板全空。

        运维修好目录权限后，不应该还需要重启容器：轮询循环会定期重试规格并就地升级。
        """
        # 用 local 而不是 mock：mock 传输在 ensure_ready 里有短路分支，不走发现逻辑
        cfg = device_cfg(transport="local", ip="192.168.1.50", token="a" * 32)
        store = FakeStore([])                       # 起初没有规格，只能盲扫
        transport = FakeTransport(cfg, miot={"3.3": 2262, "Voltage": 2262,
                                             "Electric Power": 1747, "Valve Switch": True})
        poller = mx.DevicePoller(cfg, transport, mx.Discoverer(store), mx.MetricSink(), threading.Event())
        self.assertTrue(poller.poll_once())
        self.assertEqual(poller.discovery.source, "scan-miot")
        from prometheus_client import REGISTRY, generate_latest
        self.assertIn('mijia_spec_available{device="dev"} 0.0', generate_latest(REGISTRY).decode())

        store.props = list(SPEC_PROPS)              # 规格变可用（目录权限被修好）
        poller._next_spec_retry = 0                 # 跳过 10 分钟重试间隔
        self.assertTrue(poller.poll_once())
        self.assertEqual(poller.discovery.source, "spec")
        self.assertEqual(sorted(p.label for p in poller.properties),
                         ["electric-power", "valve-switch", "voltage"])
        self.assertIn('mijia_spec_available{device="dev"} 1.0', generate_latest(REGISTRY).decode())

    def test_spec_available_is_1_for_manual_and_spec(self):
        """manual 是用户有意为之，不该被当成降级。"""
        cfg = device_cfg(transport="local", ip="192.168.1.50", token="a" * 32, discovery="manual",
                         miot_props={"electric-power": {"siid": 3, "piid": 6, "unit": "watt"}})
        transport = FakeTransport(cfg, miot={"electric-power": 100})
        poller = mx.DevicePoller(cfg, transport, mx.Discoverer(FakeStore(SPEC_PROPS)),
                                 mx.MetricSink(), threading.Event())
        self.assertTrue(poller.poll_once())
        self.assertTrue(poller.discovery.source.startswith("manual"))
        from prometheus_client import REGISTRY, generate_latest
        self.assertIn('mijia_spec_available{device="dev"} 1.0', generate_latest(REGISTRY).decode())

    def test_empty_result_is_an_error(self):
        poller, transport = self.make_poller()
        transport._miot = {}
        self.assertFalse(poller.poll_once())
        self.assertEqual(poller.last_error, "设备没有返回任何属性值")

    def test_string_only_device_yields_error(self):
        cfg = device_cfg()
        transport = FakeTransport(cfg, miot={"Name": "abc"})
        poller = mx.DevicePoller(cfg, transport, mx.Discoverer(FakeStore([])), mx.MetricSink(), threading.Event())
        self.assertFalse(poller.poll_once())               # 字符串属性在发现阶段就被过滤


# ------------------------------------------------------------------ 多设备
class TestMultiDevice(unittest.TestCase):
    def test_two_devices_are_independent(self):
        config = {
            "poll_interval_seconds": 60,
            "devices": [
                {"name": "meter", "transport": "mock"},
                {"name": "lamp", "transport": "mock"},
            ],
        }
        pollers = mx.Exporter(config, ".mijia-auth.json").setup()
        self.assertEqual([p.cfg.name for p in pollers], ["meter", "lamp"])
        for poller in pollers:
            self.assertTrue(poller.poll_once())

    def test_one_bad_device_does_not_block_others(self):
        config = {
            "devices": [
                {"name": "bad", "transport": "local", "ip": "", "token": "x"},   # 配置非法 → 跳过
                {"name": "good", "transport": "mock"},
            ],
        }
        pollers = mx.Exporter(config, ".mijia-auth.json").setup()
        self.assertEqual([p.cfg.name for p in pollers], ["good"])

    def test_mock_device_end_to_end(self):
        config = {"devices": [{"name": "demo", "transport": "mock"}]}
        poller = mx.Exporter(config, ".mijia-auth.json").setup()[0]
        self.assertTrue(poller.poll_once())
        self.assertEqual(poller.discovery.source, "mock")
        # mock 直接给规格单位的值：电压应该在市电范围，功率在合理范围
        self.assertTrue(200 <= poller.values["Voltage"] <= 250)
        self.assertTrue(0 <= poller.values["Electric Power"] <= 1000)
        self.assertIn("Power Consumption", poller.values)


# ------------------------------------------------------------------ 云端传输
class TestCloudTransport(unittest.TestCase):
    class FakeApi:
        def __init__(self):
            self.batches = []

        def get_devices_list(self):
            return [{"did": "d1", "model": "loock.lock.t1", "name": "锁", "fwVersion": "1.0", "mac": "aa"}]

        def get_devices_prop(self, data):
            self.batches.append(data)
            return [
                {"did": "d1", "siid": item["siid"], "piid": item["piid"], "code": 0, "value": 88}
                for item in data
            ]

    def test_connect_and_read(self):
        cfg = device_cfg(transport="cloud", model="loock.lock.t1", did="")
        transport = mx.CloudTransport(cfg, "auth.json")
        fake = self.FakeApi()
        mx.CloudTransport._api = fake           # 注入假客户端，不联网
        try:
            transport.connect()
            self.assertEqual(transport.did, "d1")
            self.assertEqual(transport.firmware, "1.0")
            values = transport.read_properties([Property(2, 1, "Battery", "uint8", "percent")])
            self.assertEqual(values, {"Battery": 88})
            self.assertEqual(fake.batches[0][0]["did"], "d1")
        finally:
            mx.CloudTransport._api = None

    def test_unknown_device_raises(self):
        cfg = device_cfg(transport="cloud", model="nope.model", did="")
        transport = mx.CloudTransport(cfg, "auth.json")
        mx.CloudTransport._api = self.FakeApi()
        try:
            with self.assertRaises(mx.ConfigError):
                transport.connect()
        finally:
            mx.CloudTransport._api = None


# ------------------------------------------------------------------ 外部心跳
class TestHeartbeat(unittest.TestCase):
    def make(self, **kwargs):
        raw = {"enabled": True, "url": "https://external.example/ping"}
        raw.update(kwargs)
        cfg = mx.HeartbeatConfig.parse(raw)
        sent: list[str] = []
        return mx.Heartbeat(cfg, [], threading.Event(), sender=sent.append), sent

    def test_disabled_by_default(self):
        # 不写 heartbeat 段、或写了空值 → 关闭
        self.assertFalse(mx.HeartbeatConfig.parse(None).enabled)
        # 写了 heartbeat 块并给了 url（没写 enabled）→ 视为开启，避免「配了 URL 却没生效」
        self.assertTrue(mx.HeartbeatConfig.parse({"url": "https://external.example/ping"}).enabled)
        # 写了块但既没 url 也没 enabled → 快速失败，而不是静默忽略
        with self.assertRaises(mx.ConfigError):
            mx.HeartbeatConfig.parse({})
        exporter = mx.Exporter({"devices": [{"name": "d", "transport": "mock"}]}, "auth.json")
        self.assertFalse(exporter.heartbeat_cfg.enabled)
        self.assertIsNone(exporter.heartbeat)      # 未启用就不会创建心跳线程

    def test_validation(self):
        with self.assertRaises(mx.ConfigError):   # 开了但没有 url
            mx.HeartbeatConfig.parse({"enabled": True})
        with self.assertRaises(mx.ConfigError):   # 不是 http(s)
            mx.HeartbeatConfig.parse({"enabled": True, "url": "ftp://x"})
        with self.assertRaises(mx.ConfigError):   # 不支持的方法
            mx.HeartbeatConfig.parse({"enabled": True, "url": "https://x", "method": "PUT"})
        with self.assertRaises(mx.ConfigError):   # 间隔太小
            mx.HeartbeatConfig.parse({"enabled": True, "url": "https://x", "interval_seconds": 5})
        # 配置写错要在启动时就报出来，而不是跑起来才发现
        with self.assertRaises(mx.ConfigError):
            mx.Exporter({"devices": [{"name": "d", "transport": "mock"}],
                         "heartbeat": {"enabled": True}}, "auth.json")

    def test_beat_sends_and_records(self):
        hb, sent = self.make()
        self.assertTrue(hb.beat())
        self.assertEqual(sent, ["https://external.example/ping"])
        self.assertIsNotNone(hb.last_success_at)

    def test_beat_failure_never_raises(self):
        cfg = mx.HeartbeatConfig.parse({"enabled": True, "url": "https://external.example/ping"})

        def boom(url):
            raise RuntimeError("network down")

        hb = mx.Heartbeat(cfg, [], threading.Event(), sender=boom)
        self.assertFalse(hb.beat())          # 关键：心跳失败不能把 exporter 带崩
        self.assertIsNone(hb.last_success_at)

    def test_require_all_devices_ready(self):
        poller = mx.Exporter({"devices": [{"name": "never", "transport": "mock"}]}, "a.json").setup()[0]
        self.assertFalse(poller.ready())     # 还没读过 → 未就绪

        hb, sent = self.make(require_all_devices_ready=True)
        hb.pollers = [poller]
        self.assertFalse(hb.beat())          # 未就绪且没配 fail_url → 这一拍不打
        self.assertEqual(sent, [])

        hb2, sent2 = self.make(require_all_devices_ready=True, fail_url="https://external.example/fail")
        hb2.pollers = [poller]
        self.assertTrue(hb2.beat())
        self.assertEqual(sent2, ["https://external.example/fail"])
        from prometheus_client import REGISTRY, generate_latest
        self.assertIn('mijia_heartbeat_sent_total{result="fail"}', generate_latest(REGISTRY).decode())

        poller.poll_once()                   # 设备健康后回到正常 url
        self.assertTrue(poller.ready())
        hb3, sent3 = self.make(require_all_devices_ready=True)
        hb3.pollers = [poller]
        self.assertTrue(hb3.beat())
        self.assertEqual(sent3, ["https://external.example/ping"])

    def test_thread_beats_immediately_then_stops(self):
        hb, sent = self.make(interval_seconds=600)
        hb.start()
        time.sleep(0.3)
        hb.stop_event.set()
        hb.join(timeout=2)
        self.assertFalse(hb.is_alive())
        self.assertGreaterEqual(len(sent), 1)   # 起来就先打一拍


# ------------------------------------------------------------------ 云端登录态
class TestCloudAuth(unittest.TestCase):
    def tearDown(self):
        mx.CloudTransport._api = None

    def test_missing_auth_file_is_actionable(self):
        transport = mx.CloudTransport(device_cfg(transport="cloud", model="loock.lock.t1"),
                                      "/nonexistent/mijia-auth.json")
        mx.CloudTransport._api = None
        with self.assertRaises(mx.ConfigError) as ctx:
            transport.connect()
        self.assertIn("make token", str(ctx.exception))

    def test_expired_auth_does_not_fall_into_qr_flow(self):
        """刻意不用 api.login()：它在刷新失败时会退化成二维码登录并空等约 2 分钟。"""
        import json
        from mijiaAPI import mijiaAPI as _Api

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "auth.json"
            path.write_text(json.dumps({"passToken": "x", "ssecurity": "y", "userId": 1}), encoding="utf-8")
            original = _Api._refresh_token
            _Api._refresh_token = lambda self: (_ for _ in ()).throw(RuntimeError("刷新Token失败"))
            try:
                transport = mx.CloudTransport(device_cfg(transport="cloud", model="m"), str(path))
                mx.CloudTransport._api = None
                with self.assertRaises(mx.ConfigError) as ctx:
                    transport.connect()
                self.assertIn("make token", str(ctx.exception))
                self.assertIn("passToken", str(ctx.exception))
            finally:
                _Api._refresh_token = original


# ------------------------------------------------------------------ HTTP 端点
class TestHttpEndpoints(unittest.TestCase):
    """端点就是交付出去的接口，必须有覆盖 —— 曾因 Handler 上没有 heartbeat 属性导致索引页 500。"""

    def setUp(self):
        self.exporter = mx.Exporter({"devices": [{"name": "httpdev", "transport": "mock"}]}, "auth.json")
        self.exporter.setup()
        mx.Handler.pollers = self.exporter.pollers
        mx.Handler.heartbeat = None
        for poller in self.exporter.pollers:
            poller.poll_once()
            poller.refresh_derived()
        self.server = mx.ThreadingHTTPServer(("127.0.0.1", 0), mx.Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def get(self, path: str) -> tuple[int, str]:
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=5) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    def test_index_lists_devices_and_heartbeat_state(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn("mijia-exporter", body)
        self.assertIn("httpdev", body)
        self.assertIn("外部心跳：未启用（默认关闭）", body)

    def test_metrics_ready_health_and_404(self):
        status, body = self.get("/metrics")
        self.assertEqual(status, 200)
        self.assertIn("mijia_watts", body)
        status, body = self.get("/ready")
        self.assertEqual(status, 200)
        self.assertIn('"ready": true', body)
        self.assertEqual(self.get("/health")[0], 200)
        self.assertEqual(self.get("/nope")[0], 404)

    def test_ready_returns_503_when_stale(self):
        poller = self.exporter.pollers[0]
        poller.cfg.stale_after = 0          # 立刻算过期
        poller.refresh_derived()
        status, body = self.get("/ready")
        self.assertEqual(status, 503)
        self.assertIn('"ready": false', body)


# ------------------------------------------------------------------ 指标文本
class TestExposition(unittest.TestCase):
    def test_device_metrics_and_types(self):
        config = {"devices": [{"name": "expo", "transport": "mock"}]}
        poller = mx.Exporter(config, ".mijia-auth.json").setup()[0]
        poller.poll_once()
        poller.refresh_derived()
        from prometheus_client import REGISTRY, generate_latest
        text = generate_latest(REGISTRY).decode()
        lines = series(text)
        self.assertIn('mijia_device_ready{device="expo"}', lines)
        self.assertIn('mijia_device_properties{device="expo"}', lines)
        self.assertIn('mijia_last_success_timestamp_seconds{device="expo"}', lines)
        self.assertIn('mijia_build_info{version="1.0.3"}', lines)
        self.assertTrue(any(x.startswith('mijia_device_info{device="expo"') for x in lines))
        # 累计量必须是「绝对值 gauge + `_total`」，不能用 Counter 的 inc 累加
        self.assertIn("# TYPE mijia_kilowatt_hours_total gauge", text)
        self.assertIn("# TYPE mijia_poll_errors_total counter", text)
        self.assertIn("# TYPE mijia_watts gauge", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
