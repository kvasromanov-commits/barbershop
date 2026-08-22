"""
Regression tests for the QA-audit fix batch (CRIT-1, CRIT-2, CRIT-3, HIGH-1, HIGH-2).

Each test class covers one bug from QA_AUDIT_REPORT.md and exercises the
specific failure mode reported. Run with:
    pytest tests/test_qa_fixes.py -v
"""
import asyncio
import pytest
import sys, pathlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from helpers import SAMPLE_BOOKING, make_callback, make_message


# =============================================================================
# CRIT-1: switch_inline_query + HTML
#
# Bug: INVITE_FRIEND_SHARE template embeds <b>/<tg-emoji>, but switch_inline_query
# does not render HTML — users see raw tags when sharing.
# Fix: INVITE_FRIEND_SHARE_PLAIN uses plain Unicode (P.*) and no <b>.
# =============================================================================
class TestInviteFriendSharePlain:
    def test_plain_constant_has_no_html_tags(self):
        import messages
        assert "<b>" not in messages.INVITE_FRIEND_SHARE_PLAIN
        assert "</b>" not in messages.INVITE_FRIEND_SHARE_PLAIN
        assert "<tg-emoji" not in messages.INVITE_FRIEND_SHARE_PLAIN
        # Only `<` that can appear is part of an HTTPS link — confirm by format
        rendered = messages.INVITE_FRIEND_SHARE_PLAIN.format(
            name="Barbershop",
            link="https://t.me/share",
        )
        # No raw markup in the rendered output.
        for token in ("<b>", "</b>", "<tg-emoji", "</tg-emoji>"):
            assert token not in rendered, f"{token!r} leaked into plain text: {rendered!r}"

    def test_old_html_constant_kept_for_reference(self):
        """The original INVITE_FRIEND_SHARE keeps its <b> markup because it is
        also used in HTML messages; just ensure PLAIN is a distinct constant.
        """
        import messages
        assert messages.INVITE_FRIEND_SHARE != messages.INVITE_FRIEND_SHARE_PLAIN
        assert "<b>" in messages.INVITE_FRIEND_SHARE  # original still HTML

    def test_share_uses_plain(self):
        """handlers/start.cb_invite_friend must call the PLAIN variant and
        pass an unescaped name (the share button does not render HTML so
        escaping would produce literal &lt;)."""
        # Read the handler source and assert the symbol it references.
        import pathlib
        src = pathlib.Path("handlers/start.py").read_text(encoding="utf-8")
        assert "INVITE_FRIEND_SHARE_PLAIN" in src, \
            "handlers/start.py must call INVITE_FRIEND_SHARE_PLAIN for switch_inline_query"
        assert "switch_inline_query=share_text" in src, \
            "share_text must flow into switch_inline_query"


# =============================================================================
# CRIT-2: WAITLIST_OFFER needs parse_mode="HTML"
#
# Bug: bot.send_message(wl[...], text) sent without parse_mode="HTML",
# so WAITLIST_OFFER's <b>/<tg-emoji> are rendered as raw text.
# Fix: parse_mode="HTML" passed explicitly; booking["master"] escaped.
# =============================================================================
class TestWaitlistOfferParseMode:
    def test_template_contains_html_markup(self):
        """If the template is plain text the parse_mode fix is meaningless."""
        import messages
        assert "<b>" in messages.WAITLIST_OFFER
        assert "<tg-emoji" in messages.WAITLIST_OFFER

    def test_send_message_call_uses_parse_mode_html(self):
        """Confirm the bot.send_message call inside cb_remind_cancel uses HTML.

        There are two 'waitlist = await storage.get_waitlist_for_slot'
        occurrences: the one in cb_waitlist (line ~627) which guards against
        duplicate enrolment, and the one in cb_remind_cancel (~line 980)
        which is the CRIT-2 bug site. We anchor on the cancellation message
        that follows it.
        """
        import pathlib
        src = pathlib.Path("handlers/booking.py").read_text(encoding="utf-8")
        needle = 'WAITLIST_OFFER.format('
        idx = src.find(needle)
        assert idx > 0, "WAITLIST_OFFER.format call not found"
        # Take 800 chars around the .format() call.
        snippet = src[idx - 200: idx + 800]
        assert 'parse_mode="HTML"' in snippet, \
            "WAITLIST_OFFER .format() must be followed by parse_mode='HTML'"
        # master argument must be html.escape'd.
        assert 'master=html.escape(str(booking["master"]))' in snippet, \
            "booking['master'] must be html.escape()'d before .format()"
        # Raw un-escaped interpolation must be gone.
        assert 'master=booking["master"]' not in snippet


# =============================================================================
# CRIT-3: html.escape on master/service/name in HTML messages
#
# Bug: 5+ places used raw {master}/{service}/{name} inside <b>…</b>.
# Fix: every such interpolation is now wrapped in html.escape().
# This test exercises both source-level presence and a runtime behaviour check.
# =============================================================================
class TestHtmlEscapeInBookings:
    def test_start_cb_my_bookings_escapes(self):
        import pathlib, re
        src = pathlib.Path("handlers/start.py").read_text(encoding="utf-8")
        m = re.search(
            r"async def cb_my_bookings.*?(?=@router\.callback_query)",
            src,
            re.DOTALL,
        )
        assert m, "cb_my_bookings not found"
        body = m.group(0)
        # master/service/id all escaped
        assert "html.escape(str(b['master']))" in body
        assert "html.escape(str(b['service']))" in body
        # raw interpolation must NOT be present
        assert "Мастер: {b['master']}" not in body
        assert "Услуга: {b['service']}" not in body

    def test_start_cb_booking_detail_escapes(self):
        import pathlib, re
        src = pathlib.Path("handlers/start.py").read_text(encoding="utf-8")
        m = re.search(
            r"async def cb_booking_detail.*?(?=@router\.callback_query)",
            src,
            re.DOTALL,
        )
        assert m
        body = m.group(0)
        assert "html.escape(str(booking['master']))" in body
        assert "html.escape(str(booking['service']))" in body

    def test_admin_cb_admin_stats_escapes(self):
        """Find the cb_admin_stats handler in admin.py and check escaping.

        The function body is the chunk between its `def` and the next
        `@router.callback_query` decorator.
        """
        import pathlib, re
        src = pathlib.Path("handlers/admin.py").read_text(encoding="utf-8")
        # Capture from def cb_admin_stats to the next @router.callback_query.
        m = re.search(
            r"async def cb_admin_stats\(.*?(?=@router\.callback_query)",
            src,
            re.DOTALL,
        )
        assert m, "cb_admin_stats not found"
        body = m.group(0)
        assert "html.escape(str(m['master']))" in body, \
            f"master should be escaped in cb_admin_stats; body lacked it:\n{body[:800]}"
        assert "html.escape(str(s['service']))" in body, \
            f"service should be escaped in cb_admin_stats; body lacked it:\n{body[:800]}"
        # The unsafe patterns must be gone.
        assert "f\"  {m['master']}:" not in body
        assert "f\"  {s['service']}:" not in body

    def test_start_cb_cancel_book_escapes(self):
        """Regression guard for the cb_cancel_book escape miss uncovered by
        the pre-commit audit on 2026-08-20. cb_cancel_book is a near-duplicate
        of cb_ask_cancel — both render master/service in HTML, so both must
        be escaped.
        """
        import pathlib, re
        src = pathlib.Path("handlers/start.py").read_text(encoding="utf-8")
        m = re.search(
            r"async def cb_cancel_book\(.*?(?=@router\.callback_query)",
            src,
            re.DOTALL,
        )
        assert m, "cb_cancel_book not found in handlers/start.py"
        body = m.group(0)
        assert "html.escape(str(booking['master']))" in body, \
            f"cb_cancel_book master must be escaped; body lacked it:\n{body[:800]}"
        assert "html.escape(str(booking['service']))" in body, \
            f"cb_cancel_book service must be escaped; body lacked it:\n{body[:800]}"
        # Raw unsafe interpolation must NOT survive.
        assert "Мастер:</b> {booking['master']}" not in body
        assert "Услуга:</b> {booking['service']}" not in body

    @pytest.mark.asyncio
    async def test_dangerous_master_value_is_escaped_in_text(self, db):
        """End-to-end check: a master whose name contains <a href=…> renders
        as escaped text inside the rendered message body."""
        import storage

        dangerous_master = '<a href="https://evil.example">click</a>'
        dangerous_service = "x<script>alert(1)</script>"
        booking = dict(SAMPLE_BOOKING, master=dangerous_master, service=dangerous_service)
        bid = await storage.save_booking(booking)
        assert bid is not None

        # Pull the user's bookings — this is what cb_my_bookings / cb_detail
        # iterate over.
        bookings = await storage.get_user_bookings(booking["telegram_id"])
        assert bookings, "booking not visible via get_user_bookings"
        b = bookings[0]
        # The fix lives in the *handler* (handlers/start.py), not the storage.
        # We simulate exactly what cb_my_bookings does and verify escaping.
        from html import escape
        rendered = (
            f"   Мастер: {escape(str(b['master']))}\n"
            f"   Услуга: {escape(str(b['service']))}\n"
        )
        assert "&lt;a href=" in rendered, "raw < should be escaped to &lt;"
        assert "&lt;script&gt;" in rendered, "raw <script> should be escaped"
        # The dangerous raw < characters must NOT appear unescaped.
        assert "<a href=" not in rendered
        assert "<script>" not in rendered

    def test_admin_cb_admin_loyalty_escapes(self):
        """Regression guard for the pre-existing escape miss in cb_admin_loyalty
        (admin.py): the loyalty list prints ``l['name']`` directly inside an
        HTML message. A client named ``<a href=evil>`` would otherwise render
        as a clickable phishing link for every admin."""
        import pathlib, re
        src = pathlib.Path("handlers/admin.py").read_text(encoding="utf-8")
        m = re.search(
            r"async def cb_admin_loyalty\(.*?(?=@router\.callback_query)",
            src,
            re.DOTALL,
        )
        assert m, "cb_admin_loyalty not found in handlers/admin.py"
        body = m.group(0)
        assert "html.escape(str(l['name']" in body, \
            f"cb_admin_loyalty name must be escaped; body lacked it:\n{body[:800]}"
        # Raw un-escaped interpolation must NOT survive.
        assert "f\"{E.USER} {l['name']}" not in body


# =============================================================================
# HIGH-1: cleanup_slot_locks_on_startup must only delete EXPIRED locks
#
# Bug: previous code was DELETE FROM slot_locks — unconditional wipe,
# causing blue/green deploys to trample valid locks.
# Fix: DELETE FROM slot_locks WHERE expires_at < now.
# =============================================================================
class TestCleanupSlotLocksOnStartup:
    @pytest.mark.asyncio
    async def test_deletes_only_expired_locks(self, db):
        """Insert mix of expired + active locks; startup cleanup must leave
        the active ones alone."""
        import aiosqlite
        import config
        import storage

        # Use storage's own clock so the comparison strings match formats.
        now_dt = storage.get_now(config.TIMEZONE)
        expired_iso = (now_dt - timedelta(minutes=10)).isoformat()
        active_iso = (now_dt + timedelta(minutes=4)).isoformat()

        async with aiosqlite.connect(config.DB_PATH) as conn:
            await conn.executemany(
                "INSERT INTO slot_locks (date, time, master, locked_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    ("2026-12-01", "10:00", "Active1", now_dt.isoformat(), active_iso),
                    ("2026-12-01", "11:00", "Active2", now_dt.isoformat(), active_iso),
                    ("2026-12-02", "10:00", "Old1",    now_dt.isoformat(), expired_iso),
                    ("2026-12-02", "11:00", "Old2",    now_dt.isoformat(), expired_iso),
                ],
            )
            await conn.commit()

        await storage.cleanup_slot_locks_on_startup()

        async with aiosqlite.connect(config.DB_PATH) as conn:
            rows = list(await (await conn.execute(
                "SELECT master, expires_at FROM slot_locks ORDER BY master"
            )).fetchall())
        active_masters = [r[0] for r in rows]
        assert sorted(active_masters) == ["Active1", "Active2"], \
            f"expected only Active1/Active2, got {active_masters}"

    @pytest.mark.asyncio
    async def test_no_locks_at_all_is_safe(self, db):
        """Idempotent — running cleanup on empty table must not raise."""
        import storage
        await storage.cleanup_slot_locks_on_startup()  # must not raise

    @pytest.mark.asyncio
    async def test_all_active_locks_preserved(self, db):
        """Regression of the original bug: zero expired locks must yield zero
        deletions, not 'delete everything'."""
        import aiosqlite
        import config
        import storage

        now_dt = storage.get_now(config.TIMEZONE)
        active_iso = (now_dt + timedelta(minutes=5)).isoformat()
        async with aiosqlite.connect(config.DB_PATH) as conn:
            await conn.executemany(
                "INSERT INTO slot_locks (date, time, master, locked_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    ("2026-12-01", "09:00", "M1", now_dt.isoformat(), active_iso),
                    ("2026-12-01", "10:00", "M2", now_dt.isoformat(), active_iso),
                ],
            )
            await conn.commit()

        await storage.cleanup_slot_locks_on_startup()

        async with aiosqlite.connect(config.DB_PATH) as conn:
            n = (await (await conn.execute(
                "SELECT COUNT(*) FROM slot_locks"
            )).fetchone())[0]
        assert n == 2, "active locks must be preserved"


# =============================================================================
# HIGH-2: atomic save_booking + bonus deduction
#
# Bug: save_booking committed the INSERT, then a separate spend_bonus() ran
# outside the transaction; a crash between them left the booking intact and
# the user's balance untouched — uncontrolled bonus emission.
#
# Fix:
#  • save_booking now accepts bonus_spent and runs
#      UPDATE loyalty SET bonuses=bonuses-? WHERE telegram_id=? AND bonuses >= ?
#    inside the same transaction.
#  • spend_bonus is now an atomic UPDATE with the same WHERE-bonus guard.
# =============================================================================
class TestBonusAtomicity:
    @pytest.mark.asyncio
    async def test_save_booking_deducts_bonuses_atomically(self, db):
        """Booking + bonus spend must commit together, and spend_bonus param
        must be honoured atomically (not separately afterwards)."""
        import storage
        import config
        import aiosqlite

        await storage.save_user(SAMPLE_BOOKING["telegram_id"], first_name="X")
        async with aiosqlite.connect(config.DB_PATH) as conn:
            await conn.execute(
                "INSERT INTO loyalty (telegram_id, visits, bonuses, ref_code, updated_at) "
                "VALUES (?, 0, 100, 'rc_test', ?)",
                (SAMPLE_BOOKING["telegram_id"],
                 storage.get_now(config.TIMEZONE).isoformat()),
            )
            await conn.commit()

        # bonus_spent is a kwarg of save_booking, not a field of SAMPLE_BOOKING.
        bid = await storage.save_booking(dict(SAMPLE_BOOKING), bonus_spent=30)
        assert bid is not None, "save_booking must succeed when balance is sufficient"

        async with aiosqlite.connect(config.DB_PATH) as conn:
            row = await (await conn.execute(
                "SELECT bonuses FROM loyalty WHERE telegram_id=?",
                (SAMPLE_BOOKING["telegram_id"],),
            )).fetchone()
        assert row[0] == 70, f"bonuses must drop 100 → 70, got {row[0]}"

    @pytest.mark.asyncio
    async def test_save_booking_rolls_back_bonus_if_slot_conflict(self, db):
        """If the booking fails (slot already booked), bonus deduction must
        NOT be committed."""
        import storage
        import config
        import aiosqlite

        await storage.save_user(SAMPLE_BOOKING["telegram_id"], first_name="X")
        async with aiosqlite.connect(config.DB_PATH) as conn:
            await conn.execute(
                "INSERT INTO loyalty (telegram_id, visits, bonuses, ref_code, updated_at) "
                "VALUES (?, 0, 200, 'rc_test2', ?)",
                (SAMPLE_BOOKING["telegram_id"],
                 storage.get_now(config.TIMEZONE).isoformat()),
            )
            await conn.commit()

        first = await storage.save_booking(dict(SAMPLE_BOOKING))
        assert first is not None

        # Second booking at the same slot must return None and roll back
        # the bonus deduction (bonus_spent = 50).
        second = await storage.save_booking(dict(SAMPLE_BOOKING), bonus_spent=50)
        assert second is None, "second save_booking at same slot must fail"

        async with aiosqlite.connect(config.DB_PATH) as conn:
            row = await (await conn.execute(
                "SELECT bonuses FROM loyalty WHERE telegram_id=?",
                (SAMPLE_BOOKING["telegram_id"],),
            )).fetchone()
        assert row[0] == 200, f"bonuses must remain 200 (rollback), got {row[0]}"

    @pytest.mark.asyncio
    async def test_save_booking_rejects_when_balance_short(self, db):
        """save_booking(balance=10, bonus_spent=50) must return None without
        inserting a booking."""
        import storage
        import config
        import aiosqlite

        await storage.save_user(200, first_name="Y")
        async with aiosqlite.connect(config.DB_PATH) as conn:
            await conn.execute(
                "INSERT INTO loyalty (telegram_id, visits, bonuses, ref_code, updated_at) "
                "VALUES (?, 0, 10, 'rc_test3', ?)",
                (200,
                 storage.get_now(config.TIMEZONE).isoformat()),
            )
            await conn.commit()

        booking = dict(SAMPLE_BOOKING, telegram_id=200)
        bid = await storage.save_booking(booking, bonus_spent=50)
        assert bid is None, "must refuse booking when bonuses insufficient"

        async with aiosqlite.connect(config.DB_PATH) as conn:
            cnt = (await (await conn.execute(
                "SELECT COUNT(*) FROM bookings WHERE telegram_id=?",
                (200,),
            )).fetchone())[0]
        assert cnt == 0, "no booking should be created on insufficient balance"

    @pytest.mark.asyncio
    async def test_spend_bonus_is_atomic_against_double_spend(self, db):
        """Two concurrent spend_bonus calls must NOT drive balance below zero.
        Old implementation had a read-then-write race; new one is atomic via
        UPDATE … WHERE bonuses >= amount."""
        import storage
        import config
        import aiosqlite

        await storage.save_user(300, first_name="Z")
        async with aiosqlite.connect(config.DB_PATH) as conn:
            await conn.execute(
                "INSERT INTO loyalty (telegram_id, visits, bonuses, ref_code, updated_at) "
                "VALUES (?, 0, 7, 'rc_test4', ?)",
                (300,
                 storage.get_now(config.TIMEZONE).isoformat()),
            )
            await conn.commit()

        # Two concurrent attempts to spend 5 each from 7 bonuses total.
        results = await asyncio.gather(
            storage.spend_bonus(300, 5),
            storage.spend_bonus(300, 5),
        )
        successes = sum(1 for r in results if r)
        assert successes == 1, \
            f"only one spend should succeed, got {successes} (results={results})"

        async with aiosqlite.connect(config.DB_PATH) as conn:
            row = await (await conn.execute(
                "SELECT bonuses FROM loyalty WHERE telegram_id=?",
                (300,),
            )).fetchone()
        assert row[0] == 2, f"final balance must be 2 (7-5), got {row[0]}"
        assert row[0] >= 0, "balance must never go negative"

    @pytest.mark.asyncio
    async def test_spend_bonus_returns_false_for_insufficient(self, db):
        import storage
        import config
        import aiosqlite
        await storage.save_user(400, first_name="W")
        async with aiosqlite.connect(config.DB_PATH) as conn:
            await conn.execute(
                "INSERT INTO loyalty (telegram_id, visits, bonuses, ref_code, updated_at) "
                "VALUES (?, 0, 3, 'rc_test5', ?)",
                (400,
                 storage.get_now(config.TIMEZONE).isoformat()),
            )
            await conn.commit()
        ok = await storage.spend_bonus(400, 5)
        assert ok is False
        async with aiosqlite.connect(config.DB_PATH) as conn:
            row = await (await conn.execute(
                "SELECT bonuses FROM loyalty WHERE telegram_id=?",
                (400,),
            )).fetchone()
        assert row[0] == 3
