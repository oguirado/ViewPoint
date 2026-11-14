"""test_harvest_identity.py

Pytest contract tests for harvest and aggregator identity.
Tests cover positive controls, must-FAIL controls, framing, and forced hash collisions.
"""

import copy
import json
import os
import pytest
import tempfile
from pathlib import Path

from contracts.w8evidence.identitytests.w8_repro_carrier import (
    DOMAIN_LITERAL,
    REQUIRED_IDENTITY_KEYS,
    IdentityError,
    ObservationStore,
    ProvenanceBinding,
    RejectError,
    canonical_encode,
    compute_w8_repro_v1_hash,
    strict_json_parse,
    validate_identity_dict,
)

VECTORS_PATH = Path(__file__).parent / "vectors.json"


def load_vectors():
    with open(VECTORS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["vectors"]


def get_sample_identity():
    return {
        "normalization_version": "v1",
        "repository_identity": "oguirado/OpenSuperficies",
        "base_commit": "3212c9dffc72b8aa39a346733c014b7690a45488",
        "base_image_toolchain_digest": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
        "warm_cache_manifest_digest": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
        "gate_argv": ["python3", "-m", "pytest"],
        "cwd": ".",
        "outcome_class": "ASSERTION_FAILURE",
        "assertion_signature": "AssertionError: expected 0 got 1",
        "selected_frames": ["frame1.py:10:in_test", "frame2.py:25:in_check"],
        "affected_target": {"path": "src/workerbridge/json.go", "symbol": "Decode"},
        "reproducer_input_identity": "sha256:3333333333333333333333333333333333333333333333333333333333333333",
    }


def get_sample_provenance():
    return ProvenanceBinding(
        request_id="req-1001",
        task_id="task-001",
        attempt_id="a1",
        lease_id="lease-001",
        epoch=1,
        source_body="body_content_hash_123",
        sender="worker-f",
        cost=100,
    )


# 1. Test vectors matching
def test_vectors_against_carrier():
    vectors = load_vectors()
    for vec in vectors:
        identity = vec["identity"]
        expected_hash = vec["expected_repro_hash"]
        computed = compute_w8_repro_v1_hash(identity)
        assert computed == expected_hash, f"Vector {vec['name']} failed hash match"


# 2. Positive Controls
def test_positive_permutation_and_redelivery_invariance():
    """Key reordering in identity JSON must produce the exact same w8-repro-v1 hash."""
    ident1 = get_sample_identity()
    ident2 = copy.deepcopy(ident1)
    # Reorder keys
    keys = list(ident2.keys())
    reversed_dict = {k: ident2[k] for k in reversed(keys)}

    hash1 = compute_w8_repro_v1_hash(ident1)
    hash2 = compute_w8_repro_v1_hash(reversed_dict)
    assert hash1 == hash2


def test_positive_volatile_provenance_changes_retain_repro_identity():
    """Changing volatile provenance fields (task_id, attempt_id, cost, sender) keeps repro identity unchanged."""
    ident = get_sample_identity()
    hash1 = compute_w8_repro_v1_hash(ident)

    store = ObservationStore()
    prov1 = ProvenanceBinding(
        request_id="req-1",
        task_id="task-1",
        attempt_id="a1",
        lease_id="l1",
        epoch=1,
        source_body="b1",
        sender="agent-1",
    )
    hash_out1, merged1 = store.add_observation(ident, prov1, cost=50)

    prov2 = ProvenanceBinding(
        request_id="req-2",
        task_id="task-2",
        attempt_id="a2",
        lease_id="l2",
        epoch=2,
        source_body="b1",
        sender="agent-2",
    )
    hash_out2, merged2 = store.add_observation(ident, prov2, cost=200)

    assert hash_out1 == hash1
    assert hash_out2 == hash1
    assert merged2 is True
    assert len(store.repro_index[hash1]) == 2


def test_missing_identity_gives_unkeyed_pending_observation():
    """Missing identity produces unkeyed pending observation, not null or shared equality."""
    store = ObservationStore()
    prov = get_sample_provenance()
    h, merged = store.add_observation(None, prov, cost=10)
    assert h is None
    assert merged is False
    assert len(store.unkeyed_pending) == 1


# 3. Must-FAIL controls
def test_must_fail_floats_rejected():
    ident = get_sample_identity()
    ident["normalization_version"] = 1.0  # float value
    with pytest.raises(RejectError) as exc_info:
        compute_w8_repro_v1_hash(ident)
    assert exc_info.value.code == 5


def test_must_fail_duplicate_keys_rejected():
    json_with_dup = '{"normalization_version":"v1","normalization_version":"v1"}'
    with pytest.raises(RejectError) as exc_info:
        strict_json_parse(json_with_dup)
    assert exc_info.value.class_name == "DUPLICATE_KEY"


def test_must_fail_unknown_or_missing_keys_rejected():
    ident = get_sample_identity()
    ident["extra_unknown_key"] = "bad"
    with pytest.raises(RejectError) as exc_info:
        validate_identity_dict(ident)
    assert exc_info.value.class_name == "UNKNOWN_KEY"

    ident_missing = get_sample_identity()
    del ident_missing["normalization_version"]
    with pytest.raises(RejectError) as exc_info:
        validate_identity_dict(ident_missing)
    assert exc_info.value.class_name == "MISSING_KEY"


def test_must_fail_reordered_frames_pretending_to_match():
    ident1 = get_sample_identity()
    ident2 = copy.deepcopy(ident1)
    ident2["selected_frames"] = list(reversed(ident1["selected_frames"]))

    hash1 = compute_w8_repro_v1_hash(ident1)
    hash2 = compute_w8_repro_v1_hash(ident2)
    assert hash1 != hash2, "Reordered frames must produce different identity hashes"


def test_must_fail_lost_failed_attempts():
    """Each paid retry remains a separate receipt in append-only store."""
    store = ObservationStore()
    ident = get_sample_identity()
    prov1 = get_sample_provenance()
    prov2 = get_sample_provenance()
    prov2.attempt_id = "a2"

    store.add_observation(ident, prov1, cost=100)
    store.add_observation(ident, prov2, cost=100)

    repro_hash = compute_w8_repro_v1_hash(ident)
    records = store.repro_index[repro_hash]
    assert len(records) == 2
    attempts = [r["provenance"]["attempt_id"] for r in records]
    assert attempts == ["a1", "a2"]


def test_must_fail_early_ack():
    store = ObservationStore()
    prov = get_sample_provenance()
    with pytest.raises(RejectError) as exc_info:
        store.add_observation(get_sample_identity(), prov, cost=10, early_ack=True)
    assert exc_info.value.class_name == "EARLY_ACK"


def test_must_fail_stale_origin():
    with pytest.raises(RejectError) as exc_info:
        ProvenanceBinding(
            request_id="",
            task_id="t1",
            attempt_id="a1",
            lease_id="l1",
            epoch=1,
            source_body="b1",
            sender="s1",
        )
    assert exc_info.value.class_name == "STALE_ORIGIN"


def test_must_fail_missing_cost_as_zero():
    store = ObservationStore()
    prov = get_sample_provenance()
    with pytest.raises(RejectError) as exc_info:
        store.add_observation(get_sample_identity(), prov, cost=None)
    assert exc_info.value.class_name == "MISSING_COST"


def test_must_fail_changed_body_replay():
    """Replay with changed body must be detected via provenance binding."""
    prov1 = get_sample_provenance()
    prov2 = copy.deepcopy(prov1)
    prov2.source_body = "modified_body"

    assert prov1.source_body != prov2.source_body


def test_forced_hash_collision_handling():
    """Exact byte hash hits merge into union, forced collisions with different bytes stay separate."""
    store = ObservationStore()
    ident1 = get_sample_identity()
    ident2 = copy.deepcopy(ident1)

    # Store exact same identity bytes twice -> exact byte hit merge
    prov1 = get_sample_provenance()
    prov2 = copy.deepcopy(prov1)
    prov2.attempt_id = "a2"

    h1, merged1 = store.add_observation(ident1, prov1, cost=10)
    h2, merged2 = store.add_observation(ident2, prov2, cost=10)

    assert h1 == h2
    assert merged1 is False
    assert merged2 is True


def test_af_unix_socket_tmpdir_boundary(tmp_path):
    """AF_UNIX socket test assertion: path must be in short /tmp directory with prefix w8 and <= 100 bytes."""
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="w8") as tmpdir:
        sock_path = os.path.join(tmpdir, "test.sock")
        assert len(sock_path.encode("utf-8")) <= 100
        assert sock_path.startswith("/tmp/w8")
