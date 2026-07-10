import pytest

from app.services import network_safety


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://[::1]/admin",
        "http://169.254.169.254/latest/meta-data",
        "http://10.0.0.5/internal",
        "http://localhost:8000/",
    ],
)
def test_rejects_literal_private_targets(url):
    with pytest.raises(network_safety.UnsafeUrlError, match="local or private"):
        network_safety.validate_public_http_url(url)


def test_rejects_hostname_resolving_to_private_ip(monkeypatch):
    monkeypatch.setattr(
        network_safety, "_resolve_host", lambda host, port: {"192.168.1.10"},
    )
    with pytest.raises(network_safety.UnsafeUrlError, match="local or private"):
        network_safety.validate_public_http_url("https://example.test/article")


def test_accepts_hostname_resolving_only_to_public_ips(monkeypatch):
    monkeypatch.setattr(
        network_safety,
        "_resolve_host",
        lambda host, port: {"93.184.216.34", "2606:2800:220:1:248:1893:25c8:1946"},
    )
    network_safety.validate_public_http_url("https://example.com/article")


def test_private_fetch_escape_hatch(monkeypatch):
    monkeypatch.setenv("YTS_ALLOW_PRIVATE_FETCHES", "1")
    network_safety.validate_public_http_url("http://127.0.0.1/internal")
