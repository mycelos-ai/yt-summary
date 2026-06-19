import pytest

from app.services.playlist_index import PlaylistApiError, _playlist_id_from_url


def test_playlist_id_from_url_extracts_list_param():
    url = "https://www.youtube.com/playlist?list=PLabc123"
    assert _playlist_id_from_url(url) == "PLabc123"


def test_playlist_id_from_url_with_extra_params():
    url = "https://www.youtube.com/playlist?list=PLxyz&si=foo"
    assert _playlist_id_from_url(url) == "PLxyz"


def test_playlist_id_from_url_raises_without_list():
    with pytest.raises(PlaylistApiError):
        _playlist_id_from_url("https://www.youtube.com/watch?v=abc")
