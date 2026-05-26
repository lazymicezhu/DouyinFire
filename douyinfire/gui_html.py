HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>DouyinFire</title>
  <style>
    :root { color-scheme: light; --bg:#f6f7f8; --panel:#fff; --text:#202428; --muted:#66707a; --line:#d9dee3; --accent:#0f766e; --danger:#b42318; --ok:#12805c; --warn:#a15c00; }
    * { box-sizing: border-box; }
    body { margin:0; font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; background:var(--bg); color:var(--text); overflow:hidden; }
    header { display:flex; align-items:center; justify-content:space-between; padding:14px 20px; border-bottom:1px solid var(--line); background:var(--panel); position:sticky; top:0; z-index:2; }
    h1 { font-size:18px; margin:0; letter-spacing:0; }
    h2 { font-size:15px; margin:0 0 10px; }
    h3 { font-size:14px; margin:18px 0 10px; }
    main { display:grid; grid-template-columns:320px minmax(0,1fr); height:calc(100vh - 57px); overflow:hidden; }
    aside { border-right:1px solid var(--line); padding:16px; background:#fbfbfc; overflow-y:auto; }
    section { padding:16px 20px; overflow-y:auto; }
    button, select, input, textarea { border:1px solid var(--line); border-radius:6px; font:inherit; background:var(--panel); color:var(--text); }
    button, select { height:34px; padding:0 10px; }
    button.primary { background:var(--accent); border-color:var(--accent); color:white; }
    button.danger { border-color:#f0b6b0; color:var(--danger); }
    button:disabled { opacity:.55; cursor:not-allowed; }
    input, textarea, select { width:100%; padding:8px 10px; }
    textarea { min-height:84px; resize:vertical; }
    pre { margin:0; min-height:260px; max-height:420px; overflow:auto; border:1px solid var(--line); border-radius:6px; padding:12px; background:#111827; color:#e5e7eb; font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; }
    label { display:grid; gap:5px; color:var(--muted); align-content:start; }
    label span { color:var(--text); font-weight:600; }
    .row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin-bottom:12px; }
    .grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; align-items:start; }
    .grid3 { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }
    .panel { border:1px solid var(--line); background:var(--panel); border-radius:6px; padding:14px; margin-bottom:14px; }
    .status { display:grid; gap:8px; margin-bottom:16px; }
    .line { display:flex; justify-content:space-between; gap:12px; border-bottom:1px solid var(--line); padding-bottom:6px; }
    .muted { color:var(--muted); }
    .users { display:grid; gap:8px; margin-bottom:16px; }
    .user { border:1px solid var(--line); border-radius:6px; padding:10px; background:var(--panel); }
    .user strong { display:block; margin-bottom:3px; }
    .tabs { display:flex; gap:6px; margin:-16px 0 12px; padding:16px 0 0; border-bottom:1px solid var(--line); background:var(--bg); position:sticky; top:-16px; z-index:5; }
    .tab { border:0; border-radius:0; background:transparent; border-bottom:2px solid transparent; }
    .tab.active { border-bottom-color:var(--accent); color:var(--accent); }
    .tab-actions { position:sticky; top:35px; z-index:4; background:var(--bg); padding:4px 0 12px; border-bottom:1px solid var(--line); }
    .hidden { display:none; }
    .bar { height:10px; background:#e7eaee; border-radius:999px; overflow:hidden; margin:8px 0 14px; }
    .bar > div { height:100%; width:0%; background:var(--accent); transition:width .2s ease; }
    .steps { display:grid; gap:7px; margin-bottom:12px; }
    .step { display:grid; grid-template-columns:minmax(0,1fr) 78px 76px; gap:10px; align-items:center; border-bottom:1px solid var(--line); padding:8px 0; }
    .badge { width:max-content; border:1px solid var(--line); border-radius:999px; padding:2px 8px; color:var(--muted); }
    .badge.done { color:var(--ok); border-color:#a9dec8; }
    .badge.running { color:var(--warn); border-color:#f2c17e; }
    .badge.failed { color:var(--danger); border-color:#f0b6b0; }
    .progress-grid { display:grid; grid-template-columns:minmax(0, 1fr) minmax(0, 1fr); gap:22px; align-items:start; }
    .panel-title { display:flex; align-items:center; justify-content:space-between; gap:12px; }
    .countdown { color:var(--warn); font-variant-numeric:tabular-nums; }
    .hint { margin-top:6px; color:var(--muted); font-size:12px; }
    .summary { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:12px; margin-bottom:14px; }
    .metric { border:1px solid var(--line); border-radius:6px; padding:12px; background:var(--panel); }
    .metric strong { display:block; font-size:22px; margin-top:4px; }
    .result-list { display:grid; gap:8px; }
    .result-row { display:grid; grid-template-columns:1.2fr 90px 1.2fr 1.4fr; gap:10px; align-items:center; border-bottom:1px solid var(--line); padding:8px 0; }
    .result-screenshots { display:grid; gap:12px; margin-top:16px; }
    .result-screenshot { display:grid; gap:8px; }
    .result-screenshot img { width:100%; max-height:560px; object-fit:contain; border:1px solid var(--line); border-radius:6px; background:#fff; }
    .contact-editor { display:grid; gap:8px; grid-column:1 / -1; }
    .contact-title { display:flex; gap:12px; align-items:baseline; flex-wrap:wrap; }
    .contact-title h3 { margin-bottom:0; }
    .contact-note { color:var(--muted); font-size:12px; font-weight:400; }
    .contact-head,.contact-row { display:grid; grid-template-columns:1fr 1.4fr 34px; gap:8px; align-items:center; }
    .contact-head { color:var(--muted); font-weight:600; font-size:12px; }
    .contact-row button { width:34px; padding:0; color:var(--danger); }
    .icon-button { width:34px; padding:0; font-size:18px; line-height:1; }
    details textarea { min-height:260px; font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; }
    @media (max-width:900px) { main,.grid,.grid3,.progress-grid,.summary,.result-row,.contact-head,.contact-row { grid-template-columns:1fr; } aside { border-right:0; border-bottom:1px solid var(--line); } .contact-row button,.icon-button { width:100%; } }
  </style>
</head>
<body>
  <header>
    <h1>DouyinFire</h1>
    <div class="row" style="margin:0">
      <button id="interruptService" class="danger">中断服务</button>
    </div>
  </header>
  <main>
    <aside>
      <h2>状态</h2>
      <div class="status" id="status"></div>
      <h2>用户</h2>
      <div class="users" id="users"></div>
      <div class="row">
        <select id="userSelect"></select>
        <button id="login">登录</button>
        <button id="loginNew">登录新账号</button>
        <button id="runUser" class="primary">运行用户</button>
        <button id="runAll">运行全部</button>
      </div>
    </aside>
    <section>
      <div class="tabs">
        <button class="tab active" data-tab="config">配置</button>
        <button class="tab" data-tab="logs">日志</button>
        <button class="tab" data-tab="result">结果</button>
      </div>
      <div id="tab-config">
        <div class="row tab-actions">
          <button id="saveForm" class="primary">保存配置</button>
          <button id="initConfig">生成默认配置</button>
          <span class="muted" id="saveStatus"></span>
        </div>
        <form id="configForm">
          <div class="panel">
            <h2>基础配置</h2>
            <div class="grid">
              <label><span>数据目录</span><input name="data_dir"></label>
              <label><span>日志目录</span><input name="log_dir"></label>
              <label><span>截图目录</span><input name="screenshot_dir"></label>
              <label><span>失败通知阈值</span><input name="failure_notify_threshold" type="number" min="1"></label>
            </div>
          </div>
          <div class="panel">
            <h2>浏览器</h2>
            <div class="grid">
              <label><span>运行后端</span><select name="browser.backend"><option value="playwright">Playwright</option><option value="cloakbrowser">CloakBrowser</option></select></label>
              <label><span>运行时无头</span><select name="browser.run_headless"><option value="false">否</option><option value="true">是</option></select></label>
              <label><span>登录时无头</span><select name="browser.login_headless"><option value="false">否</option><option value="true">是</option></select></label>
              <label><span>登录态文件</span><input name="browser.storage_state_path"></label>
            </div>
          </div>
          <div class="panel">
            <h2>等待时间</h2>
            <div class="grid3">
              <label><span>首页等待秒数</span><input name="timeouts.home_ready_seconds" type="number" min="1"></label>
              <label><span>私信面板等待秒数</span><input name="timeouts.message_panel_seconds" type="number" min="1"></label>
              <label><span>联系人搜索等待秒数</span><input name="timeouts.contact_search_seconds" type="number" min="1"></label>
              <label><span>输入框等待秒数</span><input name="timeouts.input_box_seconds" type="number" min="1"></label>
              <label><span>发送后等待秒数</span><input name="timeouts.after_send_seconds" type="number" min="1"></label>
            </div>
          </div>
          <div class="panel">
            <h2>用户</h2>
            <div class="grid">
              <label><span>用户名称</span><input name="users.0.name"></label>
              <label><span>启用用户</span><select name="users.0.enabled"><option value="true">启用</option><option value="false">停用</option></select></label>
              <label style="grid-column:1 / -1"><span>全局消息</span><textarea name="users.0.message"></textarea><div class="hint">联系人没有自选消息时，发送这里的内容。</div></label>
              <div class="contact-editor">
                <div class="contact-title">
                  <h3>联系人</h3>
                  <span class="contact-note">请将需要发送消息的联系人置顶，建议只设置8位以下的联系人，超出失败风险会提高</span>
                </div>
                <div class="contact-head"><span>联系人</span><span>自选消息（可空）</span><span></span></div>
                <div id="contactRows"></div>
                <button type="button" id="addContact" class="icon-button" title="新增联系人">+</button>
              </div>
            </div>
          </div>
          <details class="panel">
            <summary>高级 YAML</summary>
            <div class="row" style="margin-top:12px"><button type="button" id="saveYaml">保存 YAML</button></div>
            <textarea id="configText" spellcheck="false"></textarea>
          </details>
        </form>
      </div>
      <div id="tab-logs" class="hidden">
        <div class="panel">
          <h2 class="panel-title"><span>运行进度</span><span class="muted" id="elapsedText"></span></h2>
          <div class="bar"><div id="progressBar"></div></div>
          <div class="progress-grid">
            <div class="steps" id="stepsLeft"></div>
            <div class="steps" id="stepsRight"></div>
          </div>
        </div>
        <div class="row tab-actions"><button id="reloadLogs">刷新日志</button></div>
        <pre id="logs"></pre>
      </div>
      <div id="tab-result" class="hidden">
        <div class="row tab-actions">
          <button id="retryFailed" class="primary">重试失败</button>
          <span class="muted" id="retryStatus"></span>
        </div>
        <div id="result"></div>
      </div>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    let state = null;
    let formDirty = false;
    let yamlDirty = false;
    let submitting = false;
    let previousRunning = false;
    let lastResultRenderKey = '';

    async function api(path, options = {}) {
      const res = await fetch(path, options);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || res.statusText);
      return data;
    }
    async function refresh() {
      const wasRunning = previousRunning;
      state = await api('/api/state');
      previousRunning = !!state.job.running;
      renderState();
      if (wasRunning && !state.job.running && state.job.kind) activateTab('result');
      if (!formDirty) await loadForm();
      if (!yamlDirty) await loadYaml();
      await loadLogs();
    }
    async function loadForm() {
      try {
        const data = await api('/api/config/form');
        fillForm(data.config);
      } catch (e) { $('saveStatus').textContent = e.message; }
    }
    async function loadYaml() {
      const cfg = await api('/api/config');
      $('configText').value = cfg.text || '';
    }
    function renderState() {
      const c = state.config;
      $('status').innerHTML = [
        row('配置', c.ok ? 'ok' : c.error),
        row('服务', state.service),
        row('任务', state.job.running ? `${state.job.kind} running` : state.job.message),
        row('后端', c.browser?.backend || ''),
        row('路径', c.paths?.config || '')
      ].join('');
      $('users').innerHTML = (c.users || []).map(u => `<div class="user"><strong>${esc(u.name)}</strong><div class="muted">${u.enabled ? '启用' : '停用'} · ${u.contacts.length} 个联系人 · 有头登录 profile ${u.profile ? '存在' : '缺失'} · 无头登录态 ${u.storage_state ? '存在' : '缺失'}</div></div>`).join('');
      $('userSelect').innerHTML = (c.users || []).map(u => `<option value="${esc(u.name)}">${esc(u.name)}</option>`).join('');
      renderResult(state.job);
      renderSteps(state.job.steps || []);
      document.querySelectorAll('button').forEach(b => { if (b.id !== 'interruptService') b.disabled = state.job.running || submitting; });
      $('interruptService').disabled = !state.job.running || submitting || !!state.job.cancel_requested;
      const failed = resultRows(state.job.result).filter(r => !r.success).length;
      $('retryFailed').disabled = state.job.running || submitting || failed === 0;
    }
    function renderSteps(steps) {
      const done = steps.filter(s => s.status === 'done').length;
      const pct = steps.length ? Math.round(done / steps.length * 100) : 0;
      $('progressBar').style.width = pct + '%';
      $('elapsedText').textContent = state?.job?.started_at ? `已执行 ${formatDuration(state.job.duration_seconds || 0)}` : '';
      const byKey = new Map(steps.map(step => [step.key, step]));
      const leftKeys = ['open_home','open_messages','contact_recent_list','open_conversation'];
      const rightKeys = ['input_box','input_message','press_enter','done'];
      $('stepsLeft').innerHTML = leftKeys.map(key => byKey.get(key)).filter(Boolean).map(stepHtml).join('');
      $('stepsRight').innerHTML = rightKeys.map(key => byKey.get(key)).filter(Boolean).map(stepHtml).join('');
    }
    function stepHtml(s) {
      return `<div class="step"><strong>${esc(s.label)}</strong><span class="badge ${esc(s.status)}">${esc(statusName(s.status))}</span><span class="countdown" data-step="${esc(s.key)}">${esc(countdownText(s))}</span></div>`;
    }
    function rerenderCountdowns() {
      if (!state?.job?.steps) return;
      for (const s of state.job.steps) {
        const el = document.querySelector(`[data-step="${CSS.escape(s.key)}"]`);
        if (el) el.textContent = countdownText(s);
      }
      if (state.job.started_at) $('elapsedText').textContent = `已执行 ${formatDuration(elapsedSeconds(state.job.started_at, state.job.ended_at))}`;
    }
    function countdownText(step) {
      if (step.status !== 'running' || !step.started_at) return '';
      const expected = expectedSeconds(step.key);
      if (!expected) return '';
      const elapsed = (Date.now() - Date.parse(step.started_at)) / 1000;
      return Math.max(0, expected - elapsed).toFixed(1) + 's';
    }
    function elapsedSeconds(startedAt, endedAt) {
      const end = endedAt ? Date.parse(endedAt) : Date.now();
      return Math.max(0, (end - Date.parse(startedAt)) / 1000);
    }
    function formatDuration(seconds) {
      const total = Math.max(0, Math.floor(Number(seconds) || 0));
      const minutes = Math.floor(total / 60);
      const rest = total % 60;
      return minutes ? `${minutes}m${rest}s` : `${rest}s`;
    }
    function expectedSeconds(key) {
      const t = state?.config?.timeouts || {};
      return ({
        open_home: t.home_ready_seconds,
        open_messages: t.message_panel_seconds,
        contact_recent_list: t.contact_search_seconds,
        open_conversation: t.contact_search_seconds,
        input_box: t.input_box_seconds,
        input_message: 1,
        press_enter: t.after_send_seconds,
        done: 1
      })[key] || 0;
    }
    function fillForm(c) {
      setVal('data_dir', c.data_dir);
      setVal('log_dir', c.log_dir);
      setVal('screenshot_dir', c.screenshot_dir);
      setVal('failure_notify_threshold', c.failure_notify_threshold);
      setVal('browser.backend', c.browser?.backend || 'playwright');
      setVal('browser.run_headless', String(!!c.browser?.run_headless));
      setVal('browser.login_headless', String(!!c.browser?.login_headless));
      setVal('browser.storage_state_path', c.browser?.storage_state_path || 'data/states/main.json');
      for (const [k,v] of Object.entries(c.timeouts || {})) setVal('timeouts.' + k, v);
      const u = (c.users || [])[0] || { name:'main', enabled:true, contacts:[], message:'' };
      setVal('users.0.name', u.name);
      setVal('users.0.enabled', String(!!u.enabled));
      setVal('users.0.message', u.message || '');
      renderContactRows(u.contacts || []);
    }
    function collectForm() {
      return {
        data_dir: val('data_dir'),
        log_dir: val('log_dir'),
        screenshot_dir: val('screenshot_dir'),
        failure_notify_threshold: Number(val('failure_notify_threshold')),
        browser: {
          backend: val('browser.backend'),
          run_headless: val('browser.run_headless') === 'true',
          login_headless: val('browser.login_headless') === 'true',
          storage_state_path: val('browser.storage_state_path')
        },
        timeouts: {
          home_ready_seconds: Number(val('timeouts.home_ready_seconds')),
          message_panel_seconds: Number(val('timeouts.message_panel_seconds')),
          contact_search_seconds: Number(val('timeouts.contact_search_seconds')),
          input_box_seconds: Number(val('timeouts.input_box_seconds')),
          after_send_seconds: Number(val('timeouts.after_send_seconds'))
        },
        users: [{
          name: val('users.0.name'),
          enabled: val('users.0.enabled') === 'true',
          contacts: collectContacts(),
          message: val('users.0.message')
        }]
      };
    }
    function setVal(name, value) { const el = document.querySelector(`[name="${name}"]`); if (el) el.value = value ?? ''; }
    function val(name) { const el = document.querySelector(`[name="${name}"]`); return el ? el.value : ''; }
    function renderContactRows(contacts) {
      const normalized = (contacts || []).map(c => typeof c === 'string' ? { name:c, message:'' } : c);
      const rows = normalized.length ? normalized : [{ name:'', message:'' }];
      $('contactRows').innerHTML = rows.map(contactRowHtml).join('');
    }
    function contactRowHtml(contact = {}) {
      return `<div class="contact-row">
        <input data-contact-field="name" value="${escAttr(contact.name || '')}" placeholder="联系人">
        <input data-contact-field="message" value="${escAttr(contact.message || '')}" placeholder="留空使用全局消息">
        <button type="button" data-remove-contact title="删除联系人">×</button>
      </div>`;
    }
    function collectContacts() {
      return Array.from(document.querySelectorAll('.contact-row')).map(row => ({
        name: row.querySelector('[data-contact-field="name"]').value.trim(),
        message: row.querySelector('[data-contact-field="message"]').value.trim()
      })).filter(contact => contact.name);
    }
    function statusName(v) { return ({pending:'等待', running:'运行中', done:'完成', failed:'失败'}[v] || v); }
    function renderResult(job) {
      const renderKey = JSON.stringify({
        message: job.message || '',
        duration_seconds: job.duration_seconds || 0,
        result: job.result || null
      });
      if (renderKey === lastResultRenderKey) return;
      lastResultRenderKey = renderKey;

      const rows = resultRows(job.result);
      const total = rows.length;
      const success = rows.filter(r => r.success).length;
      const failed = rows.filter(r => !r.success).length;
      const skipped = rows.filter(r => r.skipped).length;
      const screenshots = resultScreenshots(job.result);
      $('result').innerHTML = `
        <div class="summary">
          ${metric('总数', total)}
          ${metric('成功', success)}
          ${metric('失败', failed)}
          ${metric('跳过', skipped)}
          ${metric('耗时', formatDuration(job.duration_seconds || 0))}
        </div>
        <div class="panel">
          <h2>联系人结果</h2>
          <div class="result-list">
            ${rows.length ? rows.map(resultRow).join('') : '<div class="muted">暂无运行结果</div>'}
          </div>
          ${screenshots.length ? `<div class="result-screenshots">${screenshots.map(resultScreenshot).join('')}</div>` : ''}
        </div>`;
    }
    function resultUsers(result) {
      if (!result) return [];
      if (Array.isArray(result)) return result;
      if (Array.isArray(result.users)) return result.users;
      if (Array.isArray(result.results)) return [result];
      return [];
    }
    function resultRows(result) {
      return resultUsers(result).flatMap(u => (u.results || []).map(item => ({...item, user:u.user})));
    }
    function resultScreenshots(result) {
      return resultUsers(result)
        .filter(u => u.final_message_panel_screenshot)
        .map(u => ({ user:u.user || '', path:u.final_message_panel_screenshot }));
    }
    function metric(label, value) { return `<div class="metric"><span class="muted">${esc(label)}</span><strong>${esc(value)}</strong></div>`; }
    function resultRow(row) {
      const status = row.success ? (row.skipped ? '跳过' : '成功') : '失败';
      return `<div class="result-row"><strong>${esc(row.contact || '')}</strong><span class="badge ${row.success ? 'done' : 'failed'}">${status}</span><span class="muted">${esc(row.reason || '')}</span><span class="muted"></span></div>`;
    }
    function resultScreenshot(item) {
      const src = `/api/screenshot?path=${encodeURIComponent(item.path)}`;
      return `<div class="result-screenshot"><strong>${esc(item.user)} 流程结束截图</strong><img src="${src}" alt="${esc(item.user)} 流程结束截图" onerror="this.replaceWith(Object.assign(document.createElement('div'), { className:'muted', textContent:'截图文件未生成或已不存在' }))"></div>`;
    }
    function row(k, v) { return `<div class="line"><span>${esc(k)}</span><span class="muted">${esc(String(v || ''))}</span></div>`; }
    function esc(s) { return String(s).replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch])); }
    function escAttr(s) { return esc(s); }
    async function loadLogs() {
      const data = await api('/api/logs?limit=220');
      $('logs').textContent = data.text || '';
    }
    async function post(path, payload = {}) {
      submitting = true;
      renderState();
      try {
        await api(path, { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload) });
        await refresh();
      } finally {
        submitting = false;
        if (state) renderState();
      }
    }
    function activateTab(name) {
      document.querySelectorAll('.tab').forEach(b => b.classList.toggle('active', b.dataset.tab === name));
      ['config','logs','result'].forEach(tab => $('tab-' + tab).classList.toggle('hidden', tab !== name));
    }
    $('reloadLogs').onclick = loadLogs;
    $('saveForm').onclick = async () => {
      try {
        await post('/api/config/form', collectForm());
        formDirty = false;
        yamlDirty = false;
        $('saveStatus').textContent = '已保存';
      } catch (e) { $('saveStatus').textContent = e.message; }
    };
    $('saveYaml').onclick = async () => {
      try {
        await post('/api/config', { text: $('configText').value });
        yamlDirty = false;
        formDirty = false;
        $('saveStatus').textContent = 'YAML 已保存';
      } catch (e) { $('saveStatus').textContent = e.message; }
    };
    $('initConfig').onclick = () => post('/api/init', { overwrite:true });
    $('login').onclick = () => { activateTab('logs'); post('/api/login', { user:$('userSelect').value, wait_seconds:180 }); };
    $('loginNew').onclick = () => {
      const user = prompt('请输入新账号名称');
      if (!user || !user.trim()) return;
      activateTab('logs');
      post('/api/login-new', { user:user.trim(), source:$('userSelect').value, wait_seconds:180 });
    };
    $('runUser').onclick = () => { activateTab('logs'); post('/api/run', { user:$('userSelect').value }); };
    $('runAll').onclick = () => { activateTab('logs'); post('/api/run-all'); };
    $('retryFailed').onclick = async () => {
      $('retryStatus').textContent = '';
      try { activateTab('logs'); await post('/api/retry-failed'); }
      catch (e) { $('retryStatus').textContent = e.message; }
    };
    $('interruptService').onclick = async () => {
      submitting = true;
      if (state) renderState();
      try {
        await api('/api/interrupt', { method:'POST', headers:{'Content-Type':'application/json'}, body:'{}' });
        await refresh();
      } finally {
        submitting = false;
        if (state) renderState();
      }
    };
    $('addContact').onclick = () => {
      $('contactRows').insertAdjacentHTML('beforeend', contactRowHtml());
      formDirty = true;
    };
    $('contactRows').addEventListener('click', event => {
      const button = event.target.closest('[data-remove-contact]');
      if (!button) return;
      const rows = Array.from(document.querySelectorAll('.contact-row'));
      if (rows.length <= 1) {
        rows[0].querySelectorAll('input').forEach(input => input.value = '');
      } else {
        button.closest('.contact-row').remove();
      }
      formDirty = true;
    });
    $('configForm').addEventListener('input', () => formDirty = true);
    $('configText').addEventListener('input', () => yamlDirty = true);
    document.querySelectorAll('.tab').forEach(btn => btn.onclick = () => activateTab(btn.dataset.tab));
    refresh();
    setInterval(refresh, 2500);
    setInterval(rerenderCountdowns, 100);
  </script>
</body>
</html>
"""
