"""
Regression tests for the follow-up QA-audit fix batch (SEC-13, BUG-6, UX-7, UX-9, MED-8).

Covers the items left open after the first QA commit:
  * SEC-13  — global concurrency semaphore middleware
  * BUG-6   — alert admins when an external webhook is still configured
  * UX-7    — "Отменить запись" button on the 2h reminder keyboard
  * UX-9    — /admin shown in /help for admins
  * MED-8   — reject dangerous characters in master/service names

Run: pytest tests/test_audit_followup.py -v
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import middleware  # noqa: E402
import bot as bot_module  # noqa: E402
import keyboards  # noqa: E402
import handlers.admin as admin_module  # noqa: E402
import handlers.start as start_module  # noqa: E402
import config  # noqa: E402


# =============================================================================
# SEC-13: GlobalConcurrencyMiddleware bounds concurrent handler executions.
# =============================================================================
class TestGlobalConcurrency:
    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrency(self):
        limit = 3
        mw = middleware.GlobalConcurrencyMiddleware(limit=limit)
        active = 0
        max_active = 0

        async def handler(event, data):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.02)
            active -= 1
            return "ok"

        # Fire 20 updates concurrently — only `limit` may run at once.
        await asyncio.gather(*[mw(handler, None, {}) for _ in range(20)])
        assert max_active <= limit, f"concurrency {max_active} exceeded limit {limit}"
        assert max_active == limit, "semaphore should have been saturated"

    @pytest.mark.asyncio
    async def test_handler_result_passes_through(self):
        mw = middleware.GlobalConcurrencyMiddleware(limit=5)

        async def handler(event, data):
            return "payload"

        result = await mw(handler, None, {})
        assert result == "payload"


# =============================================================================
# BUG-6: detect external webhook and alert admins.
# =============================================================================
class _FakeWebhook:
    def __init__(self, url):
        self.url = url


class _FakeBot:
    def __init__(self, url):
        self._wh = _FakeWebhook(url)
        self.sent = []

    async def get_webhook_info(self):
        return self._wh

    async def send_message(self, chat_id, text, parse_mode=None):
        self.sent.append((chat_id, text))


class TestWebhookConflict:
    @pytest.mark.asyncio
    async def test_alerts_admins_when_webhook_set(self, monkeypatch):
        bot = _FakeBot("https://evil.example/hook")
        monkeypatch.setattr(config, "ADMIN_IDS", [111, 222])
        url = await bot_module.check_webhook_conflict(bot)
        assert url == "https://evil.example/hook"
        assert len(bot.sent) == 2
        combined = (bot.sent[0][1] + bot.sent[1][1]).lower()
        assert "webhook" in combined

    @pytest.mark.asyncio
    async def test_returns_none_when_no_webhook(self, monkeypatch):
        bot = _FakeBot("")
        monkeypatch.setattr(config, "ADMIN_IDS", [111])
        url = await bot_module.check_webhook_conflict(bot)
        assert url is None
        assert bot.sent == []

    @pytest.mark.asyncio
    async def test_swallows_getwebhook_errors(self, monkeypatch):
        class _BrokenBot:
            async def get_webhook_info(self):
                raise RuntimeError("network down")

        monkeypatch.setattr(config, "ADMIN_IDS", [111])
        # Must not raise — startup must not break on a webhook-info failure.
        result = await bot_module.check_webhook_conflict(_BrokenBot())
        assert result is None


# =============================================================================
# UX-7: 2h reminder keyboard gains a cancel button.
# =============================================================================
class TestRemind2hKeyboard:
    def test_has_confirm_and_cancel(self):
        kb = keyboards.remind_2h_kb("abc123")
        cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
        assert "remind_confirm:abc123" in cbs
        assert "remind_cancel:abc123" in cbs
        assert len(cbs) == 2


# =============================================================================
# MED-8: master/service name validation.
# =============================================================================
class TestEntityNameValidation:
    def test_accepts_normal_name(self):
        ok, err = admin_module.is_safe_entity_name("Алибек", max_bytes=40)
        assert ok is True and err == ""

    def test_rejects_html_chars(self):
        ok, _ = admin_module.is_safe_entity_name('<a href="x">', max_bytes=40)
        assert ok is False
        ok2, _ = admin_module.is_safe_entity_name('Name"quoted', max_bytes=40)
        assert ok2 is False

    def test_rejects_empty_and_whitespace(self):
        assert admin_module.is_safe_entity_name("   ", max_bytes=40)[0] is False
        assert admin_module.is_safe_entity_name("", max_bytes=40)[0] is False

    def test_rejects_overlong(self):
        long = "я" * 100  # each char ~2 bytes -> ~200 bytes
        assert admin_module.is_safe_entity_name(long, max_bytes=40)[0] is False

    def test_service_max_bytes_enforced(self):
        ok, _ = admin_module.is_safe_entity_name("x" * 40, max_bytes=35)
        assert ok is False
        ok2, _ = admin_module.is_safe_entity_name("x" * 30, max_bytes=35)
        assert ok2 is True


# =============================================================================
# UX-9: /help surfaces /admin for admins.
# =============================================================================
class TestAdminHelp:
    @pytest.mark.asyncio
    async def test_admin_sees_admin_command(self, monkeypatch):
        captured = {}

        async def fake_send(bot, chat_id, text, **kwargs):
            captured["text"] = text

        monkeypatch.setattr(start_module, "send_with_retry", fake_send)
        monkeypatch.setattr(config, "ADMIN_IDS", [999])

        class _User:
            id = 999

        class _Chat:
            id = 1

        class _Msg:
            text = "/help"
            bot = None
            chat = _Chat()
            from_user = _User()

        await start_module.cmd_help(_Msg())
        assert "/admin" in captured["text"]

    @pytest.mark.asyncio
    async def test_non_admin_does_not_see_admin_command(self, monkeypatch):
        captured = {}

        async def fake_send(bot, chat_id, text, **kwargs):
            captured["text"] = text

        monkeypatch.setattr(start_module, "send_with_retry", fake_send)
        monkeypatch.setattr(config, "ADMIN_IDS", [999])  # 123 is NOT an admin

        class _User:
            id = 123

        class _Chat:
            id = 1

        class _Msg:
            text = "/help"
            bot = None
            chat = _Chat()
            from_user = _User()

        await start_module.cmd_help(_Msg())
        assert "/admin" not in captured["text"]
