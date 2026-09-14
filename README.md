# CloudOps Bot · 基于即时通讯机器人的多云自动化运维系统

> 把 AWS、DigitalOcean 等异构云平台收进一个聊天窗口：**在手机上用几条指令完成创建、查询、销毁**，
> 不用再记住各家控制台的入口和菜单。

[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![CI](https://github.com/bbemby/cloudops-bot/actions/workflows/ci.yml/badge.svg)](https://github.com/bbemby/cloudops-bot/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-205%20passed-brightgreen)](tests/)
[![Dependencies](https://img.shields.io/badge/deps-requests%20%7C%20aiohttp%20%7C%20cryptography-informational)](requirements.txt)

---

## 这是什么

本项目是论文《基于即时通讯机器人的多云自动化运维系统设计与实现》的工程实现。
它把"ChatOps"落到具体代码上：机器人作为**统一运维入口**，后端适配多家云厂商 API，
让运维人员（尤其是只有手机的深夜值班场景）在聊天窗口里完成日常操作。

解决的问题：

| 痛点 | 本项目的做法 |
| --- | --- |
| 多云控制台碎片化，每家的术语、入口都不同 | 统一指令层：`/create`、`/list`、`/status`、`/delete` 对所有云平台一致 |
| 控制台在手机上难用、加载慢 | 全部操作都在聊天窗口完成，长耗时任务由机器人**主动推送**结果 |
| 误删、误创建代价高 | 危险操作**二次确认**（确认码 + 内联按钮），所有操作写审计日志 |
| API 密钥散落在各台机器的配置文件里 | 凭证统一落库并**加密存储**，聊天里只显示指纹，密钥不回显 |
| 人员权限不清、谁都能改线上 | 管理员 / 普通用户 / 只读 / 未授权四级权限矩阵，越权尝试留痕 |

## 功能一览

- **多云统一接入**：DigitalOcean、AWS EC2，以及用于演示的 Mock 云平台；新增云厂商只需实现一个适配器并注册。
- **资源创建**：`/create do fra1 s-2vcpu-4gb ubuntu name=web count=2` —— 位置参数 + `key=value` 混用，创建后异步等待公网 IP 并推送给你。
- **资源盘点**：`/list` 一次拉取所有已绑定平台的实例；`/status` 看单台详情；`/list_ip` 只看 IP，方便复制。
- **安全销毁**：`/delete` → 二次确认 → 调用销毁接口，终结态实例会被识别并友好提示。
- **凭证管理**：`/bind do main token=xxx` 边绑定边校验，`/creds` 查看（字段名脱敏、密钥不回显），`/switch` 切换工作区，`/unbind` 确认后删除。
- **审计与任务**：`/logs` 看操作日志，`/tasks` 看后台任务，两者都按权限自动过滤范围。
- **可运维性**：`manage.py doctor` 自检配置/数据库/云凭证，`--check` 启动前体检，优雅退出时等待后台任务收尾。
- **中英双语 + 可插拔即时通讯层**：界面文案全部走 i18n 词表；Telegram 只是当前实现，`TelegramTransport` 协议留好了替换接口。

## 架构

```
      Telegram App（手机/桌面）
              │  long polling (aiohttp)
              ▼
   ┌──────────────────────────────────────────────┐
   │ bot/          telegram.py   客户端与传输层协议  │
   │               dispatcher.py 指令路由/权限/限流  │
   │               confirm.py    二次确认状态机      │
   │               jobs.py       后台任务与线程池    │
   │               formatter.py  消息渲染            │
   └───────────────▲──────────────────────────────┘
                   │ Context（reply / log / t）
   ┌───────────────┴──────────────────────────────┐
   │ commands/     basic  credentials  instances   │  ← 业务用例（论文 4.3 的用例）
   │               lifecycle(u)                     │
   └───────────────▲──────────────────────────────┘
                   │ CredentialStore（凭证 → provider 实例）
   ┌───────────────┴──────────────────────────────┐
   │ cloud/        registry.py   适配器注册表      │
   │               base.py       Provider 抽象     │
   │               digitalocean.py  aws_ec2.py     │  ← 论文 4.4 的多云适配
   │               aws_sigv4.py     http.py        │
   │               mock.py       演示用云平台      │
   └───────────────▲──────────────────────────────┘
                   │ HTTPS / JSON / Query(XML)
                   ▼
        DigitalOcean API      AWS EC2 Query API

   贯穿各层：config.py（配置） crypto.py（加密） db.py（SQLite） i18n.py（双语） errors.py（异常→文案）
```

分层原则：**云厂商差异只存在于 `cloud/`，用户界面差异只存在于 `bot/`**，
中间的 `commands/` 只写业务用例，因此新增一家云或换一个 IM 平台都不需要动业务代码。

## 快速开始

```bash
git clone https://github.com/bbemby/cloudops-bot.git
cd cloudops-bot

python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
python manage.py gen-secret        # 生成 SECRET_KEY，写进 .env
#   在 @BotFather 处申请 TELEGRAM_BOT_TOKEN
#   在 @userinfobot 处查询自己的数字 ID，填进 BOT_ADMIN_IDS

python main.py --check             # 体检：配置、数据库
python main.py                     # 启动机器人
```

打开与机器人的私聊（或把它拉进群），发送 `/start` 就能看到指令菜单。

**没有云账号也能跑通全流程**：在 `.env` 里设置 `ENABLE_MOCK_PROVIDER=true`，
`/bind mock demo token=any` 之后，`/create mock`、`/list mock`、`/delete` 全部可用，
数据落在 `./data/mock-cloud.json` 里，方便演示与验收测试。

## 指令速查

| 指令 | 权限 | 说明 |
| --- | --- | --- |
| `/start` | 所有人 | 欢迎信息与上手示例 |
| `/help` | 所有人 | 按权限显示可用指令 |
| `/ping` | 所有人 | 存活探测（含运行时长） |
| `/whoami` | 所有人 | 查看自己的身份、权限与已绑定凭证概览 |
| `/providers [平台]` | 所有人 | 支持的云平台；带参数时显示该平台的字段要求与默认配置 |
| `/list [平台]` | 所有人 | 列举所有已绑定平台的实例（可指定单个平台） |
| `/list_ip [平台]` | 所有人 | 只列名称与公网 IP，便于复制 |
| `/status <平台> <实例ID或IP>` | 所有人 | 单台实例详情 |
| `/logs [条数]` | 所有人 | 最近操作日志（普通用户只看自己的） |
| `/tasks` | 所有人 | 后台任务状态 |
| `/credentials`（别名 `/creds`） | 所有人 | 查看已绑定凭证（密钥不回显） |
| `/bind` | 管理员 | 绑定云平台凭证，先校验再落库 |
| `/switch` | 管理员 | 切换当前生效的凭证工作区 |
| `/unbind` | 管理员 | 删除凭证（二次确认） |
| `/create` | 管理员 | 创建实例（后台任务，就绪后推送公网 IP） |
| `/delete` | 管理员 | 销毁实例（二次确认） |
| `/confirm` `/cancel` | 管理员 | 确认 / 放弃待执行的危险操作 |

常见写法：

```text
/bind aws prod ak=AKIAxxx sk=xxxxx region=ap-northeast-1
/create sgp1 1gb                                   # 用默认平台的默认规格
/create do fra1 s-2vcpu-4gb ubuntu name=web count=2
/create aws us-east-1 t3.micro al2023 tags=prod
/delete 203.0.113.5                                # 需要确认码
/status do 203.0.113.5
```

## 配置

所有配置项集中在 `.env`（模板见 [`.env.example`](.env.example)，逐项带中文注释），
代码中**不出现任何真实密钥**，仓库可直接开源。

必填三项：

| 变量 | 说明 |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | @BotFather 申请 |
| `BOT_ADMIN_IDS` | 管理员 Telegram 数字 ID，逗号分隔 |
| `SECRET_KEY` | `python manage.py gen-secret` 生成，用于加密落库凭证 |

其余按需：语言/时区、限流、创建超时、各云平台默认区域与规格、代理、
以及可选的"开箱即用凭证预置"（`DIGITALOCEAN_TOKEN` / `AWS_ACCESS_KEY_ID` 等）。

> **权限矩阵的取值来源**：白名单来自 `.env`（`BOT_ADMIN_IDS` / `BOT_READONLY_IDS`），
> 用户角色记录在数据库里，可用 `python manage.py users|promote|demote|revoke` 维护。

## 安全设计

1. **凭证加密**：云厂商密钥用 Fernet 加密后入库，密钥由 `SECRET_KEY` 经 HKDF 派生；
   聊天界面只显示 `#id`、平台、标签与字段名，Token 永不回显。
2. **二次确认**：销毁、删除凭证等不可逆操作必须用确认码或内联按钮确认，
   确认码短时效（默认 300 秒）、一次性、绑定发起人。
3. **权限与审计**：四级角色；越权尝试（含未知指令）写入 `denied` 日志，可选私聊通知管理员。
4. **限流**：按用户滑动窗口限流，避免误触或脚本狂刷把云平台配额打满。
5. **数据最小化**：只保存运维必需的数据（实例 ID/名称/IP、操作日志），不拉取与运维无关的资源。

发现安全问题请私下联系维护者，不要在公开 issue 里贴 Token。

## 开发与测试

```bash
python -m unittest discover -s tests -v      # 205 个用例，约 9 秒，零额外依赖
```

CI 会在 Python 3.11 / 3.12 / 3.13 上跑全部用例，外加两段启动冒烟
（用假 Token 验证"配置齐全 → 建库 → 退出码 0"，以及"启动失败只给人话、不留连接泄漏"），
并用 `ruff check` 做静态检查。

测试覆盖：配置解析、加密与密钥派生、数据库迁移与凭证生命周期、
AWS SigV4 签名（对照 AWS 官方已知答案）、各云适配器（含 XML 命名空间这类真实坑）、
指令路由与权限矩阵、确认流、i18n 词表一致性、以及端到端的"绑定 → 创建 → 查询 → 确认销毁"闭环。

```bash
python manage.py doctor --check-cloud   # 对已绑定凭证做一次真实 API 调用
```

目录结构：

```
cloudops-bot/
├── main.py               # 启动入口（--check 体检 / --version）
├── manage.py             # 运维工具（gen-secret/doctor/users/promote/…）
├── cloudops/
│   ├── app.py            # 应用装配与长轮询主循环
│   ├── bot/              # Telegram 客户端、路由、限流、确认、格式化
│   ├── commands/         # 业务用例
│   ├── cloud/            # 云厂商适配器（含 SigV4、Mock）
│   ├── config.py crypto.py db.py i18n.py errors.py utils.py
│   └── ...
├── tests/                # 单元 + 集成测试
├── docs/DEPLOYMENT.md    # 部署（systemd / Docker / 反向代理）
└── .env.example
```

## 部署

`.env` 填好后：

```bash
# systemd（推荐，见 docs/DEPLOYMENT.md）
sudo cp deploy/cloudops-bot.service /etc/systemd/system/ && sudo systemctl enable --now cloudops-bot

# 或 Docker：用 CI 构建好的镜像
cp .env.example .env && python manage.py gen-secret   # 填好 Token / 管理员 ID / SECRET_KEY
mkdir -p data && sudo chown -R 10001:10001 data       # 容器内以 uid 10001 运行
docker compose pull && docker compose up -d           # 或 docker compose up -d --build 本地构建
docker compose logs -f
```

镜像同时发布在 `ghcr.io/bbemby/cloudops-bot:latest`，每次推送到 `main`
都会重新构建并跑一遍容器级冒烟测试（`docker compose up` → 处理指令 → 写库 →
`docker stop` 优雅退出 → 校验数据卷），通过后才推送，见 `.github/workflows/docker.yml`。

注意：**同一个 Bot Token 只能有一个进程在长轮询**，多开会看到 409 冲突；
系统检测到 409 会打印明确提示并退避重试。

> 换过 `SECRET_KEY` 又想复用旧的 `data/` 卷时，启动会直接失败并告诉你两种修复方式
> ——不会等你调用云凭证时才报"解密失败"。

## 路线图

- [ ] 更多云平台适配器：阿里云 ECS、腾讯云 CVM、Oracle Cloud（论文展望的"异构多云"延伸）
- [ ] 接入大模型 Agent：自然语言 → 指令（"帮我在新加坡开一台 2 核 4G 的机器"）
- [ ] 对象存储与数据调度：通过 Rclone 统一管理多云存储桶，支持跨云同步与备份（论文展望）
- [ ] 成本看板：按平台/项目汇总实例开销，超预算提醒
- [ ] 定时快照与自动回收：按标签 TTL 释放临时环境

## 许可证

[MIT](LICENSE)。

---

## English overview

**CloudOps Bot** is a ChatOps control plane for multi-cloud operations. It turns a Telegram
chat into a unified console for provisioning, inspecting and destroying compute instances
across providers (currently DigitalOcean and AWS EC2), with:

- a provider adapter layer (`cloud/`) that isolates every API difference,
- encrypted-at-rest credentials (Fernet + HKDF) that are never echoed back into chat,
- two-step confirmation and an audit log for every irreversible action,
- a four-level role matrix (admin / user / readonly / guest) enforced per command,
- background jobs that push results (e.g. the public IP) back to the chat when ready,
- bilingual messages (zh/en) and a pluggable IM transport.

It is the reference implementation of the thesis *Design and Implementation of a
Multi-Cloud Automated Operations System Based on Instant Messaging Bots*.
See [`.env.example`](.env.example) for configuration and run
`python -m unittest discover -s tests` for the test suite.

Licensed under the MIT License.
