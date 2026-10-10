from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

from .config import BrowserConfig, TimeoutsConfig
from .identities import group_conversation_id, group_identity

# The real signed-in headless session reached /jingxuan; the bare root routed
# the same profile to a verification interstitial. Use the observed entry route.
DOUYIN_URL = "https://www.douyin.com/jingxuan"
_PROFILE_HOSTS = frozenset({"www.douyin.com", "douyin.com"})
_CONTACT_HOSTS = _PROFILE_HOSTS | {"v.douyin.com"}
_IDENTITY = re.compile(r"[A-Za-z0-9_-]{1,256}\Z")


class BrowserError(RuntimeError):
    """An operation failed before a message could be submitted."""


class BrowserInterrupted(BrowserError):
    """The user interrupted an operation before submission."""


class LoginRequired(BrowserError):
    """The saved profile needs the user's attention."""


class UnsupportedPage(BrowserError):
    """The observed page cannot provide the required identity or receipt."""


class SendUncertain(BrowserError):
    """Submission may have happened; do not retry automatically."""


class PageAdapter(Protocol):
    """Page observations and actions, separated from submission bookkeeping.

    Observations must come from the rendered website, not display-name guesses.
    read_account returns None only when the page is still waiting for login.
    confirm_message returns a durable platform receipt or None while pending.
    Implementations must never submit from any method except trigger_send.
    """

    def read_account(self, page: Any) -> dict[str, str] | None: ...
    def read_contact(self, page: Any) -> dict[str, str]: ...
    def list_conversations(self, page: Any, kind: str = "group") -> list[dict[str, str]]: ...
    def resolve_conversation(self, page: Any, identity: str, kind: str = "group") -> dict[str, str]: ...
    def open_conversation(self, page: Any, identity: str) -> None: ...
    def conversation_identity(self, page: Any) -> str: ...
    def prepare_message(self, page: Any, message: str) -> None: ...
    def message_snapshot(self, page: Any) -> set[str]: ...
    def trigger_send(self, page: Any) -> None: ...
    def confirm_message(self, page: Any, before: Any, message: str, identity: str) -> dict[str, str] | None: ...


class DouyinDOMAdapter:
    """Conservative production adapter; unfamiliar pages fail before sending.

    These paths were observed on the signed-in website. Read only the necessary
    fields attached to specific rendered nodes; never inspect a global store,
    invoke an internal API, or identify a conversation from its display name.
    Historical receipts establish a schema, not a live-send acceptance test.
    """

    _DIALOG = "[data-e2e='im-dialog']"
    _HEADER = _DIALOG + " .StackLayoutStackChatHeaderuser"
    _EDITOR = _DIALOG + " [data-e2e='msg-input'] [contenteditable='true'][data-slate-editor]"
    _EDITOR_TEXT = """element => {
        // innerText adds an extra newline for Chromium's <div><br></div>.
        // Preserve each logical block/BR so blank lines and emoji stay exact.
        function read(node) {
            if (node.nodeType === Node.TEXT_NODE) return node.nodeValue.replace(/\\uFEFF/g, '');
            if (node.nodeType !== Node.ELEMENT_NODE) return '';
            if (node.hasAttribute('data-slate-zero-width') && /^[\\uFEFF\\u200B]*$/.test(node.textContent || '')) return '';
            // Observed live editor: ace-line ends with this dedicated empty
            // leaf even after input. Its ZWSP is a caret sentinel, not text.
            // Never trim zero-width characters from an ordinary text leaf.
            if (node.matches('span[data-string="true"][data-enter="true"][data-leaf="true"]')
                && node.parentElement?.matches('.ace-line[data-node="true"]')
                && node.childElementCount === 0 && /^[\\uFEFF\\u200B]+$/.test(node.textContent || '')) return '';
            if (node.tagName === 'BR') return '\\n';
            let result = '', previousBlock = false;
            for (const [index, child] of [...node.childNodes].entries()) {
                const block = child.nodeType === Node.ELEMENT_NODE && ['DIV', 'P'].includes(child.tagName);
                if (index > 0 && (block || previousBlock)) result += '\\n';
                let value = read(child);
                if (block && value === '\\n') value = '';
                result += value;
                previousBlock = block;
            }
            return result;
        }
        return read(element);
    }"""
    _HEADER_ID = """e => {
        const key = Object.keys(e).find(k => k.startsWith('__reactProps'));
        return e[key]?.children?.[0]?.props?.conversation?.toParticipantSecUserId ?? null;
    }"""
    _LIST_ID = """e => {
        const key = Object.keys(e).find(k => k.startsWith('__reactProps'));
        return e[key]?.children?.[0]?.props?.children?.props?.conversation?.toParticipantSecUserId ?? null;
    }"""
    _HEADER_TARGET = """e => {
        const key = Object.keys(e).find(k => k.startsWith('__reactProps'));
        const c = e[key]?.children?.[0]?.props?.conversation;
        if (!c) return null;
        return {type: c.type, id: c.id, direct_id: c.toParticipantSecUserId};
    }"""
    _LIST_CONVERSATION = """e => {
        const key = Object.keys(e).find(k => k.startsWith('__reactProps'));
        const c = e[key]?.children?.[0]?.props?.children?.props?.conversation;
        if (!c) return null;
        const names = e.querySelectorAll('.conversationConversationItemtitle');
        return {type: c.type, id: c.id, direct_id: c.toParticipantSecUserId,
                name: names.length === 1 ? names[0].innerText : null};
    }"""
    _BUTTON_READY = """e => {
        const key = Object.keys(e).find(k => k.startsWith('__reactProps'));
        return typeof e[key]?.onClick === 'function';
    }"""
    _MESSAGES = """elements => elements.map(e => {
        const key = Object.keys(e).find(k => k.startsWith('__reactProps'));
        const m = e[key]?.children?.[1]?.props?.children?.[0]?.props?.children?.[1]?.props?.message;
        if (!m) return null;
        const contents = e.querySelectorAll('[data-e2e="msg-item-content"]');
        return {
            server_id: typeof m.serverId === 'string' ? m.serverId : null,
            client_id: typeof m.clientId === 'string' ? m.clientId : null,
            server_status: typeof m.serverStatus === 'number' ? m.serverStatus : null,
            from_me: typeof m.isFromMe === 'boolean' ? m.isFromMe : null,
            sender: typeof m.secSender === 'string' ? m.secSender : null,
            conversation_id: typeof m.conversationId === 'string' ? m.conversationId : null,
            conversation_type: typeof m.conversationType === 'number' ? m.conversationType : null,
            created_at: m.createdAt instanceof Date ? m.createdAt.getTime() : null,
            text: contents.length === 1 ? contents[0].innerText : null
        };
    })"""

    def __init__(self, check_interrupted: Callable[[], None] | None = None, expected_account_id: str = "") -> None:
        self._check_interrupted = check_interrupted or (lambda: None)
        self.expected_account_id = expected_account_id
        self._own_page: Any = None
        self._own_page_url = ""
        self._snapshot_page: Any = None
        self._snapshot_identity = ""
        self._snapshot_account = ""
        self._snapshot_ids: set[str] = set()
        self._snapshot_clients: set[str] = set()
        self._prepared_page: Any = None
        self._prepared_message = ""
        self._triggered_at: float | None = None

    def _wait(self, page: Any, observe: Callable[[], Any], seconds: float = 15, required: bool = True) -> Any:
        deadline = time.monotonic() + seconds
        while True:
            self._check_interrupted()
            if _verification_required(page):
                raise LoginRequired("抖音要求人工验证，请从网页重新打开登录窗口。")
            result = observe()
            if result:
                return result
            if time.monotonic() >= deadline:
                if not required:
                    return None
                raise UnsupportedPage("会话页面尚未就绪或页面结构已变化，已停止发送。")
            page.wait_for_timeout(200)

    def read_account(self, page: Any) -> dict[str, str] | None:
        if not _allowed_navigation_url(page.url) or urlsplit(page.url).hostname not in _PROFILE_HOSTS:
            raise UnsupportedPage("当前页面不是可核验的抖音账号页面。")
        # Observed on the signed-in site: /user/self navigation + the document's
        # RENDER_DATA.app.user (isLogin, statusCode, info.secUid, info.nickname).
        # Never substitute a feed author's profile, nickname or avatar for this.
        account = self._rendered_account(page)
        if page is not self._own_page or page.url != self._own_page_url:
            self._own_page = None
            entries = []
            links = page.locator("a[href]")
            for index in range(min(links.count(), 500)):
                link = links.nth(index)
                if not link.is_visible():
                    continue
                href = link.get_attribute("href", timeout=1000) or ""
                target = urljoin(page.url, href)
                try:
                    parts = urlsplit(target)
                except ValueError:
                    continue
                if _allowed_navigation_url(target) and parts.hostname in _PROFILE_HOSTS and parts.path.rstrip("/") == "/user/self":
                    # The real site renders the same self-profile entry in the
                    # sidebar, avatar menu and history tabs with different query
                    # parameters. They all identify the same signed-in profile.
                    entries.append("https://www.douyin.com/user/self")
            if len(set(entries)) > 1:
                raise UnsupportedPage("页面出现多个不同的个人主页入口，无法可靠核对账号。")
            if not entries:
                if account is None:
                    return None
                if _one_visible(page.get_by_role("button", name="登录", exact=True)) is not None:
                    return None
                raise UnsupportedPage("页面没有可核验的本人主页入口，暂时无法绑定登录账号。")
            if account:
                return self._record_account(account)
            # Login can update the live page without replacing its initial SSR
            # document. Following the observed self entry obtains a fresh one.
            page.goto(entries[0], wait_until="domcontentloaded", timeout=10_000)
            if not _allowed_navigation_url(page.url):
                raise LoginRequired("个人主页跳转异常，请在登录窗口重新登录。")
            if _verification_required(page):
                return None
            self._own_page = page
            self._own_page_url = page.url
            account = self._rendered_account(page)
        if account is False:
            raise UnsupportedPage("页面缺少已验证格式的稳定账号标识，登录态尚未绑定。")
        return self._record_account(account) if account else None

    def _record_account(self, account: dict[str, str]) -> dict[str, str]:
        if self.expected_account_id and account["identity"] != self.expected_account_id:
            raise LoginRequired("当前账号与本次已核对的账号不一致，请重新登录正确账号。")
        self.expected_account_id = account["identity"]
        return account

    @staticmethod
    def _rendered_account(page: Any) -> dict[str, str] | None | bool:
        scripts = page.locator("script#RENDER_DATA[type='application/json']")
        if scripts.count() != 1:
            return False
        raw = scripts.nth(0).text_content(timeout=1000) or ""
        if not raw or len(raw) > 4_000_000:
            return False
        try:
            data = json.loads(unquote(raw))
            user = data["app"]["user"]
            if user.get("isLogin") is False:
                return None
            if user.get("isLogin") is not True or type(user.get("statusCode")) is not int or user["statusCode"] != 0:
                return False
            info = user["info"]
            identity, name = info["secUid"], info["nickname"]
            if not isinstance(identity, str) or not _IDENTITY.fullmatch(identity) or not isinstance(name, str) or not name.strip():
                return False
            return {"identity": identity, "name": name.strip()}
        except (KeyError, TypeError, ValueError, AttributeError):
            return False

    def read_contact(self, page: Any) -> dict[str, str]:
        identity = _profile_identity(page.url)
        # Observed on the actual profile: other h1s belong to surrounding content.
        heading = _one_visible(page.locator("[data-e2e='user-info'] h1"))
        if not identity or heading is None:
            raise UnsupportedPage("联系人主页缺少可核对的身份或姓名，请检查主页链接。")
        name = heading.inner_text(timeout=1000).strip()
        if not name:
            raise UnsupportedPage("联系人主页尚未显示姓名，请稍后重试。")
        return {"identity": identity, "name": name, "profile_url": _canonical_profile(identity)}

    def _loaded_conversations(self, page: Any) -> list[tuple[Any, dict[str, Any]]] | None:
        items = page.locator("[data-e2e='conversation-item']")
        if items.count() > 200:
            raise UnsupportedPage("当前已加载会话过多，请在抖音打开目标会话后重试。")
        rows = []
        for index in range(items.count()):
            self._check_interrupted()
            item = items.nth(index)
            if not item.is_visible():
                continue
            value = item.evaluate(self._LIST_CONVERSATION)
            if not isinstance(value, dict):
                return None
            rows.append((item, value))
        return rows or None

    def _open_message_list(self, page: Any) -> list[tuple[Any, dict[str, Any]]]:
        if _one_visible(page.locator(self._DIALOG)) is None:
            # The visible entry wrapper has no handler. Its observed child does.
            selector = "[data-e2e='im-entry'] [data-e2e='something-button']"

            def entry_ready() -> Any:
                button = _one_visible(page.locator(selector))
                return button if button is not None and button.evaluate(self._BUTTON_READY) else None

            entry = self._wait(page, entry_ready)
            self._check_interrupted()
            entry.click(timeout=1500)
        return self._wait(page, lambda: self._loaded_conversations(page))

    @staticmethod
    def _group_candidate(value: dict[str, Any]) -> dict[str, str]:
        try:
            identity = group_identity(value.get("id"))
        except (ValueError, TypeError) as exc:
            raise UnsupportedPage("页面缺少可靠的群聊会话标识，已停止操作。") from exc
        return _validated_group({"kind": "group", "identity": identity, "name": value.get("name"), "profile_url": ""})

    def _current_account_identity(self, page: Any) -> str:
        account = self._rendered_account(page)
        if not account:
            raise LoginRequired("无法核对当前账号，请重新登录后选择联系人。")
        return self._record_account(account)["identity"]

    @staticmethod
    def _direct_candidate(value: dict[str, Any], account_id: str) -> dict[str, str] | None:
        identity, name = value.get("direct_id"), value.get("name")
        if (
            not isinstance(identity, str) or not _IDENTITY.fullmatch(identity)
            or identity in {account_id, "self", "me"}
            or not isinstance(name, str) or not name.strip()
        ):
            return None
        return {"kind": "direct", "identity": identity, "name": name.strip(), "profile_url": _canonical_profile(identity)}

    def list_conversations(self, page: Any, kind: str = "group") -> list[dict[str, str]]:
        if kind not in {"direct", "group"}:
            raise BrowserError("请选择有效的联系人类型。")
        account_id = self._current_account_identity(page) if kind == "direct" else ""
        rows = self._open_message_list(page)
        candidates = []
        expected_type = 1 if kind == "direct" else 2
        for _, value in rows:
            if type(value.get("type")) is not int or value["type"] != expected_type:
                continue
            candidate = self._direct_candidate(value, account_id) if kind == "direct" else self._group_candidate(value)
            if candidate is not None:
                candidates.append(candidate)
        if len({item["identity"] for item in candidates}) != len(candidates):
            raise UnsupportedPage("页面出现重复的会话身份，已停止绑定。")
        return candidates

    def resolve_conversation(self, page: Any, identity: str, kind: str = "group") -> dict[str, str]:
        _validate_target_identity(kind, identity)
        account_id = self._current_account_identity(page) if kind == "direct" else ""
        if kind == "direct" and identity in {account_id, "self", "me"}:
            raise BrowserError("不能将当前账号自己作为私聊联系人。")
        expected_type = 1 if kind == "direct" else 2
        field = "direct_id" if kind == "direct" else "id"
        raw_id = identity if kind == "direct" else group_conversation_id(identity)
        rows = self._open_message_list(page)
        matches = [(item, value) for item, value in rows
                   if type(value.get("type")) is int and value["type"] == expected_type and value.get(field) == raw_id]
        if not matches:
            raise BrowserError("当前已加载会话中找不到这个联系人，请先在抖音打开目标会话，再刷新列表。")
        if len(matches) != 1:
            raise UnsupportedPage("联系人身份对应多个可见会话，已停止操作。")
        item, value = matches[0]
        candidate = self._direct_candidate(value, account_id) if kind == "direct" else self._group_candidate(value)
        if candidate is None:
            raise UnsupportedPage("联系人身份或名称不完整，请重新选择。")
        refreshed = item.evaluate(self._LIST_CONVERSATION)
        if not isinstance(refreshed, dict) or refreshed.get(field) != raw_id or type(refreshed.get("type")) is not int or refreshed["type"] != expected_type:
            raise UnsupportedPage("会话列表发生变化，请重新选择。")
        self._check_interrupted()
        item.click(timeout=1500)

        def ready() -> bool:
            if _one_visible(page.locator(self._HEADER)) is None:
                return False
            if self.conversation_identity(page) != identity:
                raise BrowserError("打开的会话身份与所选联系人不一致，已停止操作。")
            return _one_visible(page.locator(self._EDITOR)) is not None

        self._wait(page, ready)
        return candidate

    def open_conversation(self, page: Any, identity: str) -> None:
        self._check_interrupted()
        if _profile_identity(page.url) != identity:
            raise BrowserError("打开会话前的联系人主页身份不一致，已停止发送。")

        def ready() -> bool:
            header = _one_visible(page.locator(self._HEADER))
            editor = _one_visible(page.locator(self._EDITOR))
            if header is None:
                return False
            observed = header.evaluate(self._HEADER_ID)
            if isinstance(observed, str) and observed and observed != identity:
                raise BrowserError("当前聊天对象与目标联系人不一致，已停止打开会话。")
            return editor is not None and observed == identity

        if ready():
            return
        # The actual profile's scoped 私信 button opens its own conversation.
        buttons = page.locator("[data-e2e='user-detail']").get_by_role("button", name="私信", exact=True)
        button = _one_visible(buttons)
        # The real SSR button was visible before its React click handler existed.
        # Observe hydration; never invoke the handler or guess a fixed sleep.
        if button is not None and self._wait(page, lambda: button.evaluate(self._BUTTON_READY), required=False):
            self._check_interrupted()
            button.click(timeout=1500)
            if self._wait(page, ready, required=False):
                return

        # A live profile sometimes showed an enabled private-message button but
        # its first click did not select a chat. Opening the verified list is a
        # bounded, non-submitting fallback, never a reason to loosen identity.
        if _one_visible(page.locator(self._DIALOG)) is None:
            entry = _one_visible(page.locator("[data-e2e='im-entry']"))
            if entry is None:
                raise UnsupportedPage("该主页没有可用的私信入口，已停止发送。")
            self._check_interrupted()
            entry.click(timeout=1500)

        def find_contact() -> Any:
            items = page.locator("[data-e2e='conversation-item']")
            if items.count() > 200:
                raise UnsupportedPage("会话列表超出可安全核对的范围。")
            matches = []
            for index in range(items.count()):
                self._check_interrupted()
                item = items.nth(index)
                if item.is_visible() and item.evaluate(self._LIST_ID) == identity:
                    matches.append(item)
            if len(matches) > 1:
                raise UnsupportedPage("会话列表出现重复的联系人身份，已停止发送。")
            return matches[0] if matches else None

        item = self._wait(page, find_contact)
        if item.evaluate(self._LIST_ID) != identity:
            raise UnsupportedPage("联系人列表发生变化，已停止发送。")
        self._check_interrupted()
        item.click(timeout=1500)
        self._wait(page, ready)

    def conversation_identity(self, page: Any) -> str:
        header = _one_visible(page.locator(self._HEADER))
        target = header.evaluate(self._HEADER_TARGET) if header is not None else None
        if not isinstance(target, dict) or type(target.get("type")) is not int:
            raise UnsupportedPage("当前会话没有可核对的稳定身份，已停止发送。")
        if target["type"] == 2:
            try:
                return group_identity(target.get("id"))
            except (ValueError, TypeError) as exc:
                raise UnsupportedPage("群聊头部缺少可靠的会话标识，已停止发送。") from exc
        identity = target.get("direct_id") if target["type"] == 1 else None
        if not isinstance(identity, str) or not _IDENTITY.fullmatch(identity):
            raise UnsupportedPage("当前会话没有可核对的稳定身份，已停止发送。")
        return identity

    def prepare_message(self, page: Any, message: str) -> None:
        editor = _one_visible(page.locator(self._EDITOR))
        if editor is None:
            raise UnsupportedPage("当前会话没有唯一的消息输入框，已停止发送。")
        existing = self._editor_text(editor)
        expected = message.replace("\r\n", "\n").strip("\n")
        if existing:
            raise BrowserError("会话中已有未发送的草稿，请先在抖音处理草稿，避免覆盖。")
        editor.fill(message, timeout=1500)
        actual = self._editor_text(editor)
        if actual != expected:
            raise BrowserError("输入内容未能与待发消息一致，已停止发送。")
        self._prepared_page, self._prepared_message = page, expected

    def discard_prepared(self, page: Any) -> None:
        """Only remove this attempt's unchanged draft before the send boundary."""
        if page is not self._prepared_page or page is not self._snapshot_page or self._triggered_at is not None:
            return
        if self.conversation_identity(page) != self._snapshot_identity:
            return
        account = self._rendered_account(page)
        if not account or account["identity"] != self._snapshot_account:
            return
        editor = _one_visible(page.locator(self._EDITOR))
        if editor is not None and self._editor_text(editor) == self._prepared_message:
            # The live controlled editor ignored fill("") but accepted ordinary
            # Select All + Backspace. Never use Enter or its internal handlers.
            editor.press("ControlOrMeta+A", timeout=1500)
            account = self._rendered_account(page)
            if (
                self.conversation_identity(page) != self._snapshot_identity
                or not account or account["identity"] != self._snapshot_account
                or self._editor_text(editor) != self._prepared_message
            ):
                return
            editor.press("Backspace", timeout=1500)
            deadline = time.monotonic() + 1.5
            while self._editor_text(editor):
                if time.monotonic() >= deadline:
                    raise BrowserError("本次未发送的草稿未能清空，请在抖音核对并处理。")
                # Cleanup also runs after cancellation; finish this bounded,
                # non-submitting check without performing another deletion.
                page.wait_for_timeout(100)
        self._prepared_page, self._prepared_message = None, ""

    @staticmethod
    def _editor_text(editor: Any) -> str:
        result = editor.evaluate(DouyinDOMAdapter._EDITOR_TEXT)
        if not isinstance(result, str):
            raise UnsupportedPage("输入框的文本结构无法可靠读取，已停止发送。")
        return result.replace("\r\n", "\n").strip("\n")

    def _messages(self, page: Any) -> list[dict[str, Any]]:
        rows = page.locator(self._DIALOG + " .messageMessageBoxmessageBox").evaluate_all(self._MESSAGES)
        if not isinstance(rows, list) or any(
            not isinstance(row, dict) or not isinstance(row.get("client_id"), str)
            or not row["client_id"] or not isinstance(row.get("server_id"), str)
            or type(row.get("from_me")) is not bool or not isinstance(row.get("sender"), str)
            or type(row.get("server_status")) is not int
            or type(row.get("created_at")) not in (int, float)
            for row in rows
        ):
            raise UnsupportedPage("页面消息结构已变化，无法可靠读取发送回执。")
        return rows

    def message_snapshot(self, page: Any) -> set[str]:
        identity = self.conversation_identity(page)
        account = self._rendered_account(page)
        if not account:
            raise LoginRequired("消息准备前无法核对本人身份，请重新登录。")
        self._record_account(account)
        rows = self._messages(page)
        if identity.startswith("group:"):
            raw_id = group_conversation_id(identity)
            if any(row.get("conversation_id") != raw_id or type(row.get("conversation_type")) is not int or row["conversation_type"] != 2 for row in rows):
                raise UnsupportedPage("消息列表与当前群聊身份不一致，已停止发送。")
        self._snapshot_page, self._snapshot_identity = page, identity
        self._snapshot_account = account["identity"]
        self._snapshot_ids = {row["server_id"] for row in rows if re.fullmatch(r"[1-9][0-9]*", row["server_id"])}
        self._snapshot_clients = {row["client_id"] for row in rows}
        self._prepared_page, self._prepared_message = None, ""
        self._triggered_at = None
        return set(self._snapshot_ids)

    def trigger_send(self, page: Any) -> None:
        if page is not self._snapshot_page or page is not self._prepared_page:
            raise BrowserError("消息准备状态已失效，已停止发送。")
        if self.conversation_identity(page) != self._snapshot_identity:
            raise BrowserError("发送前聊天对象发生变化，已停止发送。")
        account = self._rendered_account(page)
        if not account or account["identity"] != self._snapshot_account:
            raise LoginRequired("发送前登录账号发生变化，已停止发送。")
        editor = _one_visible(page.locator(self._EDITOR))
        if editor is None or self._editor_text(editor) != self._prepared_message:
            raise BrowserError("发送前消息内容发生变化，已停止发送。")
        button = _one_visible(page.locator("[data-e2e='im-dialog'] [data-e2e='msg-input'] .e2e-send-msg-btn"))
        if button is None or button.get_attribute("aria-disabled", timeout=1000) == "true":
            raise BrowserError("当前会话没有可用的发送按钮。")
        self._check_interrupted()
        self._triggered_at = time.time() * 1000
        button.click(timeout=1500)

    def confirm_message(self, page: Any, before: Any, message: str, identity: str) -> dict[str, str] | None:
        if page is not self._snapshot_page or identity != self._snapshot_identity or before != self._snapshot_ids or self._triggered_at is None:
            raise UnsupportedPage("本次发送缺少可靠的基准记录，结果待核实。")
        if self.conversation_identity(page) != identity:
            raise UnsupportedPage("会话身份发生变化，结果待核实。")
        account = self._rendered_account(page)
        if not account or account["identity"] != self._snapshot_account:
            raise LoginRequired("发送后无法重新核对本人身份，结果待核实。")
        matches = []
        expected = message.replace("\r\n", "\n").strip("\n")
        group_id = group_conversation_id(identity) if identity.startswith("group:") else None
        for row in self._messages(page):
            text = row.get("text")
            if (
                re.fullmatch(r"[1-9][0-9]*", row["server_id"])
                and row["server_id"] not in before
                and row["client_id"] not in self._snapshot_clients
                and row["server_status"] == 0
                and row["from_me"] is True and row["sender"] == account["identity"]
                and (group_id is None or (row.get("conversation_id") == group_id and type(row.get("conversation_type")) is int and row["conversation_type"] == 2))
                # A previously unmounted historical duplicate must never pass.
                # Round down only to account for server timestamp precision.
                and int(self._triggered_at // 1000) * 1000 <= row["created_at"] <= time.time() * 1000 + 1000
                and isinstance(text, str) and text.replace("\r\n", "\n").strip("\n") == expected
            ):
                matches.append(row)
        if len(matches) > 1:
            raise UnsupportedPage("出现多个符合条件的发送回执，结果待核实。")
        if not matches:
            return None
        return {"status": "confirmed", "identity": identity, "message_id": matches[0]["server_id"],
                "evidence": "new serverId/clientId; own sender; exact rendered text; current createdAt; serverStatus=0"}


class DouyinBrowser(AbstractContextManager["DouyinBrowser"]):
    def __init__(
        self,
        profile_dir: Path,
        screenshot_dir: Path,
        headless: bool = False,
        timeouts: TimeoutsConfig | None = None,
        browser_config: BrowserConfig | None = None,
        storage_state_path: Path | None = None,
        mode: str = "run",
        progress: Callable[[str, str, str], None] | None = None,
        should_stop: Callable[[], bool] | None = None,
        expected_account_id: str = "",
        page_adapter: PageAdapter | None = None,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.screenshot_dir = Path(screenshot_dir)
        self.timeouts = timeouts or TimeoutsConfig()
        self.browser_config = browser_config or BrowserConfig()
        # Compatibility only: login and run share the same persistent profile.
        # Cross-browser state exports are no longer a source of account identity.
        self.storage_state_path = storage_state_path or self.browser_config.storage_state_path
        self.mode = mode
        self.headless = mode != "login"
        self.progress = progress
        self.should_stop = should_stop
        self.expected_account_id = expected_account_id
        self.adapter = page_adapter or DouyinDOMAdapter(
            check_interrupted=self._check_interrupted, expected_account_id=expected_account_id,
        )
        self._playwright: Any = None
        self.browser: Any = None
        self.context: Any = None
        self.page: Any = None
        self._contact_navigation = False
        self._blocked_navigation = False

    def __enter__(self) -> "DouyinBrowser":
        self.start()
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()

    def start(self) -> None:
        self._check_interrupted()
        if self.context is not None:
            raise BrowserError("浏览器已经打开。")
        try:
            self._start_playwright_persistent(headless=self.headless)
        except BaseException:
            # __exit__ is not invoked when __enter__ fails.
            self.close()
            raise

    def _start_playwright_persistent(self, headless: bool) -> None:
        sync_playwright = _load_sync_playwright()
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        self.context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            channel="chromium",
            headless=headless,
            viewport={"width": 1440, "height": 1000},
            timeout=30_000,
        )
        self.context.route("**/*", self._guard_navigation)
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(1500)
        self.page.set_default_navigation_timeout(15_000)

    def _guard_navigation(self, route: Any) -> None:
        request = route.request
        if self._contact_navigation and request.is_navigation_request() and request.frame.parent_frame is None:
            try:
                valid = _allowed_navigation_url(request.url)
            except (TypeError, ValueError):
                valid = False
            if not valid:
                self._blocked_navigation = True
                route.abort("blockedbyclient")
                return
        route.continue_()

    def close(self) -> None:
        # Attempt every cleanup even when a disconnected browser raises on close.
        for attribute, method in (("context", "close"), ("browser", "close"), ("_playwright", "stop")):
            resource = getattr(self, attribute)
            setattr(self, attribute, None)
            if resource is not None:
                try:
                    getattr(resource, method)()
                except Exception:
                    logging.warning("Browser cleanup failed for %s", attribute, exc_info=True)
        self.page = None

    def login(self, wait_seconds: int | None = 180) -> dict[str, str]:
        if self.mode != "login":
            raise BrowserError("请从网页点击登录，打开专用登录窗口。")
        self._goto(DOUYIN_URL)
        deadline = time.monotonic() + min(max(float(wait_seconds or 180), 1), 600)
        last_unsupported: UnsupportedPage | None = None
        while True:
            self._check_interrupted()
            if _verification_required(self._require_page()):
                account = None
                last_unsupported = None
            else:
                try:
                    account = self.adapter.read_account(self._require_page())
                except UnsupportedPage as exc:
                    # Keep the explicit login window available during hydration
                    # or while the user finishes logging in.
                    account = None
                    last_unsupported = exc
            if account:
                account = _validated_identity(account, "账号")
                self._check_account(account)
                # Chromium persists the entire account profile when it closes.
                # Never export an unauthenticated or unverified shared state file.
                return account
            if time.monotonic() >= deadline:
                if last_unsupported is not None:
                    raise last_unsupported
                raise LoginRequired("尚未确认登录成功，请重新打开登录窗口完成登录。")
            self._pause(min(0.25, deadline - time.monotonic()))

    def verify_session(self) -> dict[str, str]:
        self._goto(DOUYIN_URL)
        def observe_account() -> dict[str, str] | None:
            self._check_login_gate()
            account = self.adapter.read_account(self._require_page())
            self._check_login_gate()
            return account

        account = self._poll(
            observe_account,
            max(20, self.timeouts.home_ready_seconds),
            retry_unsupported=True,
        )
        self._check_login_gate()
        if not account:
            raise LoginRequired("登录已失效或尚未完成，请从网页重新登录。")
        account = _validated_identity(account, "账号")
        self._check_account(account)
        return account

    def _check_account(self, account: dict[str, str]) -> None:
        if self.expected_account_id and account["identity"] != self.expected_account_id:
            raise LoginRequired("当前浏览器账号与已绑定账号不一致，请重新登录正确账号。")

    def resolve_contact(self, profile_url: str) -> dict[str, str]:
        url = validate_contact_url(profile_url)
        self._contact_navigation = True
        self._blocked_navigation = False
        try:
            self._goto(url)
            self._check_login_gate()
            self._check_navigation()
            expected = _profile_identity(self._require_page().url)
            if not expected:
                raise BrowserError("链接没有打开有效的抖音用户主页，请复制联系人主页链接。")
            observed = self._poll(
                lambda: self.adapter.read_contact(self._require_page()),
                max(20, self.timeouts.contact_search_seconds),
                retry_unsupported=True,
            )
            contact = _validated_identity(observed, "联系人")
            if contact["identity"] != expected:
                raise BrowserError("联系人主页身份与页面内容不一致，已停止操作。")
            contact["profile_url"] = _canonical_profile(expected)
            return contact
        finally:
            self._contact_navigation = False

    def list_conversations(self, kind: str = "group") -> list[dict[str, str]]:
        if kind not in {"direct", "group"}:
            raise BrowserError("请选择有效的联系人类型。")
        account = self.verify_session()
        self._contact_navigation = True
        self._blocked_navigation = False
        try:
            values = self.adapter.list_conversations(self._require_page(), kind=kind)
            self._check_login_gate()
            self._check_navigation()
            if not isinstance(values, list) or len(values) > 200:
                raise UnsupportedPage("会话列表结构无法核对，请稍后重试。")
            result = [_validated_conversation(item, kind) for item in values]
            if kind == "direct":
                result = [item for item in result if item["identity"] != account["identity"]]
            if len({item["identity"] for item in result}) != len(result):
                raise UnsupportedPage("页面出现重复的会话身份，已停止绑定。")
            return result
        finally:
            self._contact_navigation = False

    def resolve_conversation(self, identity: str, kind: str = "group") -> dict[str, str]:
        _validate_target_identity(kind, identity)
        account = self.verify_session()
        if kind == "direct" and identity in {account["identity"], "self", "me"}:
            raise BrowserError("不能将当前账号自己作为私聊联系人。")
        self._contact_navigation = True
        self._blocked_navigation = False
        try:
            return self._resolve_listed_conversation(identity, kind)
        finally:
            self._contact_navigation = False

    def _resolve_group(self, identity: str) -> dict[str, str]:
        return self._resolve_listed_conversation(identity, "group")

    def _resolve_listed_conversation(self, identity: str, kind: str) -> dict[str, str]:
        self._check_interrupted()
        result = _validated_conversation(self.adapter.resolve_conversation(self._require_page(), identity, kind=kind), kind)
        self._check_login_gate()
        self._check_navigation()
        if result["identity"] != identity:
            raise BrowserError("打开的会话与所选联系人身份不一致，已停止操作。")
        self._check_conversation(identity)
        return result

    def send_message(
        self,
        contact: str,
        message: str,
        screenshot_prefix: str,
        profile_url: str = "",
        identity: str = "",
        before_send: Callable[[], None] | None = None,
        kind: str = "direct",
    ) -> dict[str, str]:
        submitted = False
        try:
            _validate_target_identity(kind, identity)
            if not message.strip():
                raise BrowserError("发送内容不能为空。")
            self._step_start("verify_account", contact)
            self.verify_session()
            self._step_done("verify_account", contact)
            self._step_start("open_conversation", contact)
            page = self._require_page()
            if kind == "group":
                if profile_url:
                    raise BrowserError("群聊不能使用个人主页链接，已停止发送。")
                self._contact_navigation = True
                self._blocked_navigation = False
                self._resolve_group(identity)
            else:
                resolved = self.resolve_contact(profile_url)
                if resolved["identity"] != identity:
                    raise BrowserError("联系人主页与已保存身份不一致，已停止发送。")
                self._contact_navigation = True
                self._check_interrupted()
                self.adapter.open_conversation(page, identity)
            self._check_login_gate()
            self._check_navigation()
            self._check_conversation(identity)
            self._step_done("open_conversation", contact)
            self._step_start("input_message", contact)
            before = self.adapter.message_snapshot(page)
            if not isinstance(before, (set, frozenset)) or not all(isinstance(item, str) and item for item in before):
                raise UnsupportedPage("当前会话没有可比较的消息标识，已停止发送。")
            self.adapter.prepare_message(page, message)
            self._check_navigation()
            self._check_conversation(identity)
            self._step_done("input_message", contact)
            self._step_start("press_enter", contact)
            self._check_interrupted()
            # The callback can fail after committing the durable transition.
            # Treat that boundary conservatively even if trigger_send is not run.
            submitted = True
            if before_send is not None:
                before_send()
            # Once the durable ledger has been marked, any exception is uncertain.
            # In particular, cancellation cannot convert this back into retryable.
            self._check_interrupted()
            self.adapter.trigger_send(page)
            self._step_done("press_enter", contact)
            self._step_start("confirm_message", contact)
            receipt = self._poll(
                lambda: self._read_receipt(before, message, identity),
                max(15, self.timeouts.after_send_seconds),
                retry_unsupported=True,
            )
            if not receipt:
                raise SendUncertain("发送动作已触发，但平台结果尚未确认，请核实会话后处理，系统不会自动重发。")
            self._step_done("confirm_message", contact)
            return receipt
        except BaseException as exc:
            if submitted:
                if isinstance(exc, SendUncertain):
                    raise
                raise SendUncertain("发送动作可能已发生，结果待核实；系统不会自动重发。") from exc
            if isinstance(exc, (BrowserError, KeyboardInterrupt, SystemExit)):
                raise
            raise BrowserError(f"尚未触发发送：{exc}") from exc
        finally:
            self._contact_navigation = False
            if not submitted and hasattr(self.adapter, "discard_prepared"):
                try:
                    self.adapter.discard_prepared(self._require_page())
                except Exception:
                    logging.warning("Could not clear this attempt's draft; please inspect the conversation.")

    def _read_receipt(self, before: Any, message: str, identity: str) -> dict[str, str] | None:
        self._check_login_gate()
        self._check_navigation()
        self._check_conversation(identity)
        receipt = self.adapter.confirm_message(self._require_page(), before, message, identity)
        if receipt is None:
            return None
        if (
            not isinstance(receipt, dict)
            or receipt.get("status") != "confirmed"
            or receipt.get("identity") != identity
            or not receipt.get("message_id")
            or receipt.get("message_id") in before
            or not receipt.get("evidence")
        ):
            raise SendUncertain("页面没有提供完整的发送确认，结果待核实。")
        return dict(receipt)

    def _check_conversation(self, identity: str) -> None:
        self._check_interrupted()
        if self.adapter.conversation_identity(self._require_page()) != identity:
            raise BrowserError("当前聊天对象身份与联系人不一致，已停止操作。")

    def _check_navigation(self) -> None:
        if self._blocked_navigation or not _allowed_navigation_url(self._require_page().url):
            raise BrowserError("页面跳转到了不受支持的地址，已停止操作。")

    def _goto(self, url: str) -> None:
        self._check_interrupted()
        self._require_page().goto(url, wait_until="domcontentloaded", timeout=15_000)
        self._check_interrupted()

    def _check_login_gate(self) -> None:
        if _verification_required(self._require_page()):
            raise LoginRequired("抖音要求验证码或安全验证，请从网页点击登录，在可见窗口完成人工验证。")

    def _poll(self, observe: Callable[[], Any], seconds: float, retry_unsupported: bool = False) -> Any:
        deadline = time.monotonic() + min(max(float(seconds), 0), 60)
        last_unsupported: UnsupportedPage | None = None
        while True:
            self._check_interrupted()
            try:
                value = observe()
            except UnsupportedPage as exc:
                if not retry_unsupported:
                    raise
                last_unsupported = exc
                value = None
            if value:
                return value
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if last_unsupported is not None:
                    raise last_unsupported
                return None
            self._pause(min(0.25, remaining))

    def _pause(self, seconds: float) -> None:
        self._check_interrupted()
        self._require_page().wait_for_timeout(max(0, seconds) * 1000)
        self._check_interrupted()

    def screenshot(self, name: str) -> Path:
        path = self.screenshot_dir / f"{_safe_name(name)}.png"
        try:
            self.screenshot_dir.mkdir(parents=True, exist_ok=True)
            self._require_page().screenshot(path=str(path), full_page=True, timeout=3000)
        except Exception:
            logging.warning("Could not capture diagnostic screenshot", exc_info=True)
        return path

    def capture_final_message_panel(self, screenshot_name: str) -> Path:
        self._check_interrupted()
        # Preserve the current verified conversation; do not reopen by nickname.
        return self.screenshot(screenshot_name)

    def _require_page(self) -> Any:
        if self.page is None:
            raise BrowserError("浏览器尚未启动。")
        return self.page

    def _step_start(self, step: str, detail: str = "") -> None:
        self._check_interrupted()
        if self.progress:
            self.progress(step, "running", detail)

    def _step_done(self, step: str, detail: str = "") -> None:
        if self.progress:
            self.progress(step, "done", detail)

    def _check_interrupted(self) -> None:
        if self.should_stop and self.should_stop():
            raise BrowserInterrupted("已中断，尚未触发发送。")


def validate_contact_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048 or any(ch.isspace() for ch in value.strip()):
        raise BrowserError("请输入完整的抖音用户主页或分享链接。")
    try:
        parts = urlsplit(value.strip())
        valid = _allowed_navigation_url(value.strip())
    except (TypeError, ValueError):
        valid = False
        parts = None
    if not valid or parts is None:
        raise BrowserError("仅支持 HTTPS 抖音用户主页及 v.douyin.com 分享链接。")
    if parts.hostname in _PROFILE_HOSTS:
        identity = _profile_identity(value.strip())
        if not identity:
            raise BrowserError("请使用抖音用户主页链接，不能使用视频或其他页面链接。")
        return _canonical_profile(identity)
    if not re.fullmatch(r"/[A-Za-z0-9_-]+/?", parts.path):
        raise BrowserError("抖音分享链接格式无效。")
    return urlunsplit(("https", "v.douyin.com", parts.path, "", ""))


def _allowed_navigation_url(value: str) -> bool:
    parts = urlsplit(value)
    return (
        parts.scheme == "https"
        and parts.hostname in _CONTACT_HOSTS
        and parts.username is None
        and parts.password is None
        and parts.port in (None, 443)
    )


def _profile_identity(value: str) -> str:
    try:
        parts = urlsplit(value)
        if not _allowed_navigation_url(value) or parts.hostname not in _PROFILE_HOSTS:
            return ""
        match = re.fullmatch(r"/user/([A-Za-z0-9_-]{1,256})/?", parts.path)
        return match.group(1) if match and match.group(1) not in {"self", "me"} else ""
    except (TypeError, ValueError):
        return ""


def _canonical_profile(identity: str) -> str:
    return f"https://www.douyin.com/user/{identity}"


def _validated_identity(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not isinstance(value.get("identity"), str) or not _IDENTITY.fullmatch(value["identity"]):
        raise UnsupportedPage(f"无法从页面可靠读取{label}身份，已停止操作。")
    return {"identity": value["identity"], "name": str(value.get("name", "")).strip()}


def _validate_target_identity(kind: str, identity: str) -> None:
    if kind == "group":
        try:
            group_conversation_id(identity)
        except (TypeError, ValueError) as exc:
            raise BrowserError("群聊尚未绑定可靠身份，请从会话列表重新选择群聊。") from exc
    elif kind != "direct" or not isinstance(identity, str) or not _IDENTITY.fullmatch(identity):
        raise BrowserError("联系人类型或身份无效，请在网页重新绑定。")


def _validated_group(value: Any) -> dict[str, str]:
    if not isinstance(value, dict) or value.get("kind") != "group" or value.get("profile_url", ""):
        raise UnsupportedPage("页面没有提供可核对的群聊身份。")
    identity, name = value.get("identity"), value.get("name")
    try:
        group_conversation_id(identity)
    except (TypeError, ValueError) as exc:
        raise UnsupportedPage("页面群聊标识无效，已停止操作。") from exc
    if not isinstance(name, str) or not name.strip():
        raise UnsupportedPage("群聊名称尚未加载，请稍后重试。")
    return {"kind": "group", "identity": identity, "name": name.strip(), "profile_url": ""}


def _validated_conversation(value: Any, kind: str) -> dict[str, str]:
    if kind == "group":
        return _validated_group(value)
    if kind != "direct" or not isinstance(value, dict) or value.get("kind") != "direct":
        raise UnsupportedPage("页面没有提供可核对的私聊身份。")
    identity, name = value.get("identity"), value.get("name")
    if (
        not isinstance(identity, str) or not _IDENTITY.fullmatch(identity) or identity in {"self", "me"}
        or not isinstance(name, str) or not name.strip()
        or value.get("profile_url") != _canonical_profile(identity)
    ):
        raise UnsupportedPage("私聊联系人身份或主页信息不完整，已停止绑定。")
    return {"kind": "direct", "identity": identity, "name": name.strip(), "profile_url": _canonical_profile(identity)}


def _one_visible(locator: Any) -> Any | None:
    visible = [locator.nth(index) for index in range(locator.count()) if locator.nth(index).is_visible()]
    if len(visible) > 1:
        raise UnsupportedPage("页面出现多个候选元素，无法安全继续。")
    return visible[0] if visible else None


def _verification_required(page: Any) -> bool:
    # The title "验证码中间页" was observed on the real site with an empty body.
    # Keep checking the title even when no challenge widgets have rendered.
    title = page.title().strip().lower()
    if any(marker in title for marker in ("验证码中间页", "安全验证", "人机验证", "captcha")):
        return True
    frames = page.locator("iframe[src]")
    for index in range(min(frames.count(), 30)):
        frame = frames.nth(index)
        if not frame.is_visible():
            continue
        source = (frame.get_attribute("src", timeout=1000) or "").lower()
        label = (frame.get_attribute("title", timeout=1000) or "").lower()
        if "captcha" in source or any(marker in label for marker in ("验证码", "安全验证", "captcha")):
            return True
    return False


def _safe_name(value: str) -> str:
    name = "".join(ch if ch.isalnum() else "_" for ch in value)[:60] or "diagnostic"
    return f"{name}_{hashlib.sha256(value.encode()).hexdigest()[:8]}"


def _load_sync_playwright() -> Any:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserError(_playwright_install_message()) from exc
    return sync_playwright


def _playwright_install_message() -> str:
    return "浏览器运行环境尚未准备好，请从本地网页完成浏览器安装。"
