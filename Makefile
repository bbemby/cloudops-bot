# 常用开发/运维动作的快捷入口：make help 看全部
# 依赖：Python >= 3.11、make；容器相关目标需要本机有 docker
.DEFAULT_GOAL := help
SHELL := /bin/sh

PY ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: help venv install test test-v check doctor run gen-secret lint docker-build docker-up docker-down docker-logs clean

help:  ## 显示所有可用目标
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

venv:  ## 创建虚拟环境
	$(PY) -m venv $(VENV)

install: venv  ## 安装依赖
	$(BIN)/pip install -U pip
	$(BIN)/pip install -r requirements.txt

test:  ## 跑全部测试（205 个用例，约 9 秒）
	cd tests && $(CURDIR)/$(BIN)/python -m unittest discover -v

check:  ## 启动前体检：配置 + 数据库
	$(BIN)/python main.py --check

doctor:  ## 体检：配置 + 数据库 + 云凭证连通性
	$(BIN)/python manage.py doctor --check-cloud

gen-secret:  ## 生成 SECRET_KEY（写进 .env）
	$(BIN)/python manage.py gen-secret

run:  ## 启动机器人（前台，Ctrl-C 优雅退出）
	$(BIN)/python main.py

lint:  ## 静态检查（需要 ruff）
	$(BIN)/python -m ruff check cloudops tests main.py manage.py

docker-build:  ## 构建镜像
	docker build -t cloudops-bot:latest .

docker-up:  ## 用 compose 启动（后台）
	docker compose up -d --build

docker-down:  ## 停止并移除容器
	docker compose down

docker-logs:  ## 跟踪容器日志
	docker compose logs -f

clean:  ## 清理 pycache 与测试缓存
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .coverage htmlcov
