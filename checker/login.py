"""Вход в Telegram для проверщика номеров — по QR-коду, без кодов подтверждения.

Отдельно запускать обычно не нужно: start_checker (.cmd на Windows, .command на Mac) сам покажет
QR-код, если входа ещё нет.
    telegram_logout (.cmd / .command) — выйти: Telegram закроет этот сеанс, файл сессии станет бесполезным

Вход по QR-коду устроен как подключение Telegram Desktop: телефон с вашим Telegram сканирует код,
и этот компьютер становится ещё одним «устройством». Коды подтверждения не запрашиваются,
поэтому не бывает ответа «слишком много попыток, попробуйте позже».

Файл сессии лежит в личной папке (Windows: %LOCALAPPDATA%\\MeridianChecker, Mac: ~/Library/Application
Support/MeridianChecker) и даёт полный доступ к аккаунту. Удаление файла доступ НЕ отзывает: копия
продолжит работать. Отозвать можно только в Telegram (Настройки → Устройства → завершить сеанс)
или файлом telegram_logout.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import sys
import time
from getpass import getpass

from checker_core import (
    APP_DIR, CONFIRM_PATH, CONNECT_OPTIONS, NETWORK_ERRORS, SESSION_PATH, SingleInstance, confirmed_id,
    LAUNCHER, load_config, prepare_app_dir, quiet_telethon_logs,
)

OFFLINE_MESSAGE = ("  Серверы Telegram недоступны из этой сети. Сайт web.telegram.org работает другим путём — как обычный\n"
                   "  сайт, а программам нужен прямой доступ к серверам Telegram, и его закрывает сеть.\n"
                   "  Что делать — в README, раздел «Если серверы Telegram недоступны».")

QR_PNG = APP_DIR / "state" / "login_qr.png"
TOTAL_WAIT = 180  # сколько секунд ждать сканирования, обновляя QR-код
PASSWORD_TRIES = 3
# Telegram даёт коду около 30 с. Если по часам компьютера выходит меньше 5 или больше 60 — часы сбиты
MIN_TTL, MAX_TTL, DEFAULT_TTL = 5, 60, 30


def make_client(api_id: int, api_hash: str, receive_updates: bool):
    from telethon import TelegramClient

    # receive_updates нужен только при входе: о сканировании QR-кода Telegram сообщает уведомлением
    client = TelegramClient(str(SESSION_PATH), api_id, api_hash, flood_sleep_threshold=0, receive_updates=receive_updates, **CONNECT_OPTIONS)
    client.session.save_entities = False
    return client


def show_qr(url: str) -> None:
    """Рисует QR-код в окне и сохраняет его картинкой — на случай, если камера не читает код с экрана консоли."""
    import segno

    code = segno.make(url, error="l")
    print()
    if sys.platform == "win32":
        code.terminal(compact=True, border=2)  # консоль Windows: тёмный фон
    else:
        print(ansi_qr(code))  # Mac: свои цвета, код читается при любой теме Terminal
    QR_PNG.parent.mkdir(parents=True, exist_ok=True)
    code.save(str(QR_PNG), scale=8, border=4)
    print(f"  Если камера не видит код в окне, откройте картинку: {QR_PNG}")


def ansi_qr(code, border: int = 2) -> str:
    """QR-код полублоками с явными цветами (чёрное на белом), чтобы тема Terminal не превращала его в негатив."""
    rows = [list(r) for r in code.matrix_iter(scale=1, border=border)]
    if len(rows) % 2:
        rows.append([0] * len(rows[0]))
    lines = []
    for top, bottom in zip(rows[0::2], rows[1::2]):
        # верхняя половина клетки — цвет текста, нижняя — цвет фона: 30/40 чёрный, 97/107 белый
        cells = [f"\x1b[{'30' if t else '97'};{'40' if b else '107'}m▀" for t, b in zip(top, bottom)]
        lines.append("".join(cells) + "\x1b[0m")
    return "\n".join(lines)


def _seconds_left(expires) -> float:
    return (expires - datetime.datetime.now(tz=datetime.timezone.utc)).total_seconds()


async def _password(client) -> bool:
    from telethon import errors

    for _ in range(PASSWORD_TRIES):
        pwd = getpass("  Пароль двухэтапной проверки Telegram (ввод не отображается, Enter — отмена): ")
        if not pwd:
            return False
        try:
            await client.sign_in(password=pwd)
            return True
        except errors.PasswordHashInvalidError:
            print("  Пароль неверный.")
        except errors.FloodError as exc:
            print(f"  Telegram просит подождать {_minutes(exc)} мин перед новой попыткой. Повторять раньше не нужно.")
            return False
    return False


def _minutes(exc) -> int:
    return max(1, round((getattr(exc, "seconds", 0) or 60) / 60))


async def qr_login(client) -> bool:
    """Показывает QR-код и ждёт сканирования до TOTAL_WAIT секунд. True — вход выполнен."""
    from telethon import errors

    print("\n  Вход в Telegram по QR-коду (один раз):")
    print("  1. Откройте Telegram на телефоне → Настройки → Устройства → «Подключить устройство».")
    print("  2. Наведите камеру на код ниже. Код обновляется каждые 30 секунд.")
    try:
        qr = await client.qr_login()
    except errors.SessionPasswordNeededError:  # прошлый вход остановился на пароле — продолжаем с него
        return await _password(client)
    except errors.FloodError as exc:
        print(f"  Telegram просит подождать {_minutes(exc)} мин. Повторять раньше не нужно.")
        return False
    deadline = time.monotonic() + TOTAL_WAIT
    while True:
        show_qr(qr.url)
        left = deadline - time.monotonic()
        ttl = _seconds_left(qr.expires)
        if not MIN_TTL <= ttl <= MAX_TTL:  # часы компьютера сбиты: не верим им, иначе код менялся бы каждую секунду
            ttl = DEFAULT_TTL
        try:
            await qr.wait(timeout=max(1.0, min(left, ttl)))
            return True
        except errors.SessionPasswordNeededError:
            return await _password(client)
        except asyncio.TimeoutError:
            pass
        if time.monotonic() >= deadline:
            print("  Время ожидания вышло.")
            return False
        try:
            await qr.recreate()
            if await _finish_if_done(client, qr):  # отсканировали в последний момент перед обновлением
                return True
        except errors.SessionPasswordNeededError:
            return await _password(client)
        except errors.FloodError as exc:
            print(f"  Telegram просит подождать {_minutes(exc)} мин. Повторять раньше не нужно.")
            return False
        print("  Код обновлён — отсканируйте новый.")


async def _finish_if_done(client, qr) -> bool:
    """Если код успели отсканировать перед обновлением, Telegram вместо нового кода отвечает «вход выполнен»
    или «войдите на другом сервере». Завершаем вход так же, как это делает QRLogin.wait в Telethon."""
    from telethon import functions, types

    resp = getattr(qr, "_resp", None)
    if isinstance(resp, types.auth.LoginTokenMigrateTo):
        await client._switch_dc(resp.dc_id)
        resp = await client(functions.auth.ImportLoginTokenRequest(resp.token))
    if isinstance(resp, types.auth.LoginTokenSuccess):
        await client._on_login(resp.authorization.user)
        return True
    return False


NO = ("n", "н", "no", "нет")


def _remove_qr_png() -> None:
    try:
        QR_PNG.unlink(missing_ok=True)
    except OSError:
        pass


def _confirmed_id() -> int | None:
    return confirmed_id(CONFIRM_PATH)


def _save_confirmed(user_id: int) -> None:
    CONFIRM_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIRM_PATH.write_text(json.dumps({"user_id": int(user_id)}), encoding="utf-8")


def _forget_confirmed() -> None:
    try:
        CONFIRM_PATH.unlink(missing_ok=True)
    except OSError:
        pass


async def _confirm_account(client, me) -> bool:
    """QR-код мог отсканировать не тот человек (например, при показе экрана): спрашиваем, чей это аккаунт.

    Ответ «да» запоминается (только номер аккаунта в Telegram, без имени и телефона). Пока его нет —
    например, окно закрыли на этом вопросе, — вопрос задаётся при каждом запуске.
    """
    from checker_core import mask

    name = " ".join(x for x in (getattr(me, "first_name", None), getattr(me, "last_name", None)) if x) or "без имени"
    user = f"@{me.username}" if getattr(me, "username", None) else "без @логина"
    phone = mask("+" + me.phone) if getattr(me, "phone", None) else "номер скрыт"
    print(f"\n  Вход выполнен в аккаунт: {name}, {user}, {phone}.")
    if input("  Это ваш аккаунт? Enter — да, n — нет (выйти из него): ").strip().lower() in NO:
        _forget_confirmed()
        if await client.log_out():
            print(f"  Вышли из этого аккаунта. Запустите {LAUNCHER} снова и отсканируйте код своим телефоном.")
        else:
            # Хотя бы на этом компьютере чужого входа не останется. На Windows открытый файл не удаляется,
            # поэтому сначала отключаемся (Telethon закрывает файл сессии)
            if client.is_connected():
                await client.disconnect()
            deleted = bool(client.session.delete()) if getattr(client, "session", None) is not None else False
            if deleted:
                print("  Telegram не подтвердил выход. Файл сессии удалён на этом компьютере; владельцу аккаунта "
                      "стоит завершить сеанс в Telegram → Настройки → Устройства.")
            else:
                print(f"  Telegram не подтвердил выход, и файл сессии удалить не удалось. Удалите его вручную: "
                      f"{SESSION_PATH.with_suffix('.session')}")
        return False
    _save_confirmed(me.id)
    print("  Вход сохранён. Больше входить не нужно.")
    return True


async def ensure_login(api_id: int, api_hash: str, interactive: bool = True) -> str:
    """ready — вход есть или выполнен; not_logged_in — входа нет; offline — серверы Telegram недоступны; error — Telegram отказал."""
    from telethon import errors

    _remove_qr_png()  # картинка могла остаться, если окно закрыли крестиком
    client = None
    try:
        # Проверка «вход уже есть» — без получения уведомлений и сообщений аккаунта
        probe = make_client(api_id, api_hash, receive_updates=False)
        try:
            await probe.connect()  # неверные ключи API Telegram отвергает уже здесь
            if await probe.is_user_authorized():
                me = await probe.get_me()
                if _confirmed_id() == getattr(me, "id", None):
                    return "ready"
                if not interactive:
                    return "not_logged_in"
                return "ready" if await _confirm_account(probe, me) else "not_logged_in"
        finally:
            if probe.is_connected():
                await probe.disconnect()
        if not interactive:
            return "not_logged_in"
        client = make_client(api_id, api_hash, receive_updates=True)
        await client.connect()
        while True:
            if await qr_login(client):
                return "ready" if await _confirm_account(client, await client.get_me()) else "not_logged_in"
            answer = input("  Показать новый QR-код? Enter — да, n — продолжить без входа: ").strip().lower()
            if answer in NO:
                return "not_logged_in"
    except NETWORK_ERRORS:
        print(OFFLINE_MESSAGE)
        return "offline"
    except errors.ApiIdInvalidError:
        print("  Telegram не принял ключи API: проверьте TELEGRAM_API_ID и TELEGRAM_API_HASH в checker\\.env.")
        return "error"
    except errors.RPCError as exc:
        print(f"  Telegram отказал во входе: {type(exc).__name__}.")
        return "error"
    finally:
        if client is not None and client.is_connected():
            await client.disconnect()
        _remove_qr_png()


async def logout(api_id: int, api_hash: str) -> None:
    client = make_client(api_id, api_hash, receive_updates=False)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            print("Вход не выполнен — выходить не из чего.")
            return
        _forget_confirmed()
        if await client.log_out():  # Telegram завершает сеанс, Telethon удаляет файл сессии
            print("Сеанс закрыт в Telegram, файл сессии удалён.")
        else:
            print("Telegram не подтвердил выход. Завершите сеанс вручную: Telegram → Настройки → Устройства.")
    finally:
        if client.is_connected():
            await client.disconnect()


def main() -> None:
    quiet_telethon_logs()
    cfg = load_config()
    if not (cfg.api_id and cfg.api_hash):
        sys.exit("Заполните TELEGRAM_API_ID и TELEGRAM_API_HASH в checker\\.env (их выдаёт my.telegram.org → API development tools).")
    if not prepare_app_dir():
        print("Внимание: не удалось ограничить доступ к папке сессии только вашей учётной записью.")
    lock = SingleInstance()
    if not lock.acquire():
        sys.exit("Сначала закройте окно проверщика: вход и проверщик не могут работать одновременно.")
    try:
        if "--logout" in sys.argv[1:]:
            asyncio.run(logout(cfg.api_id, cfg.api_hash))
        else:
            state = asyncio.run(ensure_login(cfg.api_id, cfg.api_hash))
            print({"ready": f"Вход есть. Запускайте {LAUNCHER}", "not_logged_in": "Вход не выполнен.",
                   "offline": "Вход не выполнен: серверы Telegram недоступны.", "error": "Вход не выполнен."}[state])
    except KeyboardInterrupt:
        sys.exit("Отменено.")
    finally:
        lock.release()


if __name__ == "__main__":
    main()
