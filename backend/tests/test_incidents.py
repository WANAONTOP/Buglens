"""M1 evidence-intake tests: incidents, uploads, limits, auth, traversal."""

import hashlib
import json
import struct
import zlib

import pytest
from fastapi.testclient import TestClient

from app import config
from app import store as store_module
from app.main import app

TEST_TOKEN = "test-token-0123456789abcdef01"
VALID_SHA = "a" * 40


@pytest.fixture()
def token(monkeypatch):
    monkeypatch.setenv("BUGLENS_API_TOKEN", TEST_TOKEN)
    return TEST_TOKEN


@pytest.fixture()
def data_dir(tmp_path, monkeypatch):
    target = tmp_path / "buglens-data"
    monkeypatch.setenv("BUGLENS_DATA_DIR", str(target))
    return target


@pytest.fixture()
def client():
    return TestClient(app)


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def incident_payload(**overrides):
    payload = {
        "title": "Nav clips on scroll",
        "observed_behavior": "The top nav is cut off after scrolling.",
        "expected_behavior": "The nav stays fully visible.",
        "reproduction_steps": ["Open the page", "Scroll down 500px"],
        "repo": "octo/demo",
        "ref": "main",
        "browser_viewport": "1440x900",
        "permitted_source_paths": ["src/nav.tsx"],
    }
    payload.update(overrides)
    return payload


def create_incident(client, token, **overrides):
    response = client.post(
        "/api/incidents", json=incident_payload(**overrides), headers=headers(token)
    )
    assert response.status_code == 201, response.text
    return response.json()


def upload(client, token, incident_id, content, filename, content_type, **form):
    form.setdefault("kind", "screenshot")
    return client.post(
        f"/api/incidents/{incident_id}/evidence",
        files={"file": (filename, content, content_type)},
        data=form,
        headers=headers(token),
    )


# --- Minimal valid media builders -------------------------------------------


def make_png(width=3, height=2):
    signature = bytes.fromhex("89504e470d0a1a0a")

    def chunk(ctype, payload):
        body = ctype + payload
        return struct.pack(">I", len(payload)) + body + struct.pack(
            ">I", zlib.crc32(body) & 0xFFFFFFFF
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    rows = b"".join(b"\x00" + bytes([row % 256, 0, 0]) * width for row in range(height))
    return (
        signature
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def make_jpeg(width=4, height=5):
    sof0 = b"\xff\xc0\x00\x0b\x08" + struct.pack(">HH", height, width) + b"\x01\x01\x11\x00"
    return b"\xff\xd8" + sof0 + b"\xff\xd9"


def make_webp(width=7, height=9):
    canvas = (
        b"\x00"
        + (width - 1).to_bytes(3, "little")
        + (height - 1).to_bytes(3, "little")
        + b"\x00\x00\x00"
    )
    vp8x = b"VP8X" + struct.pack("<I", len(canvas)) + canvas
    return b"RIFF" + struct.pack("<I", 4 + len(vp8x)) + b"WEBP" + vp8x


def make_mp4():
    return b"\x00\x00\x00\x18ftypisom\x00\x00\x00\x00" + b"\x00" * 64


def make_webm():
    return bytes.fromhex("1a45dfa3") + b"\x00" * 64


# --- Incidents ---------------------------------------------------------------


def test_create_get_list_incidents(client, token, data_dir):
    created = create_incident(client, token)
    assert created["intake_status"] == "blocked"
    assert created["blockers"] == [
        "missing commit_sha: pin the target commit",
        "missing evidence: upload a screenshot or recording",
    ]

    fetched = client.get(f"/api/incidents/{created['id']}", headers=headers(token))
    assert fetched.status_code == 200
    assert fetched.json()["id"] == created["id"]

    second = create_incident(client, token, title="Second bug")
    listing = client.get("/api/incidents", headers=headers(token))
    assert listing.status_code == 200
    ids = [item["id"] for item in listing.json()]
    assert ids == [second["id"], created["id"]]  # newest first
    assert listing.json()[0]["evidence_count"] == 0

    assert client.get("/api/incidents/does-not-exist", headers=headers(token)).status_code == 404
    assert client.get(f"/api/incidents/{'c' * 32}", headers=headers(token)).status_code == 404


@pytest.mark.parametrize(
    "overrides",
    [
        {"title": "   "},
        {"observed_behavior": ""},
        {"reproduction_steps": []},
        {"reproduction_steps": ["  "]},
        {"repo": "noslash-here"},
        {"repo": "owner/na me"},
        {"ref": "x" * 257},
        {"browser_viewport": "wide"},
        {"browser_viewport": "99x99"},
        {"commit_sha": "short"},
        {"commit_sha": "z" * 40},
        {"permitted_source_paths": [""]},
        {"permitted_source_paths": ["/abs/path.ts"]},
        {"permitted_source_paths": ["C:\\win\\path.ts"]},
        {"permitted_source_paths": ["src/../escape.ts"]},
    ],
)
def test_create_incident_rejects_invalid_payloads(client, token, data_dir, overrides):
    response = client.post(
        "/api/incidents", json=incident_payload(**overrides), headers=headers(token)
    )
    assert response.status_code == 422, response.text


def test_pin_commit_moves_status(client, token, data_dir):
    created = create_incident(client, token)
    bad = client.patch(
        f"/api/incidents/{created['id']}/commit",
        json={"commit_sha": "not-a-sha"},
        headers=headers(token),
    )
    assert bad.status_code == 422

    pinned = client.patch(
        f"/api/incidents/{created['id']}/commit",
        json={"commit_sha": VALID_SHA.upper()},
        headers=headers(token),
    )
    assert pinned.status_code == 200
    assert pinned.json()["commit_sha"] == VALID_SHA
    assert pinned.json()["intake_status"] == "blocked"  # still no evidence

    assert (
        client.patch(
            f"/api/incidents/{'d' * 32}/commit",
            json={"commit_sha": VALID_SHA},
            headers=headers(token),
        ).status_code
        == 404
    )


# --- Drafts and investigation gating -----------------------------------------


def test_create_draft_without_repository_details(client, token, data_dir):
    minimal = {
        "title": "Screenshot-only draft",
        "observed_behavior": "Something looks wrong.",
        "expected_behavior": "Something looks right.",
        "reproduction_steps": ["Open the page"],
    }
    response = client.post("/api/incidents", json=minimal, headers=headers(token))
    assert response.status_code == 201, response.text
    draft = response.json()
    assert draft["repo"] is None
    assert draft["ref"] is None
    assert draft["commit_sha"] is None
    assert draft["browser_viewport"] is None
    assert draft["investigation_blockers"] == [
        "missing repository: set the owner/name to investigate",
        "missing ref: set the branch or ref to investigate",
        "missing commit_sha: pin the target commit",
        "missing permitted_source_paths: approve which files may change",
    ]
    # Drafts still appear in listings with null repository fields.
    listing = client.get("/api/incidents", headers=headers(token)).json()
    assert listing[0]["repo"] is None
    assert listing[0]["ref"] is None


def test_blank_repository_details_normalize_to_missing(client, token, data_dir):
    created = create_incident(
        client, token, repo="  ", ref="", browser_viewport="", permitted_source_paths=[]
    )
    assert created["repo"] is None
    assert created["ref"] is None
    assert created["browser_viewport"] is None


def test_investigation_blockers_shrink_as_details_land(client, token, data_dir):
    created = create_incident(client, token, permitted_source_paths=[])
    assert created["investigation_blockers"] == [
        "missing commit_sha: pin the target commit",
        "missing permitted_source_paths: approve which files may change",
    ]


def test_incomplete_draft_cannot_start_investigation(client, token, data_dir):
    from fastapi import HTTPException

    from app import guards
    from app.models import Incident

    draft_body = client.post(
        "/api/incidents",
        json={
            "title": "Draft",
            "observed_behavior": "o",
            "expected_behavior": "e",
            "reproduction_steps": ["s"],
        },
        headers=headers(token),
    ).json()
    with pytest.raises(HTTPException) as exc_info:
        guards.require_investigation_ready(Incident(**draft_body))
    assert exc_info.value.status_code == 409
    assert len(exc_info.value.detail["missing"]) == 4

    # A fully specified incident passes the gate (execution itself lands later).
    full_body = create_incident(
        client, token, commit_sha=VALID_SHA, permitted_source_paths=["src/a.ts"]
    )
    assert guards.require_investigation_ready(Incident(**full_body)) is None
    refetched = client.get(f"/api/incidents/{full_body['id']}", headers=headers(token)).json()
    assert refetched["investigation_blockers"] == []


# --- Uploads -----------------------------------------------------------------


def test_upload_png_roundtrip(client, token, data_dir):
    created = create_incident(client, token)
    body = make_png(3, 2)
    response = upload(client, token, created["id"], body, "shot.png", "image/png")
    assert response.status_code == 201, response.text
    evidence = response.json()
    assert evidence["kind"] == "screenshot"
    assert evidence["width"] == 3 and evidence["height"] == 2
    assert evidence["size_bytes"] == len(body)
    assert evidence["sha256"] == hashlib.sha256(body).hexdigest()
    assert evidence["stored_filename"].endswith(".png")
    assert evidence["stored_filename"] != "shot.png"  # server-generated name

    incident = client.get(
        f"/api/incidents/{created['id']}", headers=headers(token)
    ).json()
    assert len(incident["evidence"]) == 1

    download = client.get(
        f"/api/incidents/{created['id']}/evidence/{evidence['id']}/download",
        headers=headers(token),
    )
    assert download.status_code == 200
    assert download.content == body
    assert download.headers["content-type"] == "image/png"


@pytest.mark.parametrize(
    "content,filename,content_type,kind,extra",
    [
        (make_jpeg(), "photo.jpg", "image/jpeg", "screenshot", {}),
        (make_jpeg(), "photo.jpeg", "image/jpg", "screenshot", {}),  # alias accepted
        (make_webp(), "shot.webp", "image/webp", "screenshot", {}),
        (
            make_mp4(),
            "rec.mp4",
            "video/mp4",
            "video",
            {"client_width": "1280", "client_height": "720", "client_duration_ms": "1500"},
        ),
        (make_webm(), "rec.webm", "video/webm", "video", {}),
        (
            make_png(),
            "frame.png",
            "image/png",
            "video_frame",
            {"timestamp_ms": "2500"},  # local-source frame: no source id required
        ),
    ],
)
def test_upload_supported_media(client, token, data_dir, content, filename, content_type, kind, extra):
    created = create_incident(client, token)
    response = upload(client, token, created["id"], content, filename, content_type, kind=kind, **extra)
    assert response.status_code == 201, response.text
    evidence = response.json()
    assert evidence["content_type"] in (
        "image/png",
        "image/jpeg",
        "image/webp",
        "video/mp4",
        "video/webm",
    )
    if kind == "video" and extra:
        assert evidence["width"] == 1280
        assert evidence["duration_ms"] == 1500
    if kind == "video_frame":
        assert evidence["timestamp_ms"] == 2500


def test_upload_rejects_unsupported_content(client, token, data_dir):
    created = create_incident(client, token)
    assert (
        upload(client, token, created["id"], b"just text", "note.txt", "text/plain").status_code
        == 415
    )
    assert (
        upload(client, token, created["id"], b"\x00" * 64, "fake.png", "image/png").status_code
        == 415
    )
    # Declared type disagrees with magic bytes.
    assert (
        upload(client, token, created["id"], make_png(), "shot.png", "image/jpeg").status_code
        == 415
    )
    # Kind disagrees with bytes.
    assert (
        upload(client, token, created["id"], make_png(), "shot.png", "image/png", kind="video").status_code
        == 415
    )
    assert (
        upload(client, token, created["id"], b"", "empty.png", "image/png").status_code == 400
    )


def test_upload_enforces_size_limit(client, token, data_dir, monkeypatch):
    created = create_incident(client, token)
    monkeypatch.setattr(config, "MAX_IMAGE_BYTES", 16)
    response = upload(client, token, created["id"], make_png(), "big.png", "image/png")
    assert response.status_code == 413


def test_upload_enforces_incident_caps(client, token, data_dir, monkeypatch):
    created = create_incident(client, token)
    monkeypatch.setattr(config, "MAX_EVIDENCE_PER_INCIDENT", 1)
    first = upload(client, token, created["id"], make_png(), "one.png", "image/png")
    assert first.status_code == 201
    second = upload(client, token, created["id"], make_png(), "two.png", "image/png")
    assert second.status_code == 413


def test_upload_enforces_incident_byte_budget(client, token, data_dir, monkeypatch):
    created = create_incident(client, token)
    monkeypatch.setattr(config, "MAX_INCIDENT_BYTES", 10)
    response = upload(client, token, created["id"], make_png(), "shot.png", "image/png")
    assert response.status_code == 413


def test_video_frame_source_rules(client, token, data_dir):
    created = create_incident(client, token)
    video = upload(
        client, token, created["id"], make_webm(), "rec.webm", "video/webm", kind="video"
    ).json()
    shot = upload(client, token, created["id"], make_png(), "s.png", "image/png").json()

    ok = upload(
        client,
        token,
        created["id"],
        make_png(),
        "frame.png",
        "image/png",
        kind="video_frame",
        timestamp_ms="120",
        source_video_evidence_id=video["id"],
    )
    assert ok.status_code == 201, ok.text
    assert ok.json()["source_video_evidence_id"] == video["id"]

    missing_ts = upload(
        client, token, created["id"], make_png(), "f.png", "image/png", kind="video_frame"
    )
    assert missing_ts.status_code == 400

    bad_source = upload(
        client,
        token,
        created["id"],
        make_png(),
        "f.png",
        "image/png",
        kind="video_frame",
        timestamp_ms="1",
        source_video_evidence_id="e" * 32,
    )
    assert bad_source.status_code == 400

    not_a_video = upload(
        client,
        token,
        created["id"],
        make_png(),
        "f.png",
        "image/png",
        kind="video_frame",
        timestamp_ms="1",
        source_video_evidence_id=shot["id"],
    )
    assert not_a_video.status_code == 400

    jpeg_frame_rejected = upload(
        client,
        token,
        created["id"],
        make_webp(),
        "f.webp",
        "image/webp",
        kind="video_frame",
        timestamp_ms="1",
    )
    assert jpeg_frame_rejected.status_code == 415


def test_clicked_point_rules(client, token, data_dir):
    created = create_incident(client, token)
    marked = upload(
        client, token, created["id"], make_png(), "s.png", "image/png", clicked_x="10", clicked_y="20"
    )
    assert marked.status_code == 201, marked.text
    assert marked.json()["clicked_point"] == {"x": 10, "y": 20}

    on_video = upload(
        client, token, created["id"], make_webm(), "r.webm", "video/webm",
        kind="video", clicked_x="1", clicked_y="1",
    )
    assert on_video.status_code == 400

    half = upload(
        client, token, created["id"], make_png(), "s.png", "image/png", clicked_x="1"
    )
    assert half.status_code == 400


def test_evil_filename_never_reaches_disk(client, token, data_dir):
    created = create_incident(client, token)
    body = make_png()
    response = upload(
        client, token, created["id"], body, "../../etc/passwd", "image/png"
    )
    assert response.status_code == 201, response.text
    evidence = response.json()
    assert "/" not in evidence["stored_filename"] and "\\" not in evidence["stored_filename"]
    assert "/" not in evidence["original_filename"]
    artifacts = list((data_dir / "artifacts").glob("*"))
    assert len(artifacts) == 1 and artifacts[0].name == evidence["stored_filename"]


def test_download_missing_bytes_is_server_error(client, token, data_dir):
    created = create_incident(client, token)
    evidence = upload(client, token, created["id"], make_png(), "s.png", "image/png").json()
    (data_dir / "artifacts" / evidence["stored_filename"]).unlink()
    response = client.get(
        f"/api/incidents/{created['id']}/evidence/{evidence['id']}/download",
        headers=headers(token),
    )
    assert response.status_code == 500


# --- Auth --------------------------------------------------------------------


def test_api_requires_bearer_token(client, token, data_dir):
    payload = incident_payload()
    assert client.post("/api/incidents", json=payload).status_code == 401
    assert client.get("/api/incidents").status_code == 401
    assert client.get(f"/api/incidents/{'a' * 32}").status_code == 401
    assert (
        client.post(
            f"/api/incidents/{'a' * 32}/evidence", files={"file": ("x", b"x")}
        ).status_code
        == 401
    )
    assert (
        client.get(f"/api/incidents/{'a' * 32}/evidence/{'b' * 32}/download").status_code
        == 401
    )
    wrong = {"Authorization": "Bearer wrong-token-0123456789"}
    assert client.get("/api/incidents", headers=wrong).status_code == 401
    assert (
        client.patch(
            f"/api/incidents/{'a' * 32}/commit", json={"commit_sha": VALID_SHA}
        ).status_code
        == 401
    )


def test_missing_server_token_is_503_but_health_is_public(client, data_dir, monkeypatch):
    monkeypatch.delenv("BUGLENS_API_TOKEN", raising=False)
    assert client.get("/api/incidents", headers=headers(TEST_TOKEN)).status_code == 503
    assert client.get("/health").status_code == 200


def test_wrong_scheme_rejected_when_configured(client, token, data_dir):
    assert (
        client.get("/api/incidents", headers={"Authorization": "Token " + TEST_TOKEN}).status_code
        == 401
    )


# --- Traversal ---------------------------------------------------------------


@pytest.mark.parametrize("bad_id", ["..", "../x", "%2e%2e", "not-an-id", "A" * 32, "abc"])
def test_malformed_ids_are_404(client, token, data_dir, bad_id):
    assert (
        client.get(f"/api/incidents/{bad_id}", headers=headers(token)).status_code == 404
    )
    created = create_incident(client, token)
    assert (
        client.get(
            f"/api/incidents/{created['id']}/evidence/{bad_id}/download",
            headers=headers(token),
        ).status_code
        == 404
    )


@pytest.mark.parametrize("evil", ["../evil.png", "..\\evil.png", "sub/evil.png", "/abs/evil.png", ""])
def test_safe_artifact_path_rejects_escape(data_dir, evil):
    with pytest.raises(ValueError):
        store_module.safe_artifact_path(evil)


def test_safe_artifact_path_accepts_plain_name(data_dir):
    path = store_module.safe_artifact_path("abc123.png")
    assert path.parent == (data_dir / "artifacts").resolve()


# --- Persistence -------------------------------------------------------------


def test_records_and_bytes_survive_restart(client, token, data_dir):
    created = create_incident(client, token, commit_sha=VALID_SHA)
    body = make_png(3, 2)
    evidence = upload(client, token, created["id"], body, "s.png", "image/png").json()

    records_file = data_dir / "incidents.json"
    artifact_file = data_dir / "artifacts" / evidence["stored_filename"]
    assert records_file.exists() and artifact_file.exists()

    # Fresh client against the same data dir: the store keeps no in-memory
    # state, so this is equivalent to reading after a backend restart.
    fresh = TestClient(app)
    refetched = fresh.get(
        f"/api/incidents/{created['id']}", headers=headers(token)
    )
    assert refetched.status_code == 200
    assert refetched.json()["intake_status"] == "intake_complete"
    redownload = fresh.get(
        f"/api/incidents/{created['id']}/evidence/{evidence['id']}/download",
        headers=headers(token),
    )
    assert redownload.status_code == 200
    assert redownload.content == body

    with open(records_file, encoding="utf-8") as handle:
        raw = json.load(handle)
    assert created["id"] in raw["incidents"]
