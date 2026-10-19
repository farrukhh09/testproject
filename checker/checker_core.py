"""Ядро проверщика номеров: нормализация, маскирование, лимит проверок, поиск в Telegram.

Поиск в Telegram — официальный метод contacts.resolvePhone через аккаунт пользователя
(протокол MTProto, библиотека Telethon). Номер в контакты не добавляется.
Полный номер нигде не записывается: в журнал и в файл лимита попадает только маска или время.

Файл сессии (полный доступ к аккаунту) и файл лимита лежат не в папке проекта, а в личной папке
пользователя: Windows — %LOCALAPPDATA%\\MeridianChecker, Mac — ~/Library/Application Support/MeridianChecker.
Так их не скопируют вместе с проектом.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberFormat, PhoneNumberType

BASE_DIR = Path(__file__).resolve().parent


def _app_dir(platform: str, env, home: Path) -> Path:
    """Личная папка проверщика вне проекта: так файл сессии не скопируют вместе с папкой проекта."""
    if platform == "win32":
        return Path(env.get("LOCALAPPDATA") or home) / "MeridianChecker"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "MeridianChecker"
    return Path(env.get("XDG_DATA_HOME") or home / ".local" / "share") / "MeridianChecker"


APP_DIR = _app_dir(sys.platform, os.environ, Path.home())
# Файлы для запуска двойным щелчком: .cmd на Windows, .command на Mac
LAUNCHER = "start_checker.cmd" if sys.platform == "win32" else "start_checker.command"
LOGOUT_LAUNCHER = "telegram_logout.cmd" if sys.platform == "win32" else "telegram_logout.command"
SESSION_PATH = APP_DIR / "session" / "meridian"  # Telethon сам добавит .session
STATE_PATH = APP_DIR / "state" / "limit.json"
LOCK_PATH = APP_DIR / "state" / "checker.lock"
CONFIRM_PATH = APP_DIR / "state" / "account_confirmed.json"  # номер аккаунта, который вы подтвердили как свой
HOUR = 3600
DAY = 24 * HOUR
# Неизвестная ошибка Telegram (например, ограничение аккаунта): не обращаемся к API час
UNKNOWN_ERROR_PAUSE = HOUR
# Самая длинная пауза, которую храним: требование Telegram подождать бывает дольше суток
PAUSE_CAP = 7 * DAY
# Подключение к серверам Telegram: две быстрые попытки вместо шести по умолчанию.
# Если сеть закрывает серверы, это выясняется за ~20 секунд, а не за минуту
CONNECT_OPTIONS = {"connection_retries": 1, "retry_delay": 1, "timeout": 10}
# Только сетевые ошибки: проблемы с файлами (OSError, PermissionError) не должны выдаваться за закрытую сеть
NETWORK_ERRORS = (ConnectionError, TimeoutError)


def quiet_telethon_logs() -> None:
    """Telethon пишет в окно «Attempt N at connecting failed» — нам хватает своего понятного сообщения."""
    import logging

    logging.getLogger("telethon").setLevel(logging.ERROR)


class PhoneError(ValueError):
    """Номер не удалось разобрать. Текст ошибки показывается пользователю."""


@dataclass(frozen=True)
class Config:
    api_id: int | None
    api_hash: str | None
    region: str
    port: int
    max_per_hour: int
    max_per_day: int
    min_gap: int


def _int_env(name: str, default: int, low: int, high: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw.isdigit():
        return default
    return max(low, min(high, int(raw)))


def load_config() -> Config:
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env")
    raw_id = os.getenv("TELEGRAM_API_ID", "").strip()
    return Config(
        api_id=int(raw_id) if raw_id.isdigit() else None,
        api_hash=os.getenv("TELEGRAM_API_HASH", "").strip() or None,
        region=(os.getenv("CHECKER_DEFAULT_REGION", "TJ").strip().upper() or "TJ"),
        port=_int_env("CHECKER_PORT", 8765, 1024, 65535),
        # Верхние границы: больше — риск, что Telegram ограничит аккаунт за поиск по номерам
        max_per_hour=_int_env("CHECKER_MAX_PER_HOUR", 20, 1, 60),
        max_per_day=_int_env("CHECKER_MAX_PER_DAY", 50, 1, 200),
        min_gap=_int_env("CHECKER_MIN_GAP_SECONDS", 5, 1, 600),
    )


def prepare_app_dir() -> bool:
    """Создаёт личную папку проверщика и оставляет доступ к ней только текущему пользователю."""
    for sub in ("session", "state"):
        (APP_DIR / sub).mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        for path in (APP_DIR, APP_DIR / "session", APP_DIR / "state"):
            os.chmod(path, 0o700)  # только владелец: остальные пользователи Mac не откроют файлы внутри
        return True
    user, domain = os.getenv("USERNAME"), os.getenv("USERDOMAIN")
    if not user:
        return False
    # Полный путь: иначе Windows сначала ищет icacls.exe в текущей папке. Пользователь — вместе с доменом
    icacls = Path(os.getenv("SystemRoot", r"C:\Windows")) / "System32" / "icacls.exe"
    account = f"{domain}\\{user}" if domain else user
    done = subprocess.run([str(icacls), str(APP_DIR), "/inheritance:r", "/grant:r", f"{account}:(OI)(CI)F"], capture_output=True)
    return done.returncode == 0


class SingleInstance:
    """Не даёт запустить второй проверщик или вход, пока первый работает: они делят один файл сессии."""

    def __init__(self, path: Path = LOCK_PATH):
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.path, "a+")
        try:
            if sys.platform == "win32":
                import msvcrt

                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            self._fh.close()
            self._fh = None
            return False

    def release(self) -> None:
        if self._fh is not None:
            self._fh.close()  # закрытие файла снимает блокировку
            self._fh = None


def confirmed_id(path: Path = CONFIRM_PATH) -> int | None:
    """Номер аккаунта Telegram, который пользователь подтвердил как свой при входе (или None)."""
    try:
        return int(json.loads(path.read_text(encoding="utf-8"))["user_id"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def normalize(raw: str, region: str) -> str:
    """Приводит номер к виду E.164 (+992901234567) или бросает PhoneError."""
    text = (raw or "").strip()
    if not text:
        raise PhoneError("Введите номер телефона.")
    if len(text) > 32:
        raise PhoneError("Слишком длинная строка для номера телефона.")
    if any(ch in text for ch in ",;/\\|") or text.count("+") > 1:
        raise PhoneError("Введите один номер.")
    compact = "".join(ch for ch in text if ch not in " -(). ")
    if compact.startswith("00"):
        text = "+" + compact[2:]  # 00992… — международный формат без плюса
    try:
        number = phonenumbers.parse(text, region or None)
    except NumberParseException:
        raise PhoneError("Не похоже на номер телефона.") from None
    if not phonenumbers.is_valid_number(number):
        if region == "TJ" and compact.startswith("8") and not compact.startswith("+"):
            raise PhoneError("Номер с 8 в начале не подходит: введите без восьмёрки (901234567) или с кодом +992.")
        raise PhoneError("Такого номера не бывает: проверьте код страны и количество цифр.")
    if phonenumbers.number_type(number) == PhoneNumberType.FIXED_LINE:
        raise PhoneError("Это городской номер. Проверка имеет смысл только для мобильных.")
    return phonenumbers.format_number(number, PhoneNumberFormat.E164)


def mask(e164: str) -> str:
    """+992901234567 -> +992•••••••67: видны только код страны и две последние цифры."""
    try:
        head = "+" + str(phonenumbers.parse(e164, None).country_code)
    except NumberParseException:
        head = "+"
    hidden = max(0, len(e164) - len(head) - 2)
    return head + "•" * hidden + e164[-2:]


class RateLimiter:
    """Ограничивает проверки: пауза между ними, лимит за скользящий час и за сутки, пауза по требованию Telegram.

    В файле хранятся только моменты проверок и время окончания паузы (без номеров), поэтому лимит
    переживает перезапуск. Повреждённый файл не обнуляет лимит: проверки блокируются на час.
    """

    def __init__(self, max_per_hour: int, min_gap: int, max_per_day: int | None = None, path: Path | None = STATE_PATH, clock=time.time):
        self.max_per_hour = max_per_hour
        self.max_per_day = max_per_day or max_per_hour * 24
        self.min_gap = min_gap
        self.path = path
        self.clock = clock
        self.load_error = False
        self.save_error = False
        self._lock = threading.Lock()
        self._times, self._blocked_until = self._load()

    def _load(self) -> tuple[list[float], float]:
        if not self.path or not self.path.exists():
            return [], 0.0
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            times = sorted(t for t in (float(x) for x in data.get("times", [])) if math.isfinite(t))
            blocked = float(data.get("blocked_until", 0))
            return times, blocked if math.isfinite(blocked) else self.clock() + HOUR
        except (OSError, ValueError, TypeError, AttributeError):
            self.load_error = True
            return [], self.clock() + HOUR

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"times": self._times, "blocked_until": self._blocked_until}), encoding="utf-8")
            os.replace(tmp, self.path)  # запись целиком или никак: сбой не оставит обрезанный файл
            self.save_error = False
        except OSError:
            self.save_error = True  # лимит продолжает работать в памяти; сервер сообщит в журнал

    def _prune(self, now: float) -> None:
        # «Будущие» отметки (часы перевели назад) считаем сделанными сейчас: счётчик не обнуляется и не залипает
        self._times = sorted(min(t, now) for t in self._times if now - t < DAY)
        if self._blocked_until - now > PAUSE_CAP:
            self._blocked_until = now + PAUSE_CAP

    def _wait(self, now: float) -> tuple[int, str] | None:
        self._prune(now)
        if self._blocked_until > now:
            return math.ceil(self._blocked_until - now), "blocked"
        hour = [t for t in self._times if now - t < HOUR]
        if hour and now - hour[-1] < self.min_gap:
            return math.ceil(self.min_gap - (now - hour[-1])), "gap"
        if len(hour) >= self.max_per_hour:
            return math.ceil(HOUR - (now - hour[0])), "hour"
        if len(self._times) >= self.max_per_day:
            return math.ceil(DAY - (now - self._times[0])), "day"
        return None

    def take(self) -> tuple[int, str] | None:
        """None — проверку можно делать, и она учтена. Иначе (сколько секунд ждать, причина)."""
        with self._lock:
            now = self.clock()
            wait = self._wait(now)
            if wait is None:
                self._times.append(now)
                self._save()
            return wait

    def block(self, seconds: int) -> None:
        """Пауза по требованию Telegram или после неизвестной ошибки. Сохраняется в файле."""
        with self._lock:
            self._blocked_until = max(self._blocked_until, self.clock() + max(1, min(PAUSE_CAP, int(seconds))))
            self._save()

    def info(self) -> dict:
        with self._lock:
            now = self.clock()
            self._prune(now)
            hour = [t for t in self._times if now - t < HOUR]
            return {
                "used": len(hour), "max": self.max_per_hour,
                "reset_in": math.ceil(HOUR - (now - hour[0])) if hour else 0,
                "day_used": len(self._times), "day_max": self.max_per_day,
                "blocked_for": max(0, math.ceil(self._blocked_until - now)),
            }


@dataclass(frozen=True)
class LookupResult:
    # found | not_found | flood | not_logged_in | banned | no_keys | error
    status: str
    wait: int | None = None
    detail: str | None = None
    pause: int | None = None  # сколько секунд не обращаться к Telegram после этого ответа


class TelegramLookup:
    """Клиент Telethon живёт в отдельном потоке со своим циклом asyncio.

    Запросы от веб-сервера передаются в этот цикл и ждут ответа не дольше timeout секунд.
    Telethon не повторяет запросы сам: при требовании подождать ошибка сразу возвращается.
    """

    def __init__(self, api_id: int | None, api_hash: str | None, session_path: Path = SESSION_PATH, timeout: float = 30,
                 blocked: str | None = None, check_owner: bool = False, confirm_path: Path = CONFIRM_PATH):
        self.api_id = api_id
        self.api_hash = api_hash
        self.session_path = session_path
        self.timeout = timeout
        # Проверять, что вошли в аккаунт, подтверждённый как свой. Делается один раз, при первом удачном подключении
        self._owner_pending = check_owner
        self._confirm_path = confirm_path
        self._client = None
        self._conn_lock: asyncio.Lock | None = None
        # not_logged_in | banned | error — до перезапуска проверщика к Telegram не обращаемся.
        # blocked задаёт это сразу: например, вход не подтвердили как свой
        self._lost_auth: str | None = blocked
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, name="telegram", daemon=True)
        self._thread.start()

    @property
    def has_keys(self) -> bool:
        return bool(self.api_id and self.api_hash)

    def _run(self, coro):
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        try:
            return future.result(self.timeout)
        except TimeoutError:
            future.cancel()
            raise

    def _make_client(self):
        from telethon import TelegramClient

        self.session_path.parent.mkdir(parents=True, exist_ok=True)
        client = TelegramClient(
            str(self.session_path), self.api_id, self.api_hash,
            flood_sleep_threshold=0,  # не ждать и не повторять молча: решение о паузе принимает лимит
            request_retries=0,  # ровно одна попытка (1 у Telethon означает две)
            receive_updates=False,  # проверщику не нужны сообщения и события аккаунта
            **CONNECT_OPTIONS,
        )
        # Не копить в файле сессии данные найденных людей (имя, номер, логин)
        client.session.save_entities = False
        return client

    async def _connect(self) -> bool:
        if self._conn_lock is None:
            self._conn_lock = asyncio.Lock()
        async with self._conn_lock:  # два запроса одновременно не должны подключаться дважды
            if self._client is None:
                self._client = self._make_client()
            if not self._client.is_connected():
                await self._client.connect()
            if not await self._client.is_user_authorized():
                return False
            if self._owner_pending:
                me = await self._client.get_me()
                if me is None or getattr(me, "id", None) != confirmed_id(self._confirm_path):
                    self._lost_auth = "not_logged_in"  # вход не подтверждён как свой — проверки через него не делаем
                    return False
                self._owner_pending = False
            return True

    def status(self) -> str:
        """ready | not_logged_in | banned | no_keys | offline | error"""
        if not self.has_keys:
            return "no_keys"
        if self._lost_auth:
            return self._lost_auth
        try:
            return "ready" if self._run(self._connect()) else "not_logged_in"
        except NETWORK_ERRORS:  # серверы Telegram недоступны из этой сети
            return "offline"
        except Exception:  # повреждённая сессия, неверные ключи
            return "error"

    def resolve(self, e164: str) -> LookupResult:
        if not self.has_keys:
            return LookupResult("no_keys")
        if self._lost_auth:
            return LookupResult(self._lost_auth)
        try:
            return self._run(self._resolve(e164))
        except TimeoutError:
            return LookupResult("error", detail="Telegram не ответил вовремя")
        except Exception as exc:  # обрыв связи и прочее вне протокола Telegram — без паузы
            return LookupResult("error", detail=type(exc).__name__)

    async def _resolve(self, e164: str) -> LookupResult:
        from telethon import errors, functions

        if not await self._connect():
            return LookupResult("not_logged_in")
        try:
            result = await self._client(functions.contacts.ResolvePhoneRequest(phone=e164.lstrip("+")))
        except errors.FrozenMethodInvalidError:  # аккаунт заморожен Telegram (класс наследует FloodError)
            self._lost_auth = "banned"
            return LookupResult("banned", pause=DAY)
        except errors.FloodError as exc:  # FloodWait, FloodPremiumWait, FloodTestPhoneWait
            seconds = int(getattr(exc, "seconds", 0) or 0) or UNKNOWN_ERROR_PAUSE
            return LookupResult("flood", wait=seconds, pause=seconds)
        # У этих классов в exc.message общее «BAD_REQUEST», поэтому ловим по типу
        except errors.PhoneNotOccupiedError:
            return LookupResult("not_found")
        except errors.PhoneNumberInvalidError:
            return LookupResult("not_found", detail="Telegram считает номер некорректным")
        except (errors.UserDeactivatedError, errors.UserDeactivatedBanError):
            self._lost_auth = "banned"
            return LookupResult("banned", pause=DAY)
        except (errors.UnauthorizedError, errors.AuthKeyDuplicatedError) as exc:  # сеанс завершён, ключ отозван или занят
            self._lost_auth = "not_logged_in"
            return LookupResult("not_logged_in", detail=type(exc).__name__)
        except errors.RPCError as exc:
            if exc.message == "PHONE_NOT_OCCUPIED":
                return LookupResult("not_found")
            return LookupResult("error", detail=f"{type(exc).__name__}: {exc.message}", pause=UNKNOWN_ERROR_PAUSE)
        return LookupResult("found" if getattr(result, "users", None) else "not_found")

    async def _aclose(self) -> None:
        if self._client is not None:
            await self._client.disconnect()

    def close(self) -> None:
        try:
            self._run(self._aclose())  # отключаемся в том же цикле, где живёт клиент
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(5)
        if not self._thread.is_alive():
            self._loop.close()
