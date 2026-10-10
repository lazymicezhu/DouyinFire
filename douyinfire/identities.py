"""Stable recipient identities shared by configuration, browser, and outbox."""
from __future__ import annotations

import base64
import binascii
import re
import unicodedata


GROUP_PREFIX = "group:"
MAX_CONVERSATION_ID_BYTES = 512
_MAX_GROUP_TOKEN_LENGTH = (MAX_CONVERSATION_ID_BYTES * 4 + 2) // 3


def _conversation_bytes(conversation_id: str) -> bytes:
    if not isinstance(conversation_id, str) or not conversation_id:
        raise ValueError("群会话标识必须是非空字符串。")
    if len(conversation_id) > MAX_CONVERSATION_ID_BYTES or any(
        char.isspace() or unicodedata.category(char) in {"Cc", "Cf", "Cs"}
        for char in conversation_id
    ):
        raise ValueError("群会话标识包含空白、控制字符或超出长度限制。")
    encoded = conversation_id.encode("utf-8")
    if len(encoded) > MAX_CONVERSATION_ID_BYTES:
        raise ValueError("群会话标识超出长度限制。")
    return encoded


def group_identity(conversation_id: str) -> str:
    """Encode an exact platform string ID; never infer an ID from a display name."""
    token = base64.urlsafe_b64encode(_conversation_bytes(conversation_id)).decode("ascii").rstrip("=")
    return GROUP_PREFIX + token


def group_conversation_id(identity: str) -> str:
    """Decode only the canonical group identity. Invalid input raises ValueError."""
    if not isinstance(identity, str) or not identity.startswith(GROUP_PREFIX):
        raise ValueError("群身份必须使用 group: 标识。")
    token = identity[len(GROUP_PREFIX):]
    if not token or len(token) > _MAX_GROUP_TOKEN_LENGTH or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise ValueError("群身份编码无效。")
    try:
        decoded = base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        conversation_id = decoded.decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as exc:
        raise ValueError("群身份编码无效。") from exc
    if group_identity(conversation_id) != identity:
        raise ValueError("群身份必须使用规范编码。")
    return conversation_id


def validate_contact_identity(kind: str, identity: str, *, allow_empty: bool = True) -> str:
    """Validate recipient kind without rewriting any existing direct-message ID."""
    if not isinstance(kind, str) or kind not in {"direct", "group"}:
        raise ValueError("联系人类型必须是 direct 或 group。")
    if not isinstance(identity, str):
        raise ValueError("联系人身份必须是字符串。")
    if not identity:
        if allow_empty:
            return identity
        raise ValueError("请先在网页中核对并绑定联系人。")
    if kind == "group":
        group_conversation_id(identity)
    elif identity.startswith(GROUP_PREFIX):
        raise ValueError("私聊联系人不能使用群身份。")
    return identity
