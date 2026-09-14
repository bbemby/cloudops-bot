"""Web 管理面板的 HTTP 层。

用 aiohttp（已是 Bot 长轮询的依赖）与 Bot 跑在同一个事件循环里，因此面板与
Bot 共享内存中的凭证缓存、任务管理器，不需要额外的进程或消息队列。

安全上的几条底线：

* 除 ``/login`` 与 ``/healthz`` 外，所有路径都要求有效会话；
* 所有写操作（销毁实例、增删凭证、改角色）都要求 CSRF token，且受
  ``WEB_READONLY`` 全局只读开关约束；
* 响应统一带 ``X-Content-Type-Options`` / ``X-Frame-Options`` / ``Referrer-Policy``
  与一条 ``script-src 'self'`` 的 CSP（前端没有内联脚本，因此不需要放宽）；
* 面板不返回任何密钥明文，凭证一律脱敏后再出后端。
"""

from __future__ import annotations

import json
import logging
from html import escape
from pathlib import Path
from typing import Any, Dict, Optional

from aiohttp import web

from .. import __version__
from ..errors import CloudError, CloudOpsError, ConfigError, NotFound, ValidationError
from .auth import (
    CSRF_FIELD,
    CSRF_HEADER,
    LoginThrottle,
    Session,
    SessionManager,
    generate_password,
    hash_password,
    verify_password,
)
from .service import PanelService

logger = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = PACKAGE_DIR / "templates"
STATIC_DIR = PACKAGE_DIR / "static"

#: 允许直接取用的静态资源（白名单，避免路径穿越）
STATIC_FILES = ("app.css", "app.js")

#: 无需登录即可访问的路径
PUBLIC_PATHS = frozenset({"/login", "/healthz", "/favicon.ico"})

#: 面板响应统一附带的响应头
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    ),
    "Cache-Control": "no-store",
}

#: 异常类型 → HTTP 状态码
ERROR_STATUS = {
    ValidationError: 400,
    NotFound: 404,
    CloudError: 502,
}


class WebServer:
    """面板服务器：随 :class:`cloudops.app.Application` 一起起停。"""

    def __init__(self, settings, db, store, jobs) -> None:
        self.settings = settings
        self.web_config = settings.web
        self.service = PanelService(settings, db, store, jobs)
        self.sessions = SessionManager(
            settings.secret_key,
            ttl=self.web_config.session_ttl_seconds,
            secure=self.web_config.secure_cookie,
        )
        self.throttle = LoginThrottle(max_attempts=self.web_config.max_login_attempts,
                                      window=float(self.web_config.login_window_seconds))
        self.password_hash = ""
        self.generated_password: Optional[str] = None
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self.app = self._build_app()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    async def start(self) -> bool:
        """启动监听。返回是否真的起了（``WEB_ENABLED=false`` 时返回 False）。"""
        if not self.web_config.enabled:
            logger.info("Web 管理面板已按配置关闭（WEB_ENABLED=false）")
            return False
        self.prepare_password()
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.web_config.host, self.web_config.port)
        try:
            await self._site.start()
        except OSError as exc:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            raise ConfigError(
                f"Web 管理面板无法监听 {self.web_config.host}:{self.web_config.port}（{exc}）。"
                "若端口被占用，请改 WEB_PORT，或设 WEB_ENABLED=false 关闭面板。",
                key="error.web_bind_failed",
                params={"host": self.web_config.host, "port": self.web_config.port},
            ) from exc
        logger.info("Web 管理面板已监听 http://%s:%s（只读模式=%s）",
                    self.web_config.host, self.web_config.port,
                    "是" if self.web_config.readonly else "否")
        return True

    def prepare_password(self) -> None:
        """确定登录口令。未配置时生成随机口令并打到日志，绝不出现"无口令面板"。"""
        if self.password_hash:
            return
        if self.web_config.admin_password:
            self.password_hash = hash_password(self.web_config.admin_password)
            return
        generated = generate_password()
        self.generated_password = generated
        self.password_hash = hash_password(generated)
        logger.warning("=" * 68)
        logger.warning("Web 管理面板本次启动口令：%s", generated)
        logger.warning("（未设置 WEB_ADMIN_PASSWORD 时每次启动都会重新生成；"
                       "在 .env 里写死可固定口令）")
        logger.warning("面板地址：http://<主机>:%s/", self.web_config.port)
        logger.warning("=" * 68)

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            logger.info("Web 管理面板已停止。")

    @property
    def running(self) -> bool:
        return self._site is not None

    @property
    def port_in_use(self) -> int:
        """实际监听的端口（``WEB_PORT=0`` 时由内核分配，日志里要打真实值）。"""
        for _, port in getattr(self._runner, "addresses", ()) or ():
            return int(port)
        return self.web_config.port

    # ------------------------------------------------------------------ #
    # 路由装配
    # ------------------------------------------------------------------ #
    def _build_app(self) -> web.Application:
        app = web.Application(middlewares=[
            self._error_middleware, self._headers_middleware, self._auth_middleware,
        ])
        app.router.add_get("/", self.handle_index)
        app.router.add_get("/login", self.handle_login_page)
        app.router.add_post("/login", self.handle_login)
        app.router.add_post("/logout", self.handle_logout)
        app.router.add_get("/healthz", self.handle_health)
        app.router.add_get("/favicon.ico", self.handle_favicon)
        app.router.add_get("/api/bootstrap", self.handle_bootstrap)
        app.router.add_get("/api/overview", self.handle_overview)
        app.router.add_get("/api/instances", self.handle_instances)
        app.router.add_post("/api/instances/{provider}/{instance_id}/destroy",
                            self.handle_destroy)
        app.router.add_get("/api/credentials", self.handle_credentials)
        app.router.add_post("/api/credentials", self.handle_add_credential)
        app.router.add_post("/api/credentials/{credential_id}/activate",
                            self.handle_activate_credential)
        app.router.add_delete("/api/credentials/{credential_id}", self.handle_delete_credential)
        app.router.add_get("/api/users", self.handle_users)
        app.router.add_post("/api/users/{telegram_id}/role", self.handle_set_role)
        app.router.add_get("/api/logs", self.handle_logs)
        app.router.add_get("/api/tasks", self.handle_tasks)
        # 注意：这里必须是字面量 "{name}"。之前写成 f"/static/{name}" 会让循环变量
        # 被插值成 /static/app.css 这种固定路径，请求静态资源时 match_info 里取不到
        # name，直接 500。白名单校验放在 handle_static 里做。
        app.router.add_get("/static/{name}", self.handle_static)
        return app

    # ------------------------------------------------------------------ #
    # 中间件
    # ------------------------------------------------------------------ #
    @web.middleware
    async def _error_middleware(self, request: web.Request, handler):
        """业务异常 → JSON 错误；HTTP 异常（401/403/404…）照常交给 aiohttp。"""
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except CloudOpsError as exc:
            response = exception_to_response(self.settings, exc)
            assert response is not None
            logger.warning("面板接口 %s %s 失败：%s", request.method, request.path, exc)
            return response
        except Exception:                        # pragma: no cover - 兜底
            logger.exception("面板接口 %s %s 出现未预期错误", request.method, request.path)
            return self.json_error(self.service.t("error.internal"), 500, key="error.internal")

    @web.middleware
    async def _headers_middleware(self, request: web.Request, handler):
        response = await handler(request)
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response

    @web.middleware
    async def _auth_middleware(self, request: web.Request, handler):
        if request.path in PUBLIC_PATHS or request.path.startswith("/static/"):
            return await handler(request)
        session = self.session_of(request)
        if session is None:
            if request.path.startswith("/api/"):
                return self.json_error("unauthorized", 401, key="web.error.unauthorized")
            raise web.HTTPFound("/login")
        request["session"] = session
        return await handler(request)

    # ------------------------------------------------------------------ #
    # 会话与校验
    # ------------------------------------------------------------------ #
    def session_of(self, request: web.Request) -> Optional[Session]:
        raw = request.cookies.get(self.sessions.cookie_name)
        return self.sessions.verify(raw, self.password_hash)

    def client_key(self, request: web.Request) -> str:
        """登录限速的计数键（默认只信 socket 来源，避免伪造 XFF 绕过限速）。"""
        if self.web_config.trust_proxy:
            forwarded = request.headers.get("X-Forwarded-For", "")
            if forwarded:
                return forwarded.split(",")[0].strip()
        return request.remote or "unknown"

    def check_csrf(self, request: web.Request, session: Session) -> None:
        provided = request.headers.get(CSRF_HEADER) or request.get("csrf_token", "")
        if not provided or provided != session.csrf_token:
            raise web.HTTPForbidden(text="csrf", content_type="text/plain")

    def require_writable(self) -> None:
        """只读模式下拒绝一切写操作。"""
        if self.web_config.readonly:
            raise web.HTTPForbidden(
                text=self.service.t("web.readonly.blocked"), content_type="text/plain")

    # ------------------------------------------------------------------ #
    # 页面与静态资源
    # ------------------------------------------------------------------ #
    def render(self, name: str, **context: Any) -> str:
        html = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
        for key, value in context.items():
            html = html.replace("{{" + key + "}}", str(value))
        return html

    async def handle_index(self, request: web.Request) -> web.Response:
        session: Session = request["session"]
        return web.Response(
            text=self.render(
                "index.html",
                title=self.web_config.title,
                version=__version__,
                csrf=session.csrf_token,
                language=self.settings.language,
            ),
            content_type="text/html", charset="utf-8",
        )

    async def handle_login_page(self, request: web.Request) -> web.Response:
        if self.session_of(request) is not None:
            raise web.HTTPFound("/")
        return self.login_response()

    def login_response(self, *, error: str = "", status: int = 200) -> web.Response:
        """登录页在服务端渲染：未登录时不加载任何脚本，错误也能直接呈现。"""
        t = self.service.t
        error_block = ""
        if error:
            error_block = f'<p class="err">{escape(error)}</p>'
        return web.Response(
            text=self.render(
                "login.html",
                title=escape(self.web_config.title),
                version=__version__,
                language=self.settings.language,
                error_block=error_block,
                heading=escape(t("web.login.title")),
                password_label=escape(t("web.login.password")),
                submit_label=escape(t("web.login.submit")),
                footer=escape(t("web.login.footer", port=self.web_config.port)),
            ),
            content_type="text/html", charset="utf-8", status=status,
        )

    async def handle_login(self, request: web.Request) -> web.Response:
        t = self.service.t
        key = self.client_key(request)
        blocked = self.throttle.blocked(key)
        if blocked > 0:
            logger.warning("面板登录被限速（来源 %s，还需 %.0fs）", key, blocked)
            response = self.login_response(
                error=t("web.login.throttled", seconds=int(blocked) + 1), status=429)
            response.headers["Retry-After"] = str(int(blocked) + 1)
            return response

        password = await self._read_password(request)
        if not verify_password(password, self.password_hash):
            self.throttle.record_failure(key)
            remaining = max(0, self.web_config.max_login_attempts
                            - len(self.throttle._failures.get(key, [])))  # noqa: SLF001
            logger.warning("面板登录失败（来源 %s，剩余尝试 %d）", key, remaining)
            return self.login_response(error=t("web.login.failed"), status=401)

        self.throttle.reset(key)
        session = self.sessions.issue(self.password_hash)
        logger.info("面板登录成功（来源 %s）", key)
        response = web.Response(status=303, headers={"Location": "/"})
        response.headers["Set-Cookie"] = self.sessions.set_cookie_header(session)
        return response

    async def _read_password(self, request: web.Request) -> str:
        content_type = (request.content_type or "").lower()
        try:
            if "json" in content_type:
                payload = await request.json()
                return str((payload or {}).get("password", ""))
            form = await request.post()
            return str(form.get("password", ""))
        except Exception:                      # 表单畸形一律当作没给口令
            return ""

    async def handle_logout(self, request: web.Request) -> web.Response:
        """登出同样要校验 CSRF：否则任意页面都能用一个 <form> 把管理员踢下线。"""
        session: Session = request["session"]
        token = ""
        content_type = (request.content_type or "").lower()
        try:
            if "json" in content_type:
                token = str((await self._json_body(request)).get(CSRF_FIELD, ""))
            else:
                token = str((await request.post()).get(CSRF_FIELD, ""))
        except web.HTTPException:
            raise
        except Exception:
            token = ""
        provided = request.headers.get(CSRF_HEADER) or token
        if not provided or provided != session.csrf_token:
            raise web.HTTPForbidden(text="csrf", content_type="text/plain")
        response = web.Response(status=303, headers={"Location": "/login"})
        response.headers["Set-Cookie"] = self.sessions.clear_cookie_header()
        return response

    async def handle_health(self, request: web.Request) -> web.Response:
        """容器/反代的存活探针：不带任何敏感信息，因此不需要登录。"""
        return web.json_response({
            "status": "ok",
            "version": __version__,
            "uptime_seconds": int(self.service.uptime_seconds()),
        })

    async def handle_favicon(self, request: web.Request) -> web.Response:
        return web.Response(status=204)

    async def handle_static(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if name not in STATIC_FILES:
            raise web.HTTPNotFound()
        content_type = "text/css" if name.endswith(".css") else "application/javascript"
        return web.Response(text=(STATIC_DIR / name).read_text(encoding="utf-8"),
                            content_type=content_type, charset="utf-8")

    # ------------------------------------------------------------------ #
    # JSON 接口
    # ------------------------------------------------------------------ #
    @staticmethod
    def json_error(message: str, status: int, *, key: str = "") -> web.Response:
        return web.json_response({"error": message, "key": key or None}, status=status)

    async def handle_bootstrap(self, request: web.Request) -> web.Response:
        return web.json_response(self.service.bootstrap())

    async def handle_overview(self, request: web.Request) -> web.Response:
        return web.json_response(self.service.overview())

    async def handle_instances(self, request: web.Request) -> web.Response:
        provider = request.query.get("provider") or None
        return web.json_response(await self.service.instances(provider))

    async def handle_destroy(self, request: web.Request) -> web.Response:
        session: Session = request["session"]
        payload = await self._json_body(request)
        self.check_csrf(request, session)
        self.require_writable()
        result = await self.service.destroy(
            request.match_info["provider"], request.match_info["instance_id"],
            str(payload.get("confirm", "")),
        )
        return web.json_response(result)

    async def handle_credentials(self, request: web.Request) -> web.Response:
        return web.json_response(self.service.credentials())

    async def handle_add_credential(self, request: web.Request) -> web.Response:
        session: Session = request["session"]
        payload = await self._json_body(request)
        self.check_csrf(request, session)
        self.require_writable()
        result = await self.service.add_credential(
            str(payload.get("provider", "")), str(payload.get("label", "")),
            payload.get("fields") or {},
        )
        return web.json_response(result, status=201)

    async def handle_delete_credential(self, request: web.Request) -> web.Response:
        session: Session = request["session"]
        self.check_csrf(request, session)
        self.require_writable()
        credential_id = self._int_param(request.match_info["credential_id"])
        return web.json_response(self.service.delete_credential(credential_id))

    async def handle_activate_credential(self, request: web.Request) -> web.Response:
        session: Session = request["session"]
        self.check_csrf(request, session)
        self.require_writable()
        credential_id = self._int_param(request.match_info["credential_id"])
        return web.json_response(self.service.activate_credential(credential_id))

    async def handle_users(self, request: web.Request) -> web.Response:
        return web.json_response(self.service.users())

    async def handle_set_role(self, request: web.Request) -> web.Response:
        session: Session = request["session"]
        payload = await self._json_body(request)
        self.check_csrf(request, session)
        self.require_writable()
        telegram_id = self._int_param(request.match_info["telegram_id"])
        return web.json_response(self.service.set_role(telegram_id, str(payload.get("role", ""))))

    async def handle_logs(self, request: web.Request) -> web.Response:
        return web.json_response(self.service.logs(request.query.get("limit", "50")))

    async def handle_tasks(self, request: web.Request) -> web.Response:
        return web.json_response(self.service.tasks(request.query.get("limit", "50")))

    # ------------------------------------------------------------------ #
    # 工具
    # ------------------------------------------------------------------ #
    @staticmethod
    async def _json_body(request: web.Request) -> Dict[str, Any]:
        # 空 body 视为 {}：curl / 脚本调用（如 /api/.../destroy 且不需要额外字段）时
        # 不该因为"没发 JSON"就吃 400。发出来的 body 一律要求是 JSON 对象。
        raw = await request.read()
        if not raw.strip():
            return {}
        try:
            payload = json.loads(raw)
        except Exception:
            raise web.HTTPBadRequest(text="invalid json", content_type="text/plain") from None
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="invalid payload", content_type="text/plain")
        return payload

    @staticmethod
    def _int_param(value: str) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            raise web.HTTPBadRequest(text="invalid id", content_type="text/plain") from None


def exception_to_response(settings, exc: Exception) -> Optional[web.Response]:
    """业务异常 → JSON 响应（统一状态码映射，避免每个 handler 各写一套）。"""
    if not isinstance(exc, CloudOpsError):
        return None
    from ..i18n import get_translator

    status = 400
    for error_type, code in ERROR_STATUS.items():
        if isinstance(exc, error_type):
            status = code
            break
    t = get_translator(settings.language)
    return web.json_response(
        {"error": t(exc.key, **getattr(exc, "params", {})), "key": exc.key}, status=status)
