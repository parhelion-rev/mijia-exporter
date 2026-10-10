# 变更记录

本文件遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

指标名与标签（`mijia_*`）是对外 API，重命名属于破坏性变更，会让面板与告警静默变空，因此只在主版本号里做。

> 1.0.0 是首个公开发布版本。下面的「变更」与「修复」记录的是发布前开发过程中相对早期内部版本的变化，
> 保留下来是为了说明现状是怎么来的，并非相对上一个公开版本的差异。

## [Unreleased]

### 修复

- **阈值/门限属性不再混进测量指标族**。规格常不给门限属性单位（实测 `overvoltage-alarm`、
  `undervoltage-alarm`、`overcurrent-alarm` 都没有），按名字兜底推断单位时会被当成电压/电流，
  于是「过压门限 265 V」被 Grafana 画进了电压曲线。现在名字含 `alarm` / `limit` / `threshold` /
  `max` / `min` 等词的属性单独成族（`mijia_volts_limit`、`mijia_amperes_limit`），
  测量族保持干净；可用 `overrides` 的 `limit` 字段双向纠正，`metric` 字段优先级最高。

## [1.0.1] - 2026-10-10

### 新增

- `mijia_spec_available{device}`：这次发现是否用上了 miot-spec 规格（0 = 已退化为盲扫、指标名降级）。
- `mijia_spec_cache_writable`：规格缓存目录是否可写，配合 `MijiaSpecCacheNotWritable` 告警，
  让「指标名降级」这件事本身可被发现——它此前会让面板整片变空而容器依旧是 healthy。

### 修复

- **退化为盲扫后不再需要重启容器**：每 10 分钟重试一次规格，成功就地把属性升级回规格模式。
  此前运维修好缓存目录权限后，必须重启 exporter 才能恢复带单位的指标名。
- HTTP 服务不再为健康检查的提前断开打印 `BrokenPipeError` 堆栈。

## [1.0.0] - 2026-10-10

首个公开发布版本，从单设备脚本重写为通用米家 exporter。

### 新增

- **指标命名由规格推导**：按属性的单位归类（`watt` → `mijia_watts`、`kwh` → `mijia_kilowatt_hours_total`、
  `bool` → `mijia_state`），换设备不用改代码；单位缺失时按属性名兜底推断。
- **三级属性发现**：miot-spec 官方规格 → MIoT `siid.piid` 盲扫 → 旧版 `get_prop` 盲扫，
  另有 `discovery: manual` 手工兜底。规格缓存落盘，取过一次后离线可用。
- **多设备**：一台 exporter 接多台设备，各自独立的轮询间隔与批次设置；单台设备初始化失败不影响其它设备，且会自动重试。
- **两种传输**：本地 miIO（低延迟、不依赖外网）与米家云（批量读属性，适合不在同网段或拿不到 token 的设备）。
- **`overrides` 校正层**：`scale` / `metric` / `name` / `unit` / `format` / `cumulative` / `ignore`。
- **校准工具**：`make dump` 打印属性到指标的映射，`make watch` 反复采样原始值。
- **可选外部心跳**：默认关闭、不做任何外联，用于发现「监控自己死了」这类 Prometheus 自己发不出的故障。
- **告警**：1 条记录规则 + 6 条告警，其中包含「设备从未采上」这一盲区；13 个 promtool 用例覆盖触发与不触发。
- 41 个离线单测，覆盖命名规则、三级发现、多设备、云端登录态、心跳与 HTTP 端点。
- GitHub Actions：CI（构建、单测、规则测试、mock 端到端冒烟、静态校验）与 Release（多架构推镜像）。

### 变更

- **内置极简 miIO 客户端**（`exporter/miio_client.py`）替代 python-miio。后者带有 mDNS 发现、
  固件更新器等与按 IP 读写属性无关的依赖，且 `netifaces` 没有 py3.12 的预编译 wheel。
- **镜像瘦身**：基镜像换成 alpine，去掉 pillow（二维码改走 `qrcode` 与 `pypng` 的纯 Python 路径），
  运行镜像内不再保留 pip。体积从 285MB 降到约 100MB，依赖层从 75MB 降到 18MB，且不再需要编译器。
- 取 token 统一走 `make token`（米家 App 扫码）。账号密码登录会被小米的图形验证码拦下（实测 `code=87001`），
  旧的 `micloud` 路径已移除。
- 云端传输不再调用 `api.login()`。它在刷新失败时会退化成二维码登录，在无头环境里空等约 2 分钟，
  改为只做刷新，失败时直接给出可操作的报错。

### 修复

- miIO 发现包长度少 4 字节（28 字节），导致扫描不到任何设备。
- `get_properties` 未携带 `did` 字段时，部分固件（`chuangmi.plug.212a01`）直接拒绝请求。
- 规格里的 `none`、`-` 等假单位会让指标名退化成 `mijia_none` 或 `mijia_value`。
- `rule_files` 通配符把 promtool 测试文件也扫了进去，导致 Prometheus 反复 `exit 2`。
- 容器以非 root 运行时读不到配置（宿主 umask 077 使文件成为 600），镜像内与 `make init` 均已处理。
- `--entrypoint` 写在镜像名之后时会被当成容器命令。

[1.0.0]: https://github.com/parhelion-rev/mijia-exporter/releases/tag/v1.0.0
[1.0.1]: https://github.com/parhelion-rev/mijia-exporter/releases/tag/v1.0.1
