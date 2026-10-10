import base64

import pytest

from douyinfire.identities import (
    MAX_CONVERSATION_ID_BYTES,
    group_conversation_id,
    group_identity,
    validate_contact_identity,
)


@pytest.mark.parametrize("raw", ["1234567890123456789", "0:1:synthetic_group-id", "合成群标识", "x" * MAX_CONVERSATION_ID_BYTES])
def test_group_identity_round_trip_is_exact_and_canonical(raw):
    identity = group_identity(raw)
    assert identity.startswith("group:") and "=" not in identity
    assert group_conversation_id(identity) == raw
    assert validate_contact_identity("group", identity, allow_empty=False) == identity


@pytest.mark.parametrize("raw", [
    "", " ", "a b", "group\nname", "a\x00b", "a\x7fb", "a\x85b", "a\u200bb",
    "a\ud800b", "x" * (MAX_CONVERSATION_ID_BYTES + 1), "群" * 171,
    None, 1234567890123456789, 1.2345678901234568e18, True,
])
def test_group_id_rejects_invalid_values_without_coercion(raw):
    with pytest.raises(ValueError):
        group_identity(raw)


@pytest.mark.parametrize("identity", [
    "", "group:", "Group:YQ", "group:YQ=", "group:YQ==", "group:YR",
    "group:YQ\n", "group:YQ ", "group:YQ/", "group:+w", "group:A", "group:_w",
    "group:" + "A" * 684, "direct-id", None, 123,
])
def test_group_identity_rejects_noncanonical_or_invalid_encoding(identity):
    with pytest.raises(ValueError):
        group_conversation_id(identity)


def test_encoded_control_character_is_rejected_after_decoding():
    identity = "group:" + base64.urlsafe_b64encode(b"id\x00suffix").decode().rstrip("=")
    with pytest.raises(ValueError):
        group_conversation_id(identity)


def test_contact_validation_preserves_direct_ids_and_allows_unbound_setup():
    original = "legacy-secUid_123-ABC"
    assert validate_contact_identity("direct", original) == original
    for kind in ("direct", "group"):
        assert validate_contact_identity(kind, "") == ""
        with pytest.raises(ValueError):
            validate_contact_identity(kind, "", allow_empty=False)


@pytest.mark.parametrize("kind,identity", [
    ("private", "id"), (None, "id"), ([], "id"),
    ("group", "legacy-secUid"), ("direct", group_identity("1234567890123456789")),
    ("direct", None), ("group", 1234567890123456789),
])
def test_contact_kind_and_identity_must_match(kind, identity):
    with pytest.raises(ValueError):
        validate_contact_identity(kind, identity)
