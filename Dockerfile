# syntax=docker/dockerfile:1
# -----------------------------------------------------------------------------
# CloudOps Bot 容器镜像
#
#   用官方预构建镜像（推荐）：
#     docker pull ghcr.io/bbemby/cloudops-bot:latest
#
#   自己构建：
#     docker build -t cloudops-bot .
#     docker run -d --name cloudops-bot --env-file .env -v "$PWD/data:/app/data" cloudops-bot
#
# 说明：
#   * 镜像里不含任何密钥，全部通过 --env-file / 编排文件注入；
#   * 以非 root 用户运行（uid 固定为 10001，便于宿主机挂载目录授权）；
#   * 数据目录 /app/data 挂在卷上（SQLite 与 mock 状态都在里面）；
#   * 同一个 Bot Token 只能跑一个实例，扩缩容前请先看 docs/DEPLOYMENT.md。
# -----------------------------------------------------------------------------
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai \
    PYTHONPATH=/app

WORKDIR /app

# 先装依赖，利用镜像层缓存：只要 requirements.txt 不变就不会重装
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 再拷代码（.dockerignore 已经排除了 .env / data / 测试缓存）
COPY . .

# 非 root 运行：uid/gid 固定，宿主机 bind mount 的 ./data 用 10001 授权即可。
# 固定 uid 是为了让文档与脚本里的 chown 不必靠猜（useradd 自动取号会随基础镜像变化）。
RUN groupadd --gid 10001 cloudops \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin cloudops \
    && mkdir -p /app/data \
    && chown -R cloudops:cloudops /app
USER cloudops

# 健康检查只看本机状态，不打外部 API（避免云厂商抖动导致容器被反复重启）。
# 判定逻辑与诊断输出见 deploy/healthcheck.py。
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python deploy/healthcheck.py

ENTRYPOINT ["python", "main.py"]
