# Docker 部署教程（从零到跑起来）

这篇是**手把手**的容器部署步骤：从一台干净的 Linux 服务器开始，到机器人能在
Telegram 里回话、管理面板能在浏览器里打开。所有命令都可以直接复制。

> 只想最快跑起来：跳到 [1. 五分钟上手](#1-五分钟上手)。
> 想连域名 + HTTPS：看 [5. 让面板走域名与 HTTPS](#5-让面板走域名与-https)。

---

## 0. 你会得到什么

```
┌──────────────────────────── 宿主服务器 ────────────────────────────┐
│                                                                   │
│  ./data/            ← 卷：SQLite（凭证/日志/任务）+ mock 云状态     │
│  .env               ← 密钥与配置（不进镜像、不进 Git）              │
│                                                                   │
│  ┌──────────────── cloudops-bot 容器（非 root，uid 10001）──────┐  │
│  │  python main.py                                            │  │
│  │    ├── Telegram 长轮询  ──→  api.telegram.org  (出网)       │  │
│  │    ├── 云平台适配器     ──→  DigitalOcean / AWS (出网)      │  │
│  │    └── Web 管理面板     ←──  :9878  （可选，默认开）        │  │
│  └────────────────────────────────────────────────────────────┘  │
└───────────────────────────────────────────────────────────────────┘
```

三个关键点，先记住：

1. **同一个 Bot Token 只能有一个进程在长轮询**。不要 `--scale 2`，否则 Telegram 回 409。
2. **`SECRET_KEY` 必须和 `data/` 里的数据库成对保存**。换密钥又留着旧库 → 启动直接失败（故意的）。
3. **面板能改凭证、能销毁机器**。要么给它强口令 + 防火墙，要么 `WEB_ENABLED=false` 关掉。

---

## 1. 五分钟上手

### 前置条件

| 项目 | 要求 | 检查命令 |
| --- | --- | --- |
| Docker | 20.10+ | `docker version` |
| Docker Compose | v2（`docker compose` 子命令形式） | `docker compose version` |
| 出网 | 能访问 `api.telegram.org` 与云厂商 API | `curl -sI https://api.telegram.org` |
| Bot Token | 在 [@BotFather](https://t.me/BotFather) 创建 | — |
| 管理员 ID | 自己的 Telegram 数字 ID，问 [@userinfobot](https://t.me/userinfobot) | — |

### 第一步：拿到代码

```bash
# 方式 A：git 克隆
git clone https://github.com/bbemby/cloudops-bot.git
cd cloudops-bot

# 方式 B：不想要源码，只想跑容器 —— 只下载编排文件与样例配置
mkdir cloudops-bot && cd cloudops-bot
curl -fsSLO https://raw.githubusercontent.com/bbemby/cloudops-bot/main/docker-compose.yml
curl -fsSLO https://raw.githubusercontent.com/bbemby/cloudops-bot/main/.env.example
mv .env.example .env
```

### 第二步：生成配置

```bash
# 用你自己喜欢的编辑器填 3 个值
$EDITOR .env
```

必填的就三项（其余保持默认即可）：

```bash
TELEGRAM_BOT_TOKEN=123456789:AA...          # @BotFather 给你的
BOT_ADMIN_IDS=123456789                     # 你的 Telegram 数字 ID（多个用逗号分隔）
SECRET_KEY=                                  # 先留空，下一条命令生成
```

```bash
# 生成并直接写入 .env 的 SECRET_KEY 行（其余内容和注释保持不动）
python3 manage.py gen-secret -w .env

# 顺手校验一遍有没有漏项（不联网、不连 Telegram）
python3 main.py -e .env --check
```

> 没有本机 Python 3.9+？用容器代跑：
> `docker run --rm -v "$PWD:/app" -w /app python:3.11-slim python manage.py gen-secret -w .env`
>
> 如果 `data/` 里已经有数据库，这条命令会先提醒你"换密钥会让已入库的凭证解不开"
> —— 看到这行提醒就停下来确认一下，别顺手覆盖。

### 第三步：准备数据目录的属主

容器内以**固定的非 root 账号**运行（`cloudops`，uid/gid = `10001`）。挂载目录必须是它可写的：

```bash
mkdir -p data
sudo chown -R 10001:10001 data
```

> 这步忘了会怎样？容器起来后报 `unable to open database file`，日志里能看到，
> 健康检查也会是 `unhealthy`。补一句 `chown` 再 `docker compose restart` 就行。

### 第四步：起服务

```bash
# 推荐：用 CI 构建好并冒烟测过的镜像
docker compose pull
docker compose up -d

# 或者本地构建（改过代码、或拉不到 GHCR 时）
docker compose up -d --build
```

### 第五步：确认真的好了

```bash
docker compose ps                                  # STATUS 里应出现 (healthy)，约 20~60 秒后
docker compose logs -f --tail=50                   # Ctrl-C 退出，不影响服务
```

日志里应该能看到这几行：

```
INFO  cloudops.bot.telegram: Telegram Bot 已连接：@your_bot (id=123...)
INFO  cloudops.web.server: Web 管理面板已监听 http://0.0.0.0:9878（只读模式=否）
INFO  cloudops.app: 开始长轮询（timeout=30s）…
```

然后在 Telegram 里给机器人发一条：

```
/ping
```

收到 `🏓 pong` 就说明整条链路（容器 → 出网 → Telegram → 你的账号权限）都通了。

面板这边：

```bash
curl -s http://127.0.0.1:9878/healthz     # {"status": "ok", ...}
```

浏览器打开 `http://<服务器IP>:9878`，用 `.env` 里的 `WEB_ADMIN_PASSWORD` 登录。

> **口令没设会怎样？** `WEB_ADMIN_PASSWORD` 留空时，每次启动会**随机生成**一个，
> 并以 `WARNING` 打在容器日志里（只打一次）。生产环境请显式设置，
> 否则每次重启都要去翻日志。

---

## 2. 不用 Compose：纯 `docker run`

```bash
docker build -t cloudops-bot .                 # 或用 ghcr.io/bbemby/cloudops-bot:latest
docker run -d --name cloudops-bot --restart unless-stopped \
  --env-file .env \
  -e DATABASE_PATH=/app/data/cloudops.db \
  -e MOCK_STATE_PATH=/app/data/mock-cloud.json \
  -p 9878:9878 \
  -v "$PWD/data:/app/data" \
  --security-opt no-new-privileges:true \
  --log-opt max-size=10m --log-opt max-file=3 \
  ghcr.io/bbemby/cloudops-bot:latest
```

等价于 compose 文件里的设置。`-e DATABASE_PATH` 这两项是**故意显式写死**的，
免得 `.env` 里的相对路径在容器里解析到意外位置。

---

## 3. 配置项速查（容器相关）

`.env` 的完整清单在仓库根目录的 `.env.example` 里都有注释，这里只列容器部署时会动的：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | — | 必填。Bot Token |
| `BOT_ADMIN_IDS` | — | 必填。管理员数字 ID，逗号分隔 |
| `SECRET_KEY` | — | 必填。加密云端凭证用；**和 `data/` 一起备份** |
| `WEB_ENABLED` | `true` | `false` 则完全不监听端口（只跑 ChatOps） |
| `WEB_HOST` | `0.0.0.0` | 容器里保持默认；只在容器**内部**监听 |
| `WEB_PORT` | `9878` | 容器内端口，改了要同步改端口映射 |
| `WEB_ADMIN_PASSWORD` | 空→随机 | 面板口令，**生产必须显式设置** |
| `WEB_READONLY` | `false` | `true` 则面板只读（能看不能动，适合给同事看） |
| `WEB_SECURE_COOKIE` | `false` | 走 HTTPS 时设 `true`（cookie 加 `Secure`） |
| `WEB_TRUST_PROXY` | `false` | 反向代理后面设 `true`（登录限速按 `X-Forwarded-For` 计来源） |
| `WEB_SESSION_TTL_SECONDS` | `28800` | 会话有效期（8 小时） |
| `WEB_MAX_LOGIN_ATTEMPTS` / `WEB_LOGIN_WINDOW_SECONDS` | `5` / `300` | 登录限速：5 分钟内 5 次失败即拦 |
| `ENABLE_MOCK_PROVIDER` | `false` | 打开后有个假云平台可练手，不花一分钱 |
| `LOG_LEVEL` | `INFO` | 排障时临时 `DEBUG` |

宿主机端口由 compose 的 `WEB_PANEL_PORT` 决定（不进 `.env`，是编排层变量）：

```bash
WEB_PANEL_PORT=8080 docker compose up -d     # 宿主机 8080 → 容器 9878
```

---

## 4. 面板：开门之前先想清楚

面板和机器人**共用同一个库**，聊天里刚开的机器刷新面板就能看到，面板上的销毁也会
在 `operation_log` 里留下同样格式的留痕。代价是：面板 = 你的云账号操作台。

裸奔公网的后果：任何扫到 9878 端口的人，只要口令够弱，就能删掉你的生产机器。

按风险选一档：

| 场景 | 做法 |
| --- | --- |
| 只要 ChatOps | `WEB_ENABLED=false`，删掉 `ports:` 段 |
| 本机/内网用 | compose 端口改成 `"127.0.0.1:9878:9878"`，用 SSH 隧道访问：`ssh -L 9878:127.0.0.1:9878 user@server` |
| 团队看，不需写 | `WEB_READONLY=true` + 内网 |
| 公网 + 域名 | 见下一节：反代 + TLS + 强口令，并**务必**设 `WEB_SECURE_COOKIE=true` |

面板自身已有的防线（`cloudops/web/`）：

- 口令用 `scrypt` 存储，会话是 HMAC 签名的 cookie（`HttpOnly` + `SameSite=Lax`）；
- 所有写操作都要 CSRF token；登录有失败计数限速；
- 响应统一带 `X-Content-Type-Options` / `X-Frame-Options: DENY` / `Referrer-Policy` / 严格 CSP；
- 静态资源走白名单（只有 `app.css` / `app.js`），路径穿越返回 404；
- 接口**永远不返回密钥明文**，凭证一律脱敏（`supe****oken` 这种形态）；
- 改口令 / 换 `SECRET_KEY` 会让旧会话立即失效。

---

## 5. 让面板走域名与 HTTPS

思路：**容器只监听本机**，由反代终止 TLS。

```yaml
# docker-compose.yml 里改这一行
    ports:
      - "127.0.0.1:9878:9878"
```

```bash
# .env 里加两项
WEB_SECURE_COOKIE=true
WEB_TRUST_PROXY=true
```

### nginx

```nginx
server {
    listen 443 ssl http2;
    server_name ops.example.com;

    ssl_certificate     /etc/letsencrypt/live/ops.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/ops.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:9878;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Caddy（自动签证书，两行搞定）

```caddyfile
ops.example.com {
    reverse_proxy 127.0.0.1:9878
}
```

外部只需要开 `443`（以及 `80` 用于 ACME 校验），**不要**再对公网开 `9878`：

```bash
sudo ufw allow 80,443/tcp && sudo ufw delete allow 9878/tcp
```

---

## 6. 升级与回滚

```bash
docker compose pull && docker compose up -d      # 升级
docker compose logs -f --tail=30                 # 看新版本起没起来
```

想固定版本（生产建议）：

```yaml
    image: ghcr.io/bbemby/cloudops-bot:v1.0.0    # 用 tag 而不是 latest
```

回滚 = 把 tag 改回上一个版本再 `up -d`。数据库结构升级是**向前兼容**的
（启动时建表/补列，不改已有数据），但跨大版本回滚前请先备份 `data/`。

---

## 7. 备份与恢复

`data/` 里就是全部状态：`cloudops.db`（凭证、日志、任务）与 `mock-cloud.json`。

```bash
# 热备一份（SQLite 的 .backup 不会撕裂写入中的事务）
docker compose exec -T cloudops-bot python - <<'PY'
import sqlite3, os
src = sqlite3.connect("/app/data/cloudops.db")
dst = sqlite3.connect("/app/data/backup-snapshot.db")
src.backup(dst); dst.close(); src.close()
print("ok", os.path.getsize("/app/data/backup-snapshot.db"), "bytes")
PY

# 带走（连同 .env —— 里面的 SECRET_KEY 缺了，凭证就解不开）
tar czf cloudops-backup-$(date +%F).tar.gz data/ .env
```

**恢复**：停容器 → 把 `data/` 和 `.env` 放回原位 → `docker compose up -d`。
`SECRET_KEY` 与库里的凭证必须配对：不匹配时启动会打印两种修复方式（改回旧密钥，
或删库重新 `/bind`）后退出，不会等到用的时候才炸。

---

## 8. 日常维护命令

```bash
docker compose logs -f --tail=100              # 跟日志
docker compose exec cloudops-bot sh            # 进容器
docker compose restart                         # 重启（SIGTERM 优雅退出）
docker inspect -f '{{.State.Health.Status}}' cloudops-bot     # healthy / unhealthy
docker inspect -f '{{json .State.Health}}' cloudops-bot | tail -c 400   # 不健康的原因
docker stats --no-stream cloudops-bot          # 资源占用
docker image prune -f                          # 清构建缓存（升级后腾空间）
```

优雅停止的行为：发 `SIGTERM` → 停长轮询 → 等后台创建/销毁任务收尾 → 关 HTTP 会话 →
退出。实测毫秒级，不会被 `docker stop` 的 10 秒宽限期 `SIGKILL` 掉。

---

## 9. 故障排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `unable to open database file` | `./data` 属主不是 10001 | `sudo chown -R 10001:10001 data` |
| 启动即退出，日志说 `SECRET_KEY 与数据库中已有凭证不匹配` | 换过密钥/机器，又留着旧库 | 改回旧 `SECRET_KEY`，或删 `data/cloudops.db` 重新 `/bind` |
| 日志刷 `409 Conflict` | 同一个 Token 有第二个进程在长轮询 | 关掉多余实例（含以前 `nohup` 跑的裸机进程） |
| 面板 502 / 打不开 | 容器没监听、端口映射写错、或 `WEB_ENABLED=false` | `docker compose logs` 看有没有"面板已监听"那行；`curl -s 127.0.0.1:9878/healthz` |
| 面板登录后马上又要登录 | 走 HTTPS 但没设 `WEB_SECURE_COOKIE=true`（或反之，HTTP 下设了 true） | 让 `WEB_SECURE_COOKIE` 与实际协议一致 |
| 面板提示「会话已失效」 | 改了 `WEB_ADMIN_PASSWORD` / `SECRET_KEY`，或超过会话 TTL | 重新登录；TTL 用 `WEB_SESSION_TTL_SECONDS` 调 |
| `docker compose pull` 拉不动 GHCR | 网络/代理 | 本地构建：`docker compose up -d --build` |
| 容器 healthy 但机器人不回话 | Telegram 出网被墙，或 Token 不对 | 设 `TELEGRAM_PROXY`，或 `TELEGRAM_API_BASE` 指向自建网关；`/ping` 无响应时先看日志里的 `getMe` 结果 |
| 时间显示不对 | 容器时区 | Dockerfile 默认 `TZ=Asia/Shanghai`，需要别的就加 `-e TZ=...` |

排障三板斧：`docker compose logs -f`（看应用日志）→
`docker inspect -f '{{json .State.Health}}'`（看健康检查说了什么）→
`curl -s 127.0.0.1:9878/healthz`（面板活没活）。

---

## 10. 安全清单（上线前逐条打勾）

- [ ] `.env` 没有进 Git（`.gitignore` 已含 `.env`），也没有被 `docker commit` 固化进镜像
- [ ] `SECRET_KEY` 是随机生成的 32+ 字节，不是手打的短字符串
- [ ] `WEB_ADMIN_PASSWORD` 显式设置且足够强（面板留空会随机生成，但重启即换）
- [ ] 公网只开 `443`；`9878` 只绑 `127.0.0.1` 或用防火墙挡住
- [ ] 走 HTTPS 时 `WEB_SECURE_COOKIE=true`；反代后面 `WEB_TRUST_PROXY=true`
- [ ] 云厂商凭证用**最小权限**（只给需要的 region/动作），定期在面板或 `/bind` 轮换
- [ ] 容器保持非 root（镜像默认）、`no-new-privileges:true`
- [ ] `data/` 有定时备份，`.env` 与之同存
- [ ] 只想让机器人干活就给 `WEB_ENABLED=false`

---

## 11. 这份教程的验证程度（不吹牛）

诚实说明哪些是实测过的、哪些只是写好但没在容器里跑过：

| 内容 | 状态 |
| --- | --- |
| `Dockerfile` 构建、非 root（uid 10001）、卷写入、`SIGTERM` 优雅退出、密钥指纹守卫 | **在真实服务器（Debian 12 / Docker 29.7.2）实测通过** |
| 容器级冒烟：`compose up` → `getMe` → 注册指令 → 编译 → `/bind` → `docker stop` 退出码 0 | **实测通过**（`docker compose` 在 CI 里每次推送都重跑一遍） |
| 管理面板（登录、CSRF、实例列表/销毁、凭证脱敏、审计）| 在真实进程 + 真实 HTTP 上端到端验证通过（非容器内） |
| 面板在容器内的端口暴露（`EXPOSE 9878` / `9878:9878`） | CI 的容器冒烟会打 `/healthz` 断言 |
| nginx / Caddy / ufw / tar 备份等外围命令 | 按标准用法撰写，未在本项目环境逐条执行 |

发现问题欢迎开 issue，或在服务器上跑 `docker inspect -f '{{json .State.Health}}' cloudops-bot`
把输出贴上来。
