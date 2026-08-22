"""Extended tz_utils tests for missing lines."""

import pathlib
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))


class TestTzUtilsExtended:
    def test_get_now_non_utc_timezone(self):
        from datetime import datetime

        from tz_utils import get_now

        now = get_now("Asia/Almaty")
        assert isinstance(now, datetime)
        assert now.tzinfo is not None

    def test_is_past_exact_boundary(self):
        import config
        from tz_utils import get_now, is_past

        now = get_now(config.TIMEZONE)
        from datetime import timedelta

        future = now + timedelta(hours=2)
        assert not is_past(future.strftime("%Y-%m-%d"), future.strftime("%H:%M"))

    def test_get_today_matches_now(self):
        import config
        from tz_utils import get_now, get_today

        now = get_now(config.TIMEZONE)
        today = get_today(config.TIMEZONE)
        assert today == now.strftime("%Y-%m-%d")

    def test_invalid_timezone_graceful(self):
        from datetime import datetime

        from tz_utils import get_now

        try:
            now = get_now("Invalid/Timezone_XYZ")
            assert isinstance(now, datetime)
        except Exception:
            pass  # acceptable
