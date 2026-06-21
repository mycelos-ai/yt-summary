import asyncio
from unittest.mock import patch, AsyncMock
from app.models import VideoKind


def _run(c): return asyncio.get_event_loop().run_until_complete(c)


def test_text_kind_is_thumbnail_eligible():
    # The eligibility predicate must include TEXT (mirrors EMAIL/WEB).
    import app.pipeline as p
    # The predicate lives inline at pipeline.py:261; assert via a tiny helper
    # we extract in Step 3. After refactor, `p._wants_stock_thumbnail(kind)`:
    assert p._wants_stock_thumbnail(VideoKind.TEXT) is True
    assert p._wants_stock_thumbnail(VideoKind.WEB) is True
    assert p._wants_stock_thumbnail(VideoKind.EMAIL) is True
    assert p._wants_stock_thumbnail(VideoKind.YOUTUBE) is False


def test_text_kind_uses_standard_summary_prompt():
    # content_kind for TEXT must be the plain 'youtube' path, NOT 'email'.
    import app.pipeline as p
    assert p._content_kind_for(VideoKind.TEXT) == "youtube"
    assert p._content_kind_for(VideoKind.EMAIL) == "email"
