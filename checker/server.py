"""Локальный проверщик номеров. Запуск: start_checker.cmd (Windows) или start_checker.command (Mac) в папке
MARKETING_CRM — браузер откроет страницу сам.

Отдаёт прототип CRM и проверяет по одному номеру: есть ли аккаунт Telegram.
WhatsApp проверяется вручную на странице (официальной проверки нет).

Защита:
- сервер слушает только 127.0.0.1 и отвечает только на адреса 127.0.0.1 / localhost (защита от подмены DNS);
- ключ для запросов страница получает только по ссылке из окна проверщика (/start?k=…) и хранит
  его в своей вкладке; по обычному адресу страница ключа не получает. Запросы к /api/ несут ключ
  в заголовке и проверяются по Origin и Sec-Fetch-Site: чужие сайты и программы на других портах
  этого компьютера не смогут проверять номера через ваш аккаунт;
- проверки идут строго по одной; лимит в час и в сутки, пауза между проверками и по требованию Telegram;
- в журнал пишется только маска номера.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import socket
import sys
import threading
import time
import webbrowser
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from checker_core import (
    BASE_DIR, LookupResult, PhoneError, RateLimiter, SingleInstance, TelegramLookup,
    LAUNCHER, load_config, mask, normalize, prepare_app_dir, quiet_telethon_logs,
)

PROTOTYPE = BASE_DIR.parent / "prototype" / "crm_prototype.html"
MAX_BODY = 1024
COMMON_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
}
# Страница может обращаться только к своему серверу (connect-src 'self'): ключ и номера не уйдут на чужой адрес
PAGE_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
            "form-action 'none'; frame-ancestors 'none'")
API_CSP = "default-src 'none'; frame-ancestors 'none'"


@dataclass(frozen=True)
class Keys:
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))   # ключ в заголовке запросов
    launch: str = field(default_factory=lambda: secrets.token_urlsafe(32))  # ключ в ссылке запуска


def log(text: str) -> None:
    print(time.strftime("%H:%M:%S"), text, flush=True)


def render_page(token: str | None) -> bytes:
    """Прототип с ключом проверщика или с отметкой «ключа нет». Файл читается каждый раз, чтобы правки были видны сразу."""
    html = PROTOTYPE.read_text(encoding="utf-8")
    if "</head>" not in html:
        raise RuntimeError("В прототипе нет </head>")
    data = {"token": token, "launcher": LAUNCHER} if token else {"locked": True, "launcher": LAUNCHER}
    inject = "<script>window.MERIDIAN_CHECKER = " + json.dumps(data) + ";</script>\n"
    return html.replace("</head>", inject + "</head>", 1).encode("utf-8")


LOCKED_PAGE = ("<!doctype html><meta charset='utf-8'><title>Проверщик номеров</title>"
               "<body style='font:15px system-ui;margin:40px'><h2>Ссылка не подошла</h2>"
               "<p>Откройте ссылку из окна проверщика. Она меняется при каждом запуске.</p></body>").encode("utf-8")


def limit_message(wait: int, reason: str, limiter: RateLimiter) -> str:
    minutes = max(1, round(wait / 60))
    return {
        "gap": f"Подождите {wait} с между проверками.",
        "hour": f"Не больше {limiter.max_per_hour} проверок в час. Следующая через {minutes} мин.",
        "day": f"Не больше {limiter.max_per_day} проверок в сутки. Следующая через {minutes} мин.",
        "blocked": f"Проверки приостановлены по требованию Telegram или после ошибки. Можно через {minutes} мин.",
    }.get(reason, f"Подождите {minutes} мин.")


class LocalServer(ThreadingHTTPServer):
    """Многопоточный, чтобы «пустые» соединения браузера не блокировали остальные запросы.

    Порт берётся монопольно: на Windows иначе второй проверщик молча занимает тот же порт.
    """

    daemon_threads = True
    # Без SO_REUSEADDR везде: на Windows он пускает второй сервер на тот же порт, на Mac — поверх программы,
    # слушающей 0.0.0.0 (например, Docker). Если после перезапуска порт ещё занят, open_server возьмёт следующий
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def build_handler(region: str, keys: Keys, telegram, limiter: RateLimiter, page):
    """page(token_or_None) возвращает байты страницы. Отдельно, чтобы тесты могли подставить свою."""
    one_at_a_time = threading.Lock()  # две вкладки не должны проверять одновременно

    class Handler(BaseHTTPRequestHandler):
        server_version = "MeridianChecker"
        sys_version = ""
        timeout = 10  # зависшее соединение закрывается через 10 с

        def log_message(self, fmt, *args):  # стандартный журнал пишет адрес клиента и путь — не нужен
            pass

        # ---------- ответы ----------
        def _send(self, status: int, body: bytes, ctype: str, csp: str = API_CSP) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Security-Policy", csp)
            for name, value in COMMON_HEADERS.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, obj: dict) -> None:
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        # ---------- проверки запроса ----------
        def _hosts(self) -> set[str]:
            port = self.server.server_address[1]
            return {f"127.0.0.1:{port}", f"localhost:{port}"}

        def _host_ok(self) -> bool:
            return self.headers.get("Host", "") in self._hosts()

        def _site_ok(self) -> bool:
            # Браузер помечает запросы с других сайтов и других портов; такие отклоняем сразу
            return self.headers.get("Sec-Fetch-Site", "none") in ("none", "same-origin")

        def _origin_ok(self) -> bool:
            origin = self.headers.get("Origin")
            return origin is None or origin in {"http://" + h for h in self._hosts()}

        def _token_ok(self) -> bool:
            return hmac.compare_digest(self.headers.get("X-Checker-Token", "").encode(), keys.token.encode())

        def _api_ok(self) -> bool:
            return self._site_ok() and self._origin_ok() and self._token_ok()

        # ---------- маршруты ----------
        def _safe(self, fn) -> None:
            try:
                fn()
            except ConnectionError:
                pass  # пользователь закрыл вкладку — отвечать некому
            except Exception as exc:  # в журнал — только тип ошибки, без данных запроса
                log(f"внутренняя ошибка: {type(exc).__name__}")
                try:
                    self._json(500, {"error": "Внутренняя ошибка проверщика. Подробности в его окне."})
                except Exception:
                    pass

        def do_GET(self) -> None:
            self._safe(self._get)

        def do_POST(self) -> None:
            self._safe(self._post)

        def _get(self) -> None:
            if not self._host_ok():
                return self._json(403, {"error": "Откройте проверщик по адресу 127.0.0.1"})
            url = urlsplit(self.path)
            if url.path == "/start":
                k = (parse_qs(url.query).get("k") or [""])[0]
                if not self._site_ok() or not hmac.compare_digest(k.encode(), keys.launch.encode()):
                    return self._send(403, LOCKED_PAGE, "text/html; charset=utf-8")
                # Страница сохранит ключ во вкладке и сразу уберёт ссылку запуска из адресной строки
                return self._send(200, page(keys.token), "text/html; charset=utf-8", csp=PAGE_CSP)
            if url.path in ("/", "/index.html"):
                return self._send(200, page(None), "text/html; charset=utf-8", csp=PAGE_CSP)
            if url.path == "/api/status":
                if not self._api_ok():
                    return self._json(403, {"error": "Нет ключа проверщика. Откройте ссылку из окна проверщика."})
                return self._json(200, {"telegram": telegram.status(), "limit": limiter.info()})
            return self._json(404, {"error": "Не найдено"})

        def _read_body(self) -> bytes | None:
            """Читаем тело до любых проверок: на Windows ответ на непрочитанный запрос может потеряться
            (соединение сбрасывается), и страница решит, что проверщик не отвечает."""
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if 0 < length <= MAX_BODY:
                return self.rfile.read(length)
            if length > MAX_BODY:
                self.close_connection = True  # большое тело не читаем — просто закрываем соединение после ответа
            return None

        def _post(self) -> None:
            body = self._read_body()
            if not self._host_ok():
                return self._json(403, {"error": "Откройте проверщик по адресу 127.0.0.1"})
            if urlsplit(self.path).path != "/api/check":
                return self._json(404, {"error": "Не найдено"})
            if not self._api_ok():
                return self._json(403, {"error": "Нет ключа проверщика. Откройте ссылку из окна проверщика."})
            if (self.headers.get("Content-Type") or "").split(";")[0].strip().lower() != "application/json":
                return self._json(415, {"error": "Нужен запрос в формате JSON"})
            if body is None:
                return self._json(413, {"error": "Неверный размер запроса"})
            try:
                data = json.loads(body.decode("utf-8"))
                raw = data.get("phone") if isinstance(data, dict) else None
                if not isinstance(raw, str):
                    raise ValueError
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"error": "Неверный запрос"})
            try:
                e164 = normalize(raw, region)
            except PhoneError as exc:
                return self._json(400, {"error": str(exc)})

            if not one_at_a_time.acquire(blocking=False):
                return self._json(429, {"error": "Идёт другая проверка — дождитесь её.", "limit": limiter.info()})
            try:
                result = self._lookup(e164)
            finally:
                one_at_a_time.release()
            if isinstance(result, dict):  # отказ лимита
                return self._json(429, result)
            log(f"проверка {mask(e164)}: telegram={result.status}" + (f" ({result.detail})" if result.status == "error" and result.detail else ""))
            digits = e164.lstrip("+")
            return self._json(200, {
                "masked": mask(e164),
                "wa_app": "whatsapp://send?phone=" + digits,
                "wa_link": "https://wa.me/" + digits,
                # Сразу чат в WhatsApp Web, без промежуточной страницы wa.me: работает без установленного приложения
                "wa_web": "https://web.whatsapp.com/send?phone=" + digits,
                "telegram": {"status": result.status, "wait": result.wait, "detail": result.detail},
                "limit": limiter.info(),
                "checked_at": time.strftime("%H:%M"),
            })

        def _lookup(self, e164: str):
            state = telegram.status()
            if state in ("no_keys", "not_logged_in", "banned"):
                return LookupResult(state)  # в Telegram не обращались — лимит не тратим
            if state == "offline":
                return LookupResult("offline")
            if state != "ready":
                return LookupResult("error", detail="Telegram не принял ключи API или файл сессии повреждён")
            wait = limiter.take()
            if wait is not None:
                return {"error": limit_message(wait[0], wait[1], limiter), "limit": limiter.info()}
            if limiter.save_error:
                log("не удалось записать файл лимита — лимит работает только до перезапуска")
            result = telegram.resolve(e164)
            if result.pause:
                limiter.block(result.pause)
            return result

        def _not_allowed(self) -> None:
            self._json(405, {"error": "Метод не поддерживается"})

        do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _not_allowed

    return Handler


PORT_TRIES = 20


def open_server(first_port: int, handler, tries: int = PORT_TRIES) -> tuple[LocalServer | None, int]:
    """Берёт первый свободный порт начиная с first_port: нужный может быть занят другой программой (например, SSH-туннелем)."""
    for port in range(first_port, min(first_port + tries, 65536)):
        try:
            return LocalServer(("127.0.0.1", port), handler), port
        except OSError:
            continue
    return None, first_port


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    quiet_telethon_logs()
    cfg = load_config()
    if not PROTOTYPE.exists():
        sys.exit(f"Не найден прототип: {PROTOTYPE}")
    if not prepare_app_dir():
        print("Внимание: не удалось ограничить доступ к папке сессии только вашей учётной записью.")
    lock = SingleInstance()
    if not lock.acquire():
        sys.exit("Проверщик уже запущен (или идёт вход в Telegram). Закройте другое окно и попробуйте снова.")
    blocked = None
    state = "no_keys"
    if cfg.api_id and cfg.api_hash:
        # Входа ещё нет — показываем QR-код прямо здесь, отдельная команда для входа не нужна
        from login import ensure_login
        try:
            state = asyncio.run(ensure_login(cfg.api_id, cfg.api_hash))
        except KeyboardInterrupt:
            print("  Вход отменён — проверщик запустится без проверки Telegram.")
            state = "not_logged_in"
        except Exception as exc:  # вход не должен мешать запуску остального прототипа
            print(f"  Вход не удался ({type(exc).__name__}) — проверщик запустится без проверки Telegram.")
            state = "error"
        # Входа нет или аккаунт заблокирован — до перезапуска к Telegram не обращаемся. При сбое связи не
        # блокируем: проверщик сам сверит аккаунт с подтверждённым при первом удачном подключении
        blocked = state if state in ("not_logged_in", "banned") else None
    keys = Keys()
    telegram = TelegramLookup(cfg.api_id, cfg.api_hash, blocked=blocked, check_owner=True)
    limiter = RateLimiter(cfg.max_per_hour, cfg.min_gap, cfg.max_per_day)
    server, port = open_server(cfg.port, build_handler(cfg.region, keys, telegram, limiter, render_page))
    if server is None:
        telegram.close()
        lock.release()
        sys.exit(f"Порты {cfg.port}–{cfg.port + PORT_TRIES - 1} заняты другими программами. Задайте другой CHECKER_PORT в .env.")

    print("Проверщик номеров Meridian")
    if port != cfg.port:
        print(f"  Порт {cfg.port} занят другой программой — проверщик работает на порту {port}.")
    print({
        "ready": "  Telegram: вход выполнен, проверка работает.",
        "not_logged_in": f"  Telegram: нет входа. Закройте это окно и снова запустите {LAUNCHER} — появится QR-код для входа.",
        "banned": "  Telegram: аккаунт заблокирован или заморожен Telegram. Проверки невозможны.",
        "no_keys": "  Telegram: нет ключей. Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в checker\\.env",
        "offline": "  Telegram: серверы недоступны из этой сети (web.telegram.org работает другим путём). См. README, «Если серверы Telegram недоступны».",
        "error": "  Telegram: ключи API не приняты или файл сессии повреждён. Проверьте checker\\.env",
    }[state if state == "offline" else telegram.status()])  # при закрытой сети второй раз тайм-аут не ждём
    if limiter.load_error:
        print("  Внимание: файл лимита повреждён — проверки приостановлены на час.")
    print(f"  Лимит: {cfg.max_per_hour} в час, {cfg.max_per_day} в сутки, пауза {cfg.min_gap} с. Регион по умолчанию: {cfg.region}")
    url = f"http://127.0.0.1:{port}/start?k={keys.launch}"
    print("  Ссылка для входа (действует до остановки проверщика, никому не пересылайте):")
    print(f"  {url}")
    print("  Остановить: Ctrl+C")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        telegram.close()
        lock.release()
        print("Проверщик остановлен.")


if __name__ == "__main__":
    main()
