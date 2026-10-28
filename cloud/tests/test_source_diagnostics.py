import json

import pytest

from ashare_cloud import source_check
from ashare_cloud.history import Pacer
from ashare_cloud.tickflow import SourceError, TickFlow, http_diagnostic
from test_history import Response


@pytest.mark.parametrize("payload,category", [
    ({"code": "INVALID_API_KEY", "message": "private-key-abc"}, "credential_invalid"),
    ({"error": {"message": "No active subscription private-key-abc"}}, "subscription_inactive"),
    ({"detail": "IP address is not allowed private-key-abc"}, "ip_restriction"),
    ({"message": "No permission for market private-key-abc"}, "permission_denied"),
    ({"message": "private-key-abc"}, "unspecified_denial"),
])
def test_error_classification_never_echoes_provider_fields(payload, category):
    r = Response(403, payload)
    r.text = json.dumps(payload)
    result = http_diagnostic(r, "/v1/klines/batch")
    assert result["response_hint"] == category and result["response_format"] == "json"
    assert "private-key-abc" not in json.dumps(result)


def test_html_edge_challenge_is_distinct_from_api_permission():
    r = Response(403, ValueError(), {"Content-Type": "text/html", "cf-mitigated": "challenge"})
    r.text = '<html>private-key-abc cf-chl-example</html>'
    result = http_diagnostic(r, "/v1/quotes")
    assert result["response_hint"] == "edge_challenge" and result["response_format"] == "html"
    assert "private-key-abc" not in json.dumps(result)


def test_plain_denial_and_unknown_path_not_echoed():
    r = Response(403, ValueError())
    r.text = "Forbidden private-key-abc"
    result = http_diagnostic(r, "/private-key-abc")
    assert result["endpoint"] == "other" and result["response_hint"] == "unspecified_denial"
    assert "private-key-abc" not in json.dumps(result)


def test_failure_stage_and_single_denied_request(monkeypatch, capsys):
    monkeypatch.setenv("TICKFLOW_API_KEY", "private-key-abc")
    calls = []
    def get(url, **kwargs):
        calls.append(url)
        return Response(403, {"message": "No active subscription private-key-abc"})
    client = TickFlow(Pacer(), get=get)
    monkeypatch.setattr(source_check, "TickFlow", lambda *a: client)
    assert source_check.main() == 1
    output = capsys.readouterr().out
    assert "历史日线" in output and "/v1/klines/batch" in output
    assert "subscription_inactive" in output and "private-key-abc" not in output
    assert len(calls) == 1 and client.disabled == "http_403"
    with pytest.raises(SourceError):
        client.quotes(["sh600000"])
    assert len(calls) == 1
