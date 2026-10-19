"""Тесты проверщика номеров. Запуск из папки checker: python -m unittest discover -s tests -v

В сеть не ходят: Telegram подменён заглушками. Номера — официальные примеры из библиотеки phonenumbers.
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime
import http.client
import io
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import phonenumbers  # noqa: E402
from telethon import errors  # noqa: E402

import checker_core  # noqa: E402
from checker_core import DAY, HOUR, LookupResult, PhoneError, RateLimiter, SingleInstance, TelegramLookup, mask, normalize  # noqa: E402
from server import Keys, LocalServer, build_handler, open_server  # noqa: E402
import login  # noqa: E402

FMT = phonenumbers.PhoneNumberFormat.E164
EXAMPLE = phonenumbers.format_number(phonenumbers.example_number_for_type("TJ", phonenumbers.PhoneNumberType.MOBILE), FMT)
NATIONAL = EXAMPLE[4:]  # без +992
FIXED = phonenumbers.format_number(phonenumbers.example_number_for_type("TJ", phonenumbers.PhoneNumberType.FIXED_LINE), FMT)


class NormalizeTest(unittest.TestCase):
    def test_formats_of_same_number(self):
        for raw in (NATIONAL, "+992 " + NATIONAL, "00992" + NATIONAL, "992" + NATIONAL, "810992" + NATIONAL,
                    f"({NATIONAL[:2]}) {NATIONAL[2:5]}-{NATIONAL[5:7]}-{NATIONAL[7:]}", f"  {EXAMPLE}  "):
            with self.subTest(raw=raw):
                self.assertEqual(normalize(raw, "TJ"), EXAMPLE)

    def test_bad_input(self):
        for raw in ("", "   ", "12345", "abc", "+992 " + NATIONAL[:4], "9" * 40):
            with self.subTest(raw=raw), self.assertRaises(PhoneError):
                normalize(raw, "TJ")

    def test_one_number_only(self):
        for raw in (f"{NATIONAL}, {NATIONAL}", f"{NATIONAL};{NATIONAL}", f"{NATIONAL}/{NATIONAL}", f"+992{NATIONAL}+992"):
            with self.subTest(raw=raw), self.assertRaisesRegex(PhoneError, "один номер"):
                normalize(raw, "TJ")

    def test_landline_and_eight_prefix(self):
        with self.assertRaisesRegex(PhoneError, "городской"):
            normalize(FIXED, "TJ")
        with self.assertRaisesRegex(PhoneError, "8"):
            normalize("8" + NATIONAL, "TJ")

    def test_without_region_needs_plus(self):
        with self.assertRaises(PhoneError):
            normalize(NATIONAL, "")
        self.assertEqual(normalize(EXAMPLE, ""), EXAMPLE)


class MaskTest(unittest.TestCase):
    def test_shows_only_country_code_and_last_two(self):
        m = mask(EXAMPLE)
        self.assertEqual(m, "+992" + "•" * (len(EXAMPLE) - 6) + EXAMPLE[-2:])
        self.assertNotIn(NATIONAL[:-2], m)


class RateLimiterTest(unittest.TestCase):
    def setUp(self):
        self.now = [1_000_000.0]
        self.lim = self.make()

    def make(self, path=None, per_hour=3, per_day=None):
        return RateLimiter(per_hour, 5, per_day, path=path, clock=lambda: self.now[0])

    def at(self, t, lim=None):
        self.now[0] = 1_000_000.0 + t
        return (lim or self.lim).take()

    def test_gap_and_hourly_limit(self):
        self.assertIsNone(self.at(0))
        self.assertEqual(self.at(2), (3, "gap"))
        self.assertIsNone(self.at(6))
        self.assertIsNone(self.at(12))
        self.assertEqual(self.at(18), (HOUR - 18, "hour"))
        self.assertEqual(self.lim.info()["used"], 3)
        self.assertIsNone(self.at(HOUR + 1))  # первая проверка вышла за пределы часа

    def test_daily_limit(self):
        lim = self.make(per_hour=60, per_day=2)
        self.assertIsNone(self.at(0, lim))
        self.assertIsNone(self.at(10, lim))
        self.assertEqual(self.at(20, lim), (DAY - 20, "day"))

    def test_refused_attempt_is_not_counted(self):
        self.at(0)
        self.at(1)
        self.assertEqual(self.lim.info()["used"], 1)

    def test_block_is_kept_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "limit.json"
            lim = self.make(path)
            self.now[0] = 1_000_000.0
            lim.block(120)
            again = self.make(path)
            self.assertEqual(self.at(10, again), (110, "blocked"))
            self.assertEqual(again.info()["blocked_for"], 110)
            self.assertIsNone(self.at(121, again))
            self.assertEqual(set(json.loads(path.read_text(encoding="utf-8"))), {"times", "blocked_until"})
            self.assertFalse(path.with_suffix(".tmp").exists())

    def test_corrupt_file_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "limit.json"
            path.write_text("{oops", encoding="utf-8")
            lim = self.make(path)
            self.assertTrue(lim.load_error)
            wait = self.at(0, lim)
            self.assertEqual(wait[1], "blocked")
            self.assertGreaterEqual(wait[0], HOUR - 1)

    def test_clock_moved_back_neither_resets_nor_sticks(self):
        self.at(0)
        self.now[0] -= DAY  # часы перевели на сутки назад
        self.assertEqual(self.lim.take(), (5, "gap"))  # отметка считается сделанной «сейчас» — счётчик не обнулён
        self.assertEqual(self.lim.info()["used"], 1)
        self.now[0] += HOUR + 1
        self.assertIsNone(self.lim.take())  # и через час освобождается

    def test_infinite_values_in_file_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "limit.json"
            path.write_text('{"times": [Infinity, 1000000.0], "blocked_until": 0}', encoding="utf-8")
            lim = self.make(path)
            self.assertEqual(lim.info()["used"], 1)


class FakeClient:
    def __init__(self, exc=None, users=()):
        self.exc = exc
        self.users = list(users)
        self.requests = []
        self.disconnected = False

    async def __call__(self, request):
        self.requests.append(request)
        if self.exc:
            raise self.exc
        return SimpleNamespace(users=self.users)

    async def disconnect(self):
        self.disconnected = True


class TelegramMappingTest(unittest.TestCase):
    def make(self, client, authorized=True, timeout=30):
        lookup = TelegramLookup(1, "0" * 32, session_path=Path(tempfile.gettempdir()) / "unused", timeout=timeout)

        async def fake_connect():
            return authorized

        lookup._connect = fake_connect
        lookup._client = client
        self.addCleanup(lookup.close)
        return lookup

    def test_found_and_phone_sent_without_plus(self):
        client = FakeClient(users=[object()])
        self.assertEqual(self.make(client).resolve(EXAMPLE).status, "found")
        self.assertEqual(client.requests[0].phone, EXAMPLE[1:])

    def test_not_found_variants(self):
        for exc in (errors.RPCError(None, "PHONE_NOT_OCCUPIED", 400), errors.PhoneNotOccupiedError(request=None)):
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(self.make(FakeClient(exc=exc)).resolve(EXAMPLE).status, "not_found")
        self.assertEqual(self.make(FakeClient(users=[])).resolve(EXAMPLE).status, "not_found")

    def test_flood_variants_are_reported_with_pause(self):
        for exc in (errors.FloodWaitError(request=None, capture=42), errors.FloodPremiumWaitError(request=None, capture=42)):
            with self.subTest(exc=type(exc).__name__):
                client = FakeClient(exc=exc)
                res = self.make(client).resolve(EXAMPLE)
                self.assertEqual((res.status, res.wait, res.pause), ("flood", 42, 42))
                self.assertEqual(len(client.requests), 1)

    def test_revoked_session_sticks_until_restart(self):
        client = FakeClient(exc=errors.AuthKeyUnregisteredError(request=None))
        lookup = self.make(client)
        self.assertEqual(lookup.resolve(EXAMPLE).status, "not_logged_in")
        self.assertEqual(lookup.status(), "not_logged_in")
        self.assertEqual(lookup.resolve(EXAMPLE).status, "not_logged_in")
        self.assertEqual(len(client.requests), 1)  # повторно в Telegram не ходили

    def test_banned_or_frozen_account(self):
        for exc in (errors.UserDeactivatedBanError(request=None), errors.FrozenMethodInvalidError(request=None)):
            with self.subTest(exc=type(exc).__name__):
                lookup = self.make(FakeClient(exc=exc))
                res = lookup.resolve(EXAMPLE)
                self.assertEqual((res.status, res.pause), ("banned", DAY))
                self.assertEqual(lookup.status(), "banned")

    def test_duplicated_key_means_relogin(self):
        lookup = self.make(FakeClient(exc=errors.AuthKeyDuplicatedError(request=None)))
        self.assertEqual(lookup.resolve(EXAMPLE).status, "not_logged_in")

    def test_other_errors(self):
        res = self.make(FakeClient(exc=errors.RPCError(None, "SOMETHING_ELSE", 400))).resolve(EXAMPLE)
        self.assertEqual((res.status, res.detail, res.pause), ("error", "RPCError: SOMETHING_ELSE", HOUR))
        res = self.make(FakeClient(exc=RuntimeError("boom"))).resolve(EXAMPLE)
        self.assertEqual((res.status, res.detail, res.pause), ("error", "RuntimeError", None))

    def test_timeout(self):
        lookup = self.make(FakeClient(), timeout=0.2)

        async def slow(_e164):
            await asyncio.sleep(5)

        lookup._resolve = slow
        res = lookup.resolve(EXAMPLE)
        self.assertEqual((res.status, res.detail), ("error", "Telegram не ответил вовремя"))

    def test_not_logged_in_and_no_keys(self):
        client = FakeClient(users=[object()])
        self.assertEqual(self.make(client, authorized=False).resolve(EXAMPLE).status, "not_logged_in")
        self.assertEqual(client.requests, [])
        bare = TelegramLookup(None, None)
        self.addCleanup(bare.close)
        self.assertEqual(bare.status(), "no_keys")
        self.assertEqual(bare.resolve(EXAMPLE).status, "no_keys")

    def test_blocked_from_start_never_calls_telegram(self):
        client = FakeClient(users=[object()])
        lookup = self.make(client)
        lookup._lost_auth = None
        blocked = TelegramLookup(1, "0" * 32, blocked="not_logged_in")
        blocked._client = client
        self.addCleanup(blocked.close)
        self.assertEqual(blocked.status(), "not_logged_in")
        self.assertEqual(blocked.resolve(EXAMPLE).status, "not_logged_in")
        self.assertEqual(client.requests, [])

    def test_owner_check_on_first_connect(self):
        class OwnerClient:
            def __init__(self, me_id):
                self.me_id, self.connected, self.requests = me_id, False, []

            def is_connected(self):
                return self.connected

            async def connect(self):
                self.connected = True

            async def is_user_authorized(self):
                return True

            async def get_me(self):
                return SimpleNamespace(id=self.me_id)

            async def __call__(self, request):
                self.requests.append(request)
                return SimpleNamespace(users=[object()])

            async def disconnect(self):
                self.connected = False

        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp) / "account_confirmed.json"
            marker.write_text(json.dumps({"user_id": 42}), encoding="utf-8")
            for me_id, path, expected in ((42, marker, "ready"), (7, marker, "not_logged_in"), (42, Path(tmp) / "none.json", "not_logged_in")):
                with self.subTest(me_id=me_id, path=path.name):
                    client = OwnerClient(me_id)
                    lookup = TelegramLookup(1, "0" * 32, check_owner=True, confirm_path=path)
                    lookup._client = client
                    self.addCleanup(lookup.close)
                    self.assertEqual(lookup.status(), expected)
                    res = lookup.resolve(EXAMPLE)
                    self.assertEqual(res.status, "found" if expected == "ready" else "not_logged_in")
                    self.assertEqual(len(client.requests), 1 if expected == "ready" else 0)

    def test_status_offline_when_servers_blocked(self):
        lookup = TelegramLookup(1, "0" * 32)
        self.addCleanup(lookup.close)

        async def blocked():
            raise ConnectionError("Connection to Telegram failed 2 time(s)")

        lookup._connect = blocked
        self.assertEqual(lookup.status(), "offline")

    def test_close_disconnects_in_own_loop_and_stops_thread(self):
        client = FakeClient()
        lookup = TelegramLookup(1, "0" * 32)
        lookup._client = client
        lookup.close()
        self.assertTrue(client.disconnected)
        self.assertFalse(lookup._thread.is_alive())

    def test_client_options(self):
        seen = {}

        class Recorder:
            def __init__(self, session, api_id, api_hash, **kwargs):
                seen.update(kwargs)
                self.session = SimpleNamespace(save_entities=True)

        with tempfile.TemporaryDirectory() as tmp, mock.patch("telethon.TelegramClient", Recorder):
            lookup = TelegramLookup(1, "0" * 32, session_path=Path(tmp) / "s" / "x")
            client = lookup._make_client()
            lookup.close()
        self.assertEqual((seen["flood_sleep_threshold"], seen["request_retries"], seen["receive_updates"]), (0, 0, False))
        self.assertEqual((seen["connection_retries"], seen["timeout"]), (1, 10))  # две быстрые попытки, а не шесть
        self.assertFalse(client.session.save_entities)


class SingleInstanceTest(unittest.TestCase):
    def test_second_copy_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "checker.lock"
            first, second = SingleInstance(path), SingleInstance(path)
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
            first.release()
            self.assertTrue(second.acquire())
            second.release()


class FakeTelegram:
    def __init__(self, state="ready", result="found", pause=None, raise_exc=False, delay=0.0):
        self.state = state
        self.result = result
        self.pause = pause
        self.raise_exc = raise_exc
        self.delay = delay
        self.calls = []

    def status(self):
        return self.state

    def resolve(self, e164):
        self.calls.append(e164)
        if self.delay:
            time.sleep(self.delay)
        if self.raise_exc:
            raise ValueError("boom")
        return LookupResult(self.result, wait=self.pause, pause=self.pause)


KEYS = Keys(token="test-token", launch="test-launch")


class ServerTest(unittest.TestCase):
    def start(self, telegram=None, max_per_hour=20):
        self.telegram = telegram or FakeTelegram()
        self.now = [1_000_000.0]
        self.limiter = RateLimiter(max_per_hour, 0, path=None, clock=lambda: self.now[0])
        page = lambda token: b"<html>" + (token or "LOCKED").encode() + b"</html>"
        self.httpd = LocalServer(("127.0.0.1", 0), build_handler("TJ", KEYS, self.telegram, self.limiter, page))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def call(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": f"127.0.0.1:{self.port}"}
        h.update(headers or {})
        conn.request(method, path, body=body, headers=h)
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        return resp.status, data, resp

    def check(self, phone, token=KEYS.token, ctype="application/json", extra=None):
        headers = {"Content-Type": ctype, "X-Checker-Token": token, "Origin": f"http://127.0.0.1:{self.port}"}
        headers.update(extra or {})
        status, data, _ = self.call("POST", "/api/check", json.dumps({"phone": phone}), headers)
        return status, json.loads(data.decode("utf-8"))

    def test_token_only_through_launch_link(self):
        self.start()
        self.assertEqual(self.call("GET", "/start?k=wrong")[0], 403)
        status, body, resp = self.call("GET", f"/start?k={KEYS.launch}")
        self.assertEqual((status, body), (200, b"<html>" + KEYS.token.encode() + b"</html>"))
        self.assertIn("connect-src 'self'", resp.getheader("Content-Security-Policy"))
        self.assertEqual(resp.getheader("X-Frame-Options"), "DENY")
        self.assertEqual(resp.getheader("Cache-Control"), "no-store")
        self.assertEqual(resp.getheader("Referrer-Policy"), "no-referrer")
        self.assertIsNone(resp.getheader("Set-Cookie"))
        self.assertEqual(self.call("GET", f"/start?k={KEYS.launch}", headers={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.call("GET", f"/start?k={KEYS.launch}", headers={"Sec-Fetch-Site": "same-site"})[0], 403)

    def test_plain_address_never_gets_token(self):
        self.start()
        self.assertEqual(self.call("GET", "/")[1], b"<html>LOCKED</html>")
        self.assertEqual(self.call("GET", "/", headers={"Cookie": "mc=anything"})[1], b"<html>LOCKED</html>")

    def test_host_check(self):
        self.start()
        self.assertEqual(self.call("GET", "/", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.call("GET", "/", headers={"Host": f"localhost:{self.port}"})[0], 200)
        self.assertEqual(self.call("POST", "/api/check", "{}", {"Host": "evil.example"})[0], 403)
        self.assertEqual(self.call("GET", "/nope")[0], 404)
        self.assertEqual(self.call("PUT", "/api/check")[0], 405)

    def test_api_needs_token_and_same_origin(self):
        self.start()
        self.assertEqual(self.check(EXAMPLE, token="")[0], 403)
        self.assertEqual(self.check(EXAMPLE, token="wrong")[0], 403)
        self.assertEqual(self.check(EXAMPLE, extra={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.check(EXAMPLE, extra={"Sec-Fetch-Site": "same-site"})[0], 403)  # другой порт этого компьютера
        self.assertEqual(self.check(EXAMPLE, extra={"Origin": "http://127.0.0.1:9999"})[0], 403)
        self.assertEqual(self.telegram.calls, [])
        self.assertEqual(self.check(EXAMPLE, extra={"Sec-Fetch-Site": "same-origin"})[0], 200)

    def test_request_format(self):
        self.start()
        self.assertEqual(self.check(EXAMPLE, ctype="text/plain")[0], 415)
        self.assertEqual(self.check(EXAMPLE, ctype="application/json; charset=utf-8")[0], 200)
        base = {"Content-Type": "application/json", "X-Checker-Token": KEYS.token}
        self.assertEqual(self.call("POST", "/api/check", "{bad json", base)[0], 400)
        self.assertEqual(self.call("POST", "/api/check", json.dumps({"phone": 123}), base)[0], 400)
        self.assertEqual(self.call("POST", "/api/check", "x" * 2000, base)[0], 413)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.putrequest("POST", "/api/check", skip_host=True)
        for name, value in {"Host": f"127.0.0.1:{self.port}", **base}.items():
            conn.putheader(name, value)
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 413)  # нет Content-Length
        conn.close()

    def test_invalid_phone_message(self):
        self.start()
        status, body = self.check("12345")
        self.assertEqual(status, 400)
        self.assertIn("номер", body["error"].lower())

    def test_check_result_and_log_has_no_full_number(self):
        self.start()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status, body = self.check(NATIONAL)
        self.assertEqual(status, 200)
        self.assertEqual(body["telegram"]["status"], "found")
        self.assertEqual(body["wa_link"], "https://wa.me/" + EXAMPLE[1:])
        self.assertEqual(body["wa_app"], "whatsapp://send?phone=" + EXAMPLE[1:])
        self.assertEqual(body["wa_web"], "https://web.whatsapp.com/send?phone=" + EXAMPLE[1:])
        self.assertEqual(body["masked"], mask(EXAMPLE))
        self.assertNotIn("e164", body)
        self.assertEqual(self.telegram.calls, [EXAMPLE])
        self.assertIn(mask(EXAMPLE), out.getvalue())
        self.assertNotIn(EXAMPLE, out.getvalue())
        self.assertNotIn(NATIONAL, out.getvalue())

    def test_hourly_limit(self):
        self.start(max_per_hour=1)
        self.assertEqual(self.check(EXAMPLE)[0], 200)
        status, body = self.check(EXAMPLE)
        self.assertEqual(status, 429)
        self.assertIn("в час", body["error"])
        self.assertEqual(len(self.telegram.calls), 1)

    def test_flood_pauses_further_checks(self):
        self.start(telegram=FakeTelegram(result="flood", pause=300))
        status, body = self.check(EXAMPLE)
        self.assertEqual((status, body["telegram"]["status"]), (200, "flood"))
        status, body = self.check(EXAMPLE)
        self.assertEqual(status, 429)
        self.assertIn("приостановлены", body["error"])
        self.assertEqual(len(self.telegram.calls), 1)

    def test_only_one_check_at_a_time(self):
        self.start(telegram=FakeTelegram(delay=1.0))
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.check(EXAMPLE))) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        codes = sorted(r[0] for r in results)
        self.assertEqual(codes, [200, 429])
        self.assertIn("другая проверка", [r for r in results if r[0] == 429][0][1]["error"])
        self.assertEqual(len(self.telegram.calls), 1)

    def test_offline_does_not_spend_limit(self):
        self.start(telegram=FakeTelegram(state="offline"), max_per_hour=1)
        for _ in range(2):
            status, body = self.check(EXAMPLE)
            self.assertEqual((status, body["telegram"]["status"]), (200, "offline"))
        self.assertEqual((self.telegram.calls, body["limit"]["used"]), ([], 0))

    def test_not_logged_in_does_not_spend_limit(self):
        self.start(telegram=FakeTelegram(state="not_logged_in"), max_per_hour=1)
        for _ in range(3):
            status, body = self.check(EXAMPLE)
            self.assertEqual((status, body["telegram"]["status"]), (200, "not_logged_in"))
        self.assertEqual(self.telegram.calls, [])
        self.assertEqual(body["limit"]["used"], 0)

    def test_internal_error_gives_500_without_details(self):
        self.start(telegram=FakeTelegram(raise_exc=True))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status, body = self.check(EXAMPLE)
        self.assertEqual(status, 500)
        self.assertIn("Внутренняя ошибка", body["error"])
        self.assertIn("ValueError", out.getvalue())
        self.assertNotIn(EXAMPLE, out.getvalue())

    def test_status_needs_token(self):
        self.start()
        self.assertEqual(self.call("GET", "/api/status")[0], 403)
        status, data, _ = self.call("GET", "/api/status", headers={"X-Checker-Token": KEYS.token})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data)["telegram"], "ready")

    def test_port_is_exclusive(self):
        self.start()
        with self.assertRaises(OSError):
            LocalServer(("127.0.0.1", self.port), build_handler("TJ", KEYS, self.telegram, self.limiter, lambda t: b""))

    def test_busy_port_falls_back_to_next_free(self):
        self.start()  # self.port занят, как SSH-туннелем
        handler = build_handler("TJ", KEYS, self.telegram, self.limiter, lambda t: b"")
        other, port = open_server(self.port, handler)
        self.assertIsNotNone(other)
        self.assertNotEqual(port, self.port)
        self.assertEqual(other.server_address[1], port)
        other.server_close()
        self.assertEqual(open_server(self.port, handler, tries=1), (None, self.port))


class FakeQR:
    def __init__(self, outcomes, expires_in=30, recreate_result=None):
        self.outcomes = list(outcomes)  # timeout | ok | password | api_invalid
        self.url = "tg://login?token=fake"
        self.expires = datetime.datetime.now(tz=datetime.timezone.utc) + datetime.timedelta(seconds=expires_in)
        self.recreated = 0
        self.timeouts = []
        self.recreate_result = recreate_result  # None | "password" | объект ответа Telegram

    async def wait(self, timeout=None):
        self.timeouts.append(timeout)
        outcome = self.outcomes.pop(0)
        if outcome == "timeout":
            raise asyncio.TimeoutError
        if outcome == "password":
            raise errors.SessionPasswordNeededError(request=None)
        if outcome == "api_invalid":
            raise errors.ApiIdInvalidError(request=None)
        return object()

    async def recreate(self):
        self.recreated += 1
        if self.recreate_result == "password":
            raise errors.SessionPasswordNeededError(request=None)
        if self.recreate_result is not None:
            self._resp = self.recreate_result


class FakeLoginClient:
    def __init__(self, qr=None, authorized=False, good_password="right", password_pending=False, logout_ok=True, connect_exc=None):
        self.qr = qr
        self.authorized = authorized
        self.good_password = good_password
        self.password_pending = password_pending
        self.passwords = []
        self.connected = False
        self.qr_asked = False
        self.logged_out = False
        self.on_login_called = False
        self.logout_ok = logout_ok
        self.connect_exc = connect_exc
        self.session = SimpleNamespace(deleted=False, deleted_while_connected=None)

        def delete():
            self.session.deleted = True
            self.session.deleted_while_connected = self.connected  # на Windows открытый файл не удалить
            return True

        self.session.delete = delete

    async def connect(self):
        if self.connect_exc:
            raise self.connect_exc
        self.connected = True

    def is_connected(self):
        return self.connected

    async def disconnect(self):
        self.connected = False

    async def is_user_authorized(self):
        return self.authorized

    async def qr_login(self):
        self.qr_asked = True
        if self.password_pending:
            raise errors.SessionPasswordNeededError(request=None)
        return self.qr

    async def sign_in(self, password):
        self.passwords.append(password)
        if password != self.good_password:
            raise errors.PasswordHashInvalidError(request=None)

    async def get_me(self):
        return SimpleNamespace(id=42, first_name="Тест", last_name=None, username="test_user", phone=EXAMPLE[1:])

    async def log_out(self):
        self.logged_out = True
        self.connected = False
        return self.logout_ok

    async def _on_login(self, user):
        self.on_login_called = True


class LoginFlowTest(unittest.TestCase):
    def run_login(self, client, inputs=(), passwords=(), interactive=True, total_wait=180, confirmed=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        png = Path(tmp.name) / "login_qr.png"
        png.write_bytes(b"old")
        self.confirm = Path(tmp.name) / "account_confirmed.json"
        if confirmed is not None:
            self.confirm.write_text(json.dumps({"user_id": confirmed}), encoding="utf-8")
        with mock.patch.object(login, "make_client", lambda *a, **k: client),                 mock.patch.object(login, "CONFIRM_PATH", self.confirm), \
                mock.patch.object(login, "show_qr", lambda url: None), \
                mock.patch.object(login, "QR_PNG", png), \
                mock.patch.object(login, "TOTAL_WAIT", total_wait), \
                mock.patch.object(login, "getpass", mock.Mock(side_effect=list(passwords))), \
                mock.patch("builtins.input", mock.Mock(side_effect=list(inputs))), \
                contextlib.redirect_stdout(io.StringIO()):
            state = asyncio.run(login.ensure_login(1, "0" * 32, interactive=interactive))
        self.assertFalse(png.exists())  # картинку QR-кода не оставляем
        self.assertFalse(client.connected)
        return state

    def test_already_logged_in_shows_no_qr(self):
        client = FakeLoginClient(authorized=True)
        self.assertEqual(self.run_login(client, confirmed=42), "ready")  # без вопросов: input не вызывался
        self.assertFalse(client.qr_asked)

    def test_unconfirmed_login_is_asked_again(self):
        # окно закрыли на вопросе «Это ваш аккаунт?» — при следующем запуске вопрос повторяется
        client = FakeLoginClient(authorized=True)
        self.assertEqual(self.run_login(client, inputs=[""]), "ready")
        self.assertEqual(json.loads(self.confirm.read_text(encoding="utf-8")), {"user_id": 42})
        client = FakeLoginClient(authorized=True)
        self.assertEqual(self.run_login(client, inputs=["n"], confirmed=7), "not_logged_in")  # подтверждён другой аккаунт
        self.assertTrue(client.logged_out)
        self.assertFalse(self.confirm.exists())

    def test_foreign_account_logout_refused_deletes_session(self):
        client = FakeLoginClient(FakeQR(["ok"]), logout_ok=False)
        self.assertEqual(self.run_login(client, inputs=["n"]), "not_logged_in")
        self.assertTrue(client.session.deleted)
        self.assertFalse(client.session.deleted_while_connected)

    def test_servers_unreachable(self):
        for exc in (ConnectionError("blocked"), TimeoutError()):
            with self.subTest(exc=type(exc).__name__):
                self.assertEqual(self.run_login(FakeLoginClient(connect_exc=exc)), "offline")

    def test_wrong_api_keys_on_connect(self):
        client = FakeLoginClient(connect_exc=errors.ApiIdInvalidError(request=None))
        self.assertEqual(self.run_login(client), "error")

    def test_not_interactive(self):
        client = FakeLoginClient()
        self.assertEqual(self.run_login(client, interactive=False), "not_logged_in")
        self.assertFalse(client.qr_asked)

    def test_expired_code_is_renewed_then_scanned(self):
        qr = FakeQR(["timeout", "ok"])
        self.assertEqual(self.run_login(FakeLoginClient(qr), inputs=[""]), "ready")
        self.assertEqual(qr.recreated, 1)
        self.assertEqual(json.loads(self.confirm.read_text(encoding="utf-8")), {"user_id": 42})

    def test_two_step_password(self):
        client = FakeLoginClient(FakeQR(["password"]))
        self.assertEqual(self.run_login(client, passwords=["wrong", "right"], inputs=[""]), "ready")
        self.assertEqual(client.passwords, ["wrong", "right"])

    def test_password_cancelled_then_declined(self):
        client = FakeLoginClient(FakeQR(["password"]))
        self.assertEqual(self.run_login(client, passwords=[""], inputs=["n"]), "not_logged_in")

    def test_time_runs_out_and_user_declines(self):
        qr = FakeQR(["timeout"])
        self.assertEqual(self.run_login(FakeLoginClient(qr), inputs=["n"], total_wait=0), "not_logged_in")
        self.assertEqual(qr.recreated, 0)

    def test_foreign_account_is_logged_out(self):
        client = FakeLoginClient(FakeQR(["ok"]))
        self.assertEqual(self.run_login(client, inputs=["n"]), "not_logged_in")
        self.assertTrue(client.logged_out)

    def test_login_stuck_on_password_resumes(self):
        # прошлый запуск остановился на пароле: Telegram больше не даёт QR-код, а сразу просит пароль
        client = FakeLoginClient(password_pending=True)
        self.assertEqual(self.run_login(client, passwords=["right"], inputs=[""]), "ready")
        self.assertEqual(client.passwords, ["right"])

    def test_password_needed_on_recreate(self):
        client = FakeLoginClient(FakeQR(["timeout"], recreate_result="password"))
        self.assertEqual(self.run_login(client, passwords=["right"], inputs=[""]), "ready")

    def test_scanned_right_before_renewal(self):
        from telethon import types
        success = types.auth.LoginTokenSuccess(authorization=types.auth.Authorization(user=types.UserEmpty(id=1)))
        client = FakeLoginClient(FakeQR(["timeout"], recreate_result=success))
        self.assertEqual(self.run_login(client, inputs=[""]), "ready")
        self.assertTrue(client.on_login_called)

    def test_wrong_pc_clock_does_not_spin(self):
        for expires_in in (-100, 600):  # часы спешат / отстают
            with self.subTest(expires_in=expires_in):
                qr = FakeQR(["ok"], expires_in=expires_in)
                self.assertEqual(self.run_login(FakeLoginClient(qr), inputs=[""]), "ready")
                self.assertEqual(qr.timeouts, [login.DEFAULT_TTL])

    def test_wrong_api_keys(self):
        self.assertEqual(self.run_login(FakeLoginClient(FakeQR(["api_invalid"]))), "error")

    def test_show_qr_writes_png(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(login, "QR_PNG", Path(tmp) / "q.png"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            login.show_qr("tg://login?token=fake")
            self.assertEqual((Path(tmp) / "q.png").read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        self.assertIn("картинку", out.getvalue())

    def test_login_client_receives_updates_lookup_client_does_not(self):
        seen = []

        class Recorder:
            def __init__(self, session, api_id, api_hash, **kwargs):
                seen.append(kwargs)
                self.session = SimpleNamespace(save_entities=True)

        with mock.patch("telethon.TelegramClient", Recorder):
            client = login.make_client(1, "0" * 32, receive_updates=True)
        self.assertTrue(seen[0]["receive_updates"])  # без уведомлений вход по QR не завершится
        self.assertEqual(seen[0]["connection_retries"], 1)
        self.assertEqual(seen[0]["flood_sleep_threshold"], 0)
        self.assertFalse(client.session.save_entities)


class CrossPlatformTest(unittest.TestCase):
    def test_app_dir_per_platform(self):
        home = Path("/home/u")
        self.assertEqual(checker_core._app_dir("win32", {"LOCALAPPDATA": r"C:\L"}, home), Path(r"C:\L") / "MeridianChecker")
        self.assertEqual(checker_core._app_dir("darwin", {}, home), home / "Library" / "Application Support" / "MeridianChecker")
        self.assertEqual(checker_core._app_dir("linux", {}, home), home / ".local" / "share" / "MeridianChecker")

    def test_launcher_name_matches_platform(self):
        expected = "start_checker.cmd" if sys.platform == "win32" else "start_checker.command"
        self.assertEqual(checker_core.LAUNCHER, expected)

    def test_mac_folder_is_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(checker_core, "APP_DIR", Path(tmp) / "M"), \
                mock.patch.object(checker_core.sys, "platform", "darwin"), \
                mock.patch.object(checker_core.os, "chmod") as chmod:
            self.assertTrue(checker_core.prepare_app_dir())
            self.assertTrue((Path(tmp) / "M" / "session").is_dir())
            expected = sorted(str(Path(tmp) / "M" / x) for x in ("", "session", "state"))
        self.assertEqual(sorted(str(c.args[0]) for c in chmod.call_args_list), expected)
        self.assertTrue(all(c.args[1] == 0o700 for c in chmod.call_args_list))

    def test_mac_qr_has_explicit_colours(self):
        import segno

        code = segno.make("tg://login?token=fake", error="l")
        text = login.ansi_qr(code)
        lines = text.split("\n")
        width = len(list(code.matrix_iter(scale=1, border=2))[0])
        self.assertEqual(len(lines), (width + 1) // 2)
        self.assertTrue(all(line.endswith("\x1b[0m") and line.count("\u2580") == width for line in lines))
        self.assertIn("\x1b[30;", text)  # тёмные клетки — чёрным цветом, независимо от темы Terminal
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(login, "QR_PNG", Path(tmp) / "q.png"), \
                mock.patch.object(login.sys, "platform", "darwin"), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            login.show_qr("tg://login?token=fake")
        self.assertIn("\x1b[", out.getvalue())

    def test_page_tells_launcher_name(self):
        import server

        page = server.render_page(None).decode("utf-8")
        self.assertIn(json.dumps({"locked": True, "launcher": checker_core.LAUNCHER}), page)


if __name__ == "__main__":
    unittest.main()
