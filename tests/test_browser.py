"""Browser safety contracts; these fixtures do not claim live-site compatibility."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import quote

import pytest

from douyinfire.browser import (
    BrowserError,
    BrowserInterrupted,
    DouyinBrowser,
    DouyinDOMAdapter,
    LoginRequired,
    SendUncertain,
    UnsupportedPage,
    validate_contact_url,
)
from douyinfire.config import BrowserConfig, TimeoutsConfig
from douyinfire.identities import group_identity


class FixturePage:
    """Only navigation/timing; identity and receipts come from an explicit stub."""

    def __init__(self, clock):
        self.url = "about:blank"
        self.clock = clock
        self.visits = []
        self.redirect = None
        self.title_text = "Fixture page"
        self.elements = {}

    def goto(self, url, **kwargs):
        self.visits.append(url)
        self.url = self.redirect or url

    def wait_for_timeout(self, milliseconds):
        self.clock[0] += milliseconds / 1000

    def title(self):
        return self.title_text

    def locator(self, selector):
        return FixtureLocator(self.elements.get(selector, []))

    def get_by_role(self, role, **kwargs):
        return FixtureLocator([])


class FixtureElement:
    def __init__(self, text="", visible=True, **attributes):
        self.text = text
        self.visible = visible
        self.attributes = attributes

    def is_visible(self):
        return self.visible

    def get_attribute(self, name, **kwargs):
        return self.attributes.get(name)

    def inner_text(self, **kwargs):
        return self.text

    def text_content(self, **kwargs):
        return self.text

    def fill(self, value, **kwargs):
        self.text = value

    def click(self, **kwargs):
        self.clicked = True

    def press(self, key, **kwargs):
        self.pressed = getattr(self, "pressed", []) + [key]
        if key == "Backspace":
            self.text = ""

    def evaluate(self, script):
        if script == DouyinDOMAdapter._EDITOR_TEXT:
            return self.text.replace("\ufeff", "")
        if script == DouyinDOMAdapter._BUTTON_READY:
            return self.attributes.get("handler_ready", False)
        if script == DouyinDOMAdapter._HEADER_TARGET:
            return self.attributes.get("react_target", {"type": 1, "direct_id": self.attributes.get("react_identity")})
        if script == DouyinDOMAdapter._LIST_CONVERSATION:
            return self.attributes.get("conversation")
        return self.attributes.get("react_identity")


class FixtureLocator:
    def __init__(self, elements):
        self.elements = elements

    def count(self):
        return len(self.elements)

    def nth(self, index):
        return self.elements[index]

    def evaluate_all(self, script):
        return self.elements

    def get_by_role(self, role, **kwargs):
        return self


class FixtureAdapter:
    def __init__(self):
        self.account = {"identity": "account_a", "name": "Account A"}
        self.contact = {"identity": "contact_a", "name": "Renamed contact"}
        self.active_identity = "contact_a"
        self.events = []
        self.confirmation = {
            "status": "confirmed",
            "identity": "contact_a",
            "message_id": "new-platform-message-id",
            "evidence": "fixture receipt, not a live Douyin receipt",
        }
        self.trigger_error = None

    def read_account(self, page):
        return self.account

    def read_contact(self, page):
        return self.contact

    def open_conversation(self, page, identity):
        self.events.append("open")

    def conversation_identity(self, page):
        return self.active_identity

    def message_snapshot(self, page):
        self.events.append("baseline")
        return {"old-platform-message-id"}

    def prepare_message(self, page, message):
        self.events.append("prepare")

    def trigger_send(self, page):
        self.events.append("send")
        if self.trigger_error:
            raise self.trigger_error

    def confirm_message(self, page, before, message, identity):
        self.events.append("observe")
        return self.confirmation


@pytest.fixture
def browser(tmp_path, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr("douyinfire.browser.time.monotonic", lambda: clock[0])
    adapter = FixtureAdapter()
    browser = DouyinBrowser(
        tmp_path / "profile",
        tmp_path / "shots",
        expected_account_id="account_a",
        page_adapter=adapter,
        timeouts=TimeoutsConfig(home_ready_seconds=1, after_send_seconds=1),
    )
    browser.page = FixturePage(clock)
    return browser


def send(browser, before_send=None, **overrides):
    values = dict(
        contact="old display name",
        message="hello",
        screenshot_prefix="fixture",
        profile_url="https://www.douyin.com/user/contact_a",
        identity="contact_a",
        before_send=before_send,
    )
    values.update(overrides)
    return browser.send_message(**values)


@pytest.mark.parametrize("url", [
    "http://www.douyin.com/user/a",
    "https://www.douyin.com.evil.example/user/a",
    "https://www.douyin.com@evil.example/user/a",
    "https://name:password@www.douyin.com/user/a",
    "https://www.douyin.com:444/user/a",
    "file:///tmp/contact",
    "https://127.0.0.1/user/a",
    "https://www.douyin.com/video/a",
    "https://www.douyin.com/user/self",
    "https://v.douyin.com/",
    "https://v.douyin.com/a/b",
    "https://www.douyin.com/user/a\n/path",
])
def test_rejects_unsupported_or_ambiguous_links(url):
    with pytest.raises(BrowserError):
        validate_contact_url(url)


def test_normalizes_profile_and_share_links():
    assert validate_contact_url("https://douyin.com/user/contact_a/?from=copy#foo") == "https://www.douyin.com/user/contact_a"
    assert validate_contact_url("https://v.douyin.com/abc123/?from=copy") == "https://v.douyin.com/abc123/"


def test_resolve_contact_rejects_external_redirect(browser):
    browser.page.redirect = "https://evil.example/user/contact_a"
    with pytest.raises(BrowserError, match="跳转"):
        browser.resolve_contact("https://v.douyin.com/abc/")


def test_resolve_contact_rejects_video_redirect(browser):
    browser.page.redirect = "https://www.douyin.com/video/123"
    with pytest.raises(BrowserError, match="用户主页"):
        browser.resolve_contact("https://v.douyin.com/abc/")


def test_shared_contact_resolves_to_stable_profile(browser):
    browser.page.redirect = "https://www.douyin.com/user/contact_a?source=share"
    assert browser.resolve_contact("https://v.douyin.com/abc/") == {
        "identity": "contact_a", "name": "Renamed contact", "profile_url": "https://www.douyin.com/user/contact_a",
    }


def test_account_mismatch_stops_before_contact_or_send(browser):
    browser.adapter.account["identity"] = "account_b"
    with pytest.raises(LoginRequired):
        send(browser)
    assert browser.adapter.events == []


def test_missing_login_has_bounded_wait(browser):
    browser.adapter.account = None
    with pytest.raises(LoginRequired):
        browser.verify_session()
    assert browser.page.clock[0] == 20


def test_login_returns_verified_identity_without_terminal_or_shared_export(browser):
    browser.mode = "login"
    browser.context = Mock()
    assert browser.login(1) == {"identity": "account_a", "name": "Account A"}
    browser.context.storage_state.assert_not_called()


def test_login_wait_is_cancellable(browser):
    browser.mode = "login"
    browser.adapter.account = None
    browser.should_stop = lambda: browser.page.clock[0] >= 0.5
    with pytest.raises(BrowserInterrupted):
        browser.login(180)
    assert browser.page.clock[0] == 0.5


def test_captcha_title_is_login_required_even_when_body_is_empty(browser):
    browser.page.title_text = "验证码中间页"
    with pytest.raises(LoginRequired, match="人工验证"):
        browser.verify_session()
    assert browser.adapter.events == []


def test_login_keeps_challenge_window_available_until_timeout(browser):
    browser.mode = "login"
    browser.page.title_text = "验证码中间页"
    with pytest.raises(LoginRequired):
        browser.login(1)
    assert browser.page.clock[0] == 1


def test_visible_captcha_frame_requires_manual_verification(browser):
    browser.page.elements["iframe[src]"] = [FixtureElement(src="https://verify.douyin.com/captcha", title="安全验证")]
    with pytest.raises(LoginRequired):
        browser.resolve_contact("https://www.douyin.com/user/contact_a")


def test_hidden_captcha_preload_does_not_block_a_valid_session(browser):
    browser.page.elements["iframe[src]"] = [FixtureElement(visible=False, src="https://verify.douyin.com/captcha")]
    assert browser.verify_session()["identity"] == "account_a"


@pytest.mark.parametrize("identity", ["", "wrong_identity"])
def test_unbound_or_mismatched_contact_cannot_send(browser, identity):
    with pytest.raises(BrowserError):
        send(browser, identity=identity)
    assert "send" not in browser.adapter.events


def test_wrong_conversation_cannot_send(browser):
    browser.adapter.active_identity = "contact_b"
    with pytest.raises(BrowserError, match="聊天对象"):
        send(browser)
    assert browser.adapter.events == ["open"]


def test_durable_callback_precedes_send_and_success_requires_receipt(browser):
    receipt = send(browser, before_send=lambda: browser.adapter.events.append("ledger"))
    assert browser.adapter.events == ["open", "baseline", "prepare", "ledger", "send", "observe"]
    assert receipt["status"] == "confirmed"
    assert browser.page.visits == ["https://www.douyin.com/jingxuan", "https://www.douyin.com/user/contact_a"]


def test_enter_without_receipt_is_uncertain(browser):
    browser.adapter.confirmation = None
    with pytest.raises(SendUncertain):
        send(browser)
    assert browser.adapter.events.count("send") == 1
    assert browser.page.clock[0] == 15


@pytest.mark.parametrize("receipt", [
    {"status": "confirmed", "identity": "contact_a"},
    {"status": "pending", "identity": "contact_a", "message_id": "new", "evidence": "pending"},
    {"status": "confirmed", "identity": "contact_b", "message_id": "new", "evidence": "received"},
    {"status": "confirmed", "identity": "contact_a", "message_id": "old-platform-message-id", "evidence": "old bubble still visible"},
])
def test_incomplete_or_wrong_receipt_never_means_success(browser, receipt):
    browser.adapter.confirmation = receipt
    with pytest.raises(SendUncertain):
        send(browser)


def test_unverifiable_message_baseline_stops_before_submission(browser):
    browser.adapter.message_snapshot = lambda page: None
    with pytest.raises(UnsupportedPage, match="消息标识"):
        send(browser)
    assert "send" not in browser.adapter.events


@pytest.mark.parametrize("error", [RuntimeError("connection closed"), BrowserInterrupted("cancelled"), KeyboardInterrupt()])
def test_every_error_after_trigger_becomes_uncertain(browser, error):
    browser.adapter.trigger_error = error
    with pytest.raises(SendUncertain):
        send(browser)
    assert browser.adapter.events.count("send") == 1


def test_cancellation_at_submission_boundary_is_uncertain_and_does_not_send(browser):
    def marked():
        browser.should_stop = lambda: True
    with pytest.raises(SendUncertain):
        send(browser, before_send=marked)
    assert "send" not in browser.adapter.events


def test_callback_failure_does_not_send_and_cannot_undo_a_possible_commit(browser):
    def failed_commit():
        raise OSError("disk write failed after commit")
    with pytest.raises(SendUncertain):
        send(browser, before_send=failed_commit)
    assert "send" not in browser.adapter.events


def test_cancellation_before_submission_is_retryable(browser):
    browser.should_stop = lambda: True
    with pytest.raises(BrowserInterrupted):
        send(browser)
    assert browser.adapter.events == []


def test_failed_start_releases_context_and_driver_even_if_close_raises(tmp_path, monkeypatch):
    browser = DouyinBrowser(tmp_path / "profile", tmp_path / "shots")
    context = Mock()
    context.close.side_effect = RuntimeError("already disconnected")
    driver = Mock()
    def partial_start(headless):
        browser.context = context
        browser._playwright = driver
        raise RuntimeError("page creation failed")
    monkeypatch.setattr(browser, "_start_playwright_persistent", partial_start)
    with pytest.raises(RuntimeError, match="page creation failed"):
        browser.start()
    context.close.assert_called_once()
    driver.stop.assert_called_once()
    assert browser.context is browser._playwright is browser.page is None


@pytest.mark.parametrize("mode,headless", [("login", False), ("run", True)])
def test_only_login_is_headed_regardless_of_legacy_browser_settings(tmp_path, monkeypatch, mode, headless):
    browser = DouyinBrowser(
        tmp_path / "profile", tmp_path / "shots", mode=mode,
        browser_config=BrowserConfig(backend="cloakbrowser", run_headless=False, login_headless=True),
    )
    starter = Mock()
    monkeypatch.setattr(browser, "_start_playwright_persistent", starter)
    browser.start()
    starter.assert_called_once_with(headless=headless)


def test_navigation_guard_blocks_external_top_level_pages(browser):
    browser._contact_navigation = True
    request = SimpleNamespace(url="https://outside.example/", is_navigation_request=lambda: True, frame=SimpleNamespace(parent_frame=None))
    route = Mock(request=request)
    browser._guard_navigation(route)
    route.abort.assert_called_once_with("blockedbyclient")
    route.continue_.assert_not_called()
    assert browser._blocked_navigation


def test_production_adapter_does_not_guess_a_conversation_from_a_nickname():
    # Passing contract tests does not assert that real Douyin selectors exist.
    page = FixturePage([0])
    page.url = "https://www.douyin.com/user/contact_a"
    with pytest.raises(UnsupportedPage):
        DouyinDOMAdapter().open_conversation(page, "contact_a")


def self_profile_fixture(browser):
    page = browser.page
    page.url = "https://www.douyin.com/"
    page.elements["a[href]"] = [FixtureElement(href="/user/self")]
    page.elements["script#RENDER_DATA[type='application/json']"] = [FixtureElement(text=quote(json.dumps({
        "app": {"user": {"isLogin": True, "statusCode": 0, "info": {"secUid": "account_a", "nickname": "Account A"}}},
    })))]
    return page


def test_account_selector_contract_requires_self_navigation_then_stable_identity(browser):
    # Field paths and self link observed on the live site, values are synthetic.
    page = self_profile_fixture(browser)
    assert DouyinDOMAdapter().read_account(page) == {"identity": "account_a", "name": "Account A"}
    assert page.visits == []
    assert page.locator("h1").count() == 0


def test_real_site_self_entry_query_variants_do_not_look_like_multiple_accounts(browser):
    # Shapes observed in the logged-in site's navigation; no personal data.
    page = self_profile_fixture(browser)
    page.elements["a[href]"] = [
        FixtureElement(href="https://www.douyin.com/user/self?from_nav=1"),
        FixtureElement(href="//www.douyin.com/user/self"),
        FixtureElement(href="//www.douyin.com/user/self?enter_from=discover&enter_method=personal_panel&from_tab_name=main&showTab=like"),
    ]
    assert DouyinDOMAdapter().read_account(page)["identity"] == "account_a"
    assert page.visits == []


def test_feed_profile_links_and_names_cannot_become_the_signed_in_account(browser):
    page = self_profile_fixture(browser)
    page.elements["a[href]"] = [FixtureElement(href="/user/account_a")]
    with pytest.raises(UnsupportedPage, match="本人主页入口"):
        DouyinDOMAdapter().read_account(page)


def test_self_profile_without_stable_id_cannot_be_bound(browser):
    page = self_profile_fixture(browser)
    page.elements["script#RENDER_DATA[type='application/json']"] = []
    with pytest.raises(UnsupportedPage, match="稳定账号标识"):
        DouyinDOMAdapter().read_account(page)


def test_inconsistent_rendered_login_status_cannot_be_bound(browser):
    page = self_profile_fixture(browser)
    page.elements["script#RENDER_DATA[type='application/json']"] = [FixtureElement(text=quote(json.dumps({
        "app": {"user": {"isLogin": True, "statusCode": False, "info": {"secUid": "account_a", "nickname": "Account A"}}},
    })))]
    with pytest.raises(UnsupportedPage, match="稳定账号标识"):
        DouyinDOMAdapter().read_account(page)


def test_production_editor_refuses_to_overwrite_existing_draft(browser):
    selector = "[data-e2e='im-dialog'] [data-e2e='msg-input'] [contenteditable='true'][data-slate-editor]"
    editor = FixtureElement(text="a draft written earlier")
    browser.page.elements[selector] = [editor]
    with pytest.raises(BrowserError, match="草稿"):
        DouyinDOMAdapter().prepare_message(browser.page, "hello")
    assert editor.text == "a draft written earlier"


def test_observed_slate_editor_contract_accepts_empty_slate_marker(browser):
    selector = "[data-e2e='im-dialog'] [data-e2e='msg-input'] [contenteditable='true'][data-slate-editor]"
    editor = FixtureElement(text="\ufeff")
    browser.page.elements[selector] = [editor]
    DouyinDOMAdapter().prepare_message(browser.page, "hello\nsecond line")
    assert editor.text == "hello\nsecond line"


def test_matching_existing_draft_is_also_preserved_without_new_task_provenance(browser):
    selector = "[data-e2e='im-dialog'] [data-e2e='msg-input'] [contenteditable='true'][data-slate-editor]"
    editor = FixtureElement(text="hello")
    editor.fill = Mock(side_effect=AssertionError("matching draft must not be rewritten"))
    browser.page.elements[selector] = [editor]
    with pytest.raises(BrowserError, match="草稿"):
        DouyinDOMAdapter().prepare_message(browser.page, "hello")
    editor.fill.assert_not_called()


def test_ordinary_zero_width_draft_is_not_treated_as_an_empty_sentinel(browser):
    editor = FixtureElement(text="\u200b")
    editor.fill = Mock(side_effect=AssertionError("ordinary draft must not be overwritten"))
    browser.page.elements[DouyinDOMAdapter._EDITOR] = [editor]
    with pytest.raises(BrowserError, match="草稿"):
        DouyinDOMAdapter().prepare_message(browser.page, "hello")
    editor.fill.assert_not_called()


def test_intentional_zero_width_characters_in_message_are_preserved(browser):
    editor = FixtureElement()
    browser.page.elements[DouyinDOMAdapter._EDITOR] = [editor]
    message = "first\u200bsecond\u200b"
    adapter = DouyinDOMAdapter()
    adapter.prepare_message(browser.page, message)
    assert adapter._editor_text(editor) == message
    assert adapter._prepared_message == message


def test_unidentified_extra_zero_width_character_does_not_pass_input_verification(browser):
    editor = FixtureElement()
    editor.fill = lambda value, **kwargs: setattr(editor, "text", value + "\u200b")
    browser.page.elements[DouyinDOMAdapter._EDITOR] = [editor]
    adapter = DouyinDOMAdapter()
    with pytest.raises(BrowserError, match="输入内容"):
        adapter.prepare_message(browser.page, "hello")
    assert adapter._prepared_page is None


def test_duplicate_send_buttons_are_not_clicked(browser):
    buttons = [FixtureElement(), FixtureElement()]
    browser.page.elements["[data-e2e='im-dialog'] [data-e2e='msg-input'] .e2e-send-msg-btn"] = buttons
    adapter = production_conversation(browser.page)
    adapter.message_snapshot(browser.page)
    adapter.prepare_message(browser.page, "hello")
    with pytest.raises(UnsupportedPage):
        adapter.trigger_send(browser.page)
    assert not any(getattr(button, "clicked", False) for button in buttons)


def production_conversation(page):
    """Synthetic observations following real field names, never private values."""
    page.url = "https://www.douyin.com/user/contact_a"
    page.elements[DouyinDOMAdapter._HEADER] = [FixtureElement(react_identity="contact_a")]
    page.elements[DouyinDOMAdapter._EDITOR] = [FixtureElement()]
    page.elements[DouyinDOMAdapter._DIALOG + " .messageMessageBoxmessageBox"] = [{
        "server_id": "100", "client_id": "old-client", "server_status": 0,
        "from_me": True, "sender": "account_a", "created_at": 1000, "text": "hello",
    }]
    page.elements["script#RENDER_DATA[type='application/json']"] = [FixtureElement(quote(json.dumps({
        "app": {"user": {"isLogin": True, "statusCode": 0, "info": {"secUid": "account_a", "nickname": "Account A"}}},
    })))]
    return DouyinDOMAdapter()


def test_profile_private_message_action_requires_matching_header(browser):
    adapter = production_conversation(browser.page)
    header = browser.page.elements.pop(adapter._HEADER)
    button = FixtureElement(handler_ready=True)
    button.click = Mock(side_effect=lambda **kwargs: browser.page.elements.__setitem__(adapter._HEADER, header))
    browser.page.elements["[data-e2e='user-detail']"] = [button]
    adapter.open_conversation(browser.page, "contact_a")
    button.click.assert_called_once()


def test_existing_verified_conversation_is_reused_without_any_click(browser):
    adapter = production_conversation(browser.page)
    button = FixtureElement(handler_ready=True)
    browser.page.elements["[data-e2e='user-detail']"] = [button]
    adapter.open_conversation(browser.page, "contact_a")
    assert not getattr(button, "clicked", False)


def test_private_message_button_waits_for_hydration_before_click(browser):
    adapter = production_conversation(browser.page)
    header = browser.page.elements.pop(adapter._HEADER)
    button = FixtureElement()
    button.evaluate = Mock(side_effect=lambda script: browser.page.clock[0] >= 0.6)
    button.click = Mock(side_effect=lambda **kwargs: browser.page.elements.__setitem__(adapter._HEADER, header))
    browser.page.elements["[data-e2e='user-detail']"] = [button]
    adapter.open_conversation(browser.page, "contact_a")
    button.click.assert_called_once()
    assert 0.6 <= browser.page.clock[0] < 1


def test_conversation_list_selects_stable_identity_without_using_names(browser):
    adapter = production_conversation(browser.page)
    entry = FixtureElement()
    wrong = FixtureElement(text="same name", react_identity="contact_b")
    target = FixtureElement(text="same name", react_identity="contact_a")
    header = browser.page.elements.pop(adapter._HEADER)
    target.click = Mock(side_effect=lambda **kwargs: browser.page.elements.__setitem__(adapter._HEADER, header))
    browser.page.elements["[data-e2e='im-entry']"] = [entry]
    browser.page.elements["[data-e2e='conversation-item']"] = [wrong, target]
    adapter.open_conversation(browser.page, "contact_a")
    assert entry.clicked
    target.click.assert_called_once()
    assert not getattr(wrong, "clicked", False)


def test_duplicate_stable_identity_list_is_rejected(browser):
    adapter = production_conversation(browser.page)
    browser.page.elements.pop(adapter._HEADER)
    browser.page.elements["[data-e2e='im-entry']"] = [FixtureElement()]
    browser.page.elements["[data-e2e='conversation-item']"] = [
        FixtureElement(react_identity="contact_a"), FixtureElement(react_identity="contact_a"),
    ]
    with pytest.raises(UnsupportedPage, match="重复"):
        adapter.open_conversation(browser.page, "contact_a")


def test_changed_header_stops_immediately_without_clicking(browser):
    adapter = production_conversation(browser.page)
    browser.page.elements["[data-e2e='user-detail']"] = [FixtureElement()]
    browser.page.elements[adapter._HEADER] = [FixtureElement(react_identity="contact_b")]
    with pytest.raises(BrowserError, match="聊天对象"):
        adapter.open_conversation(browser.page, "contact_a")
    assert browser.page.clock[0] == 0


@pytest.mark.parametrize("dialog_visible", [False, True])
def test_private_button_did_not_open_chat_falls_back_to_verified_list(browser, dialog_visible):
    adapter = production_conversation(browser.page)
    header = browser.page.elements.pop(adapter._HEADER)
    button = FixtureElement(handler_ready=True)
    entry = FixtureElement()
    target = FixtureElement(react_identity="contact_a")
    target.click = Mock(side_effect=lambda **kwargs: browser.page.elements.__setitem__(adapter._HEADER, header))
    browser.page.elements["[data-e2e='user-detail']"] = [button]
    browser.page.elements[adapter._DIALOG] = [FixtureElement(visible=dialog_visible)]
    browser.page.elements["[data-e2e='im-entry']"] = [entry]
    browser.page.elements["[data-e2e='conversation-item']"] = [target]
    adapter.open_conversation(browser.page, "contact_a")
    assert button.clicked
    assert getattr(entry, "clicked", False) is not dialog_visible
    target.click.assert_called_once()
    assert 15 <= browser.page.clock[0] < 16


def test_unhydrated_private_button_is_never_clicked(browser):
    adapter = production_conversation(browser.page)
    browser.page.elements.pop(adapter._HEADER)
    button = FixtureElement(handler_ready=False)
    browser.page.elements["[data-e2e='user-detail']"] = [button]
    with pytest.raises(UnsupportedPage):
        adapter.open_conversation(browser.page, "contact_a")
    assert not getattr(button, "clicked", False)
    assert 15 <= browser.page.clock[0] < 16


def test_adapter_wait_is_cancellable(browser):
    adapter = production_conversation(browser.page)
    adapter._check_interrupted = Mock(side_effect=BrowserInterrupted("cancelled"))
    browser.page.elements["[data-e2e='user-detail']"] = [FixtureElement()]
    with pytest.raises(BrowserInterrupted):
        adapter.open_conversation(browser.page, "contact_a")


def test_missing_message_schema_fails_before_input(browser):
    adapter = production_conversation(browser.page)
    browser.page.elements[adapter._DIALOG + " .messageMessageBoxmessageBox"] = [None]
    with pytest.raises(UnsupportedPage, match="结构"):
        adapter.message_snapshot(browser.page)
    assert browser.page.elements[adapter._EDITOR][0].text == ""


def ready_receipt(browser, monkeypatch):
    adapter = production_conversation(browser.page)
    before = adapter.message_snapshot(browser.page)
    adapter.prepare_message(browser.page, "hello")
    browser.page.elements["[data-e2e='im-dialog'] [data-e2e='msg-input'] .e2e-send-msg-btn"] = [FixtureElement()]
    monkeypatch.setattr("douyinfire.browser.time.time", lambda: 5.5)
    adapter.trigger_send(browser.page)
    new = {"server_id": "200", "client_id": "new-client", "server_status": 0,
           "from_me": True, "sender": "account_a", "created_at": 5500, "text": "hello"}
    browser.page.elements[adapter._DIALOG + " .messageMessageBoxmessageBox"].append(new)
    return adapter, before, new


def test_confirm_requires_new_server_ack_for_matching_current_own_message(browser, monkeypatch):
    adapter, before, new = ready_receipt(browser, monkeypatch)
    receipt = adapter.confirm_message(browser.page, before, "hello", "contact_a")
    assert receipt["message_id"] == "200" and receipt["status"] == "confirmed"


@pytest.mark.parametrize("changed", [
    {"server_id": "0"}, {"server_id": "100"}, {"server_id": "local-generated-id"},
    {"client_id": "old-client"}, {"server_status": 1}, {"from_me": False},
    {"sender": "account_b"}, {"created_at": 1000}, {"created_at": 99999999},
    {"text": "other content"}, {"text": None}, {"text": "hello\n已读"},
])
def test_receipt_does_not_accept_pending_incoming_or_historical_duplicates(browser, monkeypatch, changed):
    adapter, before, new = ready_receipt(browser, monkeypatch)
    new.update(changed)
    assert adapter.confirm_message(browser.page, before, "hello", "contact_a") is None


def test_multiple_new_receipts_require_manual_verification(browser, monkeypatch):
    adapter, before, new = ready_receipt(browser, monkeypatch)
    browser.page.elements[adapter._DIALOG + " .messageMessageBoxmessageBox"].append(
        dict(new, server_id="201", client_id="other-new-client")
    )
    with pytest.raises(UnsupportedPage, match="多个"):
        adapter.confirm_message(browser.page, before, "hello", "contact_a")


def test_sender_identity_is_rechecked_after_submission(browser, monkeypatch):
    adapter, before, new = ready_receipt(browser, monkeypatch)
    browser.page.elements["script#RENDER_DATA[type='application/json']"] = []
    with pytest.raises(LoginRequired):
        adapter.confirm_message(browser.page, before, "hello", "contact_a")


@pytest.mark.parametrize("changed", ["text", "identity"])
def test_trigger_rechecks_recipient_and_draft_before_click(browser, changed):
    adapter = production_conversation(browser.page)
    adapter.message_snapshot(browser.page)
    adapter.prepare_message(browser.page, "hello")
    button = FixtureElement()
    browser.page.elements["[data-e2e='im-dialog'] [data-e2e='msg-input'] .e2e-send-msg-btn"] = [button]
    if changed == "text":
        browser.page.elements[adapter._EDITOR][0].text = "new user draft"
    else:
        browser.page.elements[adapter._HEADER][0].attributes["react_identity"] = "contact_b"
    with pytest.raises(BrowserError, match="变化"):
        adapter.trigger_send(browser.page)
    assert not getattr(button, "clicked", False)


def test_only_unchanged_own_prepared_draft_is_cleared_before_submission(browser):
    adapter = production_conversation(browser.page)
    adapter.message_snapshot(browser.page)
    adapter.prepare_message(browser.page, "hello")
    adapter.discard_prepared(browser.page)
    assert browser.page.elements[adapter._EDITOR][0].text == ""
    assert browser.page.elements[adapter._EDITOR][0].pressed == ["ControlOrMeta+A", "Backspace"]


@pytest.mark.parametrize("changed", ["text", "identity", "submitted"])
def test_unknown_or_submitted_draft_is_never_cleared(browser, changed):
    adapter = production_conversation(browser.page)
    adapter.message_snapshot(browser.page)
    adapter.prepare_message(browser.page, "hello")
    editor = browser.page.elements[adapter._EDITOR][0]
    if changed == "text":
        editor.text = "user edited the draft"
    elif changed == "identity":
        browser.page.elements[adapter._HEADER][0].attributes["react_identity"] = "contact_b"
    else:
        adapter._triggered_at = 1000
    original = editor.text
    adapter.discard_prepared(browser.page)
    assert editor.text == original
    assert not getattr(editor, "pressed", [])


def test_cleanup_rechecks_draft_after_selection_before_deletion(browser):
    adapter = production_conversation(browser.page)
    adapter.message_snapshot(browser.page)
    adapter.prepare_message(browser.page, "hello")
    editor = browser.page.elements[adapter._EDITOR][0]
    editor.press = Mock(side_effect=lambda key, **kwargs: setattr(editor, "text", "changed during focus"))
    adapter.discard_prepared(browser.page)
    editor.press.assert_called_once_with("ControlOrMeta+A", timeout=1500)
    assert editor.text == "changed during focus"


def test_cleanup_waits_for_controlled_editor_without_repeating_delete(browser):
    adapter = production_conversation(browser.page)
    adapter.message_snapshot(browser.page)
    adapter.prepare_message(browser.page, "hello")
    editor = browser.page.elements[adapter._EDITOR][0]
    editor.press = Mock()
    editor.evaluate = Mock(side_effect=lambda script: "" if browser.page.clock[0] >= 0.3 else "hello")
    adapter.discard_prepared(browser.page)
    assert [c.args[0] for c in editor.press.call_args_list] == ["ControlOrMeta+A", "Backspace"]
    assert 0.3 <= browser.page.clock[0] < 0.5


def test_cleanup_timeout_does_not_repeat_or_report_cleared(browser):
    adapter = production_conversation(browser.page)
    adapter.message_snapshot(browser.page)
    adapter.prepare_message(browser.page, "hello")
    editor = browser.page.elements[adapter._EDITOR][0]
    editor.press = Mock()
    with pytest.raises(BrowserError, match="未能清空"):
        adapter.discard_prepared(browser.page)
    assert [c.args[0] for c in editor.press.call_args_list] == ["ControlOrMeta+A", "Backspace"]
    assert 1.5 <= browser.page.clock[0] < 1.7


def test_pending_receipt_structure_can_mature_without_resending(browser):
    observations = [UnsupportedPage("local message has no server ID yet"), None, browser.adapter.confirmation]

    def observe(*args):
        value = observations.pop(0)
        if isinstance(value, Exception):
            raise value
        return value

    browser.adapter.confirm_message = Mock(side_effect=observe)
    receipt = send(browser)
    assert receipt["status"] == "confirmed"
    assert browser.adapter.events.count("send") == 1
    assert browser.page.clock[0] == 0.5


def test_unrecognized_receipt_structure_times_out_as_uncertain(browser):
    browser.adapter.confirm_message = Mock(side_effect=UnsupportedPage("changed message schema"))
    with pytest.raises(SendUncertain):
        send(browser)
    assert browser.adapter.events.count("send") == 1
    assert browser.page.clock[0] == 15


def test_switched_account_cannot_confirm_its_own_message(browser, monkeypatch):
    adapter, before, new = ready_receipt(browser, monkeypatch)
    browser.page.elements["script#RENDER_DATA[type='application/json']"] = [FixtureElement(quote(json.dumps({
        "app": {"user": {"isLogin": True, "statusCode": 0, "info": {"secUid": "account_b", "nickname": "Account B"}}},
    })))]
    new["sender"] = "account_b"
    with pytest.raises(LoginRequired):
        adapter.confirm_message(browser.page, before, "hello", "contact_a")


def test_configured_account_is_checked_before_snapshot(browser):
    adapter = production_conversation(browser.page)
    adapter.expected_account_id = "account_b"
    with pytest.raises(LoginRequired):
        adapter.message_snapshot(browser.page)


def test_initially_verified_account_cannot_change_before_snapshot(browser):
    adapter = production_conversation(browser.page)
    browser.page.elements["a[href]"] = [FixtureElement(href="/user/self")]
    assert adapter.read_account(browser.page)["identity"] == "account_a"
    browser.page.elements["script#RENDER_DATA[type='application/json']"] = [FixtureElement(quote(json.dumps({
        "app": {"user": {"isLogin": True, "statusCode": 0, "info": {"secUid": "account_b", "nickname": "Account B"}}},
    })))]
    with pytest.raises(LoginRequired):
        adapter.message_snapshot(browser.page)


def test_pre_submit_failure_cleans_only_adapter_owned_draft(browser):
    browser.adapter.discard_prepared = Mock()
    browser.adapter.prepare_message = Mock(side_effect=BrowserInterrupted("cancelled"))
    with pytest.raises(BrowserInterrupted):
        send(browser)
    browser.adapter.discard_prepared.assert_called_once_with(browser.page)


def test_post_boundary_failure_never_cleans_draft(browser):
    browser.adapter.discard_prepared = Mock()
    browser.adapter.trigger_error = RuntimeError("disconnected")
    with pytest.raises(SendUncertain):
        send(browser)
    browser.adapter.discard_prepared.assert_not_called()


def group_record(raw="group-a", name="同名群"):
    return {"kind": "group", "identity": group_identity(raw), "name": name, "profile_url": ""}


def test_group_send_resolves_stable_conversation_without_profile_navigation(browser):
    target = group_record()
    browser.adapter.active_identity = target["identity"]
    browser.adapter.resolve_conversation = Mock(return_value=target)
    browser.adapter.confirmation["identity"] = target["identity"]
    boundary = Mock()
    receipt = send(browser, identity=target["identity"], profile_url="", kind="group", before_send=boundary)
    assert receipt["identity"] == target["identity"]
    browser.adapter.resolve_conversation.assert_called_once_with(browser.page, target["identity"], kind="group")
    assert browser.page.visits == ["https://www.douyin.com/jingxuan"]
    assert "open" not in browser.adapter.events
    boundary.assert_called_once()


@pytest.mark.parametrize("kind,identity", [("group", "contact_a"), ("direct", group_identity("a")), ("unknown", "contact_a")])
def test_mixed_recipient_namespaces_stop_before_browser_actions(browser, kind, identity):
    with pytest.raises(BrowserError):
        send(browser, identity=identity, kind=kind)
    assert browser.adapter.events == []
    assert browser.page.visits == []


def test_group_binding_rechecks_independent_header(browser):
    target = group_record()
    browser.adapter.resolve_conversation = Mock(return_value=target)
    with pytest.raises(BrowserError, match="聊天对象"):
        browser.resolve_conversation(target["identity"])
    assert browser.adapter.events == []


def test_group_list_can_be_empty_and_rejects_duplicate_identity(browser):
    browser.adapter.list_conversations = Mock(return_value=[])
    assert browser.list_conversations() == []
    browser.adapter.list_conversations.return_value = [group_record(), group_record()]
    with pytest.raises(UnsupportedPage, match="重复"):
        browser.list_conversations()


def production_group(page, raw="group-a"):
    adapter = production_conversation(page)
    page.elements[adapter._DIALOG] = [FixtureElement()]
    page.elements[adapter._HEADER] = [FixtureElement(react_target={"type": 2, "id": raw, "direct_id": ""})]
    rows = page.elements[adapter._DIALOG + " .messageMessageBoxmessageBox"]
    for row in rows:
        row.update(conversation_id=raw, conversation_type=2)
    return adapter


def test_group_list_filters_by_observed_type_and_keeps_duplicate_names(browser):
    adapter = production_group(browser.page)
    browser.page.elements["[data-e2e='conversation-item']"] = [
        FixtureElement(conversation={"type": 1, "id": "person", "name": "同名群"}),
        FixtureElement(conversation={"type": 2, "id": "group-a", "name": "同名群"}),
        FixtureElement(conversation={"type": 2, "id": "group-b", "name": "同名群"}),
    ]
    assert adapter.list_conversations(browser.page) == [group_record("group-a"), group_record("group-b")]


def test_group_header_numeric_id_is_never_stringified(browser):
    adapter = production_group(browser.page)
    browser.page.elements[adapter._HEADER][0].attributes["react_target"]["id"] = 1234567890123456789
    with pytest.raises(UnsupportedPage):
        adapter.conversation_identity(browser.page)


def test_group_opens_exact_stable_id_and_verifies_header(browser):
    adapter = production_group(browser.page)
    wrong = FixtureElement(conversation={"type": 2, "id": "group-b", "name": "同名群"})
    target = FixtureElement(conversation={"type": 2, "id": "group-a", "name": "同名群"})
    browser.page.elements["[data-e2e='conversation-item']"] = [wrong, target]
    assert adapter.resolve_conversation(browser.page, group_identity("group-a")) == group_record()
    assert target.clicked and not getattr(wrong, "clicked", False)


def test_group_resolution_rejects_person_with_same_raw_id(browser):
    adapter = production_group(browser.page)
    item = FixtureElement(conversation={"type": 1, "id": "group-a", "name": "同名群"})
    browser.page.elements["[data-e2e='conversation-item']"] = [item]
    with pytest.raises(BrowserError, match="找不到"):
        adapter.resolve_conversation(browser.page, group_identity("group-a"))
    assert not getattr(item, "clicked", False)


def test_group_snapshot_refuses_messages_from_another_conversation(browser):
    adapter = production_group(browser.page)
    browser.page.elements[adapter._DIALOG + " .messageMessageBoxmessageBox"][0]["conversation_id"] = "group-b"
    with pytest.raises(UnsupportedPage, match="消息列表"):
        adapter.message_snapshot(browser.page)


@pytest.mark.parametrize("conversation_id,conversation_type", [("group-a", 2), ("group-b", 2), ("group-a", 1)])
def test_group_receipt_requires_message_conversation_identity_and_type(browser, monkeypatch, conversation_id, conversation_type):
    adapter = production_group(browser.page)
    before = adapter.message_snapshot(browser.page)
    adapter.prepare_message(browser.page, "hello")
    browser.page.elements["[data-e2e='im-dialog'] [data-e2e='msg-input'] .e2e-send-msg-btn"] = [FixtureElement()]
    monkeypatch.setattr("douyinfire.browser.time.time", lambda: 5.5)
    adapter.trigger_send(browser.page)
    browser.page.elements[adapter._DIALOG + " .messageMessageBoxmessageBox"].append({
        "server_id": "200", "client_id": "new-client", "server_status": 0, "from_me": True,
        "sender": "account_a", "created_at": 5500, "text": "hello",
        "conversation_id": conversation_id, "conversation_type": conversation_type,
    })
    receipt = adapter.confirm_message(browser.page, before, "hello", group_identity("group-a"))
    assert bool(receipt) is (conversation_id == "group-a" and conversation_type == 2)


def direct_record(identity="contact_a", name="同名联系人"):
    return {"kind": "direct", "identity": identity, "name": name, "profile_url": f"https://www.douyin.com/user/{identity}"}


def direct_item(identity="contact_a", kind=1, raw="raw-conversation-a"):
    return FixtureElement(conversation={"type": kind, "id": raw, "direct_id": identity, "name": "同名联系人"})


def test_direct_candidates_filter_self_missing_identity_unknown_type_and_group(browser):
    adapter = production_conversation(browser.page)
    browser.page.elements[adapter._DIALOG] = [FixtureElement()]
    browser.page.elements["[data-e2e='conversation-item']"] = [
        direct_item("account_a"), direct_item(""), direct_item(None), direct_item(1234),
        direct_item("group:invalid"), direct_item("self"), direct_item("me"),
        direct_item("contact_a", 2), direct_item("contact_a", None), direct_item("contact_a", True),
        direct_item("contact_a"), direct_item("contact_b", raw="other-conversation"),
    ]
    assert adapter.list_conversations(browser.page, kind="direct") == [direct_record(), direct_record("contact_b")]


def test_direct_resolve_keeps_secuid_and_independently_verifies_header(browser):
    adapter = production_conversation(browser.page)
    browser.page.elements[adapter._DIALOG] = [FixtureElement()]
    target, other = direct_item(), direct_item("contact_b")
    browser.page.elements["[data-e2e='conversation-item']"] = [other, target]
    assert adapter.resolve_conversation(browser.page, "contact_a", kind="direct") == direct_record()
    assert target.clicked and not getattr(other, "clicked", False)


@pytest.mark.parametrize("type_value", [2, None, True])
def test_direct_resolve_cannot_open_another_conversation_type(browser, type_value):
    adapter = production_conversation(browser.page)
    browser.page.elements[adapter._DIALOG] = [FixtureElement()]
    item = direct_item(kind=type_value)
    browser.page.elements["[data-e2e='conversation-item']"] = [item]
    with pytest.raises(BrowserError, match="找不到"):
        adapter.resolve_conversation(browser.page, "contact_a", kind="direct")
    assert not getattr(item, "clicked", False)


def test_direct_duplicates_with_different_raw_conversation_ids_are_ambiguous(browser):
    adapter = production_conversation(browser.page)
    browser.page.elements[adapter._DIALOG] = [FixtureElement()]
    items = [direct_item(raw="raw-one"), direct_item(raw="raw-two")]
    browser.page.elements["[data-e2e='conversation-item']"] = items
    with pytest.raises(UnsupportedPage, match="重复"):
        adapter.list_conversations(browser.page, kind="direct")
    with pytest.raises(UnsupportedPage, match="多个"):
        adapter.resolve_conversation(browser.page, "contact_a", kind="direct")
    assert not any(getattr(item, "clicked", False) for item in items)


@pytest.mark.parametrize("change", [{"direct_id": "contact_b"}, {"type": 2}, {"type": True}])
def test_direct_candidate_is_rechecked_before_click(browser, change):
    adapter = production_conversation(browser.page)
    browser.page.elements[adapter._DIALOG] = [FixtureElement()]
    item = direct_item()
    original = item.attributes["conversation"]
    item.evaluate = Mock(side_effect=[dict(original), dict(original, **change)])
    browser.page.elements["[data-e2e='conversation-item']"] = [item]
    with pytest.raises(UnsupportedPage, match="发生变化"):
        adapter.resolve_conversation(browser.page, "contact_a", kind="direct")
    assert not getattr(item, "clicked", False)


@pytest.mark.parametrize("target", [{"type": 1, "direct_id": "contact_b"}, {"type": 2, "id": "contact_a", "direct_id": "contact_a"}])
def test_direct_opening_requires_independent_matching_header_type_and_identity(browser, target):
    adapter = production_conversation(browser.page)
    browser.page.elements[adapter._DIALOG] = [FixtureElement()]
    browser.page.elements[adapter._HEADER] = [FixtureElement(react_target=target)]
    browser.page.elements["[data-e2e='conversation-item']"] = [direct_item()]
    with pytest.raises(BrowserError, match="不一致"):
        adapter.resolve_conversation(browser.page, "contact_a", kind="direct")


def test_direct_self_resolution_stops_before_list_is_opened(browser):
    adapter = production_conversation(browser.page)
    adapter._open_message_list = Mock()
    with pytest.raises(BrowserError, match="自己"):
        adapter.resolve_conversation(browser.page, "account_a", kind="direct")
    adapter._open_message_list.assert_not_called()


def test_browser_direct_list_and_resolution_return_canonical_profile_without_visiting_it(browser):
    browser.adapter.list_conversations = Mock(return_value=[direct_record(), direct_record("account_a")])
    browser.adapter.resolve_conversation = Mock(return_value=direct_record())
    assert browser.list_conversations(kind="direct") == [direct_record()]
    assert browser.resolve_conversation("contact_a", kind="direct") == direct_record()
    assert all(url == "https://www.douyin.com/jingxuan" for url in browser.page.visits)
    browser.adapter.resolve_conversation.assert_called_once_with(browser.page, "contact_a", kind="direct")


def test_browser_direct_candidate_cannot_supply_an_unrelated_profile_url(browser):
    browser.adapter.list_conversations = Mock(return_value=[dict(direct_record(), profile_url="https://example.com/")])
    with pytest.raises(UnsupportedPage, match="主页"):
        browser.list_conversations(kind="direct")
