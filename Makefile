COMPOSE ?= docker compose
PYTHON ?= python3
VERSION ?= 1.0.2
IMAGE ?= mijia-exporter:$(VERSION)
PROM_IMAGE ?= prom/prometheus:v3.13.4
# 发布目标用：Docker Hub 命名空间与目标架构
PUBLISH_REPO ?= effectwei/mijia-exporter
PLATFORMS ?= linux/amd64,linux/arm64
ARGS ?=
IP ?=
TOKEN ?=

.PHONY: help init build up down logs ps token probe try-token check once dump watch \
        test test-rules validate publish release-tag login clean

help:
	@echo "make init        生成 .env / exporter/config.yaml / spec-cache，并记录宿主 uid"
	@echo "make token       米家 App 扫码登录并取 token（写进 .env）"
	@echo "make probe       局域网探测（ARGS=\"--scan-only\" 只看发现）"
	@echo "make try-token   用已知 token 试读：make try-token IP=192.168.1.50"
	@echo "make check       校验配置（不碰设备）"
	@echo "make dump        打印每台设备的 属性→指标 映射（写 overrides 用）"
	@echo "make once        真机读一次，打印 原始值/系数/物理值"
	@echo "make watch       反复采样原始值（校准系数用），ARGS=间隔秒数"
	@echo "make up          启动 exporter + Prometheus + Grafana"
	@echo "make down        停止"
	@echo "make logs        跟踪 exporter 日志"
	@echo "make test        离线单测（不需要真机）"
	@echo "make test-rules  用 promtool 验证告警规则真的会触发"
	@echo "make validate    静态校验 YAML / JSON"
	@echo ""
	@echo "发布（维护者）："
	@echo "make publish     多架构构建并推送到 Docker Hub（需要 buildx + 已登录；通常由 CI 完成）"
	@echo "make release-tag 跑完整测试后打 v\$$(VERSION) 标签；推送标签即触发 GitHub Release 流水线"

init:
	@test -f .env || (cp .env.example .env && echo "已生成 .env，记得填 token")
	@test -f exporter/config.yaml || (cp exporter/config.example.yaml exporter/config.yaml && echo "已生成 exporter/config.yaml")
	@mkdir -p exporter/spec-cache auth
	@grep -q '^EXPORTER_UID=' .env || printf '\n# 容器以宿主 uid 运行（原因见 docker-compose.yml 的 user:），make init 自动写入\nEXPORTER_UID=%s\nEXPORTER_GID=%s\n' "$$(id -u)" "$$(id -g)" >> .env
	# Prometheus 以 nobody、Grafana 以 uid 472 运行，挂载进去的配置必须可读。
	# 宿主 umask 是 077 时这些文件默认 600，容器会 permission denied 起不来。
	@chmod -R a+rX exporter/config.yaml exporter/*.py prometheus grafana

build:
	docker build -t $(IMAGE) ./exporter

up: init
	$(COMPOSE) up -d --build
	@echo ""
	@echo "Grafana     http://<本机 IP>:3000   （账号见 .env）"
	@echo "Prometheus  http://<本机 IP>:9090"
	@echo "exporter    http://<本机 IP>:9101/metrics"
	@echo ""
	@echo "不想本地构建镜像？把 .env 里的 MIJIA_IMAGE 指向已发布镜像，然后："
	@echo "  $(COMPOSE) pull && $(COMPOSE) up -d --no-build"

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f --tail=100 exporter

ps:
	$(COMPOSE) ps

# 工具类目标统一用同一个镜像，靠 --entrypoint 绕过 exporter 的 ENTRYPOINT
# （exec 形式的 ENTRYPOINT 会把命令追加在后面，不覆盖就会变成 python mijia_exporter.py xxx.py）
DOCKER_TOOL = docker run --rm -i --user "$$(id -u):$$(id -g)" -e HOME=/tmp \
	-v "$$PWD":/work -w /work --entrypoint python

token: init build
	$(DOCKER_TOOL) -e MIJIA_AUTH=/work/auth/mijia-auth.json $(IMAGE) /work/mijia_token.py $(ARGS)

probe: build
	$(DOCKER_TOOL) --network host $(IMAGE) /work/probe.py $(ARGS)

try-token: init build
	@test -n "$(IP)" || { echo '用法: make try-token IP=192.168.1.50 [TOKEN=32 位 hex，默认读 .env]'; exit 2; }
	$(DOCKER_TOOL) --network host --env-file .env $(IMAGE) /work/try_token.py $(IP) $(TOKEN)

# 不依赖 docker compose：没装 compose 插件的机器也能先验证。
# 用宿主 uid 运行 + 可写的 spec-cache；auth 目录总是挂进去（云端设备要用，空目录也无妨）。
RUN_EXPORTER = docker run --rm --user "$$(id -u):$$(id -g)" -e HOME=/tmp --env-file .env \
	-v "$$PWD/exporter/config.yaml":/app/config.yaml:ro \
	-v "$$PWD/exporter/spec-cache":/app/spec-cache \
	-v "$$PWD/auth":/app/auth \
	$(IMAGE)

check: init build
	$(RUN_EXPORTER) --check

dump: init build
	$(RUN_EXPORTER) --dump

once: init build
	$(RUN_EXPORTER) --once

# 校准用：反复采样并打印原始值。ARGS 里给间隔秒数，默认 20 秒
watch: init build
	$(RUN_EXPORTER) --watch $(or $(ARGS),20)

test: build
	docker run --rm --user "$$(id -u):$$(id -g)" -e HOME=/tmp -v "$$PWD":/src -w /src \
		--entrypoint python $(IMAGE) /src/tests/test_offline.py

# 优先用本机 promtool，没有就借用官方镜像里的
test-rules:
	@if command -v promtool >/dev/null 2>&1; then \
		promtool test rules prometheus/tests/mijia.rules.test.yml; \
	else \
		docker run --rm -v "$$PWD/prometheus":/p --entrypoint promtool $(PROM_IMAGE) \
			test rules /p/tests/mijia.rules.test.yml; \
	fi

# 静态校验。注意 exporter/config.yaml 是本地文件（已 gitignore），新克隆的仓库里没有，
# 所以它只在存在时校验 —— 否则 CI 与首次 clone 这一步会直接"文件不存在"失败。
validate:
	$(PYTHON) -c "import json, pathlib, yaml; \
json.load(open('grafana/dashboards/mijia.json')); \
required = ['docker-compose.yml','prometheus/prometheus.yml','prometheus/rules/mijia.rules.yml','exporter/config.example.yaml','grafana/provisioning/datasources/prometheus.yml','grafana/provisioning/dashboards/dashboards.yml','.github/workflows/ci.yml','.github/workflows/release.yml']; \
optional = ['exporter/config.yaml']; \
missing = [f for f in required if not pathlib.Path(f).is_file()]; \
assert not missing, f'缺少必要文件：{missing}'; \
[yaml.safe_load(open(f)) for f in required + [f for f in optional if pathlib.Path(f).is_file()]]; \
print('YAML / JSON 全部通过')"

# ------------------------------------------------------------------ 发布（维护者）
# 平时不用管；打标签后 GitHub Actions 会自动做同样的事（见 .github/workflows/release.yml）
login:
	docker login

# 多架构构建并推送。需要 buildx：docker buildx version 能跑就行
# （Docker Desktop 自带；Linux 上装 plugin：见 https://docs.docker.com/build/install-buildx/）
# 平时不必用这个目标：推 v* 标签后由 GitHub Actions 自动完成同样的事。
publish: validate test test-rules
	docker buildx build --platform $(PLATFORMS) \
		-t $(PUBLISH_REPO):$(VERSION) \
		-t $(PUBLISH_REPO):$(basename $(VERSION)) \
		-t $(PUBLISH_REPO):latest \
		--push ./exporter
	@echo "已推送：$(PUBLISH_REPO):$(VERSION)（$(PLATFORMS)）"

# 先跑完整测试再打标签；推送标签会触发 release.yml 自动发版
release-tag: validate test test-rules
	@git diff --quiet || { echo "工作区不干净，先提交再打标签"; exit 1; }
	git tag -a "v$(VERSION)" -m "v$(VERSION)"
	@echo "已打标签 v$(VERSION)。推送它即触发发布流水线："
	@echo "  git push origin main && git push origin v$(VERSION)"

clean:
	$(COMPOSE) down -v
