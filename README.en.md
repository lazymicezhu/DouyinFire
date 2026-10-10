[简体中文](README.md) | [English](README.en.md)

> **A note from the author**
>
> Thank you all for your support! I started this project a year ago, when I was first learning Python and wanted to try building something that worked with a browser.
>
> I happened to need a way to keep my Douyin streaks going, so I built this project. I remember using very basic element locators back then. I never expected it to get so many stars—at least, it feels like a lot to me.
>
> An AI agent might now handle a request like this from a single sentence, but I still do not think everything needs to be connected to AI.
>
> I have rambled on a bit. I do not have a formal background in this field, and I often use AI to bring my ideas to life. I have done my best with this update, but there are still plenty of issues. Thank you for your understanding.

# DouyinFire

A local Mac app for maintaining Douyin chat streaks. Double-click the app and manage everyday settings in a local web page. It uses Playwright to interact with Douyin web elements, without pixel coordinates or large language models.

## Opening the app and initial setup

1. After completing the local build described below, double-click `~/Applications/DouyinFire.app`. The app opens its local control page automatically; no terminal is needed.
2. Add an account and click **Scan to log in (扫码登录)**. Complete the QR-code login or platform verification in the Douyin window that appears. The app saves a separate login session and automatically checks whether headless operation is available.
3. Enter a default message, add a contact, and choose **Direct chat (私聊)** or **Group chat (群聊)**. Save, then click **Select contact (选择联系人)** or **Select group (选择群聊)** to choose a conversation from the retrieved list and confirm its binding. There is no need to copy a profile URL. Check that the displayed person or group name is correct. You can set a message for each recipient; leave it blank to use the default. Direct chats also retain a collapsible profile-URL option.
4. First use **Run now (立即执行)** with one bound contact and check the result in Douyin. Once you have confirmed that it works correctly, enable **Run automatically every day (每天自动执行)**.
5. To keep the app running after restarting your computer, open **Settings and run details (设置与运行详情)** and click **Enable background operation after login (开启登录后后台运行)**.

Daily tasks are disabled by default. The first login, an expired login, or a platform CAPTCHA requires manual interaction. Normal checks, binding, and sending use headless Chromium. Closing the control page does not stop the background process. **Quit background service (退出后台)** disables daily tasks and automatic recovery after login.

The current build targets Apple Silicon Macs and bundles Python, Playwright, and a matching version of Chromium. No separate runtime installation is required. It is signed locally and has not been notarized by Apple or published online.

## Sending and results

- Each account uses a separate browser profile directory, and its actual account identifier is verified. Direct chats are bound to a stable Douyin profile identifier. Groups are bound to a platform conversation identifier, and their identity is checked again after opening the group. Neither nicknames nor group names need to be unique.
- The direct-chat and group-chat lists include only conversations currently loaded on the Douyin web page. If the target is missing, open that conversation on Douyin's website using the relevant account, then refresh the list. Direct chats can also use the profile-URL option. Reading lists and verifying bindings do not send messages. Switching between direct and group chat requires a new binding. Existing direct-chat bindings and send records are preserved.
- Manual runs from the GUI and daily tasks use the same sending workflow. Once a send from an account to a recipient has been confirmed for the day, another run that day skips that recipient.
- A result is shown as **Sent (已发送)** only after a verifiable message result is obtained. Clicking a button or entering text alone does not count as successful delivery.
- If the send action may have occurred but its result cannot be confirmed, the attempt is recorded as **Needs verification (待核实)** and further sending stops. Restarting the app or reaching a new day does not automatically resend it.
- For an attempt that needs verification, check Douyin first, then choose **I have verified: sent (我已核实：已发送)** or **I have verified: not sent (我已核实：未发送)**. Choosing “not sent” only releases the hold; you must still start a manual run. It does not send immediately or let the scheduler retry it.
- **Stop task (停止任务)** cancels waiting and closes the worker browser. An attempt that has already entered the sending stage may still be marked as needing verification.

Daily tasks use the configured time zone, which defaults to Hong Kong time. The randomized time offset is fixed for each day and is not drawn again after a restart. Messages cannot be sent while the computer is asleep. After waking, the app runs at most once within that day's catch-up window, which defaults to 180 minutes. It does not make up for tasks missed on previous days.

If Douyin's page changes, the login expires, or the recipient cannot be verified, the app shows the reason and stops the affected operation. It does not bypass CAPTCHAs or guess the intended recipient.

## How to verify the app

The following acceptance checks are recommended, in order. No terminal is needed:

1. **Startup:** Double-click the app. The control page should open, and double-clicking again should reuse the same background process.
2. **Headless connection:** After logging in, click **Check connection (检查连接)**. The page should show **Connection normal (连接正常)** without opening a browser window.
3. **Contacts:** Choose **Direct chat (私聊)** or **Group chat (群聊)** and save. Select a recipient from the corresponding list and verify the binding. Distinguish recipients with the same name by the identifiers shown in the list. The profile-URL option remains available for direct chats. An incorrect URL, the logged-in account itself, or an unverifiable recipient should produce a failure. A personal profile cannot be used in place of a group.
4. **Single-message send:** Enable only one test contact, set an explicit test message, and run the task. Check both the content and recipient in Douyin, and compare them with the result shown on the control page.
5. **Duplicate prevention:** Run the same contact again that day. The result should be **Skipped (跳过)**, and there should be no second message in Douyin. Changing the nickname or message must not bypass daily deduplication.
6. **Stopping and recovery:** Stop a task while it is running. If an attempt needs verification, inspect it manually before resolving it; it must not be retried automatically without that check.
7. **Background operation:** In settings, temporarily set the time offset to 0, choose an execution time a little later, enable **Run automatically every day (每天自动执行)**, and save. Then enable background operation after login and close the web page. Reopen the app after the scheduled time to inspect the result. If the recipient has already been sent a message that day, the new run should show **Skipped (跳过)**. You can disable daily tasks when the test is finished.

The source code's automated tests use synthetic accounts, simulated pages, and isolated profile directories. They do not send messages to real contacts. Real-platform delivery still requires step 4 above; simulated tests cannot replace that check.

### Direct-chat list update acceptance record (2026-10-10, 0.2.2)

- **All 407 automated tests passed**, including 51 synthetic-page tests running in real Chromium. New coverage includes direct-chat candidate filtering, selecting recipients with the same name, identity checks before and after clicks, separation of direct and group chat types, compatibility with the existing profile-URL option, and saving hidden profile fields.
- Both the source version and the locally installed app retrieved direct-chat lists headlessly using a real account and successfully verified an existing contact selected from the list. The stable identity, automatically generated profile URL, existing group settings, and send records remained consistent. No test messages were sent.
- Desktop and narrow-screen layouts were checked. Adding a recipient does not require entering a name first; the name is filled in after selection. Direct chats use **Select contact (选择联系人)** by default, with the previous URL method under **Use profile URL (使用主页链接)**. Existing bindings do not need to be set up again.

### Group-chat update acceptance record (2026-10-10, 0.2.1)

- **All 364 automated tests passed**, including 45 synthetic-page tests running in real Chromium. New coverage includes group identity checks, group message ownership and receipt checks, switching between direct and group chats, the group-selection dialog, and preventing an old failed attempt from being retried against a new recipient after a binding changes.
- The 16 program modules in the app bundle matched the final source. Signature verification, the bundled browser check, and the separate worker-process self-test passed. The update preserved existing accounts, message settings, and the send ledger.
- Checks on the real Douyin website passed for headless group-list retrieval, opening the target group, and cross-checking group identity and message ownership. Existing direct chats were still recognized. The final installed app successfully completed group selection and binding through the web interface, and the saved group identifier matched the actual page. These group checks only read and bound conversations; **no group messages were sent**. Real group delivery still requires the single-message acceptance check above.
- After switching chat type or rebinding, start a new task with **Run now (立即执行)**. **Retry items confirmed not sent (重试明确未发送项)** only retries failed records whose original recipient has not changed.

### Local acceptance record (2026-10-10, 0.2.0)

- **All 251 automated tests passed**, including 38 synthetic-page tests running in real Chromium. They cover identity checks, send receipts, deduplication, cancellation, recovery, web APIs, and configuration saving.
- The final installed `.app` passed signature verification, the bundled browser check, and the separate worker-process self-test. Its 15 program modules matched the final source. Double-click startup and reusing the background process when reopening both passed.
- Headless connection checking, contact binding, text entry, and draft cleanup passed with a real account. With explicit user authorization, the final installed app sent one test message to one contact and obtained a platform confirmation receipt. The browser was then restarted for a read-only check, which confirmed that the same message was still present, its content matched, and there was only one copy.
- An empty account and a separate configuration were used to verify macOS background startup and scheduled recovery after process exit. The test registration was removed afterward. Daily tasks remained disabled for the real account, and background operation after login was not enabled.

These real-page checks fixed issues where a button appeared before its click handler was ready, invisible placeholder characters appeared at the end of the editor, and an ordinary clear operation could not remove a draft from a controlled editor. Real account identifiers, chat pages, and test records are not stored in the source repository.

## Data and migration from older versions

By default, application data is stored locally:

```text
~/Library/Application Support/DouyinFire/
  config/douyinfire.yaml     Configuration
  data/profiles/<accountID>/ Separate browser login sessions
  data/tasks.sqlite3        Task state and deduplication records
  data/runs/                Records for each run
  logs/                     Background logs
  screenshots/              Diagnostics directory
  backups/                  Backups from legacy migration
```

On the first launch from an older project, the app backs up and migrates the configuration and usable browser profiles. Old run records remain in the original project, and the original project data is preserved. After migration, scheduled tasks are disabled by default and accounts and contacts must be verified again. Legacy “success” logs lack reliable evidence of sending and are not treated as confirmed sends in the new ledger.

Do not sync, replace, or copy browser profile directories while the browser is running. Do not upload login sessions, real configuration files, chat records, or task databases to the repository. The current data directory is in local Application Support so that iCloud project syncing does not interfere with browser locks.

Before moving the app, click **Quit background service (退出后台)** on the web page. After moving and reopening it from the new location, click **Enable background operation after login (开启登录后后台运行)** again so the system uses the new app path.

## Development and local builds

The following commands are for developers. For everyday use, double-click the app.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
python -m pip install -r scripts/requirements-build.txt
python -m pip install pytest
python -m pytest -q
python scripts/build_macos.py --output "$HOME/Applications"
```

Before building an update, choose **Quit background service (退出后台)** on the web page. The builder targets the current Mac's architecture, bundles Chromium and the runtime into `DouyinFire.app` in the specified directory, and verifies its local signature. The build runs locally without uploading code or artifacts. The packaged app does not depend on the source directory, the build virtual environment, or the user's browser cache.

Build the app directly into a local directory that is not synced. iCloud can asynchronously add attributes such as FinderInfo to app bundles, potentially causing signature verification to fail for a copy in the project's `dist` directory. Use the local output path above, and do not enable the background service from a copy of the app stored in iCloud.

In source mode, `python -m douyinfire.desktop` opens the same control page. The maintenance command `python -m douyinfire.cli doctor` actually starts a blank Chromium instance to check the runtime. It does not visit Douyin or send messages.

## Main modules

| Module | Responsibility |
| --- | --- |
| `desktop.py` / `gui.py` / `gui_html.py` | Double-click startup, a local-only web page, and operation APIs |
| `manager.py` | Separate worker processes, progress, cancellation, and recovery after process exit |
| `browser.py` | Headless browser operation, element location, and verification of accounts, contacts, and messages |
| `tasks.py` / `ledger.py` | Shared execution workflow, persistent ledger, deduplication, and handling uncertain outcomes |
| `scheduler.py` / `service.py` | Time zones, daily scheduling, and recovery after macOS login |
| `config.py` / `identities.py` / `runtime.py` | Atomic configuration saving, direct and group identities, local data directories, and migration |

The local control page binds only to `127.0.0.1`. Write operations validate their origin and a random request token. Configuration updates preserve unrelated fields so that editing through the web page does not overwrite other accounts.
