"""Step 3 provider tests: missing config, bounded errors, sanitized reports.

All provider HTTP is mocked (httpx.MockTransport); no test touches the live
providers. Live smoke results are recorded in NEW_WORK.md, never fabricated
here.
"""

import json
import sys
import types

import httpx
import pytest
from fastapi.testclient import TestClient

from app import providers
from app.main import app
from app.providers import registry
from app.providers.base import sanitize

TEST_TOKEN = "test-token-0123456789abcdef01"
FAKE_META_KEY = "META-SECRET-KEY-VALUE"
FAKE_GITHUB_TOKEN = "GITHUB-SECRET-TOKEN"
FAKE_BASETEN_KEY = "BASETEN-SECRET-KEY"


@pytest.fixture(autouse=True)
def clean_registry():
    registry.clear()
    yield
    registry.clear()


@pytest.fixture()
def token(monkeypatch):
    monkeypatch.setenv("BUGLENS_API_TOKEN", TEST_TOKEN)
    for var in (
        "META_API_KEY",
        "GITHUB_TOKEN",
        "GITHUB_REPO",
        "MODAL_TOKEN_ID",
        "MODAL_TOKEN_SECRET",
        "BASETEN_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    return TEST_TOKEN


@pytest.fixture()
def client():
    return TestClient(app)


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def mock_transport(handler):
    return httpx.MockTransport(handler)


def install_transport(monkeypatch, transport):
    real_get_adapter = providers.get_adapter
    monkeypatch.setattr(
        providers, "get_adapter", lambda name: real_get_adapter(name, transport=transport)
    )


def completed_response(text="pong"):
    return {
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
    }


# --- Readiness without network -------------------------------------------------


def test_status_reports_not_configured_when_keys_missing(client, token):
    response = client.get("/api/providers/status", headers=headers(token))
    assert response.status_code == 200
    states = {item["name"]: item["state"] for item in response.json()}
    assert states == {
        "meta": "not_configured",
        "github": "not_configured",
        "modal": "not_configured",
        "baseten": "not_configured",
    }


def test_status_reports_configured_untested_when_keys_present(client, token, monkeypatch):
    monkeypatch.setenv("META_API_KEY", FAKE_META_KEY)
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_GITHUB_TOKEN)
    response = client.get("/api/providers/status", headers=headers(token))
    assert response.status_code == 200
    states = {item["name"]: item["state"] for item in response.json()}
    assert states["meta"] == "configured_untested"
    assert states["github"] == "configured_untested"
    assert states["modal"] == "not_configured"
    # A present key alone proves nothing: nothing is verified without a smoke run.
    assert "verified" not in states.values()


def test_status_requires_app_auth(client):
    assert client.get("/api/providers/status").status_code == 401
    assert client.post("/api/providers/meta/smoke").status_code == 401


def test_smoke_unknown_provider_is_404(client, token):
    response = client.post("/api/providers/nonexistent/smoke", headers=headers(token))
    assert response.status_code == 404


# --- Meta ----------------------------------------------------------------------


def meta_handler(request):
    body = json.loads(request.content.decode("utf-8"))
    assert request.url.path == "/v1/responses"
    assert request.headers["authorization"] == f"Bearer {FAKE_META_KEY}"
    assert body["store"] is False
    if body["model"] in ("muse-spark-1.3", "muse-wonder-1.0"):
        assert body["reasoning"] == {"effort": "minimal"}
        return httpx.Response(200, json=completed_response())
    if body["model"] == "sam-3.1":
        content = body["input"][0]["content"]
        kinds = {part["type"] for part in content}
        assert {"input_text", "input_image"} <= kinds
        return httpx.Response(200, json=completed_response(SAM_GRAMMAR_LINE))
    raise AssertionError(f"unexpected model {body.get('model')}")


SAM_GRAMMAR_LINE = (
    "<0f>0<|box;x1=24;y1=16;x2=71;y2=47;w=96;h=64|>"
    "<|mask;x=0;y=0;data=32,48,~abc|>"
)


def test_meta_smoke_verified(client, token, monkeypatch):
    monkeypatch.setenv("META_API_KEY", FAKE_META_KEY)
    install_transport(monkeypatch, mock_transport(meta_handler))
    response = client.post("/api/providers/meta/smoke", headers=headers(token))
    assert response.status_code == 200
    report = response.json()
    assert report["state"] == "verified"
    assert {check["name"] for check in report["checks"]} == {"spark", "wonder", "sam"}
    assert all(check["state"] == "verified" for check in report["checks"])
    assert FAKE_META_KEY not in response.text
    # The verified outcome persists for status reads in this process.
    status = client.get("/api/providers/status", headers=headers(token)).json()
    assert next(i for i in status if i["name"] == "meta")["state"] == "verified"


def test_meta_smoke_unauthorized_is_failed_and_sanitized(client, token, monkeypatch):
    monkeypatch.setenv("META_API_KEY", FAKE_META_KEY)

    def handler(request):
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    install_transport(monkeypatch, mock_transport(handler))
    response = client.post("/api/providers/meta/smoke", headers=headers(token))
    assert response.status_code == 200
    report = response.json()
    assert report["state"] == "failed"
    assert FAKE_META_KEY not in response.text
    assert "401" in report["checks"][0]["detail"]


def test_meta_smoke_retries_transient_then_verifies(client, token, monkeypatch):
    monkeypatch.setenv("META_API_KEY", FAKE_META_KEY)
    calls = {"count": 0}

    def handler(request):
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(503, json={"error": "try again"})
        body = json.loads(request.content.decode("utf-8"))
        text = SAM_GRAMMAR_LINE if body["model"] == "sam-3.1" else "pong"
        return httpx.Response(200, json=completed_response(text))

    install_transport(monkeypatch, mock_transport(handler))
    response = client.post("/api/providers/meta/smoke", headers=headers(token))
    assert response.json()["state"] == "verified"
    assert calls["count"] == 4  # one retry plus the remaining checks


def test_meta_smoke_timeout_is_failed(client, token, monkeypatch):
    monkeypatch.setenv("META_API_KEY", FAKE_META_KEY)

    def handler(request):
        raise httpx.TimeoutException("too slow")

    install_transport(monkeypatch, mock_transport(handler))
    response = client.post("/api/providers/meta/smoke", headers=headers(token))
    report = response.json()
    assert report["state"] == "failed"
    assert "TimeoutException" in report["checks"][0]["detail"]


def test_meta_smoke_incomplete_is_failed(client, token, monkeypatch):
    monkeypatch.setenv("META_API_KEY", FAKE_META_KEY)

    def handler(request):
        return httpx.Response(200, json={"status": "incomplete", "output": []})

    install_transport(monkeypatch, mock_transport(handler))
    response = client.post("/api/providers/meta/smoke", headers=headers(token))
    assert response.json()["state"] == "failed"


def test_sam_geometry_out_of_bounds_fails_smoke(client, token, monkeypatch):
    monkeypatch.setenv("META_API_KEY", FAKE_META_KEY)
    bad_line = (
        "<0f>0<|box;x1=24;y1=16;x2=971;y2=47;w=96;h=64|>"
        "<|mask;x=0;y=0;data=32,48,~abc|>"
    )

    def handler(request):
        body = json.loads(request.content.decode("utf-8"))
        text = bad_line if body["model"] == "sam-3.1" else "pong"
        return httpx.Response(200, json=completed_response(text))

    install_transport(monkeypatch, mock_transport(handler))
    response = client.post("/api/providers/meta/smoke", headers=headers(token))
    report = response.json()
    assert report["state"] == "failed"
    sam = next(check for check in report["checks"] if check["name"] == "sam")
    assert sam["state"] == "failed"
    assert "outside 96x64" in sam["detail"]


def test_sam_zero_matches_fails_expected_object_check(client, token, monkeypatch):
    monkeypatch.setenv("META_API_KEY", FAKE_META_KEY)

    def handler(request):
        body = json.loads(request.content.decode("utf-8"))
        text = "" if body["model"] == "sam-3.1" else "pong"
        return httpx.Response(200, json=completed_response(text))

    install_transport(monkeypatch, mock_transport(handler))
    response = client.post("/api/providers/meta/smoke", headers=headers(token))
    report = response.json()
    assert report["state"] == "failed"
    sam = next(check for check in report["checks"] if check["name"] == "sam")
    assert sam["state"] == "failed"
    assert "0 matches" in sam["detail"]


def test_parse_sam_output_valid_and_empty():
    from app.providers.meta import parse_sam_output

    assert parse_sam_output("", frame_w=96, frame_h=64) == []
    assert parse_sam_output("  ", frame_w=96, frame_h=64) == []
    matches = parse_sam_output(SAM_GRAMMAR_LINE, frame_w=96, frame_h=64)
    assert len(matches) == 1
    match = matches[0]
    assert (match.object_id, match.x1, match.y1, match.x2, match.y2) == (0, 24, 16, 71, 47)
    assert (match.mask_h, match.mask_w) == (32, 48)


def test_parse_sam_output_sparse_multi_record():
    from app.providers.meta import parse_sam_output

    line = (
        "<0f>0<|box;x1=1;y1=1;x2=2;y2=2;w=96;h=64|><|mask;x=0;y=0;data=2,2,~a|>,"
        "2<|box;x1=10;y1=10;x2=20;y2=20;w=96;h=64|><|mask;x=0;y=0;data=11,11,~b|>"
    )
    matches = parse_sam_output(line, frame_w=96, frame_h=64)
    assert [match.object_id for match in matches] == [0, 2]


@pytest.mark.parametrize(
    "text",
    [
        "not a frame line",
        "<1f>0<|box;x1=1;y1=1;x2=2;y2=2;w=96;h=64|><|mask;x=0;y=0;data=2,2,~a|>",
        "<0f>0<|box;x1=50;y1=10;x2=10;y2=20;w=96;h=64|><|mask;x=0;y=0;data=2,2,~a|>",
        "<0f>0<|box;x1=1;y1=1;x2=200;y2=2;w=96;h=64|><|mask;x=0;y=0;data=2,2,~a|>",
        "<0f>0<|box;x1=1;y1=1;x2=2;y2=2;w=320;h=64|><|mask;x=0;y=0;data=2,2,~a|>",
        "<0f>0<|box;x1=1;y1=1;x2=2;y2=2;w=96;h=64|>",
        "<0f>0<|box;x1=1;y1=1;x2=2;y2=2;w=96;h=64|><|mask;x=0;y=0;data=0,2,~a|>",
        "<0f>line one\n<1f>line two",
    ],
)
def test_parse_sam_output_rejects_malformed(text):
    from app.providers.meta import SamParseError, parse_sam_output

    with pytest.raises(SamParseError):
        parse_sam_output(text, frame_w=96, frame_h=64)


# --- GitHub --------------------------------------------------------------------


def test_github_smoke_auth_verified_repo_pending_without_repo(client, token, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_GITHUB_TOKEN)

    def handler(request):
        assert request.url.path == "/user"
        return httpx.Response(200, json={"login": "octocat"})

    install_transport(monkeypatch, mock_transport(handler))
    response = client.post("/api/providers/github/smoke", headers=headers(token))
    report = response.json()
    assert report["state"] == "configured_untested"
    by_name = {check["name"]: check for check in report["checks"]}
    assert by_name["auth"]["state"] == "verified"
    assert "octocat" in by_name["auth"]["detail"]
    assert by_name["repo"]["state"] == "not_configured"
    assert "GITHUB_REPO" in by_name["repo"]["detail"]
    assert FAKE_GITHUB_TOKEN not in response.text


def test_github_smoke_repo_verified(client, token, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_GITHUB_TOKEN)
    monkeypatch.setenv("GITHUB_REPO", "octo/demo")

    def handler(request):
        if request.url.path == "/user":
            return httpx.Response(200, json={"login": "octocat"})
        if request.url.path == "/repos/octo/demo":
            return httpx.Response(
                200, json={"full_name": "octo/demo", "default_branch": "main"}
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    install_transport(monkeypatch, mock_transport(handler))
    response = client.post("/api/providers/github/smoke", headers=headers(token))
    assert response.json()["state"] == "verified"


def test_github_smoke_bad_token_fails_both_checks(client, token, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_GITHUB_TOKEN)
    monkeypatch.setenv("GITHUB_REPO", "octo/demo")

    def handler(request):
        return httpx.Response(401, json={"message": "Bad credentials"})

    install_transport(monkeypatch, mock_transport(handler))
    response = client.post("/api/providers/github/smoke", headers=headers(token))
    report = response.json()
    assert report["state"] == "failed"
    assert FAKE_GITHUB_TOKEN not in response.text


# --- Modal ---------------------------------------------------------------------


@pytest.fixture(autouse=True)
def no_modal_profile(monkeypatch):
    """Neutralize any real ~/.modal.toml so tests control auth source."""
    from app.providers import modal as modal_module

    monkeypatch.setattr(modal_module, "_local_profile_present", lambda: False)


def make_fake_modal(seen, terminate=None, wait_code=0):
    class FakeProc:
        def wait(self):
            return wait_code

        class stdout:
            @staticmethod
            def read():
                return "buglens-smoke\n"

    class FakeSandbox:
        def __init__(self, **kwargs):
            seen["create_kwargs"] = kwargs

        def exec(self, *command, **kwargs):
            seen["exec_command"] = command
            seen["exec_kwargs"] = kwargs
            return FakeProc()

        def terminate(self):
            if terminate is not None:
                terminate()
            else:
                seen["terminated"] = True

    def lookup(name, create_if_missing=False):
        seen["app_lookup"] = (name, create_if_missing)
        return ("app", name)

    fake_modal = types.ModuleType("modal")
    fake_modal.App = types.SimpleNamespace(lookup=lookup)
    fake_modal.Sandbox = types.SimpleNamespace(create=lambda **kwargs: FakeSandbox(**kwargs))
    return fake_modal


def test_modal_smoke_without_credentials_reports_setup(client, token):
    response = client.post("/api/providers/modal/smoke", headers=headers(token))
    assert response.status_code == 200
    report = response.json()
    assert report["state"] == "not_configured"
    assert "modal setup" in report["detail"]
    assert "MODAL_TOKEN_ID" in report["detail"]


def test_modal_smoke_without_package_is_failed(client, token, monkeypatch):
    monkeypatch.setenv("MODAL_TOKEN_ID", "token-id-value")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "token-secret-value")
    monkeypatch.setitem(sys.modules, "modal", None)
    response = client.post("/api/providers/modal/smoke", headers=headers(token))
    report = response.json()
    assert report["state"] == "failed"
    assert "not installed" in report["detail"]


def test_modal_smoke_terminates_and_keeps_secrets_out(client, token, monkeypatch):
    monkeypatch.setenv("MODAL_TOKEN_ID", "token-id-value")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "token-secret-value")
    seen = {}
    monkeypatch.setitem(sys.modules, "modal", make_fake_modal(seen))

    response = client.post("/api/providers/modal/smoke", headers=headers(token))
    report = response.json()
    assert report["state"] == "verified"
    assert seen.get("terminated") is True
    assert seen.get("app_lookup") == ("buglens-smoke", True)
    assert seen.get("exec_command") == ("echo", "buglens-smoke")
    assert seen["create_kwargs"]["timeout"] == 120
    captured = json.dumps(seen, default=str)
    assert "token-id-value" not in captured and "token-secret-value" not in captured
    assert "environment credentials" in report["detail"]


def test_modal_smoke_failed_cleanup_is_not_verified(client, token, monkeypatch):
    monkeypatch.setenv("MODAL_TOKEN_ID", "token-id-value")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "token-secret-value")
    seen = {}

    def broken_terminate():
        raise RuntimeError("terminate exploded")

    monkeypatch.setitem(sys.modules, "modal", make_fake_modal(seen, terminate=broken_terminate))
    response = client.post("/api/providers/modal/smoke", headers=headers(token))
    report = response.json()
    assert report["state"] == "failed"
    assert "cleanup" in report["detail"]


def test_modal_smoke_failed_exec_is_not_verified(client, token, monkeypatch):
    monkeypatch.setenv("MODAL_TOKEN_ID", "token-id-value")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "token-secret-value")
    seen = {}
    monkeypatch.setitem(sys.modules, "modal", make_fake_modal(seen, wait_code=3))
    response = client.post("/api/providers/modal/smoke", headers=headers(token))
    report = response.json()
    assert report["state"] == "failed"
    assert "exit code 3" in report["detail"]
    assert seen.get("terminated") is True  # cleanup still attempted


def test_modal_accepts_local_sdk_profile(client, token, monkeypatch):
    from app.providers import modal as modal_module

    monkeypatch.setattr(modal_module, "_local_profile_present", lambda: True)
    seen = {}
    monkeypatch.setitem(sys.modules, "modal", make_fake_modal(seen))
    status = client.get("/api/providers/status", headers=headers(token)).json()
    modal_status = next(item for item in status if item["name"] == "modal")
    assert modal_status["state"] == "configured_untested"
    assert "local SDK profile" in modal_status["detail"]
    response = client.post("/api/providers/modal/smoke", headers=headers(token))
    report = response.json()
    assert report["state"] == "verified"
    assert "local SDK profile" in report["detail"]


def test_modal_smoke_returns_within_bound(client, token, monkeypatch):
    import threading
    import time

    from app import config

    monkeypatch.setenv("MODAL_TOKEN_ID", "token-id-value")
    monkeypatch.setenv("MODAL_TOKEN_SECRET", "token-secret-value")
    monkeypatch.setattr(config, "MODAL_SMOKE_TIMEOUT_S", 1.0)
    gate = threading.Event()

    class StuckSandbox:
        def exec(self, *command, **kwargs):
            assert gate.wait(timeout=30)
            raise AssertionError("worker should have been abandoned")

        def terminate(self):
            pass

    fake_modal = types.ModuleType("modal")
    fake_modal.App = types.SimpleNamespace(
        lookup=lambda name, create_if_missing=False: ("app", name)
    )
    fake_modal.Sandbox = types.SimpleNamespace(create=lambda **kwargs: StuckSandbox())
    monkeypatch.setitem(sys.modules, "modal", fake_modal)

    started = time.perf_counter()
    try:
        response = client.post("/api/providers/modal/smoke", headers=headers(token))
    finally:
        gate.set()  # release the abandoned worker for a clean test exit
    elapsed = time.perf_counter() - started
    report = response.json()
    assert report["state"] == "failed"
    assert "exceeded the 1s bound" in report["detail"]
    assert elapsed < 15


# --- Baseten (optional) ----------------------------------------------------------


def test_baseten_smoke_verified_lists_models(client, token, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", FAKE_BASETEN_KEY)

    def handler(request):
        assert request.url.path == "/v1/models"
        assert request.headers["authorization"] == f"Bearer {FAKE_BASETEN_KEY}"
        return httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]})

    install_transport(monkeypatch, mock_transport(handler))
    response = client.post("/api/providers/baseten/smoke", headers=headers(token))
    report = response.json()
    assert report["state"] == "verified"
    assert report["checks"][0]["name"] == "listing"
    assert "inference capability untested" in report["checks"][0]["detail"]
    assert FAKE_BASETEN_KEY not in response.text


def test_baseten_smoke_rejected_key_is_failed(client, token, monkeypatch):
    monkeypatch.setenv("BASETEN_API_KEY", FAKE_BASETEN_KEY)

    def handler(request):
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    install_transport(monkeypatch, mock_transport(handler))
    response = client.post("/api/providers/baseten/smoke", headers=headers(token))
    assert response.json()["state"] == "failed"


# --- Sanitizer -------------------------------------------------------------------


def test_sanitize_redacts_secrets_and_bearers():
    text = (
        "key=META-SECRET-KEY-VALUE Authorization: Bearer META-SECRET-KEY-VALUE "
        "bearer abcdefgh12345678"
    )
    cleaned = sanitize(text, ["META-SECRET-KEY-VALUE"])
    assert "META-SECRET-KEY-VALUE" not in cleaned
    assert "abcdefgh12345678" not in cleaned
    assert cleaned.count("[redacted]") >= 2
