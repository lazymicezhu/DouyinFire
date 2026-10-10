"""Chromium checks against synthetic DOM; no accounts, live HTML or network."""

import time
import pytest

from douyinfire.browser import BrowserError, DouyinDOMAdapter, LoginRequired, UnsupportedPage
from douyinfire.identities import group_identity


@pytest.fixture(scope="module")
def chromium():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def page(chromium):
    context = chromium.new_context(service_workers="block")
    context.route("**/*", lambda route: route.abort())
    page = context.new_page()
    page.set_content("""
        <!doctype html><html><head><meta charset="utf-8">
        <style>[contenteditable] { min-height: 40px; white-space: pre-wrap; }</style>
        </head><body>
        <section data-e2e="im-dialog"><div data-e2e="msg-input">
          <div id="editor" contenteditable="true" data-slate-editor="true"></div>
        </div></section>
        <script>
          window.inputEvents = 0;
          document.addEventListener('input', () => window.inputEvents++);
        </script>
        </body></html>
    """)
    try:
        yield page
    finally:
        context.close()


@pytest.mark.parametrize("placeholder", ["", '<span data-slate-zero-width="n">\ufeff<br></span>'])
@pytest.mark.parametrize("message", [
    "第一行：你好 👋🏽\n第二行 👨‍👩‍👧‍👦 🔥",
    "第一行：你好 👋🏽\n\n第二段 👨‍👩‍👧‍👦 🔥\n  保留前后的空格  ",
])
def test_empty_slate_editor_preserves_multiline_and_emoji(page, placeholder, message):
    editor = page.locator("#editor")
    editor.evaluate("(element, html) => { element.innerHTML = html; }", placeholder)
    adapter = DouyinDOMAdapter()

    assert adapter.prepare_message(page, message) is None

    assert adapter._editor_text(editor) == message
    # Chromium's innerText counts a blank <div><br></div> as two line breaks.
    # Ordinary multiple lines have an independent native readback as well.
    if "\n\n" not in message:
        assert editor.inner_text() == message
    assert page.evaluate("window.inputEvents") > 0


@pytest.mark.parametrize("draft", ["原有草稿 📝\n请保留这行", "这次打算发送的消息"])
@pytest.mark.parametrize("inside_placeholder", [False, True])
def test_existing_draft_is_rejected_without_dom_or_input_changes(page, draft, inside_placeholder):
    editor = page.locator("#editor")
    editor.evaluate("""(element, {text, insidePlaceholder}) => {
        if (insidePlaceholder) {
            const span = document.createElement('span');
            span.dataset.slateZeroWidth = 'n';
            span.textContent = text;
            element.replaceChildren(span);
        } else {
            element.textContent = text;
        }
    }""", {"text": draft, "insidePlaceholder": inside_placeholder})
    before_html = editor.inner_html()

    with pytest.raises(BrowserError, match="草稿"):
        DouyinDOMAdapter().prepare_message(page, "这次打算发送的消息")

    assert editor.inner_text() == draft
    assert editor.inner_html() == before_html
    assert page.evaluate("window.inputEvents") == 0


def test_multiple_visible_editors_are_rejected_without_mutation(page):
    page.locator("#editor").evaluate("""element => {
        const second = element.cloneNode(false);
        second.id = 'second-editor';
        element.parentElement.appendChild(second);
    }""")

    with pytest.raises(UnsupportedPage, match="多个"):
        DouyinDOMAdapter().prepare_message(page, "不应填入任何输入框")

    assert page.locator("#editor").inner_text() == ""
    assert page.locator("#second-editor").inner_text() == ""
    assert page.evaluate("window.inputEvents") == 0


def test_hidden_editor_does_not_replace_the_unique_visible_editor(page):
    page.locator("#editor").evaluate("""element => {
        const hidden = element.cloneNode(false);
        hidden.id = 'hidden-editor';
        hidden.style.display = 'none';
        hidden.textContent = '隐藏输入框的草稿';
        element.parentElement.appendChild(hidden);
    }""")

    DouyinDOMAdapter().prepare_message(page, "可见输入框 👋\n第二行")

    assert page.locator("#editor").inner_text() == "可见输入框 👋\n第二行"
    assert page.locator("#hidden-editor").text_content() == "隐藏输入框的草稿"


def install_controlled_ace_editor(page, initial_text=""):
    """Render fake controlled state from native beforeinput into the line shape."""
    page.locator("#editor").evaluate(r"""(editor, initialText) => {
        function render(text) {
            const lines = text.split('\n').map(value => {
                const line = document.createElement('div');
                line.className = 'ace-line';
                line.dataset.node = 'true';
                const body = document.createElement('span');
                body.dataset.string = 'true';
                body.dataset.leaf = 'true';
                body.textContent = value;
                const sentinel = document.createElement('span');
                sentinel.dataset.string = 'true';
                sentinel.dataset.enter = 'true';
                sentinel.dataset.leaf = 'true';
                sentinel.textContent = '\u200b';
                line.append(body, sentinel);
                return line;
            });
            editor.replaceChildren(...lines);
        }
        render(initialText);
        window.controlledInsertEvents = 0;
        window.backwardDeletionSelections = [];
        window.editorKeys = [];
        editor.addEventListener('keydown', event => window.editorKeys.push(event.key));
        editor.addEventListener('beforeinput', event => {
            if (event.inputType === 'insertText' && typeof event.data === 'string') {
                // One native beforeinput contains the complete multiline value;
                // Chromium's subsequent input events otherwise arrive per line.
                event.preventDefault();
                window.controlledInsertEvents++;
                render(event.data);
            } else if (event.inputType === 'deleteContentForward') {
                // The observed controlled editor leaves fill('') unchanged.
                event.preventDefault();
            } else if (event.inputType === 'deleteContentBackward') {
                event.preventDefault();
                window.backwardDeletionSelections.push(window.getSelection().toString());
                render('');
            }
        });
    }""", initial_text)


@pytest.mark.parametrize("message", [
    "合成火花测试 🔥",
    "第一行：你好 👋🏽\n第二行 👨‍👩‍👧‍👦",
    "第一段\n\n保留空白段落 🔥",
    "保留正文中的\u200b零宽字符 🔥",
])
def test_controlled_ace_editor_keeps_exact_body_and_ignores_line_sentinels(page, message):
    install_controlled_ace_editor(page)
    adapter = DouyinDOMAdapter()
    editor = page.locator("#editor")
    assert adapter._editor_text(editor) == ""

    adapter.prepare_message(page, message)

    assert adapter._editor_text(editor) == message
    body = editor.locator(".ace-line > span:not([data-enter])")
    assert body.all_text_contents() == message.split("\n")
    sentinels = editor.locator(".ace-line > span[data-enter='true']")
    assert sentinels.all_text_contents() == ["\u200b"] * len(message.split("\n"))
    assert page.evaluate("window.controlledInsertEvents") == 1


@pytest.mark.parametrize("draft", ["标记内的既有草稿", "\u200b标记内的草稿\u200b"])
def test_ace_line_marker_with_real_draft_is_not_discarded(page, draft):
    install_controlled_ace_editor(page)
    editor = page.locator("#editor")
    editor.locator("[data-enter]").evaluate("(node, text) => { node.textContent = text; }", draft)
    original = editor.inner_html()

    with pytest.raises(BrowserError, match="草稿"):
        DouyinDOMAdapter().prepare_message(page, "新的合成消息")

    assert editor.inner_html() == original
    assert editor.locator("[data-enter]").text_content() == draft
    assert page.evaluate("window.inputEvents") == 0


@pytest.mark.parametrize("draft", ["\u200b", "正文内的\u200b草稿"])
def test_zero_width_characters_in_ace_body_are_existing_draft(page, draft):
    install_controlled_ace_editor(page, draft)
    adapter = DouyinDOMAdapter()
    editor = page.locator("#editor")
    original = editor.inner_html()
    assert adapter._editor_text(editor) == draft

    with pytest.raises(BrowserError, match="草稿"):
        adapter.prepare_message(page, "不能覆盖正文")

    assert editor.inner_html() == original
    assert page.evaluate("window.inputEvents") == 0


def install_synthetic_profile(page):
    """Fulfill a fake profile URL in memory; no request reaches a website."""
    markup = """<!doctype html><html><head><meta charset="utf-8"></head><body>
      <section data-e2e="user-detail"><button id="profile-message">私信</button></section>
      <button data-e2e="im-entry">消息</button>
      <section data-e2e="im-dialog" hidden>
        <header class="StackLayoutStackChatHeaderuser" hidden>同名联系人</header>
        <div data-e2e="msg-input">
          <div id="profile-editor" contenteditable="true" data-slate-editor="true"
               style="min-height:40px;white-space:pre-wrap" hidden></div>
        </div>
        <div id="conversation-list"></div>
      </section>
      <script>
        window.profileEvents = [];
        window.earlyClicks = 0;
        window.openSyntheticConversation = identity => {
          document.querySelector('[data-e2e="im-dialog"]').hidden = false;
          const header = document.querySelector('header');
          header.hidden = false;
          header.__reactProps$fixture = {children: [{props: {
            conversation: {type: 1, toParticipantSecUserId: identity}
          }}]};
          document.querySelector('#profile-editor').hidden = false;
        };
        document.querySelector('#profile-message').addEventListener('click', event => {
          window.profileEvents.push('profile');
          const handler = event.currentTarget.__reactProps$fixture?.onClick;
          if (typeof handler === 'function') handler();
          else window.earlyClicks++;
        });
        document.querySelector('[data-e2e="im-entry"]').addEventListener('click', () => {
          window.profileEvents.push('entry');
          document.querySelector('[data-e2e="im-dialog"]').hidden = false;
        });
        window.addSyntheticContact = (label, identity) => {
          const item = document.createElement('button');
          item.dataset.e2e = 'conversation-item';
          item.textContent = '同名联系人';
          item.__reactProps$fixture = {children: [{props: {children: {props: {
            conversation: {toParticipantSecUserId: identity}
          }}}}]};
          item.addEventListener('click', () => {
            window.profileEvents.push(label);
            window.openSyntheticConversation(identity);
          });
          document.querySelector('#conversation-list').appendChild(item);
        };
      </script>
    </body></html>"""
    url = "https://www.douyin.com/user/synthetic_contact_a"
    page.route(url, lambda route: route.fulfill(status=200, content_type="text/html", body=markup))
    page.goto(url)


def test_profile_button_waits_for_delayed_click_handler_before_clicking(page):
    install_synthetic_profile(page)
    page.evaluate("""() => {
        const button = document.querySelector('#profile-message');
        setTimeout(() => { button.__reactProps$fixture = {}; }, 150);
        setTimeout(() => {
            button.__reactProps$fixture.onClick = () => {
                window.openSyntheticConversation('synthetic_contact_a');
            };
        }, 500);
    }""")

    DouyinDOMAdapter().open_conversation(page, "synthetic_contact_a")

    assert page.evaluate("window.earlyClicks") == 0
    assert page.evaluate("window.profileEvents") == ["profile"]
    assert DouyinDOMAdapter().conversation_identity(page) == "synthetic_contact_a"


def test_existing_target_conversation_is_reused_without_any_navigation_click(page):
    install_synthetic_profile(page)
    page.evaluate("window.openSyntheticConversation('synthetic_contact_a')")

    DouyinDOMAdapter().open_conversation(page, "synthetic_contact_a")

    assert page.evaluate("window.profileEvents") == []
    assert page.evaluate("window.earlyClicks") == 0


def test_existing_different_conversation_is_rejected_without_clicking(page):
    install_synthetic_profile(page)
    page.evaluate("window.openSyntheticConversation('synthetic_contact_b')")

    with pytest.raises(BrowserError, match="不一致"):
        DouyinDOMAdapter().open_conversation(page, "synthetic_contact_a")

    assert page.evaluate("window.profileEvents") == []
    assert DouyinDOMAdapter().conversation_identity(page) == "synthetic_contact_b"


def test_recent_list_selects_unique_stable_identity_despite_same_display_name(page):
    install_synthetic_profile(page)
    page.evaluate("""() => {
        document.querySelector('#profile-message').remove();
        window.addSyntheticContact('other', 'synthetic_contact_b');
        window.addSyntheticContact('target', 'synthetic_contact_a');
    }""")

    DouyinDOMAdapter().open_conversation(page, "synthetic_contact_a")

    assert page.evaluate("window.profileEvents") == ["entry", "target"]
    assert DouyinDOMAdapter().conversation_identity(page) == "synthetic_contact_a"


def test_duplicate_stable_id_in_recent_list_is_rejected_without_selecting(page):
    install_synthetic_profile(page)
    page.evaluate("""() => {
        document.querySelector('#profile-message').remove();
        window.addSyntheticContact('first', 'synthetic_contact_a');
        window.addSyntheticContact('second', 'synthetic_contact_a');
    }""")

    with pytest.raises(UnsupportedPage, match="重复"):
        DouyinDOMAdapter().open_conversation(page, "synthetic_contact_a")

    assert page.evaluate("window.profileEvents") == ["entry"]


def add_message(page, **overrides):
    """A deliberately tiny React-props contract attached to a synthetic row."""
    message = {
        "serverId": "200", "clientId": "new-client", "serverStatus": 0,
        "isFromMe": True, "secSender": "account_a",
        "createdAt": int(time.time() * 1000), "text": "本地测试消息 🔥",
    }
    message.update(overrides)
    page.evaluate("""message => {
        const row = document.createElement('article');
        row.className = 'messageMessageBoxmessageBox';
        const text = document.createElement('p');
        text.dataset.e2e = 'msg-item-content';
        text.textContent = message.text;
        row.appendChild(text);
        const metadata = document.createElement('span');
        metadata.textContent = '12:34 已读';
        row.appendChild(metadata);
        message.createdAt = new Date(message.createdAt);
        row.__reactProps$fixture = {children: [null, {props: {
            children: [{props: {children: [null, {props: {message}}]}}]
        }}]};
        document.querySelector('[data-e2e="im-dialog"]').appendChild(row);
    }""", message)


@pytest.fixture
def conversation(page):
    page.evaluate("""() => {
        const dialog = document.querySelector('[data-e2e="im-dialog"]');
        const header = document.createElement('header');
        header.className = 'StackLayoutStackChatHeaderuser';
        header.textContent = '任意显示名，身份只由绑定字段决定';
        header.__reactProps$fixture = {children: [{props: {
            conversation: {type: 1, toParticipantSecUserId: 'contact_a'}
        }}]};
        dialog.prepend(header);
        const script = document.createElement('script');
        script.id = 'RENDER_DATA';
        script.type = 'application/json';
        script.textContent = JSON.stringify({app: {user: {
            isLogin: true, statusCode: 0, info: {secUid: 'account_a', nickname: '合成账号'}
        }}});
        document.head.appendChild(script);
        const button = document.createElement('button');
        button.className = 'e2e-send-msg-btn';
        button.textContent = '仅记录本地点击';
        window.syntheticClicks = 0;
        button.onclick = () => { window.syntheticClicks++; };
        document.querySelector('[data-e2e="msg-input"]').appendChild(button);
    }""")
    add_message(page, serverId="100", clientId="old-client", text="旧消息")
    adapter = DouyinDOMAdapter()
    before = adapter.message_snapshot(page)
    assert before == {"100"}
    assert adapter.conversation_identity(page) == "contact_a"
    adapter.prepare_message(page, "本地测试消息 🔥")
    return adapter, before


def test_receipt_reads_only_new_own_message_body_from_rendered_row(page, conversation):
    adapter, before = conversation
    adapter.trigger_send(page)
    assert page.evaluate("window.syntheticClicks") == 1
    assert adapter.confirm_message(page, before, "本地测试消息 🔥", "contact_a") is None
    add_message(page)

    receipt = adapter.confirm_message(page, before, "本地测试消息 🔥", "contact_a")

    assert receipt["status"] == "confirmed"
    assert receipt["message_id"] == "200"
    assert receipt["identity"] == "contact_a"


@pytest.mark.parametrize("overrides", [
    {"serverId": "100"},
    {"clientId": "old-client"},
    {"secSender": "another_account"},
    {"isFromMe": False},
    {"serverStatus": 1},
    {"createdAt": 1},
    {"text": "另一条消息"},
])
def test_unrelated_or_unacknowledged_rows_cannot_confirm(page, conversation, overrides):
    adapter, before = conversation
    adapter.trigger_send(page)
    add_message(page, **overrides)

    assert adapter.confirm_message(page, before, "本地测试消息 🔥", "contact_a") is None


def test_two_matching_new_receipts_remain_ambiguous(page, conversation):
    adapter, before = conversation
    adapter.trigger_send(page)
    add_message(page)
    add_message(page, serverId="201", clientId="another-new-client")

    with pytest.raises(UnsupportedPage, match="多个"):
        adapter.confirm_message(page, before, "本地测试消息 🔥", "contact_a")


def test_changed_conversation_cannot_trigger_button(page, conversation):
    adapter, _ = conversation
    page.locator("header").evaluate("""header => {
        header.__reactProps$fixture.children[0].props.conversation.toParticipantSecUserId = 'contact_b';
    }""")

    with pytest.raises(BrowserError, match="对象发生变化"):
        adapter.trigger_send(page)

    assert page.evaluate("window.syntheticClicks") == 0


def test_account_changed_after_submission_cannot_confirm_another_sender(page, conversation):
    adapter, before = conversation
    adapter.trigger_send(page)
    page.locator("#RENDER_DATA").evaluate("""script => {
        const state = JSON.parse(script.textContent);
        state.app.user.info.secUid = 'account_b';
        script.textContent = JSON.stringify(state);
    }""")
    add_message(page, secSender="account_b")

    with pytest.raises(LoginRequired):
        adapter.confirm_message(page, before, "本地测试消息 🔥", "contact_a")


def test_discard_owned_draft_uses_selected_backspace_when_fill_empty_is_ignored(page, conversation):
    adapter, _ = conversation
    draft = "本地测试消息 🔥"
    install_controlled_ace_editor(page, draft)
    editor = page.locator("#editor")
    editor.fill("")
    assert adapter._editor_text(editor) == draft

    adapter.discard_prepared(page)

    assert adapter._editor_text(editor) == ""
    assert page.evaluate("window.editorKeys").count("Backspace") == 1
    assert page.evaluate("window.backwardDeletionSelections") == [draft + "\u200b"]
    assert editor.locator("[data-enter='true']").text_content() == "\u200b"


@pytest.mark.parametrize("changed", ["message", "identity", "submitted"])
def test_discard_does_not_touch_changed_or_already_submitted_draft(page, conversation, changed):
    adapter, _ = conversation
    draft = "用户改过的草稿" if changed == "message" else "本地测试消息 🔥"
    install_controlled_ace_editor(page, draft)
    if changed == "identity":
        page.locator("header").evaluate("""header => {
            header.__reactProps$fixture.children[0].props.conversation.toParticipantSecUserId = 'contact_b';
        }""")
    elif changed == "submitted":
        adapter.trigger_send(page)
    editor = page.locator("#editor")
    original = editor.inner_html()

    adapter.discard_prepared(page)

    assert editor.inner_html() == original
    assert page.evaluate("window.editorKeys") == []
    assert page.evaluate("window.backwardDeletionSelections") == []


def install_group_list(page, *, hidden=False):
    page.evaluate("""hidden => {
        const dialog = document.querySelector('[data-e2e="im-dialog"]');
        dialog.hidden = hidden;
        const entry = document.createElement('div');
        entry.dataset.e2e = 'im-entry';
        const entryButton = document.createElement('button');
        entryButton.dataset.e2e = 'something-button';
        entryButton.textContent = '消息';
        window.groupClicks = [];
        entryButton.onclick = () => { window.groupClicks.push('entry'); dialog.hidden = false; };
        entryButton.__reactProps$fixture = {onClick: () => {}};
        entry.appendChild(entryButton);
        document.body.prepend(entry);
        const header = document.createElement('header');
        header.className = 'StackLayoutStackChatHeaderuser';
        header.textContent = '相同群名';
        header.hidden = true;
        dialog.prepend(header);
        window.addSyntheticGroupRow = (id, type = 2) => {
            const row = document.createElement('button');
            row.dataset.e2e = 'conversation-item';
            const title = document.createElement('span');
            title.className = 'conversationConversationItemtitle';
            title.textContent = '相同群名';
            row.appendChild(title);
            const conversation = {id, type, toParticipantSecUserId: type === 1 ? 'person_a' : ''};
            row.__reactProps$fixture = {children: [{props: {children: {props: {conversation}}}}]};
            row.onclick = () => {
                window.groupClicks.push(id);
                header.hidden = false;
                header.__reactProps$fixture = {children: [{props: {conversation: {...conversation}}}]};
            };
            dialog.appendChild(row);
        };
        window.addSyntheticGroupRow('person-conversation', 1);
        window.addSyntheticGroupRow('group-a');
        window.addSyntheticGroupRow('group-b');
    }""", hidden)


def test_group_candidates_use_exact_react_conversation_type_and_id(page):
    install_group_list(page, hidden=True)
    adapter = DouyinDOMAdapter()
    values = adapter.list_conversations(page)
    assert values == [
        {"kind": "group", "identity": group_identity(raw), "name": "相同群名", "profile_url": ""}
        for raw in ["group-a", "group-b"]
    ]
    assert page.evaluate("window.groupClicks") == ["entry"]
    selected = adapter.resolve_conversation(page, group_identity("group-b"))
    assert selected["identity"] == group_identity("group-b")
    assert page.evaluate("window.groupClicks") == ["entry", "group-b"]
    assert adapter.conversation_identity(page) == selected["identity"]


def test_group_header_cannot_be_replaced_by_list_candidate_identity(page):
    install_group_list(page)
    page.evaluate("""() => {
        const row = [...document.querySelectorAll('[data-e2e="conversation-item"]')][1];
        row.onclick = () => {
            const header = document.querySelector('header');
            header.hidden = false;
            header.__reactProps$fixture = {children: [{props: {conversation: {id: 'wrong-group', type: 2}}}]};
        };
    }""")
    with pytest.raises(BrowserError, match="不一致"):
        DouyinDOMAdapter().resolve_conversation(page, group_identity("group-a"))
    assert page.locator('#editor').inner_text() == ''


def test_duplicate_group_id_is_rejected_even_when_display_names_match(page):
    install_group_list(page)
    page.evaluate("window.addSyntheticGroupRow('group-a')")
    with pytest.raises(UnsupportedPage, match="重复"):
        DouyinDOMAdapter().list_conversations(page)
    assert page.evaluate("window.groupClicks") == []


def test_group_entry_waits_until_observed_child_handler_is_ready(page):
    install_group_list(page, hidden=True)
    page.evaluate("""() => {
        const entry = document.querySelector('[data-e2e="something-button"]');
        delete entry.__reactProps$fixture;
        window.earlyGroupClick = false;
        entry.addEventListener('click', () => {
            if (!entry.__reactProps$fixture?.onClick) window.earlyGroupClick = true;
        });
        setTimeout(() => { entry.__reactProps$fixture = {onClick: () => {}}; }, 300);
    }""")
    assert len(DouyinDOMAdapter().list_conversations(page)) == 2
    assert page.evaluate("window.earlyGroupClick") is False


@pytest.mark.parametrize("message_group,message_type", [("group-a", 2), ("group-b", 2), ("group-a", 1)])
def test_group_receipt_reads_message_conversation_id_and_type(page, conversation, message_group, message_type):
    adapter, _ = conversation
    page.evaluate("""() => {
        document.querySelector('#editor').textContent = '';
        document.querySelector('header').__reactProps$fixture.children[0].props.conversation = {
            id: 'group-a', type: 2, toParticipantSecUserId: ''
        };
        for (const row of document.querySelectorAll('.messageMessageBoxmessageBox')) {
            const m = row.__reactProps$fixture.children[1].props.children[0].props.children[1].props.message;
            m.conversationId = 'group-a';
            m.conversationType = 2;
        }
    }""")
    before = adapter.message_snapshot(page)
    adapter.prepare_message(page, "本地测试消息 🔥")
    adapter.trigger_send(page)
    add_message(page, conversationId=message_group, conversationType=message_type)
    receipt = adapter.confirm_message(page, before, "本地测试消息 🔥", group_identity("group-a"))
    assert bool(receipt) is (message_group == "group-a" and message_type == 2)


def install_direct_list(page):
    install_group_list(page)
    page.evaluate("""() => {
        for (const row of document.querySelectorAll('[data-e2e="conversation-item"]')) row.remove();
        const script = document.createElement('script');
        script.id = 'RENDER_DATA'; script.type = 'application/json';
        script.textContent = JSON.stringify({app: {user: {
            isLogin: true, statusCode: 0, info: {secUid: 'account_a', nickname: '合成账号'}
        }}});
        document.head.appendChild(script);
        window.addSyntheticDirectRow = (id, directId, type = 1) => {
            const row = document.createElement('button');
            row.dataset.e2e = 'conversation-item';
            const title = document.createElement('span');
            title.className = 'conversationConversationItemtitle';
            title.textContent = '同名联系人'; row.appendChild(title);
            const conversation = {id, type, toParticipantSecUserId: directId};
            row.__reactProps$fixture = {children: [{props: {children: {props: {conversation}}}}]};
            row.onclick = () => {
                window.groupClicks.push(id);
                const header = document.querySelector('header'); header.hidden = false;
                header.__reactProps$fixture = {children: [{props: {conversation: {...conversation}}}]};
            };
            document.querySelector('[data-e2e="im-dialog"]').appendChild(row);
        };
    }""")


def test_direct_list_filters_self_missing_identity_and_types_then_opens_exact_peer(page):
    install_direct_list(page)
    page.evaluate("""() => {
        window.addSyntheticDirectRow('self-thread', 'account_a');
        window.addSyntheticDirectRow('missing-peer', '');
        window.addSyntheticDirectRow('group-with-peer', 'contact_a', 2);
        window.addSyntheticDirectRow('unknown-type', 'contact_a', null);
        window.addSyntheticDirectRow('boolean-type', 'contact_a', true);
        window.addSyntheticDirectRow('raw-thread-a', 'contact_a');
        window.addSyntheticDirectRow('raw-thread-b', 'contact_b');
    }""")
    adapter = DouyinDOMAdapter()
    candidates = adapter.list_conversations(page, kind="direct")
    assert candidates == [
        {"kind": "direct", "identity": peer, "name": "同名联系人", "profile_url": f"https://www.douyin.com/user/{peer}"}
        for peer in ["contact_a", "contact_b"]
    ]
    assert adapter.resolve_conversation(page, "contact_b", kind="direct") == candidates[1]
    assert page.evaluate("window.groupClicks") == ["raw-thread-b"]
    assert adapter.conversation_identity(page) == "contact_b"
    assert page.locator("#editor").inner_text() == ""


def test_direct_repeated_peer_with_different_conversation_ids_is_not_guessed(page):
    install_direct_list(page)
    page.evaluate("""() => {
        window.addSyntheticDirectRow('raw-thread-a', 'contact_a');
        window.addSyntheticDirectRow('raw-thread-b', 'contact_a');
    }""")
    adapter = DouyinDOMAdapter()
    with pytest.raises(UnsupportedPage, match="重复"):
        adapter.list_conversations(page, kind="direct")
    with pytest.raises(UnsupportedPage, match="多个"):
        adapter.resolve_conversation(page, "contact_a", kind="direct")
    assert page.evaluate("window.groupClicks") == []


@pytest.mark.parametrize("changed", ["identity", "type"])
def test_direct_row_is_reobserved_before_click_when_react_props_change(page, changed):
    install_direct_list(page)
    page.evaluate("""changed => {
        window.addSyntheticDirectRow('raw-thread-a', 'contact_a');
        const row = document.querySelector('[data-e2e="conversation-item"]');
        const conversation = row.__reactProps$fixture.children[0].props.children.props.conversation;
        let reads = 0;
        const field = changed === 'identity' ? 'toParticipantSecUserId' : 'type';
        Object.defineProperty(conversation, field, {get() {
            reads++;
            return changed === 'identity' ? (reads === 1 ? 'contact_a' : 'contact_b') : (reads === 1 ? 1 : 2);
        }});
    }""", changed)
    with pytest.raises(UnsupportedPage, match="发生变化"):
        DouyinDOMAdapter().resolve_conversation(page, "contact_a", kind="direct")
    assert page.evaluate("window.groupClicks") == []


@pytest.mark.parametrize("header_type,header_peer", [(1, "contact_b"), (2, "contact_a")])
def test_direct_selected_row_cannot_override_an_unrelated_header(page, header_type, header_peer):
    install_direct_list(page)
    page.evaluate("""({type, peer}) => {
        window.addSyntheticDirectRow('raw-thread-a', 'contact_a');
        const row = document.querySelector('[data-e2e="conversation-item"]');
        row.onclick = () => {
            const header = document.querySelector('header'); header.hidden = false;
            header.__reactProps$fixture = {children: [{props: {conversation: {
                id: 'raw-thread-a', type, toParticipantSecUserId: peer
            }}}]};
        };
    }""", {"type": header_type, "peer": header_peer})
    with pytest.raises(BrowserError, match="不一致"):
        DouyinDOMAdapter().resolve_conversation(page, "contact_a", kind="direct")
    assert page.locator("#editor").inner_text() == ""
