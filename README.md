> **写在最前面**
>
> 感谢大家的支持呀，这个项目是我一年前最开始接触Python，想试试能不能做一些浏览器相关的项目
>
> 当时正好有抖音续火花的需求就把这个项目做出来了，记得当初用的最传统的定位方式，没想到能获得这么多star（对我来说真挺多的了）
>
> 现在AI agent对于处理这样的需求可能也就是一句话，但是我就是觉得没有必要所有的东西都要和AI接上
>
> 絮絮叨叨说了很多，我不是科班出身，更多也是使用AI实现我的想法，这次优化尽力而为，还有很多问题，希望大家谅解



# DouyinFire
DouyinFire 是一个 Mac 本地运行的抖音续火花辅助工具。当前版本使用 Playwright 保存浏览器登录态，并提供本地图形界面来编辑配置、登录、手动运行和查看日志。

> 本项目只面向个人账号的低频自用，不提供验证码绕过、风控绕过或批量营销能力。

## 当前运行方式

默认不启用定时自动化。使用本地 GUI 调试和手动触发：

```bash
cd DouyinFire
douyinfire gui
```

打开：

```text
http://127.0.0.1:8765
```

GUI 支持：

- 查看配置、服务状态和当前任务状态
- 使用中文图形化表单编辑并保存 `config/douyinfire.yaml`
- 打开登录窗口并保存 Playwright 登录态
- 手动运行单个用户或全部用户
- 运行后自动切换到日志页，显示步骤进度条和 `logs/douyinfire.log`
- 取消 launchd 自动化服务

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
pip install -e .
```

## 配置

真实配置文件是 `config/douyinfire.yaml`，不会提交到 Git。示例：

```yaml
data_dir: data
log_dir: logs
screenshot_dir: screenshots
headless: false
failure_notify_threshold: 1

schedule:
  time: "00:05"
  jitter_minutes: 20
  min_contact_interval_seconds: 10

timeouts:
  home_ready_seconds: 5
  message_panel_seconds: 8
  contact_search_seconds: 15
  input_box_seconds: 10
  after_send_seconds: 2

browser:
  backend: cloakbrowser
  run_headless: true
  login_headless: false
  storage_state_path: data/states/main.json

users:
  - name: "main"
    enabled: true
    contacts:
      - name: "联系人备注名"
        profile_url: "https://www.douyin.com/user/..."
    message: "续火花咯"
```

联系人推荐填写抖音主页链接或分享链接。当前流程会优先打开联系人主页并点击主页里的“私信/消息/聊天”入口；如果主页入口不可用，再回退到最近会话昵称匹配。旧的纯昵称联系人仍可读取，但只适合作为最近会话兜底。

如果抖音页面加载慢，优先调大 `timeouts.contact_search_seconds` 和 `timeouts.message_panel_seconds`。

## 浏览器模式

默认配置使用 CloakBrowser 做无头运行：

- 登录仍然使用有头浏览器，便于扫码和处理平台交互。
- 登录完成后会导出 `storage_state_path`。
- 运行任务时使用 CloakBrowser headless 读取这个登录态。

如果无头模式不可用，把 GUI 中的“运行后端”改为 `Playwright`，或把“运行时无头”改为“否”。

## CLI 备用命令

```bash
douyinfire doctor
douyinfire login --user main
douyinfire run --user main
douyinfire run-all
douyinfire service uninstall
```

旧入口仍然可用：

```bash
python xuhuohua.py gui
```

## 运行数据

- `data/profiles/<user>/`：Playwright 登录态
- `data/runs/<run_id>.json`：任务结果
- `logs/douyinfire.log`：应用日志
- `screenshots/<run_id>/`：失败截图

这些运行数据默认都被 `.gitignore` 排除。

## 测试

```bash
python -m compileall douyinfire
pytest
douyinfire doctor
```
