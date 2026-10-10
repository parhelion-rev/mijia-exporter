# mijia-exporter —— 通用米家设备 → Prometheus → Grafana

[![CI](https://github.com/parhelion-rev/mijia-exporter/actions/workflows/ci.yml/badge.svg)](https://github.com/parhelion-rev/mijia-exporter/actions/workflows/ci.yml)
[![Release](https://github.com/parhelion-rev/mijia-exporter/actions/workflows/release.yml/badge.svg)](https://github.com/parhelion-rev/mijia-exporter/actions/workflows/release.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Image size](https://img.shields.io/badge/image-100MB-blue)](https://hub.docker.com/r/effectwei/mijia-exporter)
[![Platforms](https://img.shields.io/badge/platforms-amd64%20%7C%20arm64-informational)](https://hub.docker.com/r/effectwei/mijia-exporter)

一台 exporter 接多台米家（MiJia）设备，指标名由设备的 MIoT 规格自动推导，换设备不用改代码。
支持局域网 miIO 直连与米家云两种传输。

```text
                    ┌─ 本地 miIO（UDP 54321，需 token）─┐
米家设备 ────────────┤                                    ├──▶ exporter :9101/metrics ──▶ Prometheus ──▶ Grafana
（控电仪/插座/门锁…）└─ 米家云（需扫码登录态，无 token 也行）┘        （多设备、多指标族）
```

## 快速开始

```bash
git clone https://github.com/parhelion-rev/mijia-exporter && cd mijia-exporter
make init      # 生成 .env / exporter/config.yaml / spec-cache / auth，并记录宿主 uid
make token     # 米家 App 扫码登录，token 与设备 IP 自动写进 .env
make up        # 启动 exporter + Prometheus + Grafana
```

启动后：

| 入口 | 地址 | 说明 |
|---|---|---|
| Grafana | `http://<本机IP>:3000` | 面板「米家设备总览」已自动加载 |
| Prometheus | `http://<本机IP>:9090` | `Status → Targets` 里 `mijia` 应为 `up` |
| exporter | `http://<本机IP>:9101/` | 索引页列出每台设备的发现来源与状态 |

- **端口冲突**：3000 / 9090 都是热门端口（例如 NapCat 就占 3000）。在 `.env` 里改 `GRAFANA_PORT` / `PROMETHEUS_PORT` / `EXPORTER_PORT` 即可，容器内部端口不用动。
- **不想本地构建**：镜像已发布在 Docker Hub（`linux/amd64` + `linux/arm64`）。在 `.env` 里写 `MIJIA_IMAGE=effectwei/mijia-exporter:1.0.1`，然后 `docker compose pull && docker compose up -d --no-build`。
- **不加扫码工具也行**：`make token` 会在终端打印二维码；加 `ARGS="--qr-image qr.png"` 则存成图片再扫。

## 为什么用 Prometheus + Grafana

- 轮询、长期存储、曲线、阈值告警都是它的主场。家里几台设备 60 秒采一次，资源占用可以忽略。
- 标准做法是 **exporter 常驻、Prometheus 主动抓取**。不要用 Pushgateway 配定时脚本：它不处理累计量归零，旧指标永不消失，也没有真实的 `up` 语义。
- 不合适的场景：毫秒级采样、事件日志、只想在手机上看一眼瞬时值（这类需求 Home Assistant 更省事）。

## 指标契约

指标名与标签是对外 API。重命名属于破坏性变更，只在主版本号里做。

### 属性指标

属性按**单位**归到不同指标族，不同单位绝不混进同一个指标。每个属性指标都带
`device` / `model` / `property` / `siid` / `piid` 标签。

| 规格单位 | 指标族 | 说明 |
|---|---|---|
| `watt` / `W` | `mijia_watts` | 功率 |
| `volt` / `V` | `mijia_volts` | 电压 |
| `ampere` / `A` | `mijia_amperes` | 电流 |
| `kwh` / `Wh` | `mijia_kilowatt_hours_total` | 累计电量，用 `increase()` / `rate()` |
| `celsius` / `hertz` / `percent` / `seconds` / `minutes` 等 | `mijia_celsius` / `mijia_hertz` / `mijia_percent` / `mijia_seconds` / `mijia_minutes` 等 | 按单位派生 |
| 无单位 + `bool` | `mijia_state` | 1 / 0 |
| 无单位 + 数值 | `mijia_value` | 阈值、档位、故障码等 |
| 没见过的单位 | `mijia_<单位 slug>` | 如 `mg/m3` → `mijia_milligrams_per_cubic_meter` |
| 阈值类属性（名字含 `alarm` / `limit` / `threshold` / `max` / `min` 等） | `mijia_<单位族>_limit` | 如 `mijia_volts_limit`、`mijia_amperes_limit`，与测量值分开 |

单位缺失时按**属性名**兜底推断（`Voltage` → volts，`Power Consumption` → kWh 且按累计量处理）。
这层兜底是必需的：实测同一台插座的规格里，`power-consumption` 的单位写成 `none`、`voltage` 写成 `-`。

**阈值为什么单独成族**：规格常常不给门限属性单位。实测正泰 ZTY2002 的 `overvoltage-alarm`（过压门限 265）
与 `undervoltage-alarm`、`overcurrent-alarm`（过流门限 80）都没有单位，只有名字里含 voltage/current，
于是会被推断成伏特/安培、和真实测量值挤进同一族——Grafana 就会把「门限」画进电压曲线。
现在按名字把它们分到 `_limit` 族，测量曲线保持干净；判断错了可以用 `overrides.limit` 双向纠正。

### 设备级指标

标签只有 `device` / `model`。

| 指标 | 含义 |
|---|---|
| `mijia_device_info{device,model,firmware,hardware,mac,transport}` | 设备身份，值恒为 1 |
| `mijia_device_configured{device}` | 是否完成初始化与属性发现（0 = 该设备没采上，原因见日志） |
| `mijia_device_ready{device}` | 数据是否新鲜（0 = 已过期，阈值见 `stale_after_seconds`） |
| `mijia_device_properties{device}` | 发现到的可读属性个数 |
| `mijia_last_success_timestamp_seconds{device}` | 上次成功读取时间，用 `time()` 减它得到陈旧度 |
| `mijia_poll_duration_seconds{device}` | 单次读取耗时 |
| `mijia_poll_errors_total{device,reason}` | 读取失败次数（按异常类型） |
| `mijia_spec_available{device}` | 1 = 用了 miot-spec 规格（指标名带单位）；0 = 退化为盲扫（指标名降级） |
| `mijia_spec_cache_writable` | 规格缓存目录是否可写；0 表示规格无法落盘，即将退化为盲扫 |
| `mijia_build_info{version}` | exporter 版本 |

读取失败时**不清空旧值**，只把 `mijia_device_ready` 置 0 并累加错误计数，避免出现「看起来正常、其实是陈数据」的假象。

## 配置

```yaml
http_port: 9101
poll_interval_seconds: 60      # 默认轮询间隔，每台设备可覆盖
timeout_seconds: 8
stale_after_seconds: 300       # 超过这么久没读到，mijia_device_ready 置 0
spec_cache_dir: spec-cache     # miot-spec 缓存，首次联网取一次，之后离线可用

devices:
  - name: zty2002              # 会作为 device 标签
    transport: local           # local | cloud | mock
    ip: ${XIAOMI_IP}
    token: ${XIAOMI_TOKEN}
    discovery: auto            # auto | spec | scan | manual
    batch_size: 4              # 可选：每批请求的属性数
    batch_delay_seconds: 0.3   # 可选：批次之间的停顿
    overrides:
      voltage: {scale: 0.1}
```

### overrides

键可以是 `siid.piid`、规格里的属性名或它的 slug，大小写与空格不敏感。

| 字段 | 作用 |
|---|---|
| `scale` | 原始值 × scale = 物理值 |
| `metric` | 直接指定指标族名（不含 `mijia_` 前缀），用于纠正规格里单位缺失或标错 |
| `name` | 改 `property` 标签 |
| `unit` / `format` | 覆盖规格里的单位与格式 |
| `cumulative` | 强制或取消按累计量处理（决定是否加 `_total` 后缀） |
| `limit` | 强制/取消按阈值处理（阈值单独成族，如 `mijia_volts_limit`）；`metric` 优先级最高 |
| `ignore` | 丢弃该属性（也可写在设备级 `ignore: [a, b]`） |

### 属性发现

`discovery: auto` 按三级回退：miot-spec 官方规格 → MIoT `siid.piid` 盲扫 → 旧版 `get_prop` 盲扫。
规格缺失、纯 MIoT、老固件三种情况都能接上。退化为盲扫时指标名会变成 `mijia_value` / `mijia_state`，日志里有明确提示。

`discovery: manual` 使用你手写的属性表：`miot_props`（走 `get_properties`）或
`legacy_props`（走 `get_prop`，用于老设备）。两者的 `metric` / `scale` / `unit` / `format` / `cumulative` 与 `overrides` 同义。

### 外部心跳（死人开关，默认关闭）

Prometheus 就跑在这台机器上，所以「这台机器断电 / Docker 挂了 / 磁盘满了」这类故障，它自己一条告警也发不出去。
唯一的发现办法是让 exporter 定期 ping 一个**外部**服务，外部服务发现心跳停了就来通知你。

默认关闭，且不做任何外联 —— 要不要往外报、报给谁，由你自己决定：

```yaml
heartbeat:
  enabled: true                      # 默认 false
  url: "https://hc-ping.com/<uuid>"  # Uptime Kuma 的 Push 监控 / healthchecks.io 都可以
  fail_url: ""                       # 可选：设备不健康时改推这个（hc 支持 /<uuid>/fail）
  interval_seconds: 300              # 不要小于 10 秒
  require_all_devices_ready: false   # false = 只报进程活着；true = 有设备不健康就不打点或改推 fail_url
```

- 起来先打一拍，之后每 `interval_seconds` 一次。推送失败只记 WARNING 与计数，不会把 exporter 打挂。
- 指标：`mijia_heartbeat_sent_total{result="ok|fail|skipped|error"}` 与 `mijia_heartbeat_last_success_timestamp_seconds`；
  exporter 索引页也会显示心跳状态。

不想用外部心跳也可以，**告警出口自备**：加 Alertmanager，或直接用 Grafana 的 Contact points
（邮件 / 钉钉 / 企业微信 / Telegram），把 `prometheus/rules/mijia.rules.yml` 里的表达式搬过去即可。

## 校准：规格只给单位，不给系数

MIoT 规格不提供内部系数，必须自己测。实测两台设备的原始值都带系数：

| 设备 | 属性 | 原始值 | 系数 | 物理值 |
|---|---|---|---|---|
| 正泰控电仪 ZTY2002 | 电压 `3.3` | 2237~2263 | ×0.1 | 226 V |
| | 电流 `3.2` | 793~910 | ×0.01 | 9.0 A |
| | 累计电量 `3.1` | 254551 | ×0.01 | 2545.5 kWh |
| | 功率 `3.6` | 1985 | ×1 | 1985 W |
| 创米插座 212a01 | 功率 `5.6` | 5002~6408 | ×0.1 | 约 510 W |
| | 电压 `5.3` | 220 | ×1 | 220 V |

用 `make watch` 采样原始值后，靠两条判据反推：

1. **`P = U × I`**：同一瞬间三个原始值的比例关系。ZTY2002 用这组系数算出的 `P` 与 `U×I` 只差 2.1%，
   换别的组合误差在 90% 以上；插座取 ×0.1 得到约 510 W，与 `5A × 220V = 1100VA` 相容（PF≈0.46，开关电源负载），
   而取 ×1 会得到 5100 W —— 超过插座自身 2500 W 的量程，可以直接排除。
2. **累计电量增量 ÷ 时间 ≈ 平均功率**：ZTY2002 在 195 秒内涨了 11 个单位，
   `11 × 0.01 kWh ÷ 195s = 2.03 kW`，与同期功率读数 1.99 kW 吻合。

测不出来就不要填。那台插座的 `power-consumption` 三分钟一个单位都没涨，单位待定，
配置里明确留空 —— 宁可显示粗粒度，也不要编一个系数。

## 云端设备与米家登录态

设备不在同一网段、或拿不到本地 token 时，用 `transport: cloud` 走米家云批量读属性。
登录态需要 `auth/mijia-auth.json`（`make token` 生成）。缺点是慢、有频控、受设备上报间隔限制。

**登录态会自己续期**，读源码确认的行为（`mijiaAPI 4.4.0`）：

| 环节 | 行为 |
|---|---|
| `_request()` | 每次请求前先调 `_refresh_token()` |
| token 仍有效 | `self.available` 为真，直接跳过，不产生额外请求 |
| token 过期 | 用 `passToken` 换新的 `serviceToken` + `ssecurity`，成功后回写 auth 文件并重建 session |
| `passToken` 也过期 | 抛 `LoginError`，**不会退化成二维码登录**，需要人工重扫 |

因此只要 auth 文件可写，云端设备可以长期无人值守；`passToken` 官方说约一个月、实际更长，过期后 `make token` 重扫一次即可。

两个必须注意的点：

- **不要用 `api.login()`**：它内部是 `QRlogin()`，刷新失败时会进入二维码流程，在无头容器里会空等约 2 分钟长轮询，还往日志里打二维码。exporter 只调用「只刷新、不交互」的 `_refresh_token()`，失败时直接给出可操作的报错。这条有单测守着。
- **auth 文件必须可写**：`mijiaAPI` 会刷新 `serviceToken` 并回写它，只读挂载会报 `Read-only file system`。另外要挂目录而不是挂文件 —— 文件不存在时 Docker 会把挂载点建成目录。

## 实测记录

| 设备 | model | 传输 | 发现 | 结果 |
|---|---|---|---|---|
| 正泰智能控电仪 ZTY2002 | `chint1.valve.lcd` | local | spec | 13 个属性：电压 / 电流 / 功率 / 累计电量，阀门开关 / 目标水位 / 故障码，过流·过压·欠压阈值 |
| 创米智能插座 | `chuangmi.plug.212a01` | local | spec | 16 个属性：开关 / 温度 / 工作时间 / 功率 / 电量 / 浪涌功率 / 倒计时 |
| 小米全自动智能门锁 | `loock.lock.t1` | **cloud** | spec | 电量百分比 + 状态（同账号、不在同网段，走米家云） |

一台 exporter、两种传输、三套不同规格，`mijia_device_configured{device}` 全部为 1，共 18 个指标族。

## 运维与安全

- **只在内网使用**。Prometheus 没有鉴权，Grafana 只有账号密码，都是明文 HTTP。有公网入口的机器请改绑 `127.0.0.1` 或加反向代理与 TLS（exporter 默认已只绑本机）。
- **Grafana 管理员密码**：`GF_SECURITY_ADMIN_PASSWORD` 只在 Grafana 首次建库时生效，已有 `grafana-data` 卷时改 `.env` 无效。
  ```bash
  NEW=$(grep '^GRAFANA_ADMIN_PASSWORD=' .env | cut -d= -f2-)
  curl -s -X PUT -u "admin:旧密码" -H 'Content-Type: application/json' \
    -d "{\"oldPassword\":\"旧密码\",\"newPassword\":\"$NEW\"}" localhost:3000/api/user/password
  # 或者：docker exec -it mijia-grafana grafana cli admin reset-admin-password "$NEW"
  ```
- **设备 IP 要固定**。在路由器上做静态 IP / DHCP 保留；IP 一变本地传输就失联（`MijiaDeviceStale` 能发现，但要人工改配置）。
- **备份**：配置与面板都是文件，可以入 git。TSDB 在 Docker 卷里，没有备份（家用指标通常可接受）。`exporter/spec-cache` 建议一起备份，否则新部署首跑依赖 miot-spec.org 在线。
- **日志与容量**：三个容器都配了 `10m × 3` 的日志轮转，Prometheus 保留 90 天或 5GB（先到者生效）。
- **容器权限**：exporter 以宿主 uid 运行（`make init` 写入 `.env`），Prometheus 以 `nobody`、Grafana 以 uid 472 运行，所以挂载进去的配置必须可读。`make init` 会统一 `chmod -R a+rX`。
- **`spec-cache` 与 `auth` 目录必须对 exporter 的运行 uid 可写**。这两个目录是往里写的（规格缓存、登录态续期），不可写会导致规格无法落盘：一旦 miot-spec.org 临时不可达，就会退化为盲扫、指标名降级为 `mijia_value`，面板整片变空，而容器健康检查仍是 healthy。
  在 Portainer 里用「栈」部署时，不存在的挂载源目录会由 Portainer 以 **root** 创建，于是容器里的非 root 用户写不进去：
  ```bash
  sudo chown -R 1000:1000 /opt/mijia/exporter/spec-cache /opt/mijia/auth   # uid 用 .env 里的 EXPORTER_UID
  ```
  改完**不需要重启容器**：exporter 每 10 分钟会重试一次规格并自动升级回带单位的指标名，`mijia_spec_available` 会从 0 变回 1。
- **Docker Hub 拉不动**（国内网络常见 `authentication required`）：先拉镜像源再打回官方 tag，compose 不用改。
  ```bash
  docker pull docker.m.daocloud.io/prom/prometheus:v3.13.4
  docker tag  docker.m.daocloud.io/prom/prometheus:v3.13.4 prom/prometheus:v3.13.4
  docker pull docker.m.daocloud.io/grafana/grafana:13.0.10
  docker tag  docker.m.daocloud.io/grafana/grafana:13.0.10 grafana/grafana:13.0.10
  ```

### 告警规则

`prometheus/rules/mijia.rules.yml` 里有 1 条记录规则与 6 条告警：

| 名称 | 触发条件 |
|---|---|
| `mijia:energy_kwh:increase_1d` | 记录规则：近 24 小时用电量 |
| `MijiaDeviceStale` | 设备超过 10 分钟没有新数据 |
| `MijiaDeviceNotConfigured` | 设备属性发现失败，从未采上（覆盖 `MijiaDeviceStale` 触及不到的盲区） |
| `MijiaExporterDown` | Prometheus 抓取 exporter 失败 |
| `MijiaPollErrors` | 近 30 分钟读取失败超过 5 次 |
| `MijiaHighPower` | 功率超过 3500 W（16A 插座的实际上限） |
| `MijiaSpecCacheNotWritable` | 规格缓存目录不可写，指标名已降级（面板会整片变空） |

## 排错：真机上踩过的坑

| 现象 | 原因 | 处理 |
|---|---|---|
| 属性全部读不到，日志里是 `need did` | 部分固件要求 `get_properties` 的每一项都带 `did` 字段 | 已在内置客户端里统一带上，无需处理 |
| 指标名出现 `mijia_none_total`、`mijia_value` | 规格里的单位是脏数据（`none`、`-`） | 已按属性名兜底推断；仍不对时用 `overrides.metric` 指定 |
| 设备回 `user ack timeout`，之后十几秒不响应 | 连续快发请求把便宜模组打急了 | 调小 `batch_size`、调大 `batch_delay_seconds`；盲扫阶段已内建停顿 |
| 云端设备报 `Read-only file system` | `mijiaAPI` 需要回写 auth 文件 | 挂载 `./auth` 目录且不要加 `:ro` |
| 云端设备报「登录态无法刷新」 | `passToken` 过期 | 重新执行 `make token` 扫码 |
| Prometheus 反复 `exit 2`，`promtool check` 却是通过 | `rule_files` 通配符把同目录下的单测文件也扫了进去 | 单测放在 `prometheus/tests/`，通配符收紧为 `*.rules.yml` |
| 容器报 `permission denied` 读不到配置 | 宿主 umask 077 让文件成了 600，而容器以非 root 运行 | 执行 `make init`（会统一放开读权限） |
| `make once` 报 `unrecognized arguments` | `--entrypoint` 写在了镜像名之后，被当成容器命令 | 已修正；自定义命令时注意 `docker run [OPTIONS] IMAGE [COMMAND]` 的顺序 |
| **Grafana 面板整片空白，但容器健康检查是 healthy** | 规格缓存目录不可写（挂载目录属主是 root，容器以非 root 运行）→ 规格查不到 → 退化为盲扫，指标名变成 `mijia_value`/`mijia_state`，面板查的 `mijia_watts` 根本不存在 | 看日志有没有「没有可用的规格…退化为盲扫」；`chown` 那两个目录即可。`MijiaSpecCacheNotWritable` 告警会直接指出这件事 |
| 局域网扫描不到任何设备 | miIO 发现包必须是 32 字节，少一个字节设备就丢弃 | 内置客户端里是 `21310020` + 28 × `ff`；自建探测脚本时留意 |

## 常用查询

```promql
# 每台设备的功率
mijia_watts

# 只看某个属性（同一设备可能有多个 watt 属性，例如插座的 surge-power）
mijia_watts{property="electric-power"}

# 近日 / 近月用电（滚动窗口，不是自然日）
increase(mijia_kilowatt_hours_total[24h])
increase(mijia_kilowatt_hours_total[30d])

# 平均功率（由累计电量反推，比瞬时值更适合看趋势）
rate(mijia_kilowatt_hours_total[1h]) * 1000

# 电费（按 0.55 元/kWh 估算）
increase(mijia_kilowatt_hours_total[30d]) * 0.55

# 哪台设备没采上 / 数据不新鲜
mijia_device_configured == 0
time() - mijia_last_success_timestamp_seconds > 600
```

## 验证与测试

不需要真机也能跑全部测试 —— `mock` 传输会模拟一台设备的所有属性。

```bash
make check        # 配置校验，不碰设备
make test         # 41 个离线单测：命名规则、三级发现、多设备、云端登录态、心跳、HTTP 端点
make test-rules   # 11 个 promtool 用例，确认该触发时触发、不该触发时不触发
make validate     # YAML / JSON 静态校验
```

CI 会在每次 push 与 PR 上跑同一套，外加一次 mock 端到端冒烟。
已用真机整栈验证：Prometheus 抓到 18 个指标族，Grafana 数据源与面板自动加载成功。
唯一没验证的是面板在浏览器里的渲染效果（数据链路已通过数据源代理验证）。

## 生产就绪清单

| 项 | 状态 | 说明 |
|---|---|---|
| 数据正确性 | 部分 | 控电仪四项系数有三条依据交叉验证；插座的功率系数用「超过自身量程即排除」反推，建议用一个已知功率的负载复核 |
| 故障自愈 | 可以 | 惰性发现 + 每周期重试：开机时设备离线、临时读不通都能自己恢复，不用重启容器 |
| 失败可观测 | 可以 | 6 个设备级指标覆盖「没采上」「数据过期」「反复失败」三种形态 |
| 告警覆盖 | 可以 | 6 条告警 + 1 条记录规则，含「设备从未采上」与「指标名降级」两个盲区；13 个 promtool 用例守着 |
| 告警通知出口 | 需自配 | 默认不做任何外联。内置外部心跳（默认关闭）负责发现「监控自己死了」；通知投递自行选 Alertmanager 或 Grafana Contact points |
| 磁盘增长 | 可以 | 日志轮转 10m × 3；单次成功读取记 DEBUG；TSDB 有 90 天 / 5GB 上限 |
| 密钥管理 | 需自配 | `.env` 与 `auth/` 都是 600 且已 gitignore；Grafana 默认密码务必修改 |
| 网络安全 | 看部署 | exporter 只绑本机；Prometheus 与 Grafana 适合局域网，公网入口需自行加反代与 TLS |
| 版本可复现 | 可以 | 镜像与 Python 依赖全部 pin 到具体版本 |
| 备份 | 部分 | 配置与面板可入 git；TSDB 无备份；`spec-cache` 建议一起备份，否则首跑依赖外网 |
| 指标契约稳定性 | 注意 | 指标名与标签即 API，重命名等于破坏性变更，只在主版本号里做 |
| 回归测试 | 可以 | 41 个离线单测 + 11 个规则用例 + 真机验证，CI 自动执行 |
| 发布流程 | 可以 | 推 `v*` 标签即自动多架构构建并推送镜像、生成 Release |
| 单点 | 注意 | 单机部署，主机挂了就停止采集；累计量是绝对值，恢复后不会丢计数 |

上生产前建议做三件事：

1. **接上告警出口**。加 Alertmanager，或在 Grafana 里配 Contact points（把 `prometheus/rules/mijia.rules.yml` 的表达式搬过去）。
2. **改 Grafana 管理员密码**，做法见「运维与安全」。
3. **给每台设备绑静态 IP / DHCP 保留**，并记住 `auth/mijia-auth.json` 约一个月过期。

## 已知限制

- **只读**：不提供控制能力，exporter 只做观测。
- 云端传输受小米云频控与设备上报间隔限制，精度不如本地协议；`poll_interval_seconds` 不要低于 60。
- miot-spec.org 是社区维护的公开规格库，个别型号可能没有或不准，用 `overrides` 纠正，或 `discovery: manual` 手写。
- 字符串属性不写入指标（避免高基数标签），设备身份通过 `mijia_device_info` 暴露。
- `increase()` 是滚动窗口：`increase(x[1d])` 表示最近 24 小时，不是自然日。
- 规格只给单位不给系数，系数必须自己测；测不出来就不要填。

## 目录结构

```text
mijia-exporter/
├── mijia_token.py               # 米家 App 扫码取 token（取 token 的主路径）
├── try_token.py                 # 用已知 token 试读设备
├── probe.py                     # 局域网发现 / 握手 / 属性扫描 / miot-spec 查询
├── exporter/
│   ├── mijia_exporter.py        # exporter 本体：多设备、多传输、惰性发现与自愈
│   ├── mijia_spec.py            # miot-spec 获取、缓存、解析，以及单位→指标族命名规则
│   ├── miio_client.py           # 内置极简 miIO 客户端
│   ├── config.example.yaml      # 配置模板，含所有 overrides 用法
│   ├── config.yaml              # 本机配置（gitignore，由 make init 生成）
│   ├── spec-cache/              # miot-spec 缓存（首次联网取，之后离线可用）
│   ├── requirements.txt
│   ├── .dockerignore
│   └── Dockerfile
├── prometheus/
│   ├── prometheus.yml           # 抓取配置，含 Home Assistant 备用方案
│   ├── rules/mijia.rules.yml    # 1 条记录规则 + 6 条告警
│   └── tests/mijia.rules.test.yml   # 规则的 promtool 单测
├── grafana/
│   ├── provisioning/            # 数据源与面板自动加载
│   └── dashboards/mijia.json    # 「米家设备总览」面板
├── tests/test_offline.py        # 41 个离线单测
├── .github/workflows/           # CI 与发布流水线
├── auth/                        # 米家登录态（gitignore，不要外传）
├── LICENSE / CHANGELOG.md
└── docker-compose.yml           # exporter + Prometheus + Grafana
```

## 镜像与体积

镜像刻意做小，当前约 100MB（最初版本 285MB）：

| 做法 | 收益 |
|---|---|
| 内置 `miio_client.py` 替代 python-miio | 去掉 zeroconf、netifaces、cryptography、construct、croniter、tqdm 等约 30MB，并且不再需要编译器 |
| 二维码改用 `qrcode` + `pypng` 纯 Python PNG | 去掉 pillow，约 20MB |
| 运行镜像里卸载 pip，清理字节码缓存与第三方测试目录 | 约 20MB |
| 基镜像从 `python:3.12-slim`（189MB）换成 `python:3.12-alpine`（84MB） | 约 105MB；依赖都有 musllinux wheel（amd64 与 arm64） |

不用 distroless 的原因：`gcr.io/distroless/python3-debian12` 实测 79.6MB，只比 alpine 小 4.6MB，
代价是 Python 从 3.12 降到 3.11、没有 shell（排查困难）、没有 `useradd`、依赖要用 `--target` 加 `PYTHONPATH` 绕。

## 参与开发

欢迎提 Issue 与 PR。改动 exporter 或指标命名前请注意：`mijia_*` 指标名与标签是对外 API，
重命名属于破坏性变更，需要同步更新 `prometheus/rules/mijia.rules.yml`、`grafana/dashboards/mijia.json` 与测试用例。

文档与代码风格约定：术语统一（设备 / 属性 / 指标 / 系数 / 传输 / 发现 / 抓取 / 轮询 / 登录态 / 面板），
中文用全角标点、中英文之间留空格，注释只写「为什么」而不复述代码，同一个事实全仓库只写一处。

### 维护者发布流程

```bash
# 1. 更新 exporter/mijia_exporter.py 里的 VERSION 与 CHANGELOG.md
# 2. 跑完整测试并打标签
make release-tag VERSION=1.0.2
# 3. 推送标签，GitHub Actions 会自动多架构构建、推 Docker Hub、创建 Release
git push origin main && git push origin v1.0.2
```

需要在仓库的 **Settings → Secrets and variables → Actions** 里配置 `DOCKERHUB_USERNAME` 与
`DOCKERHUB_TOKEN`（Docker Hub 的 Access Token，不是登录密码）。
想在本机直接推也可以：`make login && make publish`（需要 buildx 插件）。

## 许可

[MIT](LICENSE) © 2026 Parhelion-Rev
