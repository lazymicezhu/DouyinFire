# DouyinFire

[简体中文](README.md) | [English](README.en.md)

A local Mac app for maintaining Douyin chat streaks. Select direct-chat contacts or groups, set messages, and schedule daily runs through a web page. Everyday use does not require a terminal.

- **Select from a list:** Choose both direct chats and groups from conversation lists, with automatic verification of bindings.
- **Headless operation:** A browser opens for the first login; normal operation uses headless Chromium.
- **Multiple accounts and duplicate prevention:** Login sessions are stored separately for each account. Recipients with a confirmed send that day are automatically skipped.

Uses Playwright to interact with web elements, without pixel coordinates or large language models.

## Installation

The app currently needs to be built from source on macOS. The steps below use **Python 3.13** and have been verified on Apple Silicon Macs. The generated app includes Python and Chromium, so everyday use requires no separate runtime installation.

Run these commands once in a terminal:

```bash
git clone https://github.com/lazymicezhu/DouyinFire.git
cd DouyinFire
python3.13 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt -r scripts/requirements-build.txt
python -m playwright install chromium
python scripts/build_macos.py --output "$HOME/Applications"
```

Then double-click `~/Applications/DouyinFire.app` to open the local control page. The app is signed locally and has not been notarized by Apple. Keep it in a local directory that is not synced, so services such as iCloud do not interfere with its signature or operation.

## First use

1. **Log in:** Add an account, click **Scan to log in (扫码登录)**, and complete the QR-code login or platform verification in the browser.
2. **Set a message:** Enter a default message. Each contact can also have a separate message; leave it blank to use the default.
3. **Select recipients:** Click **Add contact (添加联系人)**, choose **Direct chat (私聊)** or **Group chat (群聊)**, and click **Save changes (保存修改)**. Then click **Select contact (选择联系人)** or **Select group (选择群聊)**, choose the target, and click **Verify and bind (核实并绑定)**. The name is filled in automatically; you do not need to enter it beforehand or copy a profile URL.
4. **Try one send:** Enable only one bound recipient, save, and click **Run now (立即执行)**. Check the recipient and message in Douyin before adding more recipients or enabling daily tasks.

Reading lists and binding recipients do not send messages. For recipients with the same name, check the identifiers shown in the list. Switching between direct and group chat requires a new binding.

## Daily automatic runs

Set a time under **Run automatically every day (每天自动执行)**, check **Enable (开启)**, and click **Save time (保存时间)**. Automatic runs are disabled by default, and the default time zone is Hong Kong time. Adjust the time offset and sending interval in **Settings and run details (设置与运行详情)**.

Daily tasks process checked contacts across all enabled accounts. **Run now (立即执行)** processes only the currently selected account.

- **Closing the web page:** The background process keeps running.
- **Resuming after a restart:** In **Settings and run details (设置与运行详情)**, click **Enable background operation after login (开启登录后后台运行)** to resume after logging in to macOS.
- **Computer sleep:** Tasks cannot run while the computer is asleep. After waking, they run only within that day's catch-up window, which defaults to 180 minutes. Missed tasks from previous days are not sent.
- **Quit background service (退出后台):** Stops the background process and disables both daily tasks and recovery after login. Enable them again when you want to resume automatic operation.

## Checking send results

| Status | Meaning and action |
| --- | --- |
| Sent (已发送) | A verifiable send result has been obtained from the platform. |
| Skipped (已跳过) | For example, this account has already sent to the same recipient today, so another send is unnecessary. |
| Not sent (未发送) | Read the explanation and address the cause, then retry tasks confirmed not sent. |
| Needs verification (待核实) | The send may already have occurred. The account stops further sending in this run, and retries for this recipient are paused. Check Douyin first. |

For an attempt that needs verification, check the result and choose **I have verified: sent (我已核实：已发送)** or **I have verified: not sent (我已核实：未发送)**. Choosing “not sent” only releases the hold; you must still start a manual run. Restarting the app or reaching a new day does not automatically resend it.

Changing a nickname or message does not bypass same-day deduplication. After rebinding a recipient, use **Run now (立即执行)** to start a new task; old failed attempts will not be sent to the new recipient. You can click **Stop task (停止任务)** during a run. Attempts that have already entered the sending stage may still need verification.

## Frequently asked questions

**Why is a contact or group missing from the list?**

The list includes only conversations currently loaded on the Douyin web page, not the full friends list. Open the target conversation on Douyin's website using the relevant account, then click **Refresh list (重新读取)**. For direct chats, you can also expand **Use profile URL (使用主页链接)** to bind a contact. Groups do not need a personal profile URL.

**What if the login expires or a CAPTCHA appears?**

Click **Scan to log in (扫码登录)** to complete platform verification, then use **Check connection (检查连接)** to confirm the status. The app does not bypass CAPTCHAs. If Douyin's page changes or the recipient cannot be verified, it stops the operation and explains why.

**Where are accounts and message records stored?**

The default location is `~/Library/Application Support/DouyinFire/`. Configuration, login sessions, and send records are stored locally. Do not upload this directory to the repository, or sync or replace it while the browser is running. When the launcher finds an older project, it backs up and migrates usable data, then requires accounts and contacts to be verified again. Legacy success logs are not treated as confirmed send records.

**How do I update or move the app?**

First click **Quit background service (退出后台)** on the control page, then rebuild or move the app. After reopening it, enable daily tasks and background operation after login again if you want automatic runs.

## Development

In the project's virtual environment with dependencies installed, you can run the source or tests directly:

```bash
python -m douyinfire.desktop
python -m pytest -q
python -m douyinfire.cli doctor
```

Automated tests use synthetic data and do not send messages to real contacts. `doctor` only checks the browser runtime; it does not log in to Douyin or send messages. See [config/douyinfire.example.yaml](config/douyinfire.example.yaml) for an example configuration.

<details>
<summary>A note from the author</summary>

> **A note from the author**
>
> Thank you all for your support! I started this project a year ago, when I was first learning Python and wanted to try building something that worked with a browser.
>
> I happened to need a way to keep my Douyin streaks going, so I built this project. I remember using very basic element locators back then. I never expected it to get so many stars—at least, it feels like a lot to me.
>
> An AI agent might now handle a request like this from a single sentence, but I still do not think everything needs to be connected to AI.
>
> I have rambled on a bit. I do not have a formal background in this field, and I often use AI to bring my ideas to life. I have done my best with this update, but there are still plenty of issues. Thank you for your understanding.

</details>
