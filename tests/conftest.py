import pytest

from matchawards_mcp import api, server


@pytest.fixture(autouse=True)
def no_egress_fetch(monkeypatch):
    """--http refreshes the ChatGPT egress list from openai.com on the first request; keep tests off the network
    (and out of the mocked API's request log) with a fetch that fails, so the bundled snapshot stays in use."""

    async def failed_fetch():
        return None

    monkeypatch.setattr(server, "fetch_egress", failed_fetch)


@pytest.fixture(autouse=True)
def no_api_timing_leak(monkeypatch):
    """http_app() points api.on_request at the metrics; put the stdio default (None) back after each test."""
    monkeypatch.setattr(api, "on_request", None)
