# DouyinFire

DouyinFire 是一个 Mac 本地运行的抖音续火花辅助工具。当前版本使用 Playwright 保存浏览器登录态，并提供本地图形界面来编辑配置、登录、手动运行和查看日志。

> 本项目只面向个人账号的低频自用，不提供验证码绕过、风控绕过或批量营销能力。

## 当前运行方式

默认不启用定时自动化。使用本地 GUI 调试和手动触发：

```bash
cd "/Users/lazymice/Library/Mobile Documents/com~apple~CloudDocs/重要:常用文件备份/Lazymice/DouyinFire-main"
/private/tmp/douyinfire-venv/bin/douyinfire gui
```

打开：

```text
http://127.0.0.1:8765
```

GUI 支持：

- 查看配置、服务状态和当前任务状态
- 编辑并保存 `config/douyinfire.yaml`
- 打开登录窗口并保存 Playwright 登录态
- 手动运行单个用户或全部用户
- 查看 `logs/douyinfire.log`
- 取消 launchd 自动化服务

## 安装

```bash
python3 -m venv /private/tmp/douyinfire-venv
source /private/tmp/douyinfire-venv/bin/activate
pip install -r requirements.txt
playwright install chromium
pip install -e .
```

项目所在路径包含冒号，Python 不允许直接在该目录下创建 `.venv`，所以建议使用 `/private/tmp/douyinfire-venv`。

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

users:
  - name: "main"
    enabled: true
    contacts:
      - "联系人昵称"
    message: "续火花咯"
```

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
