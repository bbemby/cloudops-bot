# syntax=docker/dockerfile:1
# -----------------------------------------------------------------------------
# CloudOps Bot 容器镜像
#
#   docker build -t cloudops-bot .
#   docker run -d --name cloudops-bot --env-file .env -v "$PWD/data:/app/data" cloudops-bot
#
# 说明：
#   * 镜像里不含任何密钥，全部通过 --env-file / 编排文件注入；
#   * 以非 root 用户运行，数据目录挂在卷上（SQLite 与 mock 状态都在里面）；
#   * 同一个 Bot Token 只能跑一个实例，扩缩容前请先看 docs/DEPLOYMENT.md。
# -----------------------------------------------------------------------------
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# 先装依赖，利用镜像层缓存：只要 requirements.txt 不变就不会重装
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 再拷代码（.dockerignore 已经排除了 .env / data / 测试缓存）
COPY . .

# 非 root 运行；/app/data 用于 SQLite 与 mock 云状态
RUN useradd --create-home --shell /usr/sbin/nologin cloudops \
    && mkdir -p /app/data \
    && chown -R cloudops:cloudops /app
USER cloudops

# 轻量健康检查：只看进程与数据库文件，不打外部 API
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import sqlite3,os,sys; p=os.environ.get('DATABASE_PATH','/app/data/cloudops.db'); sys.exit(0 if os.path.exists(p) else 1)"

ENTRYPOINT ["python", "main.py"]
