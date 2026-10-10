# 基镜像选 python:3.12-alpine（84MB）而不是 slim（189MB）或 distroless（79.6MB）：
#   - 所有依赖都有 musllinux / 纯 Python wheel（pycryptodome、PyYAML 都验过 x86_64 与 aarch64），
#     所以**不需要任何编译器**，也就没有多阶段构建的必要；
#   - alpine 的 python 镜像自带 CA 证书与 /usr/share/zoneinfo（requests 与 tzlocal 都要用）；
#   - distroless/python3 只小 4.6MB，代价是 Python 降到 3.11、没有 shell（排查困难）、
#     没有 useradd、依赖还得用 --target + PYTHONPATH 绕 —— 不划算。
FROM python:3.12-alpine

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    MIJIA_CONFIG=/app/config.yaml \
    MIJIA_AUTH=/app/auth/mijia-auth.json

WORKDIR /app

COPY requirements.txt ./
# mijiaAPI 单独用 --no-deps 装：它真正需要的依赖已在 requirements.txt 里按需列出。
# 清理用 python 而不是 find（busybox 的 find 不支持 `-exec ... +`），
# 且刻意写成单行 —— 避免 RUN heredoc 对 BuildKit 前端版本的依赖。
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir --no-deps mijiaAPI==4.4.0 \
    && python -m pip uninstall -y pip \
    && python -c "import pathlib,shutil; r=pathlib.Path('/usr/local/lib/python3.12'); [shutil.rmtree(p, ignore_errors=True) for p in list(r.rglob('__pycache__'))+[q for q in r.rglob('tests') if q.is_dir()]]" \
    && rm -rf /root/.cache /tmp/*

COPY mijia_exporter.py mijia_spec.py miio_client.py ./

# 不依赖宿主 umask：UMASK 077 时 COPY 进来的文件是 600，容器以非 root 运行会读不了。
RUN chmod -R a+rX /app \
    && adduser -D -u 10001 app \
    && chown -R app:app /app
# 注意：docker-compose 里用宿主 uid 覆盖这个 USER（见 compose 的 user:），
# 这样挂载进来的 config.yaml、spec-cache 不会有属主/权限问题。
USER app

EXPOSE 9101

HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9101/ready', timeout=5)"

ENTRYPOINT ["python", "/app/mijia_exporter.py"]
