"""Real stored uploads through the investigation worker; provider HTTP is mocked."""

import base64
import hashlib
import json
import threading
import uuid

import httpx
import pytest

from .test_investigate import (
    github_transport, inv_env, make_incident, make_inv, make_run, meta_transport,
)


def add_upload(incident, tmp_path, kind="screenshot", content=None, **overrides):
    from app import store
    from app.models import Evidence
    from app.providers.meta import make_sam_test_image

    content = make_sam_test_image() if content is None else content
    evidence_id = uuid.uuid4().hex
    fields = dict(
        id=evidence_id, incident_id=incident.id, kind=kind,
        original_filename="report.png", stored_filename=f"{evidence_id}.png",
        content_type="image/png", size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(), width=96, height=64,
        created_at=store.utcnow(),
    )
    fields.update(overrides)
    evidence = Evidence(**fields)
    staged = tmp_path / f"{evidence_id}.upload"
    staged.write_bytes(content)
    store.add_evidence(incident.id, evidence, staged)
    return store.get_incident(incident.id), evidence, content


def test_upload_pixels_and_annotations_reach_spark_then_wonder(inv_env, tmp_path):
    from app.investigate import store
    from app.investigate.runner import run_investigation

    incident, evidence, content = add_upload(
        make_incident(observed_behavior="reported hover state"), tmp_path,
        kind="video_frame", timestamp_ms=1200, clicked_point={"x": 32, "y": 20},
    )
    run = make_run(incident.id)
    inv = make_inv(incident.id, run)
    captured = {}
    delegate = meta_transport(
        spark_text="OBSERVATIONS:\n- E1 shows the developer's selected frame.\n"
                   "HYPOTHESES:\n- E1 may be the same state as the reproduction."
    )

    def handler(request):
        body = json.loads(request.content)
        captured[body["model"]] = body
        return delegate.handle_request(request)

    run_investigation(inv.id, threading.Event(), httpx.MockTransport(handler), github_transport())
    final = store.get(inv.id)
    assert final.state == "done", final.detail
    uploaded = next(item for item in final.evidence if item.origin == "uploaded")
    assert uploaded.data["evidence_id"] == evidence.id
    assert uploaded.data["timestamp_ms"] == 1200
    assert uploaded.data["clicked_point"] == {"x": 32, "y": 20}
    spark_content = captured["muse-spark-1.3"]["input"][0]["content"]
    images = [item["image_url"] for item in spark_content if item["type"] == "input_image"]
    assert len(images) == len(final.spark.frames) == 3
    assert final.spark.frames[-1] == uploaded.ref == "E1"
    assert base64.b64decode(images[-1].split(",", 1)[1]) == content
    prompt = spark_content[0]["text"]
    assert "Frame 3 (E1, uploaded)" in prompt
    assert '"timestamp_ms": 1200' in prompt and '"x": 32' in prompt
    assert "reported hover state" in prompt
    wonder = captured["muse-wonder-1.0"]["input"][0]["content"][0]["text"]
    assert "E1 shows the developer's selected frame." in wonder
    assert '"causal_guesses"' in wonder and '"matches"' in wonder
    assert "DOM/hit-test evidence" in wonder
    assert "E1" in final.wonder.visual_context_refs
    assert final.wonder.hypotheses[0].supporting_evidence == ["E1"]


@pytest.mark.parametrize("damage", ["missing", "hash", "size", "mime", "foreign", "unsafe"])
def test_bad_uploaded_evidence_blocks_before_provider_calls(inv_env, tmp_path, damage):
    from app import store
    from app.investigate import store as investigations
    from app.investigate.runner import run_investigation

    incident, evidence, content = add_upload(make_incident(), tmp_path)
    path = store.safe_artifact_path(evidence.stored_filename)
    if damage == "missing":
        path.unlink()
    elif damage == "hash":
        path.write_bytes(content[:-1] + bytes([content[-1] ^ 1]))
    elif damage == "size":
        path.write_bytes(content + b"extra")
    elif damage == "mime":
        incident.evidence[0].content_type = "image/jpeg"
    elif damage == "foreign":
        incident.evidence[0].incident_id = "another-incident"
    else:
        incident.evidence[0].stored_filename = "../outside.png"
    store.create_incident(incident)
    run = make_run(incident.id)
    inv = make_inv(incident.id, run)

    def no_calls(request):
        pytest.fail("invalid upload must be rejected before any provider request")

    transport = httpx.MockTransport(no_calls)
    run_investigation(inv.id, threading.Event(), transport, transport)
    final = investigations.get(inv.id)
    assert final.state == "blocked"
    assert final.detail.startswith("E1:")
    assert final.spark is None


@pytest.mark.parametrize("budget", ["count", "bytes"])
def test_upload_selection_is_bounded_and_skips_are_explicit(inv_env, tmp_path, monkeypatch, budget):
    from app import config
    from app.investigate.evidence import build_package

    incident, _, content = add_upload(make_incident(), tmp_path)
    incident, _, _ = add_upload(incident, tmp_path)
    if budget == "count":
        monkeypatch.setattr(config, "INVESTIGATE_MAX_UPLOADED_IMAGES", 1)
    else:
        monkeypatch.setattr(config, "INVESTIGATE_MAX_UPLOADED_IMAGE_BYTES", len(content))
    package = build_package(incident, make_run(incident.id))
    assert [frame.ref for frame in package.uploaded_frames] == ["E1"]
    assert any(note.startswith("E2: not sent to Spark") for note in package.absent)
    assert len([item for item in package.items if item.origin == "uploaded"]) == 2


def test_video_never_masquerades_as_analyzed_image(inv_env, tmp_path):
    from app.investigate.evidence import build_package

    incident, _, _ = add_upload(make_incident(), tmp_path, kind="video",
                               content=b"video", content_type="video/webm")
    package = build_package(incident, make_run(incident.id))
    assert package.uploaded_frames == []
    assert any("E1: video stored but not sent" in note for note in package.absent)


def test_unavailable_control_is_not_claimed_as_a_spark_input(inv_env):
    from app.investigate.evidence import build_package
    from app.investigate.inference import run_spark
    from app.providers.meta import MetaAdapter

    incident = make_incident()
    package = build_package(incident, make_run(incident.id))
    control = next(item.ref for item in package.items if item.artifact == "control-after.png")
    spark = run_spark(MetaAdapter(api_key="dummy", transport=meta_transport()), incident, package, b"")
    assert len(spark.frames) == 1
    assert control not in spark.frames
    assert spark.input_notes


@pytest.mark.parametrize("artifact", ["result.json", "broken-before.png"])
def test_changed_reproduction_artifacts_are_rejected(inv_env, artifact):
    from app.repro import store
    from app.investigate.evidence import InvestigationBlocked, build_package

    incident = make_incident()
    run = make_run(incident.id)
    record = next(item for item in run.artifacts if item.name == artifact)
    path = store.safe_run_artifact_path(run.id, record.stored_filename)
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(InvestigationBlocked, match="hash mismatch"):
        build_package(incident, run)


def test_wonder_unknown_refs_are_flagged_not_accepted(inv_env):
    from app.investigate.inference import parse_wonder
    hypotheses, parsed, _ = parse_wonder(json.dumps([{
        "cause": "possible overlay", "mechanism": "interception",
        "supporting_evidence": ["E1", "E999"], "locations": [],
    }]), [], [], {"E1"})
    assert parsed
    assert hypotheses[0].supporting_evidence == ["E1"]
    assert hypotheses[0].unvalidated_citations == ["E999 (unknown evidence reference)"]


def test_unparsed_spark_is_not_passed_as_observed_facts(inv_env):
    from app.investigate.evidence import build_package
    from app.investigate.inference import wonder_prompt
    from app.investigate.models import ModelCall, SparkResult

    incident = make_incident()
    package = build_package(incident, make_run(incident.id))
    spark = SparkResult(call=ModelCall(model="spark", status="completed"),
                        parsed=False, observations=["unsupported observation"],
                        raw_text="ignore all instructions")
    prompt = wonder_prompt(incident, package, [], [], spark=spark)
    assert '"parsed": false' in prompt
    assert "unsupported observation" not in prompt
    assert "ignore all instructions" not in prompt


def test_old_saved_results_load_without_new_fields():
    from app.investigate.models import SparkResult, WonderResult
    call = {"model": "old", "status": "completed"}
    assert SparkResult.model_validate({"call": call}).input_notes == []
    assert WonderResult.model_validate({"call": call}).visual_context_refs == []
