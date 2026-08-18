"""
Tests for all changes applied during the audit fix session.

Fix 1  handlers/start.py :: handle_contact  (MED-001: manual phone input)
Fix 2  storage.py :: get_upcoming_bookings_paged  (HIGH-003: SQL pagination)
Fix 3  handlers/admin.py :: cb_admin_cancel_booking  (CRIT-004: waitlist on admin cancel)
Fix 4  handlers/admin.py :: _show_admin_bookings_page  (HIGH-003: uses SQL pagination now)

Run: cd barbershop_deploy && pytest tests/test_new_fixes.py -v
"""
import pytest
import sys
import pathlib
import ast
import py_compile
sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))

from unittest.mock import AsyncMock, MagicMock, patch
from helpers import make_message, make_callback, make_fsm, SAMPLE_BOOKING


def _booking(overrides=None):
    b = dict(SAMPLE_BOOKING)
    if overrides:
        b.update(overrides)
    return b


async def _save_n(storage_module, n, base_hour=10):
    """Save n bookings at consecutive half-hour slots, return list of ids."""
    ids = []
    for i in range(n):
        h = base_hour + i // 2
        m = "30" if i % 2 else "00"
        bid = await storage_module.save_booking(
            _booking({"time": f"{h:02d}:{m}", "telegram_id": 111 + i})
        )
        ids.append(bid)
    return ids


# =============================================================================
# FIX 2  storage.get_upcoming_bookings_paged  (HIGH-003)
# =============================================================================

class TestGetUpcomingBookingsPaged:
    """SQL-level pagination replaces Python-side slicing (HIGH-003)."""

    async def test_empty_db_returns_empty_list_and_zero(self, db):
        import storage
        rows, total = await storage.get_upcoming_bookings_paged()
        assert rows == []
        assert total == 0

    async def test_single_booking_page0(self, db):
        import storage
        await storage.save_booking(SAMPLE_BOOKING)
        rows, total = await storage.get_upcoming_bookings_paged(offset=0, limit=5)
        assert total == 1
        assert len(rows) == 1

    async def test_total_independent_of_limit(self, db):
        import storage
        await _save_n(storage, 7)
        _, total = await storage.get_upcoming_bookings_paged(offset=0, limit=2)
        assert total == 7

    async def test_limit_respected(self, db):
        import storage
        await _save_n(storage, 8)
        rows, _ = await storage.get_upcoming_bookings_paged(offset=0, limit=5)
        assert len(rows) == 5

    async def test_offset_skips_rows(self, db):
        import storage
        ids = await _save_n(storage, 6)
        p0, _ = await storage.get_upcoming_bookings_paged(offset=0, limit=3)
        p1, _ = await storage.get_upcoming_bookings_paged(offset=3, limit=3)
        ids0 = {r["id"] for r in p0}
        ids1 = {r["id"] for r in p1}
        assert ids0.isdisjoint(ids1), "Pages must not overlap"
        assert ids0 | ids1 == set(ids), "Union of both pages == all bookings"

    async def test_offset_beyond_total_empty_rows(self, db):
        import storage
        await _save_n(storage, 3)
        rows, total = await storage.get_upcoming_bookings_paged(offset=100, limit=5)
        assert rows == []
        assert total == 3

    async def test_cancelled_excluded(self, db):
        import storage
        bid = await storage.save_booking(SAMPLE_BOOKING)
        await storage.cancel_booking(bid, telegram_id=SAMPLE_BOOKING["telegram_id"])
        rows, total = await storage.get_upcoming_bookings_paged()
        assert total == 0
        assert rows == []

    async def test_completed_excluded(self, db):
        import storage
        bid = await storage.save_booking(SAMPLE_BOOKING)
        await storage.complete_booking(bid)
        rows, total = await storage.get_upcoming_bookings_paged()
        assert total == 0
        assert rows == []

    async def test_only_active_counted(self, db):
        import storage
        ids = await _save_n(storage, 5)
        await storage.cancel_booking(ids[0], telegram_id=111)
        await storage.cancel_booking(ids[1], telegram_id=112)
        _, total = await storage.get_upcoming_bookings_paged()
        assert total == 3

    async def test_results_sorted_by_date_time(self, db):
        import storage
        await storage.save_booking(_booking({"date": "2026-12-09", "time": "14:00", "telegram_id": 201}))
        await storage.save_booking(_booking({"date": "2026-12-07", "time": "10:00", "telegram_id": 202}))
        await storage.save_booking(_booking({"date": "2026-12-07", "time": "09:00", "telegram_id": 203}))
        rows, _ = await storage.get_upcoming_bookings_paged(offset=0, limit=10)
        dt = [(r["date"], r["time"]) for r in rows]
        assert dt == sorted(dt)

    async def test_default_limit_is_5(self, db):
        import storage
        await _save_n(storage, 10)
        rows, _ = await storage.get_upcoming_bookings_paged()
        assert len(rows) == 5

    async def test_limit_larger_than_total(self, db):
        import storage
        await _save_n(storage, 3)
        rows, total = await storage.get_upcoming_bookings_paged(offset=0, limit=100)
        assert total == 3
        assert len(rows) == 3

    async def test_total_is_int(self, db):
        import storage
        await storage.save_booking(SAMPLE_BOOKING)
        _, total = await storage.get_upcoming_bookings_paged()
        assert isinstance(total, int)

    async def test_two_pages_cover_all(self, db):
        import storage
        ids = await _save_n(storage, 10)
        p0, total = await storage.get_upcoming_bookings_paged(offset=0, limit=5)
        p1, _     = await storage.get_upcoming_bookings_paged(offset=5, limit=5)
        assert total == 10
        assert {r["id"] for r in p0} | {r["id"] for r in p1} == set(ids)

    async def test_page_3_with_12_bookings(self, db):
        import storage
        ids = await _save_n(storage, 12)
        p2, total = await storage.get_upcoming_bookings_paged(offset=10, limit=5)
        assert total == 12
        assert len(p2) == 2  # only 2 left on 3rd page


# =============================================================================
# FIX 1  handlers/start.py :: handle_contact  (MED-001)
# =============================================================================

class TestHandleContactPhoneValidation:
    """MED-001: users may now type their phone number manually."""

    def _msg(self, text=None, contact=None, user_id=200):
        msg = make_message(text=text or "", user_id=user_id)
        msg.contact = contact
        return msg

    # -- Telegram contact share (existing path, must still work) ---------------

    async def test_telegram_contact_saves_phone(self, db):
        from handlers.start import handle_contact
        contact = MagicMock()
        contact.phone_number = "+77001234567"
        msg = self._msg(contact=contact, user_id=200)
        state = make_fsm()
        with patch("handlers.start.send_with_retry", new=AsyncMock()):
            await handle_contact(msg, state)
        import storage
        user = await storage.get_user(200)
        assert user is not None
        assert user["phone"] == "+77001234567"

    async def test_telegram_contact_clears_state(self, db):
        from handlers.start import handle_contact
        contact = MagicMock()
        contact.phone_number = "+77001234567"
        msg = self._msg(contact=contact)
        state = make_fsm()
        with patch("handlers.start.send_with_retry", new=AsyncMock()):
            await handle_contact(msg, state)
        state.clear.assert_called()

    # -- Manual input  valid formats ------------------------------------------

    async def test_manual_plus7_format(self, db):
        from handlers.start import handle_contact
        msg = self._msg(text="+7 700 123 45 67", user_id=201)
        state = make_fsm()
        with patch("handlers.start.send_with_retry", new=AsyncMock()):
            await handle_contact(msg, state)
        import storage
        user = await storage.get_user(201)
        assert user is not None
        assert user["phone"] is not None
        assert user["phone"].startswith("+")

    async def test_manual_8_prefix_normalized(self, db):
        from handlers.start import handle_contact
        msg = self._msg(text="87001234567", user_id=202)
        state = make_fsm()
        with patch("handlers.start.send_with_retry", new=AsyncMock()):
            await handle_contact(msg, state)
        import storage
        user = await storage.get_user(202)
        assert user is not None
        assert user["phone"] == "+77001234567"

    async def test_manual_spaces_dashes_stripped(self, db):
        from handlers.start import handle_contact
        msg = self._msg(text="+7 (700) 123-45-67", user_id=203)
        state = make_fsm()
        with patch("handlers.start.send_with_retry", new=AsyncMock()):
            await handle_contact(msg, state)
        import storage
        user = await storage.get_user(203)
        assert user is not None
        assert user["phone"] is not None

    async def test_manual_no_plus_prefix_gets_plus(self, db):
        from handlers.start import handle_contact
        msg = self._msg(text="77001234567", user_id=204)
        state = make_fsm()
        with patch("handlers.start.send_with_retry", new=AsyncMock()):
            await handle_contact(msg, state)
        import storage
        user = await storage.get_user(204)
        assert user is not None

    async def test_valid_manual_clears_state(self, db):
        from handlers.start import handle_contact
        msg = self._msg(text="+77001234567", user_id=205)
        state = make_fsm()
        with patch("handlers.start.send_with_retry", new=AsyncMock()):
            await handle_contact(msg, state)
        state.clear.assert_called()

    # -- Manual input  invalid formats  error, state NOT cleared --------------

    async def test_invalid_text_sends_error(self, db):
        from handlers.start import handle_contact
        msg = self._msg(text="not-a-phone", user_id=210)
        state = make_fsm()
        send_mock = AsyncMock()
        with patch("handlers.start.send_with_retry", new=send_mock):
            await handle_contact(msg, state)
        send_mock.assert_called_once()
        state.clear.assert_not_called()

    async def test_too_short_rejected(self, db):
        from handlers.start import handle_contact
        msg = self._msg(text="123", user_id=211)
        state = make_fsm()
        send_mock = AsyncMock()
        with patch("handlers.start.send_with_retry", new=send_mock):
            await handle_contact(msg, state)
        send_mock.assert_called_once()
        state.clear.assert_not_called()

    async def test_letters_only_rejected(self, db):
        from handlers.start import handle_contact
        msg = self._msg(text="abcdefghijk", user_id=212)
        state = make_fsm()
        send_mock = AsyncMock()
        with patch("handlers.start.send_with_retry", new=send_mock):
            await handle_contact(msg, state)
        send_mock.assert_called_once()
        state.clear.assert_not_called()

    async def test_invalid_phone_not_saved(self, db):
        from handlers.start import handle_contact
        import storage
        msg = self._msg(text="badphone!!", user_id=213)
        state = make_fsm()
        with patch("handlers.start.send_with_retry", new=AsyncMock()):
            await handle_contact(msg, state)
        user = await storage.get_user(213)
        assert user is None or user.get("phone") is None

    async def test_no_text_no_contact_sends_hint(self, db):
        from handlers.start import handle_contact
        msg = self._msg(text=None, user_id=220)
        msg.text = None
        msg.contact = None
        state = make_fsm()
        send_mock = AsyncMock()
        with patch("handlers.start.send_with_retry", new=send_mock):
            await handle_contact(msg, state)
        send_mock.assert_called_once()
        state.clear.assert_not_called()

    # -- Regression: verify the fixed send_with_retry call signature ----------

    async def test_error_reply_markup_is_not_string(self, db):
        """Regression: original bug passed 4 positional args, so reply_markup got a string."""
        from handlers.start import handle_contact
        msg = self._msg(text="bad", user_id=230)
        state = make_fsm()
        captured = []

        async def mock_send(bot, chat_id, text, reply_markup=None, **kw):
            captured.append((bot, chat_id, text, reply_markup))

        with patch("handlers.start.send_with_retry", new=mock_send):
            await handle_contact(msg, state)

        assert len(captured) == 1
        _, _, text_arg, reply_markup_arg = captured[0]
        assert not isinstance(reply_markup_arg, str), (
            "reply_markup must not be a string - duplicate-arg bug is back!"
        )
        assert isinstance(text_arg, str), "3rd arg must be the error text"


# =============================================================================
# FIX 3  handlers/admin.py :: cb_admin_cancel_booking  (CRIT-004 part 2)
# =============================================================================

class TestAdminCancelWaitlist:
    """Admin cancellation must notify waitlist users (CRIT-004)."""

    def _admin(self, uid=777):
        import config
        config.ADMIN_IDS = [uid]
        return uid

    async def test_waitlist_user_notified(self, db):
        import storage
        admin_id = self._admin()
        bid = await storage.save_booking(SAMPLE_BOOKING)
        await storage.add_to_waitlist(
            telegram_id=555, name="Waiter",
            master=SAMPLE_BOOKING["master"], service=SAMPLE_BOOKING["service"],
            date=SAMPLE_BOOKING["date"], time=SAMPLE_BOOKING["time"],
        )
        from handlers.admin import cb_admin_cancel_booking
        cb = make_callback(data=f"admin_cancel_booking:{bid}", user_id=admin_id)
        bot = AsyncMock()
        with patch("handlers.admin.scheduler.cancel_reminders", new=AsyncMock()), \
             patch("handlers.admin.send_with_retry", new=AsyncMock()), \
             patch("handlers.admin.edit_with_retry", new=AsyncMock()):
            await cb_admin_cancel_booking(cb, bot)
        notified = [c.args[0] for c in bot.send_message.call_args_list]
        assert 555 in notified

    async def test_waitlist_status_set_offered(self, db):
        import storage
        admin_id = self._admin()
        bid = await storage.save_booking(SAMPLE_BOOKING)
        await storage.add_to_waitlist(
            telegram_id=555, name="Waiter",
            master=SAMPLE_BOOKING["master"], service=SAMPLE_BOOKING["service"],
            date=SAMPLE_BOOKING["date"], time=SAMPLE_BOOKING["time"],
        )
        from handlers.admin import cb_admin_cancel_booking
        cb = make_callback(data=f"admin_cancel_booking:{bid}", user_id=admin_id)
        bot = AsyncMock()
        with patch("handlers.admin.scheduler.cancel_reminders", new=AsyncMock()), \
             patch("handlers.admin.send_with_retry", new=AsyncMock()), \
             patch("handlers.admin.edit_with_retry", new=AsyncMock()):
            await cb_admin_cancel_booking(cb, bot)
        # get_waitlist_for_slot only returns status='waiting',
        # so use get_all_waitlist to inspect the updated status
        all_wl = await storage.get_all_waitlist()
        our_entry = [
            w for w in all_wl
            if w["telegram_id"] == 555
            and w["date"] == SAMPLE_BOOKING["date"]
            and w["master"] == SAMPLE_BOOKING["master"]
        ]
        assert len(our_entry) == 1, "Waitlist entry must exist"
        assert our_entry[0]["status"] == "offered", (
            f"Expected 'offered', got '{our_entry[0]['status']}'"
        )

    async def test_empty_waitlist_no_extra_send(self, db):
        import storage
        admin_id = self._admin()
        bid = await storage.save_booking(SAMPLE_BOOKING)
        from handlers.admin import cb_admin_cancel_booking
        cb = make_callback(data=f"admin_cancel_booking:{bid}", user_id=admin_id)
        bot = AsyncMock()
        with patch("handlers.admin.scheduler.cancel_reminders", new=AsyncMock()), \
             patch("handlers.admin.send_with_retry", new=AsyncMock()), \
             patch("handlers.admin.edit_with_retry", new=AsyncMock()):
            await cb_admin_cancel_booking(cb, bot)
        assert bot.send_message.call_count == 0

    async def test_multiple_waitlist_all_notified(self, db):
        import storage
        admin_id = self._admin()
        bid = await storage.save_booking(SAMPLE_BOOKING)
        uids = [551, 552, 553]
        for uid in uids:
            await storage.add_to_waitlist(
                telegram_id=uid, name=f"U{uid}",
                master=SAMPLE_BOOKING["master"], service=SAMPLE_BOOKING["service"],
                date=SAMPLE_BOOKING["date"], time=SAMPLE_BOOKING["time"],
            )
        from handlers.admin import cb_admin_cancel_booking
        cb = make_callback(data=f"admin_cancel_booking:{bid}", user_id=admin_id)
        bot = AsyncMock()
        with patch("handlers.admin.scheduler.cancel_reminders", new=AsyncMock()), \
             patch("handlers.admin.send_with_retry", new=AsyncMock()), \
             patch("handlers.admin.edit_with_retry", new=AsyncMock()):
            await cb_admin_cancel_booking(cb, bot)
        notified = [c.args[0] for c in bot.send_message.call_args_list]
        for uid in uids:
            assert uid in notified

    async def test_send_message_failure_does_not_crash(self, db):
        import storage
        admin_id = self._admin()
        bid = await storage.save_booking(SAMPLE_BOOKING)
        await storage.add_to_waitlist(
            telegram_id=555, name="Waiter",
            master=SAMPLE_BOOKING["master"], service=SAMPLE_BOOKING["service"],
            date=SAMPLE_BOOKING["date"], time=SAMPLE_BOOKING["time"],
        )
        from handlers.admin import cb_admin_cancel_booking
        cb = make_callback(data=f"admin_cancel_booking:{bid}", user_id=admin_id)
        bot = AsyncMock()
        bot.send_message = AsyncMock(side_effect=Exception("Forbidden"))
        with patch("handlers.admin.scheduler.cancel_reminders", new=AsyncMock()), \
             patch("handlers.admin.send_with_retry", new=AsyncMock()), \
             patch("handlers.admin.edit_with_retry", new=AsyncMock()):
            await cb_admin_cancel_booking(cb, bot)
        cb.answer.assert_called()

    async def test_notification_text_contains_time_and_master(self, db):
        import storage
        admin_id = self._admin()
        bid = await storage.save_booking(SAMPLE_BOOKING)
        await storage.add_to_waitlist(
            telegram_id=555, name="Waiter",
            master=SAMPLE_BOOKING["master"], service=SAMPLE_BOOKING["service"],
            date=SAMPLE_BOOKING["date"], time=SAMPLE_BOOKING["time"],
        )
        from handlers.admin import cb_admin_cancel_booking
        cb = make_callback(data=f"admin_cancel_booking:{bid}", user_id=admin_id)
        bot = AsyncMock()
        with patch("handlers.admin.scheduler.cancel_reminders", new=AsyncMock()), \
             patch("handlers.admin.send_with_retry", new=AsyncMock()), \
             patch("handlers.admin.edit_with_retry", new=AsyncMock()):
            await cb_admin_cancel_booking(cb, bot)
        sent_text = bot.send_message.call_args.args[1]
        assert SAMPLE_BOOKING["time"] in sent_text
        assert SAMPLE_BOOKING["master"] in sent_text

    async def test_nonexistent_booking_no_waitlist_notify(self, db):
        admin_id = self._admin()
        from handlers.admin import cb_admin_cancel_booking
        cb = make_callback(data="admin_cancel_booking:nope", user_id=admin_id)
        bot = AsyncMock()
        with patch("handlers.admin.send_with_retry", new=AsyncMock()):
            await cb_admin_cancel_booking(cb, bot)
        bot.send_message.assert_not_called()

    async def test_non_admin_blocked_booking_stays_active(self, db):
        import storage, config
        config.ADMIN_IDS = []
        bid = await storage.save_booking(SAMPLE_BOOKING)
        from handlers.admin import cb_admin_cancel_booking
        cb = make_callback(data=f"admin_cancel_booking:{bid}", user_id=999)
        bot = AsyncMock()
        await cb_admin_cancel_booking(cb, bot)
        booking = await storage.get_booking_with_user(bid)
        assert booking["status"] == "active"


# =============================================================================
# FIX 4  handlers/admin.py :: _show_admin_bookings_page  (HIGH-003)
# =============================================================================

class TestShowAdminBookingsPageSQL:
    """_show_admin_bookings_page must use get_upcoming_bookings_paged, not Python slicing."""

    def _admin(self, uid=777):
        import config
        config.ADMIN_IDS = [uid]
        return uid

    async def test_calls_paged_not_full_list(self, db):
        admin_id = self._admin()
        from handlers.admin import cb_admin_bookings
        cb = make_callback(user_id=admin_id)
        with patch("handlers.admin.storage.get_upcoming_bookings_paged",
                   new=AsyncMock(return_value=([], 0))) as paged, \
             patch("handlers.admin.storage.get_upcoming_bookings",
                   new=AsyncMock(return_value=[])) as full, \
             patch("handlers.admin.edit_with_retry", new=AsyncMock()):
            await cb_admin_bookings(cb)
        paged.assert_called_once()
        full.assert_not_called()

    async def test_empty_db_shows_zero_total(self, db):
        admin_id = self._admin()
        from handlers.admin import cb_admin_bookings
        cb = make_callback(user_id=admin_id)
        edit_mock = AsyncMock()
        with patch("handlers.admin.edit_with_retry", new=edit_mock):
            await cb_admin_bookings(cb)
        rendered = edit_mock.call_args.args[1]
        assert "0" in rendered

    async def test_shows_correct_total_count(self, db):
        admin_id = self._admin()
        import storage
        await _save_n(storage, 7)
        from handlers.admin import cb_admin_bookings
        cb = make_callback(user_id=admin_id)
        edit_mock = AsyncMock()
        with patch("handlers.admin.edit_with_retry", new=edit_mock):
            await cb_admin_bookings(cb)
        rendered = edit_mock.call_args.args[1]
        assert "7" in rendered

    async def test_page0_shows_max_5_booking_rows(self, db):
        admin_id = self._admin()
        import storage
        await _save_n(storage, 8)
        from handlers.admin import cb_admin_bookings
        cb = make_callback(user_id=admin_id)
        edit_mock = AsyncMock()
        with patch("handlers.admin.edit_with_retry", new=edit_mock):
            await cb_admin_bookings(cb)
        kb = (edit_mock.call_args.kwargs.get("reply_markup")
              or edit_mock.call_args.args[2])
        booking_rows = [
            row for row in kb.inline_keyboard
            if any("admin_manage_booking" in (btn.callback_data or "")
                   for btn in row)
        ]
        assert len(booking_rows) == 5

    async def test_next_button_when_more_items(self, db):
        admin_id = self._admin()
        import storage
        await _save_n(storage, 8)
        from handlers.admin import cb_admin_bookings
        cb = make_callback(user_id=admin_id)
        edit_mock = AsyncMock()
        with patch("handlers.admin.edit_with_retry", new=edit_mock):
            await cb_admin_bookings(cb)
        kb = (edit_mock.call_args.kwargs.get("reply_markup")
              or edit_mock.call_args.args[2])
        cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        assert any("admin_bookings_page:5" in (c or "") for c in cbs), \
            "Must have next-page button pointing to offset 5"

    async def test_no_next_button_on_last_page(self, db):
        admin_id = self._admin()
        import storage
        await _save_n(storage, 3)
        from handlers.admin import cb_admin_bookings
        cb = make_callback(user_id=admin_id)
        edit_mock = AsyncMock()
        with patch("handlers.admin.edit_with_retry", new=edit_mock):
            await cb_admin_bookings(cb)
        kb = (edit_mock.call_args.kwargs.get("reply_markup")
              or edit_mock.call_args.args[2])
        cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        assert not any("admin_bookings_page:5" in (c or "") for c in cbs)

    async def test_pagination_page2_correct_offset(self, db):
        admin_id = self._admin()
        from handlers.admin import cb_admin_bookings_page
        cb = make_callback(data="admin_bookings_page:5", user_id=admin_id)
        with patch("handlers.admin.storage.get_upcoming_bookings_paged",
                   new=AsyncMock(return_value=([], 10))) as paged, \
             patch("handlers.admin.edit_with_retry", new=AsyncMock()):
            await cb_admin_bookings_page(cb)
        call = paged.call_args
        offset_used = call.kwargs.get("offset") or call.args[0]
        assert offset_used == 5

    async def test_prev_button_on_page2(self, db):
        admin_id = self._admin()
        from handlers.admin import cb_admin_bookings_page
        cb = make_callback(data="admin_bookings_page:5", user_id=admin_id)
        edit_mock = AsyncMock()
        with patch("handlers.admin.storage.get_upcoming_bookings_paged",
                   new=AsyncMock(return_value=([], 10))), \
             patch("handlers.admin.edit_with_retry", new=edit_mock):
            await cb_admin_bookings_page(cb)
        kb = (edit_mock.call_args.kwargs.get("reply_markup")
              or edit_mock.call_args.args[2])
        cbs = [btn.callback_data for row in kb.inline_keyboard for btn in row]
        assert any("admin_bookings_page:0" in (c or "") for c in cbs), \
            "Previous button must point to offset 0"

    async def test_non_admin_blocked_on_page_callback(self, db):
        import config
        config.ADMIN_IDS = []
        from handlers.admin import cb_admin_bookings_page
        cb = make_callback(data="admin_bookings_page:0", user_id=999)
        await cb_admin_bookings_page(cb)
        cb.answer.assert_called()


# =============================================================================
# Regression: info.py must NOT contain a duplicate cmd_help handler
# =============================================================================

class TestNoDuplicateCmdHelp:
    """Verify the broken duplicate cmd_help was removed from info.py."""

    def _parse(self, rel_path):
        import ast, pathlib
        src = (pathlib.Path(__file__).parent.parent / rel_path).read_bytes()
        return ast.parse(src)

    def _async_func_names(self, tree):
        import ast
        return [n.name for n in ast.walk(tree)
                if isinstance(n, ast.AsyncFunctionDef)]

    def test_cmd_help_not_in_info(self):
        tree = self._parse("handlers/info.py")
        assert "cmd_help" not in self._async_func_names(tree), (
            "cmd_help must NOT be in handlers/info.py"
        )

    def test_cmd_help_in_start(self):
        tree = self._parse("handlers/start.py")
        assert "cmd_help" in self._async_func_names(tree), (
            "/help handler must exist in handlers/start.py"
        )

    def test_info_compiles(self):
        import py_compile, pathlib
        py_compile.compile(
            str(pathlib.Path(__file__).parent.parent / "handlers" / "info.py"),
            doraise=True,
        )

    def test_start_compiles(self):
        import py_compile, pathlib
        py_compile.compile(
            str(pathlib.Path(__file__).parent.parent / "handlers" / "start.py"),
            doraise=True,
        )

    def test_admin_compiles(self):
        import py_compile, pathlib
        py_compile.compile(
            str(pathlib.Path(__file__).parent.parent / "handlers" / "admin.py"),
            doraise=True,
        )

    def test_storage_compiles(self):
        import py_compile, pathlib
        py_compile.compile(
            str(pathlib.Path(__file__).parent.parent / "storage.py"),
            doraise=True,
        )
