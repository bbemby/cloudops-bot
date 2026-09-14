# CloudOps Bot 部署手册

面向"把这套机器人真正跑起来"的场景。三种方式选一种即可：

1. [裸机 + systemd](#1-裸机--systemd推荐)
2. [Docker / Docker Compose](#2-docker--docker-compose)
3. [反向代理与网络受限环境](#3-网络受限与自建-gateway)

无论哪种方式，启动前都先做一次体检：

```bash
python main.py --check        # 配置是否齐全、数据库能否初始化
python manage.py doctor       # 加上"云凭证连通性"体检（--check-cloud）
```

---

## 1. 裸机 + systemd（推荐）

```bash
# 1) 准备用户与目录
sudo useradd -r -s /usr/sbin/nologin cloudops
sudo mkdir -p /opt/cloudops-bot && sudo chown cloudops:cloudops /opt/cloudops-bot
sudo -u cloudops git clone https://github.com/bbemby/cloudops-bot.git /opt/cloudops-bot
cd /opt/cloudops-bot

# 2) 虚拟环境与依赖
sudo -u cloudops python3 -m venv .venv
sudo -u cloudops .venv/bin/pip install -r requirements.txt

# 3) 配置
sudo -u cloudops cp .env.example .env
sudo -u cloudops .venv/bin/python manage.py gen-secret    # 把输出写进 .env 的 SECRET_KEY
sudo -u cloudops vi .env                                  # TELEGRAM_BOT_TOKEN / BOT_ADMIN_IDS
sudo chmod 600 .env                                       # 里面是密钥，只有属主可读

# 4) 注册服务
sudo cp deploy/cloudops-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cloudops-bot

# 5) 观察
sudo systemctl status cloudops-bot
sudo journalctl -u cloudops-bot -f
```

升级：

```bash
cd /opt/cloudops-bot && sudo -u cloudops git pull
sudo -u cloudops .venv/bin/pip install -r requirements.txt
sudo systemctl restart cloudops-bot      # 停机时会等待后台创建任务收尾
```

### 为什么不用 `nohup python main.py &`

`main.py` 自己处理了 `SIGINT`/`SIGTERM`：收到信号后停止长轮询、
等待后台创建/销毁任务结束、关闭 HTTP 会话后才退出。
用 systemd 托管才能让这套收尾逻辑稳定执行（容器里同理，见下）。

---

## 2. Docker / Docker Compose

```bash
cp .env.example .env && python manage.py gen-secret    # 填好 .env
docker compose up -d --build
docker compose logs -f
```

要点：

- `.env` 通过 `env_file` 注入，**不进镜像**（`.dockerignore` 已排除）；
- `./data` 挂成卷，里面是 SQLite（凭证、日志、任务）与 mock 云状态；
- 容器内以非 root 的 `cloudops` 用户运行；
- 镜像自带 `HEALTHCHECK`（只检查数据库文件存在，不打外部 API，
  避免把云厂商抖动误判成容器不健康）。

> 单实例约束：Telegram 的长轮询要求同一个 Bot Token 只有一个消费者。
> 扩容会出现 409 Conflict，程序会打印明确提示并按退避重试，
> 但正确做法是**只跑一个实例**。真需要横向扩展时，
> 请先把长轮询换成 webhook（见"路线图"）。

---

## 3. 网络受限与自建 gateway

国内网络访问 `api.telegram.org` 常需要代理：

```bash
TELEGRAM_PROXY=http://127.0.0.1:7890
```

如果使用自建的反代/网关，则改：

```bash
TELEGRAM_API_BASE=https://your-gateway.example.com
# 网关需要保持 /bot<TOKEN>/<method> 的路径语义（本项目的客户端就是这么拼的）
```

云厂商 API 出网被限制时，需要放行：

- DigitalOcean: `api.digitalocean.com`
- AWS: `ec2.<region>.amazonaws.com`、`ssm.<region>.amazonaws.com`

---

## 4. 日常运维

```bash
python manage.py doctor            # 体检
python manage.py doctor --check-cloud   # 顺带验证每条云凭证
python manage.py users             # 谁在用、角色是什么
python manage.py promote 123456    # 提权为管理员（同时记得加进 BOT_ADMIN_IDS）
python manage.py creds             # 已绑定凭证一览（字段名脱敏）
python manage.py stats             # 用户/凭证/操作/任务统计
python manage.py vacuum            # 整理 SQLite 文件
```

备份与恢复（SQLite 单文件，冷备即可）：

```bash
sudo systemctl stop cloudops-bot
sudo cp /opt/cloudops-bot/data/cloudops.db /backup/cloudops-$(date +%F).db
sudo systemctl start cloudops-bot
```

> **SECRET_KEY 必须一起备份**：它是凭证密文的解密密钥。
> 只恢复数据库而没有原 `SECRET_KEY`，所有云凭证都需要重新 `/bind`。

## 5. 故障排查

| 现象 | 原因与处理 |
| --- | --- |
| 启动即报 `409 Conflict` | 同一 Token 有另一个进程在轮询：先停掉它 |
| `配置校验失败：TELEGRAM_BOT_TOKEN 未设置` | `.env` 没被读到：确认 `-e` 路径、当前工作目录、`chmod 600` 后的属主 |
| 群里发指令没反应 | ① 是否已把 Bot 拉进群且群内允许发消息；② `journalctl -u cloudops-bot` 看是否在拒绝（未授权 ID 会留 `denied` 日志） |
| `/list` 某平台报查询失败 | 该平台凭证失效或出网被拦：`python manage.py doctor --check-cloud` 定位到具体是哪条凭证 |
| `/create` 一直不推送公网 IP | 默认等待 900 秒（`CREATE_TIMEOUT_SECONDS`）。超时后实例可能仍在创建，可用 `/list` 复查 |
| 实例创建成功但没拿到 IP | 检查安全组/子网：AWS 需要 `AWS_ASSOCIATE_PUBLIC_IP=true` 且子网是公网子网 |
| 更换了 `SECRET_KEY` | 已入库凭证无法解密，`/creds` 会提示，重新 `/bind` 一次即可 |
