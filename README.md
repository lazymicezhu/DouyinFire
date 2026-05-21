# DouyinFire

DouyinFire 是一个 Mac 本地运行的抖音续火花自动化工具。当前版本已经从早期的单文件 Selenium 脚本重构为 Playwright + CLI + 配置文件 + launchd 的结构，目标是低频、可控、可恢复地完成每日私信任务。

> 本项目只面向个人账号的低频自用自动化，不提供验证码绕过、风控绕过或批量营销能力。

## 功能

- Playwright persistent profile 保存每个用户的登录态
- YAML 配置多用户、联系人、消息和每日执行时间
- CLI 管理初始化、登录、立即运行、检查和服务安装
- 每次运行生成 JSON 记录、日志和失败截图
- 单联系人失败不会中断整批任务
- 失败达到阈值时发送 macOS 本地通知
- launchd 支持每天 `00:05` 唤醒运行

## 安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
pip install -e .
```

## 初始化配置

```bash
douyinfire init
```

这会生成 `config/douyinfire.yaml`。真实配置不会提交到 Git。也可以参考 `config/douyinfire.example.yaml`：

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

## 常用命令

```bash
douyinfire doctor
douyinfire login --user main
douyinfire run --user main
douyinfire run-all
douyinfire next-run
```

旧命令仍然可用：

```bash
python xuhuohua.py doctor
```

## 常驻运行

安装 launchd 服务：

```bash
douyinfire service install
douyinfire service status
```

卸载服务：

```bash
douyinfire service uninstall
```

launchd 会在每天 `00:05` 启动 `douyinfire run-all`。程序内部会根据配置加入随机延迟，默认 0 到 20 分钟。

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

`doctor` 会检查配置、Playwright 依赖、运行目录和用户登录态。首次运行时 profile 缺失是正常的，执行 `douyinfire login --user <name>` 后即可生成。
