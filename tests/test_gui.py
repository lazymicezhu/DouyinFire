from __future__ import annotations

import json
import threading
from contextlib import contextmanager
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from douyinfire import __version__
from douyinfire.config import config_to_dict, load_config, parse_config, save_config
from douyinfire.gui import GuiServer, run_gui


class StubManager:
    """No browser, account, or network operations can leave this fake worker."""
    def __init__(self) -> None:
        self.operation_lock = threading.RLock()
        self.running = False
        self.started: list[dict[str, Any]] = []
        self.resolved: list[tuple[str, str]] = []
        self.attempts: list[dict[str, Any]] = []
        self.closed = False
        self.result = None
        self.user_id = ""
        self.kind = ""
        self.job_id = ""

    def snapshot(self) -> dict[str, Any]:
        return {"running": self.running, "kind": self.kind, "job_id": self.job_id, "auth": {},
                "message": "测试任务" if self.running else "", "progress": {}, "error": "", "next_run": "",
                "result": self.result, "user_id": self.user_id}

    def start(self, kind: str, user_id: str = "", contact_ids: list[str] | None = None,
              payload: dict[str, Any] | None = None) -> dict[str, Any]:
        self.started.append({"kind": kind, "user_id": user_id, "contact_ids": contact_ids, "payload": payload})
        self.kind, self.user_id = kind, user_id
        self.job_id = "fake-job-" + str(len(self.started))
        self.result = None
        self.running = True
        return self.snapshot()

    def cancel(self) -> bool:
        was_running = self.running
        self.running = False
        return was_running

    def history(self, user_id: str = "") -> list[dict[str, Any]]:
        return [row for row in self.attempts if not user_id or row.get("user_id") == user_id]

    def resolve_attempt(self, attempt_id: str, resolution: str) -> dict[str, Any]:
        self.resolved.append((attempt_id, resolution))
        for row in self.attempts:
            if row["id"] == attempt_id:
                row["status"] = resolution
                return row
        raise ValueError("记录不存在")

    def close(self) -> None:
        self.closed = True


def sample_config() -> dict[str, Any]:
    return {"schedule": {"time": "12:34", "jitter_minutes": 7, "min_contact_interval_seconds": 90},
            "users": [{"id": "account-a", "name": "日常", "identity": "identity-a", "message": "默认 A", "contacts": [
                {"id": "contact-a", "name": "好友", "identity": "target-a", "profile_url": "https://www.douyin.com/user/target-a", "message": "特定问候", "custom_extra": "keep"}]},
                {"id": "account-b", "name": "工作", "message": "默认 B", "contacts": []}]}


@contextmanager
def running_server(tmp_path: Path, raw: dict[str, Any] | None = None):
    config_path = tmp_path / "config" / "douyinfire.yaml"
    if raw is not None:
        save_config(parse_config(raw, base_dir=tmp_path), config_path)
    manager = StubManager()
    actions = []

    def service(action: str) -> dict[str, Any]:
        actions.append(action)
        return {"status": "running", "action": action}

    gui = GuiServer(config_path, manager, service)
    server = ThreadingHTTPServer(("127.0.0.1", 0), gui.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield gui, manager, server, actions
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def request(server, gui, method, path, payload=None, headers=None):
    connection = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    all_headers = {"Host": f"127.0.0.1:{server.server_port}"}
    body = None
    if method == "POST":
        all_headers.update({"Content-Type": "application/json", "X-CSRF-Token": gui.csrf_token,
                            "Origin": f"http://127.0.0.1:{server.server_port}"})
        body = json.dumps(payload).encode()
    all_headers.update(headers or {})
    connection.request(method, path, body, all_headers)
    response = connection.getresponse()
    content = response.read()
    result = json.loads(content) if response.getheader("Content-Type", "").startswith("application/json") else content.decode()
    status, response_headers = response.status, dict(response.getheaders())
    connection.close()
    return status, result, response_headers


def test_empty_setup_and_account_creation_are_safe(tmp_path):
    with running_server(tmp_path) as (gui, manager, server, _):
        status, state, _ = request(server, gui, "GET", "/api/state")
        assert status == 200 and state["config"]["users"] == []
        assert state["config"]["schedule"]["enabled"] is False
        status, result, _ = request(server, gui, "POST", "/api/users", {"name": "新账号", "revision": state["revision"]})
        assert status == 200 and result["user_id"]
        user = load_config(gui.config_path).users[0]
        assert user.name == "新账号" and user.contacts == [] and user.message == ""
        assert manager.started == []


@pytest.mark.parametrize("headers,expected", [
    ({"Host": "attacker.test:8765"}, 403),
    ({"Origin": "https://attacker.test"}, 403),
    ({"X-CSRF-Token": ""}, 403),
    ({"Content-Type": "text/plain"}, 415),
    ({"Sec-Fetch-Site": "cross-site"}, 403),
])
def test_mutations_reject_foreign_pages_and_invalid_headers(tmp_path, headers, expected):
    with running_server(tmp_path, sample_config()) as (gui, manager, server, _):
        before = gui.config_path.read_bytes()
        status, result, _ = request(server, gui, "POST", "/api/users", {"name": "恶意账号"}, headers)
        assert status == expected and result["error"]
        assert gui.config_path.read_bytes() == before
        assert manager.started == []


def test_html_embeds_token_and_disables_cross_origin_framing(tmp_path):
    with running_server(tmp_path) as (gui, _, server, _):
        status, html, headers = request(server, gui, "GET", "/")
        assert status == 200 and gui.csrf_token in html
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        status, _, _ = request(server, gui, "GET", "/", headers={"Host": "attacker.test"})
        assert status == 403


def test_save_one_account_preserves_others_schedule_identity_and_extras(tmp_path):
    with running_server(tmp_path, sample_config()) as (gui, _, server, _):
        before = config_to_dict(load_config(gui.config_path))
        contact = dict(before["users"][0]["contacts"][0])
        contact["message"] = "修改后的自选消息"
        contact["identity"] = "forged-identity"
        status, _, _ = request(server, gui, "POST", "/api/users/account-a", {"message": "新默认", "contacts": [contact], "identity": "forged-user"})
        assert status == 200
        after = config_to_dict(load_config(gui.config_path))
        assert after["users"][1] == before["users"][1]
        assert after["schedule"] == before["schedule"]
        assert after["users"][0]["identity"] == "identity-a"
        assert after["users"][0]["contacts"][0]["identity"] == "target-a"
        assert after["users"][0]["contacts"][0]["custom_extra"] == "keep"
        assert after["users"][0]["contacts"][0]["message"] == "修改后的自选消息"


def test_contact_change_requires_rebinding_and_unknown_ids_are_rejected(tmp_path):
    with running_server(tmp_path, sample_config()) as (gui, _, server, _):
        contact = {"id": "contact-a", "name": "另一个人", "profile_url": "https://www.douyin.com/user/new"}
        status, _, _ = request(server, gui, "POST", "/api/users/account-a", {"contacts": [contact]})
        assert status == 200
        assert load_config(gui.config_path).users[0].contacts[0].identity == ""
        status, _, _ = request(server, gui, "POST", "/api/users/account-a", {"contacts": [{"id": "not-owned", "name": "某人"}]})
        assert status == 400


def test_busy_and_stale_configuration_cannot_overwrite_saved_data(tmp_path):
    with running_server(tmp_path, sample_config()) as (gui, manager, server, _):
        manager.running = True
        status, _, _ = request(server, gui, "POST", "/api/users/account-a", {"message": "wrong"})
        assert status == 409
        status, _, _ = request(server, gui, "POST", "/api/cancel", {})
        assert status == 200 and not manager.running
        status, _, _ = request(server, gui, "POST", "/api/users/account-a", {"revision": "stale", "message": "wrong"})
        assert status == 409
        assert load_config(gui.config_path).users[0].message == "默认 A"


def test_job_dispatch_and_uncertain_resolution_are_explicit(tmp_path):
    with running_server(tmp_path, sample_config()) as (gui, manager, server, _):
        status, _, _ = request(server, gui, "POST", "/api/jobs", {"kind": "bind", "user_id": "account-a", "payload": {"contact_id": "contact-a", "profile_url": "https://www.douyin.com/user/target-a"}})
        assert status == 200 and manager.started[0]["kind"] == "bind"
        assert manager.started[0]["user_id"] == "account-a"
        request(server, gui, "POST", "/api/cancel", {})
        manager.attempts = [{"id": "attempt-a", "user_id": "account-a", "status": "uncertain"}]
        status, _, _ = request(server, gui, "POST", "/api/attempts/attempt-a/resolve", {"resolution": "resend_now"})
        assert status == 400 and manager.resolved == []
        status, _, _ = request(server, gui, "POST", "/api/attempts/attempt-a/resolve", {"resolution": "retry_allowed"})
        assert status == 200 and manager.resolved == [("attempt-a", "retry_allowed")]
        assert len(manager.started) == 1  # Resolving never triggers another send.


def test_schedule_updates_do_not_replace_accounts(tmp_path):
    with running_server(tmp_path, sample_config()) as (gui, _, server, _):
        before = config_to_dict(load_config(gui.config_path))
        status, _, _ = request(server, gui, "POST", "/api/schedule", {"enabled": True, "time": "09:35"})
        assert status == 200
        after = config_to_dict(load_config(gui.config_path))
        assert after["users"] == before["users"]
        assert after["schedule"]["min_contact_interval_seconds"] == 90
        assert after["schedule"]["enabled"] and after["schedule"]["time"] == "09:35"


def test_gui_refuses_public_binding(tmp_path):
    with pytest.raises(ValueError, match="本机"):
        run_gui(tmp_path / "config.yaml", host="0.0.0.0", manager=StubManager())


def test_real_html_preserves_account_and_unsaved_changes(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    with running_server(tmp_path, sample_config()) as (gui, manager, server, _), playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(f"http://127.0.0.1:{server.server_port}")
        page.locator("#accountSelect").select_option("account-b")
        page.locator("#message").fill("保留我的编辑")
        page.evaluate("refresh()")
        assert page.locator("#accountSelect").input_value() == "account-b"
        assert page.locator("#message").input_value() == "保留我的编辑"
        assert page.locator("#run").is_disabled()
        page.locator("#save").click()
        playwright.expect(page.locator("#run")).to_be_enabled()
        saved = load_config(gui.config_path)
        assert saved.user("account-b").message == "保留我的编辑"
        assert saved.user("account-a").message == "默认 A"
        assert saved.schedule.min_contact_interval_seconds == 90
        assert page.locator("#accountSelect").input_value() == "account-b"
        page.locator("#check").click()
        playwright.expect(page.locator("#stop")).to_be_visible()
        assert manager.started[-1]["user_id"] == "account-b"
        assert page.locator("#save").is_disabled()
        page.locator("#stop").click()
        playwright.expect(page.locator("#run")).to_be_visible()
        playwright.expect(page.locator("#run")).to_be_enabled()
        manager.attempts = [{"id": "attempt-b", "user_id": "account-b", "contact": "同事", "message": "问候", "status": "uncertain", "date": "2026-10-10"}]
        page.evaluate("refresh()")
        page.get_by_text("我已核实：未发送", exact=True).wait_for()
        page.once("dialog", lambda dialog: dialog.accept())
        page.get_by_text("我已核实：未发送", exact=True).click()
        playwright.expect(page.get_by_text("允许再次手动执行", exact=True)).to_be_visible()
        assert manager.resolved == [("attempt-b", "retry_allowed")]
        assert len(manager.started) == 1
        assert errors == []
        browser.close()


def test_health_identifies_the_app_without_exposing_configuration(tmp_path):
    with running_server(tmp_path) as (gui, _, server, _):
        status, result, _ = request(server, gui, "GET", "/api/health")
        assert status == 200
        assert result["app"] == "DouyinFire" and result["version"] == __version__
        assert set(result) == {"app", "version", "pid", "config_id"}
        import hashlib
        assert result["config_id"] == hashlib.sha256(str(gui.config_path.resolve()).encode()).hexdigest()


def test_real_html_first_setup_and_contact_binding(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    with running_server(tmp_path) as (gui, manager, server, _), playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 390, "height": 844})
        page.goto(f"http://127.0.0.1:{server.server_port}")
        page.locator("#firstAccount").click()
        page.locator("#newAccountName").fill("日常账号")
        page.locator("#confirmAdd").click()
        playwright.expect(page.locator("#workspace")).to_be_visible()
        page.locator("#message").fill("你好")
        page.locator("#addContact").click()
        page.locator("[data-field=name]").fill("好友")
        page.locator('.profile-entry summary').click()
        page.locator("[data-field=profile_url]").fill("https://www.douyin.com/user/example")
        playwright.expect(page.locator("[data-bind]")).to_be_disabled()
        page.locator("#save").click()
        playwright.expect(page.locator("[data-bind]")).to_be_enabled()
        saved = load_config(gui.config_path)
        assert saved.users[0].contacts[0].id
        page.locator('.profile-entry summary').click()
        page.locator("[data-profile-bind]").click()
        playwright.expect(page.locator("#stop")).to_be_visible()
        assert manager.started[0]["kind"] == "bind"
        assert manager.started[0]["payload"]["contact_id"] == saved.users[0].contacts[0].id
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        browser.close()


def test_real_manager_can_initialize_empty_configuration(tmp_path):
    gui = GuiServer(tmp_path / "config" / "douyinfire.yaml", service_controller=lambda action: {"status": "test"})
    try:
        assert gui.state()["config"]["users"] == []
        assert gui.state()["manager"]["running"] is False
    finally:
        gui.manager.close()


def test_real_html_shows_migration_notes_and_failures_without_ledger(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    raw = sample_config()
    raw["migration_warnings"] = ["旧联系人需要重新绑定，自动执行已暂停。"]
    with running_server(tmp_path, raw) as (gui, manager, server, _), playwright.sync_playwright() as p:
        manager.user_id = "account-a"
        manager.result = {"user_id": "account-a", "results": [
            {"contact": "未绑定好友", "status": "failed", "reason": "请先核对并绑定此联系人。", "contact_id": "unbound"},
            {"contact": "已问候好友", "status": "confirmed", "skipped": True, "reason": "今天已确认发送，跳过重复任务", "attempt_id": "prior"}]}
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f"http://127.0.0.1:{server.server_port}")
        playwright.expect(page.locator("#migrationWarnings")).to_contain_text("旧联系人需要重新绑定")
        playwright.expect(page.locator("#latestResult")).to_contain_text("未发送 1")
        playwright.expect(page.locator("#latestResult")).to_contain_text("已跳过 1")
        playwright.expect(page.locator("#latestResult")).to_contain_text("请先核对并绑定此联系人")
        assert manager.history("account-a") == []
        browser.close()


@contextmanager
def real_manager_server(tmp_path, process_context=None):
    from douyinfire.manager import TaskManager
    path = tmp_path / "config" / "real.yaml"
    save_config(parse_config(sample_config(), base_dir=tmp_path), path)
    manager = TaskManager(path, schedule=False, process_context=process_context)
    gui = GuiServer(path, manager, lambda action: {"status": "test"})
    server = ThreadingHTTPServer(("127.0.0.1", 0), gui.handler())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield gui, manager, server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        manager.close()


@pytest.mark.parametrize("resolution,expected_status", [("confirmed_sent", "confirmed"), ("retry_allowed", "failed")])
def test_real_manager_http_resolution_updates_ledger_without_sending(tmp_path, resolution, expected_status):
    with real_manager_server(tmp_path) as (gui, manager, server):
        config = load_config(gui.config_path)
        user = config.user("account-a")
        record, owned = manager.ledger.claim(user, user.contacts[0], "2026-10-10", "特定问候")
        assert owned
        manager.ledger.transition(record["id"], "dispatching")
        manager.ledger.transition(record["id"], "uncertain", "测试用不确定结果")
        status, history, _ = request(server, gui, "GET", "/api/history?user_id=account-a")
        assert status == 200 and history["attempts"][0]["status"] == "uncertain"
        status, result, _ = request(server, gui, "POST", f'/api/attempts/{record["id"]}/resolve', {"resolution": resolution})
        assert status == 200 and result["attempt"]["status"] == expected_status
        assert manager._active is None
        assert manager.history("account-a")[0]["reason"].startswith("用户已核实")
        assert bool(manager.history("account-a")[0]["manual_only"]) == (resolution == "retry_allowed")


def recording_context():
    import queue
    class FakeQueue(queue.Queue):
        def close(self):
            pass

    class FakeProcess:
        def __init__(self, context, args):
            self.context, self.args = context, args

        def start(self):
            self.context.started.set()

        def is_alive(self):
            return False

        def join(self, timeout=None):
            pass

    class FakeContext:
        def __init__(self):
            self.started = threading.Event()
            self.process = None

        def Event(self):
            return threading.Event()

        def Queue(self):
            return FakeQueue()

        def Process(self, target, args):
            self.process = FakeProcess(self, args)
            return self.process

    return FakeContext()


def test_real_manager_cannot_start_between_config_busy_check_and_atomic_save(tmp_path, monkeypatch):
    import queue
    import douyinfire.gui as gui_module

    context = recording_context()
    entered_save, release_save, start_attempted = threading.Event(), threading.Event(), threading.Event()
    original_save = gui_module.save_config

    def held_save(config, path):
        entered_save.set()
        assert release_save.wait(5)
        return original_save(config, path)

    with real_manager_server(tmp_path, process_context=context) as (gui, manager, server):
        monkeypatch.setattr(gui_module, "save_config", held_save)
        response, failures = [], []

        def update():
            response.append(request(server, gui, "POST", "/api/users/account-a", {"message": "已完整保存的新消息"}))

        def start():
            start_attempted.set()
            try:
                manager.start("run", user_id="account-a")
            except Exception as exc:
                failures.append(exc)

        writer = threading.Thread(target=update)
        writer.start()
        assert entered_save.wait(5)
        runner = threading.Thread(target=start)
        runner.start()
        assert start_attempted.wait(5)
        assert not context.started.wait(.1)
        release_save.set()
        writer.join(5)
        runner.join(5)
        assert response[0][0] == 200 and failures == []
        assert context.started.is_set()
        assert context.process.args[0].user("account-a").message == "已完整保存的新消息"


def test_real_manager_retry_includes_failures_before_ledger_and_keeps_contact_message(tmp_path):
    context = recording_context()
    with real_manager_server(tmp_path, process_context=context) as (gui, manager, server):
        with manager.operation_lock:
            manager._last["result"] = {"user_id": "account-a", "results": [{"contact_id": "contact-a", "status": "failed", "reason": "页面尚未就绪", "recipient": "target-a", "kind": "direct"}]}
        assert manager.history("account-a") == []
        status, _, _ = request(server, gui, "POST", "/api/jobs", {"kind": "retry", "user_id": "account-a"})
        assert status == 200
        assert context.process.args[3] == ["contact-a"]
        assert context.process.args[0].user("account-a").contacts[0].message == "特定问候"


def test_real_manager_retry_can_use_an_older_manually_verified_attempt(tmp_path):
    context = recording_context()
    with real_manager_server(tmp_path, process_context=context) as (gui, manager, server):
        user = load_config(gui.config_path).user("account-a")
        contact = user.contacts[0]
        record, _ = manager.ledger.claim(user, contact, "2000-01-01", "特定问候")
        manager.ledger.transition(record["id"], "dispatching")
        manager.ledger.transition(record["id"], "uncertain")
        status, _, _ = request(server, gui, "POST", f'/api/attempts/{record["id"]}/resolve', {"resolution": "retry_allowed"})
        assert status == 200 and manager._active is None
        held, owned = manager.ledger.claim(user, contact, "2099-01-01", "特定问候", source="scheduled")
        assert not owned and held["manual_only"]
        status, result, _ = request(server, gui, "POST", "/api/jobs", {"kind": "retry", "user_id": "account-a"})
        assert status == 200, result
        assert context.process.args[3] == ["contact-a"]


def group_config():
    raw = sample_config()
    raw['users'][0]['contacts'].append({'id': 'group-a', 'name': '待选择的群', 'kind': 'group'})
    return raw


def test_switching_contact_kind_clears_binding_without_overwriting_other_contacts(tmp_path):
    with running_server(tmp_path, group_config()) as (gui, _, server, _):
        before = config_to_dict(load_config(gui.config_path))
        contacts = before['users'][0]['contacts']
        contacts[0].update(kind='group', identity='forged-group')
        status, result, _ = request(server, gui, 'POST', '/api/users/account-a', {'contacts': contacts})
        assert status == 200, result
        saved = load_config(gui.config_path)
        changed = saved.user('account-a').contacts[0]
        assert changed.id == 'contact-a' and changed.kind == 'group'
        assert changed.identity == '' and changed.profile_url == ''
        assert saved.user('account-a').contacts[1].id == 'group-a'
        assert config_to_dict(saved)['users'][1] == before['users'][1]
        contacts = config_to_dict(saved)['users'][0]['contacts']
        contacts[0].update(kind='direct', profile_url='https://www.douyin.com/user/new-target')
        status, result, _ = request(server, gui, 'POST', '/api/users/account-a', {'contacts': contacts})
        assert status == 200, result
        restored = load_config(gui.config_path).user('account-a').contacts[0]
        assert restored.identity == '' and restored.profile_url == 'https://www.douyin.com/user/new-target'


def complete_group_scan(manager, *, job_id=None, user_id='account-a', contact_id='group-a'):
    from douyinfire.identities import group_identity
    manager.running = False
    manager.result = {'job_id': job_id or manager.job_id, 'user_id': user_id, 'contact_id': contact_id,
                      'kind': 'group', 'conversations': [
                          {'kind': 'group', 'identity': group_identity('synthetic-group-1'), 'name': '同名群', 'profile_url': ''},
                          {'kind': 'group', 'identity': group_identity('synthetic-group-2'), 'name': '同名群', 'profile_url': ''}]}


def test_real_html_group_picker_requires_save_and_dispatches_selected_stable_identity(tmp_path):
    from douyinfire.identities import group_identity
    playwright = pytest.importorskip('playwright.sync_api')
    with running_server(tmp_path, sample_config()) as (gui, manager, server, _), playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 390, 'height': 844})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(f'http://127.0.0.1:{server.server_port}')
        row = page.locator('[data-contact-id="contact-a"]')
        row.locator('[data-field=kind]').select_option('group')
        assert row.locator('[data-field=profile_url]').is_hidden()
        assert row.locator('[data-bind]').is_disabled()
        page.locator('#save').click()
        playwright.expect(row.locator('[data-bind]')).to_be_enabled()
        assert load_config(gui.config_path).user('account-a').contacts[0].identity == ''
        with page.expect_response(lambda response: response.url.endswith('/api/jobs')) as dispatched:
            row.locator('[data-bind]').click()
        assert dispatched.value.status == 200
        playwright.expect(page.locator('#groupDialog')).to_be_visible()
        assert manager.started[-1]['kind'] == 'list_conversations'
        assert manager.started[-1]['payload'] == {'kind': 'group', 'contact_id': 'contact-a'}
        complete_group_scan(manager, contact_id='contact-a')
        page.evaluate('refresh()')
        playwright.expect(page.locator('.group-choice')).to_have_count(2)
        assert page.locator('#confirmGroup').is_disabled()
        page.locator('.group-choice input').nth(1).check()
        page.locator('#confirmGroup').click()
        playwright.expect(page.locator('#groupDialog')).not_to_be_visible()
        assert manager.started[-1] == {'kind': 'bind', 'user_id': 'account-a', 'contact_ids': None,
                                      'payload': {'contact_id': 'contact-a', 'identity': group_identity('synthetic-group-2')}}
        assert all(job['kind'] not in {'run', 'retry'} for job in manager.started)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert errors == []
        browser.close()


@pytest.mark.parametrize('mismatch', ['job', 'account', 'contact'])
def test_group_picker_ignores_mismatched_results_and_account_switch(tmp_path, mismatch):
    playwright = pytest.importorskip('playwright.sync_api')
    with running_server(tmp_path, group_config()) as (_, manager, server, _), playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f'http://127.0.0.1:{server.server_port}')
        with page.expect_response(lambda response: response.url.endswith('/api/jobs')) as dispatched:
            page.locator('[data-contact-id="group-a"] [data-bind]').click()
        assert dispatched.value.status == 200
        complete_group_scan(manager, job_id='old-job' if mismatch == 'job' else None,
                            user_id='account-b' if mismatch == 'account' else 'account-a',
                            contact_id='contact-a' if mismatch == 'contact' else 'group-a')
        page.evaluate('refresh()')
        assert page.locator('.group-choice').count() == 0
        assert page.locator('#confirmGroup').is_disabled()
        with page.expect_response(lambda response: response.url.endswith('/api/jobs')) as dispatched:
            page.locator('#refreshGroups').click()
        assert dispatched.value.status == 200
        assert manager.started[-1]['kind'] == 'list_conversations'
        complete_group_scan(manager)
        page.evaluate('refresh()')
        playwright.expect(page.locator('.group-choice')).to_have_count(2)
        manager.job_id = 'newer-unrelated-job'
        page.evaluate('refresh()')
        assert page.locator('.group-choice').count() == 0
        assert page.locator('#confirmGroup').is_disabled()
        page.evaluate("choose('account-b')")
        assert not page.locator('#groupDialog').is_visible()
        assert page.locator('#accountSelect').input_value() == 'account-b'
        assert all(job['kind'] == 'list_conversations' for job in manager.started)
        browser.close()


def test_direct_picker_preserves_hidden_profile_and_binds_selected_identity(tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    with running_server(tmp_path, sample_config()) as (gui, manager, server, _), playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 390, 'height': 844})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(f'http://127.0.0.1:{server.server_port}')
        row = page.locator('[data-contact-id="contact-a"]')
        assert row.locator('[data-field=profile_url]').is_hidden()
        assert row.locator('[data-bind]').inner_text() == '选择联系人'
        page.locator('#message').fill('仅修改默认问候')
        page.locator('#save').click()
        playwright.expect(row.locator('[data-bind]')).to_be_enabled()
        contact = load_config(gui.config_path).user('account-a').contacts[0]
        assert contact.identity == 'target-a'
        assert contact.profile_url == 'https://www.douyin.com/user/target-a'
        with page.expect_response(lambda response: response.url.endswith('/api/jobs')) as dispatched:
            row.locator('[data-bind]').click()
        assert dispatched.value.status == 200
        assert manager.started[-1]['payload'] == {'kind': 'direct', 'contact_id': 'contact-a'}
        assert page.locator('#conversationTitle').inner_text() == '选择联系人'
        manager.running = False
        # A group result must never populate a direct-conversation picker.
        complete_group_scan(manager, contact_id='contact-a')
        page.evaluate('refresh()')
        assert page.locator('.group-choice').count() == 0
        assert page.locator('#confirmGroup').is_disabled()
        manager.result = {'kind': 'direct', 'job_id': manager.job_id, 'user_id': 'account-a',
                          'contact_id': 'contact-a', 'conversations': [
                              {'kind': 'direct', 'identity': identity, 'name': '同名好友',
                               'profile_url': 'https://www.douyin.com/user/'+identity}
                              for identity in ['friend-a', 'friend-b']]}
        page.evaluate('refresh()')
        playwright.expect(page.locator('.group-choice')).to_have_count(2)
        assert page.locator('.group-choice input:checked').count() == 0
        assert page.locator('#confirmGroup').is_disabled()
        assert page.locator('.group-choice').nth(0).inner_text() != page.locator('.group-choice').nth(1).inner_text()
        page.locator('.group-choice input').nth(1).check()
        with page.expect_response(lambda response: response.url.endswith('/api/jobs')) as bound:
            page.locator('#confirmGroup').click()
        assert bound.value.status == 200
        assert manager.started[-1] == {'kind': 'bind', 'user_id': 'account-a', 'contact_ids': None,
                                      'payload': {'contact_id': 'contact-a', 'identity': 'friend-b'}}
        assert all(job['kind'] in {'list_conversations', 'bind'} for job in manager.started)
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert errors == []
        browser.close()


def test_new_direct_contact_can_be_saved_and_selected_without_typing_a_name(tmp_path):
    playwright = pytest.importorskip('playwright.sync_api')
    with running_server(tmp_path, sample_config()) as (gui, manager, server, _), playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(f'http://127.0.0.1:{server.server_port}')
        page.locator('#addContact').click()
        row = page.locator('[data-contact-id=""]').first
        assert row.locator('[data-field=name]').input_value() == '待选择的联系人'
        row.locator('[data-field=kind]').select_option('group')
        assert row.locator('[data-field=name]').input_value() == '待选择的群聊'
        row.locator('[data-field=kind]').select_option('direct')
        assert row.locator('[data-field=name]').input_value() == '待选择的联系人'
        # A separate typed label must survive both type changes.
        page.locator('#addContact').click()
        custom = page.locator('[data-contact-id=""]').nth(1)
        custom.locator('[data-field=name]').fill('   ')
        custom.locator('[data-field=kind]').select_option('group')
        assert custom.locator('[data-field=name]').input_value() == '待选择的群聊'
        custom.locator('[data-field=name]').fill('')
        custom.locator('[data-field=kind]').select_option('direct')
        assert custom.locator('[data-field=name]').input_value() == '待选择的联系人'
        custom.locator('[data-field=name]').fill('用户自己的称呼')
        custom.locator('[data-field=kind]').select_option('group')
        custom.locator('[data-field=kind]').select_option('direct')
        assert custom.locator('[data-field=name]').input_value() == '用户自己的称呼'
        custom.locator('[data-remove]').click()
        page.locator('#save').click()
        playwright.expect(page.locator('[data-contact-id=""]')).to_have_count(0)
        contacts = load_config(gui.config_path).user('account-a').contacts
        assert contacts[0].name == '好友' and contacts[0].identity == 'target-a'
        assert contacts[1].name == '待选择的联系人' and contacts[1].kind == 'direct'
        contact_id = contacts[1].id
        row = page.locator(f'[data-contact-id="{contact_id}"]')
        with page.expect_response(lambda response: response.url.endswith('/api/jobs')) as dispatched:
            row.locator('[data-bind]').click()
        assert dispatched.value.status == 200
        assert manager.started[-1]['payload'] == {'kind': 'direct', 'contact_id': contact_id}
        manager.running = False
        manager.result = {'kind': 'direct', 'job_id': manager.job_id, 'user_id': 'account-a',
                          'contact_id': contact_id, 'conversations': [
                              {'kind': 'direct', 'identity': 'selected-friend', 'name': '列表里的好友',
                               'profile_url': 'https://www.douyin.com/user/selected-friend'}]}
        page.evaluate('refresh()')
        playwright.expect(page.locator('.group-choice')).to_have_count(1)
        assert page.locator('.group-choice input:checked').count() == 0
        page.locator('.group-choice input').check()
        with page.expect_response(lambda response: response.url.endswith('/api/jobs')) as bound:
            page.locator('#confirmGroup').click()
        assert bound.value.status == 200
        assert manager.started[-1]['payload'] == {'contact_id': contact_id, 'identity': 'selected-friend'}
        assert all(job['kind'] in {'list_conversations', 'bind'} for job in manager.started)
        browser.close()
