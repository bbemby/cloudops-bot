"""消息文本与 i18n（zh 为默认语言，en 供开源社区使用）。

约定：

* 所有面向用户的文案都集中在这里，处理器只引用 key；
* ``tests/test_i18n.py`` 会扫描源码里的 ``t("…")`` 调用，确保每个 key
  在 zh / en 两个字典中都存在，避免出现"某个语言的文案漏翻"。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

DEFAULT_LANGUAGE = "zh"
SUPPORTED_LANGUAGES = ("zh", "en")

MESSAGES: Dict[str, Dict[str, str]] = {
    # ------------------------------------------------------------------ #
    # 通用
    # ------------------------------------------------------------------ #
    "zh": {
        "app.banner": "🛰️ CloudOps Bot v{version} 已启动 | @{bot} | provider={providers}",
        "app.stopping": "收到退出信号，正在优雅停止（等待后台任务收尾）…",
        "app.stopped": "已停止。",

        "help.title": "🤖 CloudOps Bot —— 多云运维指令中心",
        "help.intro": "在聊天框里发送指令即可完成跨云资源调度，无需登录任何 Web 控制台。",
        "help.group.read": "📖 查询类",
        "help.group.write": "🛠️ 操作类（管理员）",
        "help.group.cred": "🔑 凭证管理（管理员）",
        "help.group.other": "⚙️ 其它",
        "help.footer": "示例：/list_ip  |  /create sgp1 1gb  |  /delete 203.0.113.5",
        "help.role_hint": "当前身份：{role}（ID {user_id}）",
        "help.no_commands": "（当前身份没有可用指令）",
        "help.unknown_command": "未识别指令 `{command}`。{hint}",
        "help.unknown_hint": "发送 /help 查看全部指令。",
        "help.did_you_mean": "你是不是想输入 /{suggestion}？",

        "ping.pong": "🏓 pong\n运行时长：{uptime}\n版本：{version}\n数据库：{db}\n活动任务：{tasks}",
        "whoami.title": "👤 身份信息",
        "whoami.line_id": "Telegram ID : {user_id}",
        "whoami.line_username": "用户名      : {username}",
        "whoami.line_role": "权限等级    : {role}",
        "whoami.line_since": "首次登记    : {created}",
        "whoami.line_creds": "可用凭证    : {credentials}",
        "whoami.creds_none": "（尚未绑定任何云平台凭证）",

        # ------------------------------------------------------------------ #
        # 权限 / 速率
        # ------------------------------------------------------------------ #
        "denied.permission": "⛔ 无权限操作。\n你的身份是 {role}，该指令仅限管理员使用。\n如需授权，请让管理员执行：python manage.py promote {user_id} admin",
        "denied.guest": "⛔ 无权限操作。\n你的 Telegram ID {user_id} 尚未获得授权，已记录本次尝试。",
        "denied.rate": "⏳ 操作过于频繁，请 {seconds} 秒后再试。",
        "pending.hint": "⏳ 上一条确认还在等你处理。\n回复 /confirm <确认码> 执行，或 /cancel <确认码> 放弃。",

        # ------------------------------------------------------------------ #
        # 凭证
        # ------------------------------------------------------------------ #
        "bind.usage": "用法：\n/bind <provider> <凭证名称> <字段>=<值> [字段=值 …]\n\n示例：\n/bind do main token=dop_v1_xxx\n/bind aws prod ak=AKIAxxx sk=xxxxx region=ap-northeast-1\n\n发送 /providers <provider> 查看各平台需要的字段。",
        "bind.unknown_provider": "❌ 不支持的云平台：{value}\n可选：{options}",
        "bind.validating": "🔍 正在校验 {provider} 凭证…",
        "bind.success": "✅ 凭证已绑定并激活\n\n凭证 ID  : {credential_id}\n云平台   : {provider}\n名称     : {label}\n账户标识 : {identity}\n\n当前生效配置：{summary}",
        "bind.failed": "❌ 凭证校验失败：\n{detail}",
        "bind.missing_fields": "❌ 缺少字段：{fields}\n\n需要提供：\n{usage}",
        "creds.title": "🔑 已绑定的云平台凭证",
        "creds.none": "尚未绑定任何凭证。\n\n{usage}",
        "creds.row": "{marker} [{credential_id}] {provider}:{label}  所有者 {owner} · 创建于 {created}\n    {fields}",
        "creds.active_marker": "▶",
        "creds.inactive_marker": " ",
        "creds.footer": "共 {count} 条。使用 /switch <凭证ID> 切换当前工作区，/unbind <凭证ID> 删除。",
        "switch.usage": "用法：/switch <凭证ID>（先发送 /creds 查看 ID）",
        "unbind.usage": "用法：/unbind <凭证ID>（先发送 /creds 查看 ID）",
        "switch.done": "✅ 已切换当前工作区：{provider}:{label}（凭证 #{credential_id}）",
        "unbind.pending": "⚠️ 即将删除凭证 #{credential_id}（{provider}:{label}），删除后需重新绑定。",
        "unbind.done": "🗑️ 凭证 #{credential_id}（{provider}:{label}）已删除。",
        "providers.title": "☁️ 支持的云平台",
        "providers.row": "{marker} {name:<12} {display}",
        "providers.bound": "✅",
        "providers.unbound": "▫️",
        "providers.hint": "发送 /providers <provider> 查看字段说明与默认参数。",

        # ------------------------------------------------------------------ #
        # 查询实例
        # ------------------------------------------------------------------ #
        "list.header": "🌐 {scope} 实例总览（{count} 台）",
        "list.row": "{index}. {emoji} {name}  [{provider}]\n   状态 {status} · 区域 {region} · 规格 {size}\n   公网 {public_ip}{extra}",
        "list.extra_password": " · 初始密码 {password}",
        "list.empty": "📭 {scope} 下没有任何实例。",
        "list.summary": "共 {count} 台 · 已运行 {running} 台 · 耗时 {elapsed}",
        "list.provider_error": "⚠️ {provider} 查询失败：{detail}",
        "list.warning": "⚠️ {warning}",
        "status.usage": "用法：/status <provider> <实例ID或IP>   例如 /status do 203.0.113.5",
        "status.title": "🔎 实例详情（{provider}）",
        "status.line": "{key:<10}: {value}",

        # ------------------------------------------------------------------ #
        # 创建 / 销毁
        # ------------------------------------------------------------------ #
        "create.usage": "用法：\n/create [provider] [region] [size] [image] [key=value …]\n\n示例：\n/create sgp1 1gb                       # 使用默认云平台\n/create do fra1 s-2vcpu-4gb ubuntu     # 指定 DigitalOcean\n/create aws us-east-1 t3.micro al2023  # 指定 AWS EC2\n/create do name=web tags=prod count=2\n\n可选参数：name / region / size / image / count / tags / ssh_keys / key / user_data",
        "create.invalid_option": "❌ 参数 `{option}` 不被识别（{provider} 支持：{options}）",
        "create.invalid_count": "❌ 数量不合法：`{value}`（应为 1 及以上的整数）",
        "create.plan": "📦 即将在 {provider} 创建 {count} 台实例\n\n{summary}\n\n凭证：{credential}\n任务已下发，完成后会自动把公网 IP 推送到这里。",
        "create.progress": "⏳ 任务 {task_id} 进度：{detail}（已等待 {elapsed}）",
        "create.done": "✅ 实例创建完成\n\n{details}",
        "create.done_item": "• {name}  [{provider}]\n  实例 ID : {instance_id}\n  公网 IP : {public_ip}\n  区域    : {region} · 规格 {size} · 镜像 {image}{password}",
        "create.failed": "❌ 创建失败（任务 {task_id}）：\n{detail}",
        "create.timeout": "⌛ 等待超时：{detail}\n实例可能仍在后台创建，可稍后用 /list_ip 复查。",
        "create.limit": "❌ 单次最多创建 {limit} 台。",
        "delete.usage": "用法：/delete <实例ID或IP> [provider]\n例如：/delete 203.0.113.5\n\n为避免误删，删除前需要二次确认。",
        "delete.not_found": "❌ 未找到匹配的实例：{target}\n可先用 /list_ip 确认目标。",
        "delete.ambiguous": "⚠️ 目标 {target} 在多个云平台都存在，请显式指定 provider：\n{options}",
        "delete.terminated": "ℹ️ 实例 {instance_id} 已是 terminated 状态，无需再次销毁。",
        "delete.pending": "⚠️ 即将销毁实例，请确认：\n\n云平台 : {provider}\n实例   : {name}\n实例 ID: {instance_id}\n公网 IP: {public_ip}\n区域   : {region}\n\n确认码 {code}（{ttl} 秒内有效）\n回复 /confirm {code} 执行，或点击下方按钮。",
        "delete.done": "🗑️ 已提交销毁：{provider} {name}（{instance_id}）\n云端处理通常需要几十秒，可用 /list_ip 复查。",
        "delete.failed": "❌ 销毁失败：{detail}",

        # ------------------------------------------------------------------ #
        # 确认流
        # ------------------------------------------------------------------ #
        "confirm.required": "⚠️ 该操作属于不可逆变更，需要二次确认。\n确认码：{code}（{ttl} 秒内有效）\n回复 /confirm {code} 继续。",
        "confirm.invalid": "❌ 确认码无效或已过期，请重新发起指令。",
        "confirm.usage": "用法：/confirm <确认码>",
        "confirm.expired": "⌛ 确认码 {code} 已过期，请重新发起指令。",
        "confirm.cancelled": "✅ 已取消：{action}",
        "confirm.done": "▶️ 已确认并执行：{action}",
        "confirm.button.confirm": "✅ 确认执行",
        "confirm.button.cancel": "✖️ 取消",

        # ------------------------------------------------------------------ #
        # 日志 / 任务
        # ------------------------------------------------------------------ #
        "logs.title": "📜 最近 {count} 条操作日志",
        "logs.row": "{time} [{status}] {action} {target}  ({user})",
        "logs.empty": "暂无操作日志。",
        "logs.usage": "用法：/logs [条数]（默认 10，最多 50）",
        "tasks.title": "🧵 最近的任务",
        "tasks.row": "{time} [{status}] {kind} #{task_id} {ref}  {detail}",
        "tasks.empty": "暂无后台任务。",

        # ------------------------------------------------------------------ #
        # 错误
        # ------------------------------------------------------------------ #
        "error.internal": "💥 内部错误：{detail}\n已记录日志，可发送 /logs 查看最近操作。",
        "error.internal_plain": "💥 内部错误，请联系管理员查看日志。",
        "error.config": "⚙️ 配置错误：{detail}",
        "error.bad_arguments": "❌ 参数有误：{detail}",
        "error.permission_denied": "⛔ 无权限操作。",
        "error.confirmation_required": "⚠️ 该操作需要二次确认：{action}\n确认码 {code}（{ttl} 秒内有效）",
        "error.rate_limited": "⏳ 操作过于频繁，请 {seconds} 秒后再试。",
        "error.not_found": "❌ 未找到目标：{target}",
        "error.credential": "🔑 凭证错误：{detail}",
        "error.cloud": "☁️ 云端调用失败：{detail}",
        "error.cloud_timeout": "⌛ 等待 {provider} 实例 {id} 就绪超时（{seconds} 秒，最后状态 {status}）。",
        "error.instance_failed": "❌ {provider} 实例 {id} 状态异常：{status}",
        "error.instance_not_found": "❌ {provider} 中不存在实例：{target}",
        "error.missing_secret_key": "缺少 SECRET_KEY，请执行 `python manage.py gen-secret` 并写入 .env",
        "error.invalid_secret_key": "SECRET_KEY 不是合法的 Fernet key（应为 44 字符 base64）",
        "error.decrypt_failed": "凭证解密失败：SECRET_KEY 可能已变更，请重新绑定云平台凭证",
        "error.missing_credential_fields": "❌ {provider} 缺少凭证字段：{fields}\n\n{usage}",
        "error.credential_missing": "❌ 尚未为 {provider} 绑定凭证。\n\n{usage}",
        "error.credential_not_found": "❌ 凭证不存在：{target}",
        "error.credential_label_not_found": "❌ 未找到 {provider} 下名为 {label} 的凭证。",
        "error.invalid_credential": "❌ {provider} 凭证无效：{detail}",
        "error.duplicate_credential": "❌ {provider} 下已存在同名凭证 {label}，请换一个名称或先删除旧凭证。",
        "error.invalid_label": "❌ 凭证名称不合法（1~{limit} 个字符，不可含空格）。",
        "error.unexpected_response": "☁️ {provider} 返回了非预期响应：{detail}",
        "error.all_regions_failed": "☁️ {provider} 所有区域均调用失败：{detail}",
        "error.unknown_option": "❌ {parameter} 取值 {value} 不合法。\n可选：{options}",
        "error.ambiguous_option": "❌ {parameter} 取值 {value} 不够明确，候选：{options}",
        "error.ami_lookup_failed": "❌ 无法解析 AMI（{value}）。请在 .env 中设置 AWS_DEFAULT_AMI={options}",
        "error.mock_disabled": "❌ Mock Cloud 未启用。若只想演示，请在 .env 中设置 ENABLE_MOCK_PROVIDER=true。",
        "error.user_data_too_large": "❌ user_data 超过 {limit} 限制。",
        "error.telegram": "📡 与 Telegram 通信失败：{detail}",
        "error.job_cancelled": "任务已取消。",
        "error.generic": "❌ 操作失败：{detail}",
    },
    "en": {
        "app.banner": "🛰️ CloudOps Bot v{version} started | @{bot} | providers={providers}",
        "app.stopping": "Shutdown signal received, draining background jobs…",
        "app.stopped": "Stopped.",

        "help.title": "🤖 CloudOps Bot — multi-cloud ops command center",
        "help.intro": "Send commands in chat to drive multi-cloud resources without opening any web console.",
        "help.group.read": "📖 Read-only",
        "help.group.write": "🛠️ Write (admin)",
        "help.group.cred": "🔑 Credentials (admin)",
        "help.group.other": "⚙️ Other",
        "help.footer": "Examples: /list_ip  |  /create sgp1 1gb  |  /delete 203.0.113.5",
        "help.role_hint": "Role: {role} (ID {user_id})",
        "help.no_commands": "(no commands available for your role)",
        "help.unknown_command": "Unknown command `{command}`. {hint}",
        "help.unknown_hint": "Send /help to see all commands.",
        "help.did_you_mean": "Did you mean /{suggestion}?",

        "ping.pong": "🏓 pong\nuptime: {uptime}\nversion: {version}\ndatabase: {db}\nactive jobs: {tasks}",
        "whoami.title": "👤 Identity",
        "whoami.line_id": "Telegram ID : {user_id}",
        "whoami.line_username": "Username    : {username}",
        "whoami.line_role": "Role        : {role}",
        "whoami.line_since": "Registered  : {created}",
        "whoami.line_creds": "Credentials : {credentials}",
        "whoami.creds_none": "(no cloud credentials bound yet)",

        "denied.permission": "⛔ Permission denied.\nYour role is {role}; this command is admin-only.\nAsk your admin to run: python manage.py promote {user_id} admin",
        "denied.guest": "⛔ Permission denied.\nTelegram ID {user_id} is not authorized. This attempt has been logged.",
        "denied.rate": "⏳ Too many requests, try again in {seconds}s.",
        "pending.hint": "⏳ A previous confirmation is still waiting.\nReply /confirm <code> to proceed or /cancel <code> to drop it.",

        "bind.usage": "Usage:\n/bind <provider> <label> <field>=<value> [field=value …]\n\nExamples:\n/bind do main token=dop_v1_xxx\n/bind aws prod ak=AKIAxxx sk=xxxxx region=ap-northeast-1\n\nSend /providers <provider> for the required fields.",
        "bind.unknown_provider": "❌ Unsupported provider: {value}\nAvailable: {options}",
        "bind.validating": "🔍 Validating {provider} credentials…",
        "bind.success": "✅ Credential bound and activated\n\nID       : {credential_id}\nProvider : {provider}\nLabel    : {label}\nIdentity : {identity}\n\nEffective config: {summary}",
        "bind.failed": "❌ Credential validation failed:\n{detail}",
        "bind.missing_fields": "❌ Missing fields: {fields}\n\nRequired:\n{usage}",
        "creds.title": "🔑 Bound cloud credentials",
        "creds.none": "No credentials bound yet.\n\n{usage}",
        "creds.row": "{marker} [{credential_id}] {provider}:{label}  owner {owner} · created {created}\n    {fields}",
        "creds.active_marker": "▶",
        "creds.inactive_marker": " ",
        "creds.footer": "{count} total. Use /switch <id> to change the active workspace, /unbind <id> to delete.",
        "switch.usage": "Usage: /switch <credential id> (send /creds to list ids)",
        "unbind.usage": "Usage: /unbind <credential id> (send /creds to list ids)",
        "switch.done": "✅ Active workspace switched to {provider}:{label} (credential #{credential_id})",
        "unbind.pending": "⚠️ About to delete credential #{credential_id} ({provider}:{label}); you will have to bind it again.",
        "unbind.done": "🗑️ Credential #{credential_id} ({provider}:{label}) deleted.",
        "providers.title": "☁️ Supported cloud providers",
        "providers.row": "{marker} {name:<12} {display}",
        "providers.bound": "✅",
        "providers.unbound": "▫️",
        "providers.hint": "Send /providers <provider> for field details and defaults.",

        "list.header": "🌐 {scope} instances ({count})",
        "list.row": "{index}. {emoji} {name}  [{provider}]\n   status {status} · region {region} · size {size}\n   public {public_ip}{extra}",
        "list.extra_password": " · initial password {password}",
        "list.empty": "📭 No instances under {scope}.",
        "list.summary": "{count} total · {running} running · took {elapsed}",
        "list.provider_error": "⚠️ {provider} query failed: {detail}",
        "list.warning": "⚠️ {warning}",
        "status.usage": "Usage: /status <provider> <instance id or IP>   e.g. /status do 203.0.113.5",
        "status.title": "🔎 Instance detail ({provider})",
        "status.line": "{key:<10}: {value}",

        "create.usage": "Usage:\n/create [provider] [region] [size] [image] [key=value …]\n\nExamples:\n/create sgp1 1gb                       # default provider\n/create do fra1 s-2vcpu-4gb ubuntu\n/create aws us-east-1 t3.micro al2023\n/create do name=web tags=prod count=2\n\nOptions: name / region / size / image / count / tags / ssh_keys / key / user_data",
        "create.invalid_option": "❌ Unrecognised option `{option}` (supported by {provider}: {options})",
        "create.invalid_count": "❌ Invalid count: `{value}` (must be an integer >= 1)",
        "create.plan": "📦 Creating {count} instance(s) on {provider}\n\n{summary}\n\nCredential: {credential}\nTask accepted — the public IP will be pushed here when ready.",
        "create.progress": "⏳ Task {task_id}: {detail} (waited {elapsed})",
        "create.done": "✅ Instance creation finished\n\n{details}",
        "create.done_item": "• {name}  [{provider}]\n  instance id : {instance_id}\n  public ip   : {public_ip}\n  region      : {region} · size {size} · image {image}{password}",
        "create.failed": "❌ Creation failed (task {task_id}):\n{detail}",
        "create.timeout": "⌛ Timed out: {detail}\nThe instance may still be provisioning; check again with /list_ip.",
        "create.limit": "❌ At most {limit} instances per request.",
        "delete.usage": "Usage: /delete <instance id or IP> [provider]\ne.g. /delete 203.0.113.5\n\nA confirmation step is required to avoid accidents.",
        "delete.not_found": "❌ No matching instance for {target}\nUse /list_ip to double-check.",
        "delete.ambiguous": "⚠️ {target} exists on multiple providers, please specify one:\n{options}",
        "delete.terminated": "ℹ️ Instance {instance_id} is already terminated; nothing to do.",
        "delete.pending": "⚠️ About to terminate, please confirm:\n\nprovider : {provider}\ninstance : {name}\nid       : {instance_id}\npublic ip: {public_ip}\nregion   : {region}\n\ncode {code} (valid {ttl}s)\nReply /confirm {code} or use the buttons below.",
        "delete.done": "🗑️ Termination submitted: {provider} {name} ({instance_id})\nIt usually takes tens of seconds; verify with /list_ip.",
        "delete.failed": "❌ Termination failed: {detail}",

        "confirm.required": "⚠️ This action is irreversible and needs confirmation.\nCode: {code} (valid {ttl}s)\nReply /confirm {code} to proceed.",
        "confirm.invalid": "❌ Invalid or expired confirmation code, please start over.",
        "confirm.usage": "Usage: /confirm <code>",
        "confirm.expired": "⌛ Code {code} expired, please start over.",
        "confirm.cancelled": "✅ Cancelled: {action}",
        "confirm.done": "▶️ Confirmed and executing: {action}",
        "confirm.button.confirm": "✅ Confirm",
        "confirm.button.cancel": "✖️ Cancel",

        "logs.title": "📜 Last {count} operations",
        "logs.row": "{time} [{status}] {action} {target}  ({user})",
        "logs.empty": "No operation logs yet.",
        "logs.usage": "Usage: /logs [count] (default 10, max 50)",
        "tasks.title": "🧵 Recent tasks",
        "tasks.row": "{time} [{status}] {kind} #{task_id} {ref}  {detail}",
        "tasks.empty": "No background tasks yet.",

        "error.internal": "💥 Internal error: {detail}\nLogged for review; try /logs for recent operations.",
        "error.internal_plain": "💥 Internal error, check the logs.",
        "error.config": "⚙️ Configuration error: {detail}",
        "error.bad_arguments": "❌ Bad arguments: {detail}",
        "error.permission_denied": "⛔ Permission denied.",
        "error.confirmation_required": "⚠️ This action needs confirmation: {action}\nCode {code} (valid {ttl}s)",
        "error.rate_limited": "⏳ Too many requests, retry in {seconds}s.",
        "error.not_found": "❌ Not found: {target}",
        "error.credential": "🔑 Credential error: {detail}",
        "error.cloud": "☁️ Cloud API failure: {detail}",
        "error.cloud_timeout": "⌛ Timed out waiting for {provider} instance {id} ({seconds}s, last status {status}).",
        "error.instance_failed": "❌ {provider} instance {id} is in a bad state: {status}",
        "error.instance_not_found": "❌ No such instance in {provider}: {target}",
        "error.missing_secret_key": "SECRET_KEY missing; run `python manage.py gen-secret` and put it into .env",
        "error.invalid_secret_key": "SECRET_KEY is not a valid Fernet key (44-char base64 expected)",
        "error.decrypt_failed": "Failed to decrypt credential: SECRET_KEY may have changed, please re-bind",
        "error.missing_credential_fields": "❌ {provider} is missing fields: {fields}\n\n{usage}",
        "error.credential_missing": "❌ No credential bound for {provider}.\n\n{usage}",
        "error.credential_not_found": "❌ No such credential: {target}",
        "error.credential_label_not_found": "❌ No credential named {label} under {provider}.",
        "error.invalid_credential": "❌ Invalid {provider} credential: {detail}",
        "error.duplicate_credential": "❌ Credential {label} already exists under {provider}; rename or delete it first.",
        "error.invalid_label": "❌ Invalid credential label (1~{limit} chars, no spaces).",
        "error.unexpected_response": "☁️ Unexpected response from {provider}: {detail}",
        "error.all_regions_failed": "☁️ {provider} failed in every region: {detail}",
        "error.unknown_option": "❌ Invalid {parameter}: {value}\nOptions: {options}",
        "error.ambiguous_option": "❌ {parameter} {value} is ambiguous, candidates: {options}",
        "error.ami_lookup_failed": "❌ Could not resolve AMI ({value}). Set AWS_DEFAULT_AMI={options} in .env",
        "error.mock_disabled": "❌ Mock Cloud is disabled. Set ENABLE_MOCK_PROVIDER=true in .env for demos.",
        "error.user_data_too_large": "❌ user_data exceeds the {limit} limit.",
        "error.telegram": "📡 Telegram API failure: {detail}",
        "error.job_cancelled": "Task cancelled.",
        "error.generic": "❌ Operation failed: {detail}",
    },
}


class Translator:
    """极简翻译器：``t("list.header", count=3)``。

    注意第一个参数刻意命名为 ``_key``：文案里允许出现名为 ``key`` 的占位符
    （如 ``"status.line": "{key:<10}: {value}"``），若把参数命名为 ``key``
    就会与 ``t("status.line", key="region", …)`` 这种调用冲突。
    """

    def __init__(self, language: str = DEFAULT_LANGUAGE) -> None:
        self.language = language if language in SUPPORTED_LANGUAGES else DEFAULT_LANGUAGE

    def __call__(self, _key: str = "", **params: Any) -> str:
        key = _key or str(params.pop("key", ""))
        template = MESSAGES.get(self.language, {}).get(key)
        if template is None:
            template = MESSAGES[DEFAULT_LANGUAGE].get(key)
        if template is None:
            return key
        if not params:
            return template
        try:
            return template.format(**params)
        except (KeyError, IndexError, ValueError):  # pragma: no cover - 文案占位符缺失
            return template


_TRANSLATORS: Dict[str, Translator] = {}


def get_translator(language: Optional[str] = None) -> Translator:
    key = language or DEFAULT_LANGUAGE
    if key not in _TRANSLATORS:
        _TRANSLATORS[key] = Translator(key)
    return _TRANSLATORS[key]


def keys() -> Dict[str, set]:
    """返回每个语言已有的 key 集合（供一致性测试使用）。"""
    return {lang: set(messages) for lang, messages in MESSAGES.items()}
