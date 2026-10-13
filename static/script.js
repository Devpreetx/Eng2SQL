(() => {
  'use strict';

  const SESSION_KEY = 'eng2sql_session_id';
  const ACTIVE_DB_KEY = 'eng2sql_active_db';
  const THEME_KEY = 'eng2sql_theme';
  const USER_KEY = 'eng2sql_user';
  const HISTORY_PAGE_SIZE = 20;
  const PREVIEW_PAGE_SIZE = 50;
  const COLUMN_TYPES = ['TEXT', 'INTEGER', 'REAL', 'NUMERIC', 'BLOB'];
  const PIE_COLORS = ['#34d399', '#60a5fa', '#f5b94c', '#f47181', '#a78bfa', '#2dd4bf', '#fb923c', '#94a3b8'];

  const $ = (id) => document.getElementById(id);
  const enc = encodeURIComponent;

  // localStorage can be unavailable (private mode, blocked cookies) — every
  // use is a convenience, never a requirement.
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch (e) { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* non-fatal */ } },
    remove(key) { try { localStorage.removeItem(key); } catch (e) { /* non-fatal */ } },
  };

  const els = {
    authView: $('authView'), authForm: $('authForm'), authTitle: $('authTitle'),
    authSubtitle: $('authSubtitle'), authNameField: $('authNameField'), authName: $('authName'),
    authEmail: $('authEmail'), authPassword: $('authPassword'), authPasswordHint: $('authPasswordHint'),
    authError: $('authError'), authSubmit: $('authSubmit'), authSwitchRow: $('authSwitchRow'),
    authSwitchText: $('authSwitchText'), authSwitch: $('authSwitch'),
    app: $('app'), sidebar: $('sidebar'), scrim: $('scrim'), menu: $('menu'),
    menuBtn: $('menuBtn'), sidebarClose: $('sidebarClose'),
    userMenuBtn: $('userMenuBtn'), userAvatar: $('userAvatar'), userName: $('userName'), userEmail: $('userEmail'),
    newDbBtn: $('newDbBtn'), uploadBtn: $('uploadBtn'), fileInput: $('fileInput'),
    sideTabExplorer: $('sideTabExplorer'), sideTabHistory: $('sideTabHistory'),
    paneExplorer: $('paneExplorer'), paneHistory: $('paneHistory'),
    databaseList: $('databaseList'), databaseCount: $('databaseCount'),
    schemaPanel: $('schemaPanel'), schemaTree: $('schemaTree'), tableCount: $('tableCount'),
    createTableBtn: $('createTableBtn'), importBtn: $('importBtn'),
    historySearch: $('historySearch'), historyFilters: $('historyFilters'),
    historyList: $('historyList'), historyPagination: $('historyPagination'),
    historyTotalCount: $('historyTotalCount'),
    activeDbChip: $('activeDbChip'), newChatBtn: $('newChatBtn'),
    tabChat: $('tabChat'), tabWorkspace: $('tabWorkspace'),
    chatView: $('chatView'), log: $('log'), emptyState: $('emptyState'), emptyLead: $('emptyLead'),
    suggestions: $('suggestions'),
    composer: $('composer'), question: $('question'), askBtn: $('askBtn'),
    sqlWorkspace: $('sqlWorkspace'), sqlEditor: $('sqlEditor'), runSqlBtn: $('runSqlBtn'),
    formatSqlBtn: $('formatSqlBtn'), clearSqlBtn: $('clearSqlBtn'),
    workspaceStatus: $('workspaceStatus'), workspaceResults: $('workspaceResults'),
    preview: $('tablePreviewPanel'), previewName: $('previewName'), previewCount: $('previewCount'),
    previewContent: $('previewContent'), previewPagination: $('previewPagination'),
    previewExportCsv: $('previewExportCsv'), previewExportXlsx: $('previewExportXlsx'),
    previewOpenWorkspace: $('previewOpenWorkspace'), closePreview: $('closePreview'),
    promptModal: $('promptModal'), promptForm: $('promptForm'), promptTitle: $('promptTitle'),
    promptLabel: $('promptLabel'), promptInput: $('promptInput'), promptHint: $('promptHint'),
    promptSubmit: $('promptSubmit'),
    confirmModal: $('confirmModal'), confirmTitle: $('confirmTitle'),
    confirmMessage: $('confirmMessage'), confirmAccept: $('confirmAccept'),
    createTableModal: $('createTableModal'), createTableForm: $('createTableForm'),
    tableNameInput: $('tableNameInput'), columnList: $('columnList'), addColumnBtn: $('addColumnBtn'),
    submitCreateTable: $('submitCreateTable'),
    importModal: $('importModal'), importForm: $('importForm'), importFileInput: $('importFileInput'),
    importTableName: $('importTableName'), importMode: $('importMode'), submitImport: $('submitImport'),
    backupsModal: $('backupsModal'), backupsTitle: $('backupsTitle'), backupList: $('backupList'),
    passwordModal: $('passwordModal'), passwordForm: $('passwordForm'),
    currentPassword: $('currentPassword'), newPassword: $('newPassword'),
    passwordError: $('passwordError'), submitPassword: $('submitPassword'),
    historyDetailModal: $('historyDetailModal'), historyDetailBody: $('historyDetailBody'),
    historyDetailStar: $('historyDetailStar'), historyDetailStarLabel: $('historyDetailStarLabel'),
    historyDetailOpenSql: $('historyDetailOpenSql'), historyDetailReuse: $('historyDetailReuse'),
    toasts: $('toasts'),
  };

  const state = {
    user: null,              // { id, email, name } once signed in
    dbId: null,
    dbName: '',
    tables: [],
    openTables: new Set(),   // schema-tree rows the user expanded
    asking: false,
    preview: null,           // { dbId, table, page }
    history: { status: 'all', search: '', offset: 0, request: 0 },
    historyDetail: null,
    // Conversation memory: reused across reloads, sent with every /query.
    // The backend mints one if it is missing, so this is a convenience.
    sessionId: store.get(SESSION_KEY) || newSessionId(),
  };

  function newSessionId() {
    const id = crypto.randomUUID
      ? crypto.randomUUID()
      : `sess-${Date.now()}-${Math.random().toString(36).slice(2)}`;
    store.set(SESSION_KEY, id);
    return id;
  }

  // ==================== small helpers ====================

  const ESCAPES = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };

  function esc(value) {
    return String(value === null || value === undefined ? '' : value)
      .replace(/[&<>"']/g, (c) => ESCAPES[c]);
  }

  function icon(name, extraClass = '') {
    return `<svg class="icon ${extraClass}" viewBox="0 0 24 24" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  }

  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

  const loadingHtml = (text) => `<div class="loading"><span class="spinner"></span>${esc(text)}</div>`;

  function toast(message, kind = 'info') {
    const item = document.createElement('div');
    item.className = `toast ${kind}`;
    item.textContent = message;
    els.toasts.appendChild(item);
    setTimeout(() => item.remove(), kind === 'error' ? 7000 : 4000);
  }

  // Shown when a request gets no response at all. The usual causes are a
  // stopped server, or this page opened as a file instead of through it.
  const OFFLINE_HELP = window.location.protocol === 'file:'
    ? 'This page was opened as a file, so it has no server to talk to. Start the server and open http://localhost:8000 instead.'
    : `Could not reach the server at ${window.location.host}. Check that it is still running, then reload this page.`;

  // One place for every request: JSON in, JSON out, and a readable Error
  // for network failures and FastAPI's {detail} error bodies.
  async function api(path, { method, json, body, raw = false } = {}) {
    const options = { method: method || (json !== undefined || body ? 'POST' : 'GET') };
    if (json !== undefined) {
      options.headers = { 'Content-Type': 'application/json' };
      options.body = JSON.stringify(json);
    } else if (body) {
      options.body = body;
    }

    let res;
    try {
      res = await fetch(path, options);
    } catch (e) {
      throw new Error(OFFLINE_HELP);
    }

    if (raw && res.ok) return res;

    // The session ended (expired, or signed out elsewhere): start over at
    // the sign-in page rather than leaving a half-working app on screen.
    if (res.status === 401 && !path.startsWith('/auth/')) {
      window.location.reload();
      throw new Error('Your session has ended. Please sign in again.');
    }

    const data = await res.json().catch(() => null);
    if (!res.ok) {
      const detail = data && data.detail;
      if (typeof detail === 'string') throw new Error(detail);
      if (Array.isArray(detail)) throw new Error(detail.map((d) => d.msg).join('; '));
      throw new Error(`Server returned ${res.status}.`);
    }
    return data;
  }

  function saveBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  async function downloadFrom(path, options, fallbackName, button) {
    if (button) button.disabled = true;
    try {
      const res = await api(path, { ...options, raw: true });
      const named = /filename="?([^";]+)"?/.exec(res.headers.get('Content-Disposition') || '');
      saveBlob(await res.blob(), named ? named[1] : fallbackName);
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function copyText(text, button) {
    try {
      await navigator.clipboard.writeText(text);
    } catch (e) {
      // Clipboard API needs a secure context; fall back for plain-http LANs.
      const area = document.createElement('textarea');
      area.value = text;
      document.body.appendChild(area);
      area.select();
      document.execCommand('copy');
      area.remove();
    }
    if (button) {
      button.innerHTML = icon('check');
      setTimeout(() => { button.innerHTML = icon('copy'); }, 1200);
    }
  }

  function formatWhen(isoString) {
    const date = new Date(isoString);
    if (Number.isNaN(date.getTime())) return '';
    const time = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    const today = new Date().toDateString() === date.toDateString();
    return today ? time : `${date.toLocaleDateString([], { day: 'numeric', month: 'short' })}, ${time}`;
  }

  function formatBytes(bytes) {
    if (bytes < 1024) return `${bytes} B`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  function showFormError(element, message) {
    element.textContent = message || '';
    element.hidden = !message;
  }

  // Quotes a table name only if it isn't a simple alnum/underscore token,
  // matching the backend's own quoting style.
  function quoteIdentIfNeeded(name) {
    if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) return name;
    return `"${name.replace(/"/g, '""')}"`;
  }

  // ==================== SQL + result markup ====================

  const SQL_KEYWORDS = new Set((
    'select from where and or not in is null as join left right inner outer cross on group by ' +
    'order having limit offset insert into values update set delete create table view index ' +
    'distinct union all except intersect case when then else end like between exists asc desc ' +
    'with primary key references foreign default unique if integer text real blob numeric'
  ).split(' '));

  const SQL_TOKEN = /(--[^\n]*|\/\*[\s\S]*?\*\/)|('(?:[^']|'')*'?)|("(?:[^"]|"")*"?)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)/g;

  // Every piece of the input goes through esc() — the only markup added
  // is the <span> wrappers themselves.
  function highlightSql(sql) {
    let html = '';
    let last = 0;
    for (const match of String(sql).matchAll(SQL_TOKEN)) {
      const token = match[0];
      html += esc(sql.slice(last, match.index));
      if (match[1]) html += `<span class="tok-com">${esc(token)}</span>`;
      else if (match[2]) html += `<span class="tok-str">${esc(token)}</span>`;
      else if (match[4]) html += `<span class="tok-num">${esc(token)}</span>`;
      else if (match[5] && SQL_KEYWORDS.has(token.toLowerCase())) html += `<span class="tok-kw">${esc(token)}</span>`;
      else html += esc(token);
      last = match.index + token.length;
    }
    return html + esc(sql.slice(last));
  }

  function sqlBlock(sql) {
    return `
      <div class="sql-block">
        <pre><code>${highlightSql(sql)}</code></pre>
        <button type="button" class="icon-btn" data-copy="${esc(sql)}" title="Copy SQL" aria-label="Copy SQL">${icon('copy')}</button>
      </div>`;
  }

  function sqlBlocks(statements) {
    return (statements || []).map(sqlBlock).join('');
  }

  function cellHtml(value) {
    if (value === null || value === undefined) return '<td class="null">NULL</td>';
    if (typeof value === 'number') return `<td class="num">${esc(value)}</td>`;
    const text = String(value);
    return text.length > 40 ? `<td title="${esc(text)}">${esc(text)}</td>` : `<td>${esc(text)}</td>`;
  }

  function resultTable(columns, rows) {
    if (!columns || !columns.length || !rows || !rows.length) {
      return '<p class="empty-note">No rows.</p>';
    }
    // Headers of numeric columns are right-aligned to sit over their values.
    const head = columns.map((c) => {
      const sample = rows.find((row) => row[c] !== null && row[c] !== undefined);
      return `<th${sample && typeof sample[c] === 'number' ? ' class="num"' : ''}>${esc(c)}</th>`;
    }).join('');
    const body = rows
      .map((row) => `<tr>${columns.map((c) => cellHtml(row[c])).join('')}</tr>`)
      .join('');
    return `<div class="table-wrap"><table class="result-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
  }

  function renderPager(container, { from, to, total, onPrev, onNext }) {
    container.innerHTML = '';
    if (total <= to - from + 1) return;  // everything fits on one page

    const label = document.createElement('span');
    label.textContent = `${from}–${to} of ${total}`;

    const buttons = document.createElement('div');
    buttons.className = 'pager-buttons';
    for (const [text, handler] of [['Previous', from > 1 && onPrev], ['Next', to < total && onNext]]) {
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'btn btn-sm';
      button.textContent = text;
      button.disabled = !handler;
      if (handler) button.addEventListener('click', handler);
      buttons.appendChild(button);
    }

    container.append(label, buttons);
  }

  // ==================== charts ====================

  const charts = new Set();

  const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

  function chartPanelHtml(chart) {
    if (!chart || !chart.chart_required || chart.chart_type === 'none') return '';
    if (chart.chart_type !== 'kpi' && typeof Chart === 'undefined') return '';

    if (chart.chart_type === 'kpi') {
      return `
        <div class="chart-panel">
          <div class="chart-title">${esc(chart.chart_title || '')}</div>
          <div class="kpi-value"></div>
        </div>`;
    }

    return `
      <div class="chart-panel">
        <div class="chart-head">
          <span class="chart-title">${esc(chart.chart_title || '')}</span>
          <div class="segmented">
            <button type="button" class="segment active" data-view="chart">Chart</button>
            <button type="button" class="segment" data-view="table">Table</button>
          </div>
          <button type="button" class="btn btn-sm" data-act="png">${icon('download')} PNG</button>
        </div>
        <div class="chart-canvas"><canvas></canvas></div>
      </div>`;
  }

  // Colours come from the CSS theme tokens. Works on a plain config object
  // (before the chart exists) and on a live chart (when the theme flips).
  function paintChart(type, data, options) {
    const accent = cssVar('--accent');
    const text = cssVar('--text-dim');
    const grid = cssVar('--border');

    const dataset = data.datasets[0];
    if (type === 'pie') {
      dataset.borderColor = cssVar('--surface-2');
    } else {
      dataset.backgroundColor = type === 'line' ? cssVar('--accent-soft') : accent;
      dataset.borderColor = accent;
    }

    options.plugins.legend.labels.color = text;
    for (const scale of Object.values(options.scales)) {
      scale.ticks = { ...scale.ticks, color: text };
      scale.grid = { ...scale.grid, color: grid };
    }
  }

  // Runs AFTER chartPanelHtml()'s markup is in the DOM. Never throws out
  // to the caller — a chart failure just removes the chart panel and
  // leaves the answer, SQL and result table intact.
  function initChart(card, chart) {
    const panel = card.querySelector('.chart-panel');
    if (!panel) return;

    try {
      const rows = chart.chart_data || [];

      // KPI: no canvas — just the number, straight from the result row.
      if (chart.chart_type === 'kpi') {
        const row = rows[0] || {};
        const value = chart.chart_y_column ? row[chart.chart_y_column] : Object.values(row)[0];
        panel.querySelector('.kpi-value').textContent =
          value === undefined || value === null ? '—'
            : typeof value === 'number' ? value.toLocaleString(undefined, { maximumFractionDigits: 4 })
              : String(value);
        return;
      }

      const type = ['bar', 'line', 'pie', 'scatter'].includes(chart.chart_type) ? chart.chart_type : 'bar';
      const canvasWrap = panel.querySelector('.chart-canvas');
      const canvas = panel.querySelector('canvas');
      const label = chart.chart_y_column || 'value';

      const dataset = type === 'scatter'
        ? { label, data: rows.map((r) => ({ x: r[chart.chart_x_column], y: r[chart.chart_y_column] })) }
        : { label, data: rows.map((r) => r[chart.chart_y_column]) };

      if (type === 'pie') dataset.backgroundColor = PIE_COLORS;
      if (type === 'line') Object.assign(dataset, { fill: true, tension: 0.25, pointRadius: 3 });
      if (type === 'bar') Object.assign(dataset, { borderRadius: 4, maxBarThickness: 48 });

      const data = {
        labels: type === 'scatter' ? undefined : rows.map((r) => r[chart.chart_x_column]),
        datasets: [dataset],
      };
      const options = {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { display: type === 'pie', position: 'bottom', labels: {} },
          // Chart.js's automatic palette would override the theme colours.
          colors: { enabled: false },
        },
        scales: type === 'pie' ? {} : { x: {}, y: { beginAtZero: type === 'bar' } },
      };
      paintChart(type, data, options);
      charts.add(new Chart(canvas, { type, data, options }));

      const viewButtons = panel.querySelectorAll('[data-view]');
      viewButtons.forEach((button) => {
        button.addEventListener('click', () => {
          canvasWrap.hidden = button.dataset.view === 'table';
          viewButtons.forEach((b) => b.classList.toggle('active', b === button));
        });
      });

      panel.querySelector('[data-act="png"]').addEventListener('click', () => {
        // Paint a solid background first: the chart canvas is transparent,
        // which makes light-on-dark text unreadable once pasted elsewhere.
        const out = document.createElement('canvas');
        out.width = canvas.width;
        out.height = canvas.height;
        const ctx = out.getContext('2d');
        ctx.fillStyle = cssVar('--surface-2');
        ctx.fillRect(0, 0, out.width, out.height);
        ctx.drawImage(canvas, 0, 0);
        out.toBlob((blob) => {
          if (blob) saveBlob(blob, `${(chart.chart_title || 'chart').trim().replace(/\W+/g, '_')}.png`);
        });
      });
    } catch (e) {
      console.error('Chart render failed:', e);
      panel.remove();
    }
  }

  // ==================== result cards (AI Chat + SQL Workspace) ====================
  //
  // /query and /sql/execute answer with the same statuses, so one renderer
  // serves both. ctx = { dbId, source: 'chat' | 'workspace', question }.
  // ctx.dbId is the database the request was issued against — exports and
  // confirmations keep using it even if the user switches database later.

  function statementHtml(sql, result, index) {
    const r = result || { columns: [], rows: [] };
    const count = r.rows.length;
    const note = r.truncated
      ? `First ${count} rows shown — export to get the full result`
      : plural(count, 'row');

    return `
      <div class="statement">
        ${sqlBlock(sql)}
        ${resultTable(r.columns, r.rows)}
        <div class="result-foot">
          <span>${esc(note)}</span>
          <span class="spacer"></span>
          <button type="button" class="btn btn-sm" data-export="csv" data-idx="${index}">${icon('download')} CSV</button>
          <button type="button" class="btn btn-sm" data-export="xlsx" data-idx="${index}">${icon('download')} XLSX</button>
        </div>
      </div>`;
  }

  const NO_PREVIEW_NOTES = {
    insert: 'Adds new rows — existing rows are not changed.',
    create: 'Creates a new object — existing rows are not changed.',
  };

  function previewHtml(preview) {
    const count = preview.affected_count;
    const impact = count !== null && count !== undefined
      ? `<p class="impact">This will affect ${plural(count, 'row')}.</p>`
      : `<p class="impact unknown">${esc(NO_PREVIEW_NOTES[preview.type] || 'Row impact could not be previewed automatically.')}</p>`;

    const sample = preview.sample_rows && preview.sample_rows.length
      ? `<div class="label">Sample of affected rows</div>${resultTable(Object.keys(preview.sample_rows[0]), preview.sample_rows)}`
      : '';

    return `
      <div class="preview-item">
        <span class="badge write">${esc((preview.type || 'write').toUpperCase())}</span>
        ${sqlBlock(preview.sql)}
        ${impact}
        ${sample}
      </div>`;
  }

  function messageCard(card, kind, badge, message, statements) {
    card.className = `card ${kind}`;
    card.innerHTML = `
      ${badge ? `<div class="card-head"><span class="badge ${kind}">${esc(badge)}</span></div>` : ''}
      <p class="card-text">${esc(message)}</p>
      ${sqlBlocks(statements)}`;
  }

  function renderResult(card, data, ctx) {
    const statements = data.statements || [];
    const fromChat = ctx.source === 'chat';

    const standalone = (data.standalone_question || '').trim();
    const interpreted = standalone && ctx.question && standalone.toLowerCase() !== ctx.question.trim().toLowerCase()
      ? `<p class="card-note">Interpreted as: “${esc(standalone)}”</p>`
      : '';
    const explanation = data.explanation ? `<p class="card-text">${esc(data.explanation)}</p>` : '';

    if (data.status === 'needs_clarification') {
      messageCard(card, 'info', '', data.question || 'Could you clarify your request?');
      return;
    }

    if (data.status === 'rejected') {
      messageCard(card, 'error', 'Blocked', data.reason || 'Statement blocked by safety policy.', statements);
      return;
    }

    if (data.status === 'error') {
      messageCard(card, 'error', 'Error', data.error || 'Execution failed.', statements);
      return;
    }

    // Pure reads — one or more SELECTs, already executed.
    if (data.status === 'executed' && data.results) {
      const totalRows = data.results.reduce((sum, r) => sum + (r.rows ? r.rows.length : 0), 0);
      const meta = [plural(totalRows, 'row')];
      if (statements.length > 1) meta.push(`${statements.length} statements`);
      if (data.execution_time_ms !== undefined) meta.push(`${data.execution_time_ms} ms`);

      card.className = 'card read';
      card.innerHTML = `
        <div class="card-head">
          <span class="badge read">${fromChat ? 'Read' : 'Executed'}</span>
          <span class="meta">${esc(meta.join(' · '))}</span>
          ${fromChat ? `<button type="button" class="btn btn-sm" data-act="workspace">${icon('code')} Open in SQL Workspace</button>` : ''}
        </div>
        ${interpreted}
        ${explanation}
        ${chartPanelHtml(data.chart)}
        <div>${statements.map((sql, i) => statementHtml(sql, data.results[i], i)).join('')}</div>`;

      // Hand-off to the SQL Workspace fills the editor but never runs it.
      const handoff = card.querySelector('[data-act="workspace"]');
      if (handoff) handoff.addEventListener('click', () => openInSqlWorkspace(statements.join(';\n\n')));

      card.querySelectorAll('[data-export]').forEach((button) => {
        button.addEventListener('click', () => {
          const format = button.dataset.export;
          downloadFrom(
            '/export/query',
            { json: { db_id: ctx.dbId, sql: statements[Number(button.dataset.idx)], format } },
            `query_result.${format}`,
            button,
          );
        });
      });

      if (data.chart) initChart(card, data.chart);
      return;
    }

    // Any batch with insert/update/delete/create — nothing has run yet.
    if (data.status === 'confirmation_required') {
      const runLabel = statements.length > 1 ? `Confirm & run all ${statements.length}` : 'Confirm & run';

      card.className = 'card write';
      card.innerHTML = `
        <div class="card-head">
          <span class="badge write">Needs confirmation</span>
          <span class="meta">${esc(plural(statements.length, 'statement'))} · nothing has been changed yet</span>
        </div>
        ${interpreted}
        ${explanation}
        <div>${(data.previews || []).map(previewHtml).join('')}</div>
        <div class="confirm-row">
          <button type="button" class="btn btn-danger" data-act="confirm">${esc(runLabel)}</button>
          <button type="button" class="btn" data-act="cancel">Cancel</button>
        </div>`;

      const row = card.querySelector('.confirm-row');
      row.querySelector('[data-act="confirm"]').addEventListener('click', () => resolvePending(data.query_id, true, row, ctx));
      row.querySelector('[data-act="cancel"]').addEventListener('click', () => resolvePending(data.query_id, false, row, ctx));
      return;
    }

    messageCard(card, 'error', 'Error', 'Unrecognized response from server.');
  }

  // Confirms/cancels against the database the statement was written for,
  // through the one /confirm endpoint shared by chat and workspace.
  async function resolvePending(queryId, confirmed, row, ctx) {
    row.innerHTML = loadingHtml(confirmed ? 'Running…' : 'Cancelling…');

    const isActiveWorkspace = () => ctx.source === 'workspace' && ctx.dbId === state.dbId;
    const resolution = document.createElement('div');
    resolution.className = 'resolution';

    try {
      const data = await api('/confirm', { json: { db_id: ctx.dbId, query_id: queryId, confirm: confirmed } });

      if (data.status === 'executed') {
        const results = data.results || [];
        const total = results.reduce((sum, r) => sum + Math.max(r.rowcount || 0, 0), 0);
        resolution.classList.add('ok');
        resolution.textContent =
          `Done — ${plural(results.length, 'statement')} run, ${plural(total, 'row')} affected. ` +
          'A backup was saved first. ';
        if (data.backup) resolution.appendChild(undoButton(ctx.dbId, data.backup, resolution));
        if (isActiveWorkspace()) setWorkspaceStatus(`Success — ${plural(total, 'row')} affected`, 'success');
        await afterDataChange(ctx.dbId);
      } else if (data.status === 'cancelled') {
        resolution.textContent = 'Cancelled — no changes made.';
        if (isActiveWorkspace()) setWorkspaceStatus('Cancelled');
        loadHistory();
      } else {
        resolution.classList.add('err');
        resolution.textContent = data.error || 'Something went wrong.';
        if (isActiveWorkspace()) setWorkspaceStatus('Execution failed', 'error');
        loadHistory();
      }
    } catch (e) {
      resolution.classList.add('err');
      resolution.textContent = e.message;
      if (isActiveWorkspace()) setWorkspaceStatus('Confirmation failed', 'error');
    }

    row.replaceWith(resolution);
  }

  // "Undo" on a finished write: restores the backup taken just before it.
  function undoButton(dbId, backupLabel, resolution) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'btn btn-sm';
    button.innerHTML = `${icon('undo')} Undo`;
    button.addEventListener('click', async () => {
      const confirmed = await confirmDialog({
        title: 'Undo this change?',
        message: 'The database goes back to exactly how it was just before this change. Anything changed since then is rolled back too (the current state is backed up first).',
        confirmLabel: 'Undo change',
      });
      if (!confirmed) return;

      button.disabled = true;
      if (await restoreBackup(dbId, backupLabel.split('/').pop())) {
        resolution.className = 'resolution';
        resolution.textContent = 'Undone — the database is back to how it was before this change.';
      } else {
        button.disabled = false;
      }
    });
    return button;
  }

  // A database was modified: refresh everything that shows its contents,
  // without changing which database is active.
  async function afterDataChange(changedDbId) {
    const tasks = [loadDatabases()];
    if (changedDbId === state.dbId) {
      tasks.push(refreshSchema(), loadHistory());
      if (state.preview) tasks.push(openPreview(state.preview.table, state.preview.page));
    }
    await Promise.all(tasks);
  }

  // ==================== databases ====================

  async function loadDatabases({ restore = false } = {}) {
    let databases;
    try {
      databases = (await api('/databases')).databases || [];
    } catch (e) {
      els.databaseList.innerHTML = `<p class="empty-note">${esc(e.message)}</p>`;
      return;
    }

    renderDatabaseList(databases);

    if (restore && !state.dbId) {
      const remembered = databases.find((d) => d.db_id === store.get(ACTIVE_DB_KEY));
      if (remembered) switchDatabase(remembered, { quiet: true });
    }
  }

  function renderDatabaseList(databases) {
    els.databaseCount.textContent = databases.length || '';
    els.databaseList.innerHTML = '';

    if (!databases.length) {
      els.databaseList.innerHTML = '<p class="empty-note">No databases yet — open a file or create one.</p>';
      return;
    }

    for (const database of databases) {
      const tables = database.tables || [];
      const item = document.createElement('div');
      item.className = 'db-item';
      item.dataset.dbId = database.db_id;
      item.classList.toggle('active', database.db_id === state.dbId);
      item.innerHTML = `
        <button type="button" class="db-open">
          ${icon('database')}
          <span class="db-item-name">${esc(database.name)}</span>
          <span class="count">${esc(plural(tables.length, 'table'))}</span>
        </button>
        <button type="button" class="icon-btn db-more" aria-haspopup="menu" aria-label="Actions for ${esc(database.name)}">${icon('more')}</button>`;

      item.querySelector('.db-open').addEventListener('click', () => switchDatabase(database));

      const more = item.querySelector('.db-more');
      more.addEventListener('click', () => openMenu(more, [
        { label: 'Rename', icon: 'edit', onSelect: () => renameDatabase(database) },
        { label: 'Backups & restore', icon: 'clock', onSelect: () => openBackups(database) },
        { label: 'Download .sqlite', icon: 'download', onSelect: () => downloadDatabase(database) },
        'divider',
        { label: 'Delete database', icon: 'trash', danger: true, onSelect: () => deleteDatabase(database) },
      ]));

      els.databaseList.appendChild(item);
    }
  }

  const dbControls = () => [
    els.question, els.askBtn, els.sqlEditor, els.runSqlBtn, els.formatSqlBtn, els.clearSqlBtn, els.historySearch,
  ];

  function showActiveDatabaseName() {
    els.question.placeholder = `Ask anything about ${state.dbName}…`;
    els.activeDbChip.textContent = state.dbName;
    els.activeDbChip.classList.add('live');
  }

  // The ONE place the active database changes — upload, new database and
  // sidebar clicks all route through here, so dbId and UI never drift apart.
  function setActiveDatabase(database) {
    const changed = state.dbId !== database.db_id;

    state.dbId = database.db_id;
    state.dbName = database.name;
    store.set(ACTIVE_DB_KEY, database.db_id);

    dbControls().forEach((control) => { control.disabled = false; });
    showActiveDatabaseName();

    els.databaseList.querySelectorAll('.db-item').forEach((item) => {
      item.classList.toggle('active', item.dataset.dbId === state.dbId);
    });

    if (changed) {
      // Nothing from the previous database may linger: its table preview,
      // its workspace results, its expanded tables, its history page.
      closePreview();
      state.openTables.clear();
      els.workspaceResults.innerHTML = '';
      setWorkspaceStatus('Ready');
      state.history.offset = 0;
    }

    loadHistory();
  }

  // Back to the "no database selected" state (the active one was deleted).
  function clearActiveDatabase() {
    state.dbId = null;
    state.dbName = '';
    state.tables = [];
    state.openTables.clear();
    state.history.request += 1;   // drop any history response still in flight
    store.remove(ACTIVE_DB_KEY);

    dbControls().forEach((control) => { control.disabled = true; });
    els.question.placeholder = 'Open a database to start asking…';
    els.activeDbChip.textContent = 'No database';
    els.activeDbChip.classList.remove('live');

    closePreview();
    els.schemaPanel.hidden = true;
    els.suggestions.hidden = true;
    els.emptyLead.textContent = EMPTY_LEAD;
    els.workspaceResults.innerHTML = '';
    setWorkspaceStatus('Ready');
    els.historyList.innerHTML = '<p class="empty-note">Select a database to see its history.</p>';
    els.historyPagination.innerHTML = '';
    els.historyTotalCount.textContent = '';
  }

  async function switchDatabase(database, { quiet = false } = {}) {
    setActiveDatabase(database);
    closeSidebar();
    await refreshSchema();
    if (!quiet) toast(`Switched to ${database.name}`, 'ok');
  }

  async function uploadFile(file) {
    const body = new FormData();
    body.append('file', file);

    els.uploadBtn.disabled = true;
    toast(`Reading ${file.name}…`);

    try {
      const data = await api('/upload', { body });
      setActiveDatabase(data);
      renderSchema(data.schema);
      await loadDatabases();
      closeSidebar();
      toast(`Loaded ${file.name} — ${plural(data.tables.length, 'table')}`, 'ok');
      els.question.focus();
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      els.uploadBtn.disabled = false;
      els.fileInput.value = '';
    }
  }

  async function createDatabase() {
    const name = await promptDialog({
      title: 'New database',
      label: 'Name',
      placeholder: 'e.g. inventory',
      hint: 'Letters, numbers, hyphens and underscores. An empty SQLite database is created.',
      submitLabel: 'Create database',
    });
    if (!name) return;

    try {
      const data = await api(`/database/create?name=${enc(name)}`, { method: 'POST' });
      setActiveDatabase(data);
      renderSchema(data.schema);
      await loadDatabases();
      toast(`Created ${data.name}`, 'ok');
      els.question.focus();
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  async function renameDatabase(database) {
    const name = await promptDialog({
      title: 'Rename database',
      label: 'Name',
      value: database.name,
      hint: 'Only the display name changes — the data is untouched.',
      submitLabel: 'Rename',
    });
    if (!name || name === database.name) return;

    try {
      const updated = await api(`/database/${enc(database.db_id)}`, { method: 'PATCH', json: { name } });
      if (database.db_id === state.dbId) {
        state.dbName = updated.name;
        showActiveDatabaseName();
      }
      await loadDatabases();
      toast(`Renamed to ${updated.name}`, 'ok');
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  async function deleteDatabase(database) {
    const confirmed = await confirmDialog({
      title: `Delete ${database.name}?`,
      message: 'This permanently deletes the database together with its query history and all of its backups. It cannot be undone — download a copy first if you might need it again.',
      confirmLabel: 'Delete database',
      danger: true,
    });
    if (!confirmed) return;

    try {
      await api(`/database/${enc(database.db_id)}`, { method: 'DELETE' });
      if (database.db_id === state.dbId) clearActiveDatabase();
      await loadDatabases();
      toast(`Deleted ${database.name}`, 'ok');
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  function downloadDatabase(database) {
    // A plain link, not fetch: the browser streams the file to disk
    // instead of buffering the whole database in memory.
    const link = document.createElement('a');
    link.href = `/download/${enc(database.db_id)}`;
    link.download = '';
    document.body.appendChild(link);
    link.click();
    link.remove();
  }

  // ==================== backups ====================

  async function openBackups(database) {
    els.backupsTitle.textContent = `Backups — ${database.name}`;
    els.backupList.innerHTML = loadingHtml('Loading backups…');
    els.backupsModal.showModal();

    let backups;
    try {
      backups = (await api(`/backups/${enc(database.db_id)}`)).backups;
    } catch (e) {
      els.backupList.innerHTML = `<p class="empty-note">${esc(e.message)}</p>`;
      return;
    }

    els.backupList.innerHTML = '';
    if (!backups.length) {
      els.backupList.innerHTML = '<p class="empty-note">No backups yet. One is created automatically before the first change.</p>';
      return;
    }

    for (const backup of backups) {
      const row = document.createElement('div');
      row.className = 'backup-row';
      const when = backup.created ? new Date(backup.created).toLocaleString() : backup.name;
      row.innerHTML = `
        ${icon('clock')}
        <span class="backup-when">${esc(when)}</span>
        <span class="count">${esc(formatBytes(backup.size))}</span>
        <button type="button" class="btn btn-sm">${icon('undo')} Restore</button>`;

      row.querySelector('button').addEventListener('click', async () => {
        const confirmed = await confirmDialog({
          title: 'Restore this backup?',
          message: `${database.name} goes back to how it was on ${when}. The current state is backed up first, so you can undo the restore.`,
          confirmLabel: 'Restore',
        });
        if (confirmed && await restoreBackup(database.db_id, backup.name)) openBackups(database);
      });

      els.backupList.appendChild(row);
    }
  }

  async function restoreBackup(dbId, backupName) {
    try {
      await api(`/backups/${enc(dbId)}/restore`, { json: { name: backupName } });
      toast('Database restored from backup', 'ok');
      await afterDataChange(dbId);
      return true;
    } catch (e) {
      toast(e.message, 'error');
      return false;
    }
  }

  // ==================== schema ====================

  // Re-fetches the schema of the active database. Called after anything
  // that may have changed it, so new tables show up immediately.
  async function refreshSchema() {
    const dbId = state.dbId;
    if (!dbId) return;
    try {
      const schema = await api(`/schema/${enc(dbId)}`);
      if (dbId === state.dbId) renderSchema(schema);
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  function renderSchema(schema) {
    const names = Object.keys(schema);
    state.tables = names;
    els.tableCount.textContent = names.length;
    els.schemaTree.innerHTML = '';

    if (!names.length) {
      els.schemaTree.innerHTML = '<p class="empty-note">No tables yet. Create one, import a file, or ask the AI to create it.</p>';
    }

    for (const name of names) {
      const wrap = document.createElement('div');
      wrap.className = 'schema-table';
      wrap.classList.toggle('open', state.openTables.has(name));

      const columns = (schema[name].columns || []).map((col) => `
        <div class="schema-col">
          ${col.primary_key ? icon('key') : ''}
          <span class="col-name">${esc(col.name)}</span>
          <span class="col-type">${esc(col.type || '')}</span>
        </div>`).join('');

      wrap.innerHTML = `
        <div class="schema-row">
          <button type="button" class="icon-btn schema-toggle" aria-label="Show columns of ${esc(name)}" aria-expanded="${state.openTables.has(name)}">${icon('chevron')}</button>
          <button type="button" class="schema-name" title="Preview ${esc(name)}">
            ${icon('table')}<span>${esc(name)}</span>
            <span class="count">${(schema[name].columns || []).length}</span>
          </button>
        </div>
        <div class="schema-columns">${columns || '<p class="empty-note">No columns.</p>'}</div>`;

      const toggle = wrap.querySelector('.schema-toggle');
      toggle.addEventListener('click', () => {
        const open = wrap.classList.toggle('open');
        toggle.setAttribute('aria-expanded', open);
        if (open) state.openTables.add(name); else state.openTables.delete(name);
      });

      const nameButton = wrap.querySelector('.schema-name');
      nameButton.dataset.table = name;
      nameButton.addEventListener('click', () => {
        closeSidebar();
        openPreview(name);
      });

      els.schemaTree.appendChild(wrap);
    }

    els.schemaPanel.hidden = false;
    markPreviewedTable();
    renderSuggestions();
  }

  function markPreviewedTable() {
    const current = state.preview && state.preview.table;
    els.schemaTree.querySelectorAll('.schema-name').forEach((button) => {
      button.classList.toggle('active', button.dataset.table === current);
    });
  }

  const EMPTY_LEAD = els.emptyLead.textContent;

  function renderSuggestions() {
    const first = state.tables[0];
    els.emptyLead.textContent = first
      ? `${state.dbName} is ready. Ask a question, or start with one of these:`
      : `${state.dbName} is empty. Ask the AI to create a table, or import a file.`;
    const prompts = first
      ? ['What tables are in this database?', `Show the first 10 rows of ${first}`, `How many rows are in ${first}?`]
      : ['Create a customers table with id, name and email'];

    els.suggestions.innerHTML = '';
    for (const prompt of prompts) {
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'chip';
      chip.textContent = prompt;
      chip.addEventListener('click', () => {
        els.question.value = prompt;
        ask();
      });
      els.suggestions.appendChild(chip);
    }
    els.suggestions.hidden = false;
  }

  // ==================== table preview (right panel) ====================
  //
  // Previews render only in the right-hand panel — the chat log is never
  // touched by opening, closing or paging one.

  function closePreview() {
    state.preview = null;
    els.preview.hidden = true;
    els.app.classList.remove('preview-open');
    els.previewContent.innerHTML = '';
    markPreviewedTable();
  }

  async function openPreview(table, page = 1) {
    const dbId = state.dbId;
    if (!dbId) return;

    const sameTable = state.preview && state.preview.dbId === dbId && state.preview.table === table;
    const request = { dbId, table, page };
    state.preview = request;

    els.previewName.textContent = table;
    if (!sameTable) {
      els.previewCount.textContent = '';
      els.previewPagination.innerHTML = '';
      els.previewContent.innerHTML = loadingHtml('Loading rows…');
    }
    els.preview.hidden = false;
    els.app.classList.add('preview-open');
    markPreviewedTable();

    let data;
    try {
      data = await api(`/table/${enc(dbId)}/${enc(table)}?page=${page}&limit=${PREVIEW_PAGE_SIZE}`);
    } catch (e) {
      if (state.preview === request) {
        els.previewContent.innerHTML = `<p class="empty-note">${esc(e.message)}</p>`;
      }
      return;
    }

    // Closed, paged again, or the database changed while this was in
    // flight — don't paint stale rows.
    if (state.preview !== request) return;

    els.previewCount.textContent = plural(data.total_rows, 'row');
    els.previewContent.innerHTML = data.rows.length
      ? resultTable(data.columns, data.rows)
      : '<p class="empty-note">No rows in this table.</p>';
    els.previewContent.scrollTop = 0;

    const from = (data.page - 1) * data.limit + 1;
    renderPager(els.previewPagination, {
      from: data.rows.length ? from : 0,
      to: from + data.rows.length - 1,
      total: data.total_rows,
      onPrev: () => openPreview(table, data.page - 1),
      onNext: () => openPreview(table, data.page + 1),
    });
  }

  function exportPreviewedTable(format, button) {
    if (!state.preview) return;
    const { dbId, table } = state.preview;
    downloadFrom(`/export/table/${enc(dbId)}/${enc(table)}?format=${format}`, {}, `${table}.${format}`, button);
  }

  // ==================== create table / import ====================

  function addColumnRow() {
    const row = document.createElement('div');
    row.className = 'column-row';
    row.innerHTML = `
      <input class="column-name" type="text" placeholder="Column name" aria-label="Column name" autocomplete="off">
      <select class="column-type" aria-label="Column type">
        ${COLUMN_TYPES.map((type) => `<option value="${type}">${type}</option>`).join('')}
      </select>
      <button class="icon-btn" type="button" aria-label="Remove column">${icon('close')}</button>`;

    row.querySelector('button').addEventListener('click', () => {
      // Keep at least one column.
      if (els.columnList.children.length > 1) row.remove();
    });

    els.columnList.appendChild(row);
    return row;
  }

  function openCreateTableModal() {
    if (!state.dbId) return;
    els.tableNameInput.value = '';
    els.columnList.innerHTML = '';
    addColumnRow();
    els.createTableModal.showModal();
  }

  async function createTable(event) {
    event.preventDefault();

    const tableName = els.tableNameInput.value.trim();
    const columns = [...els.columnList.querySelectorAll('.column-row')].map((row) => ({
      name: row.querySelector('.column-name').value.trim(),
      type: row.querySelector('.column-type').value,
    }));

    if (!tableName) return;
    if (columns.some((column) => !column.name)) {
      toast('Every column needs a name.', 'error');
      return;
    }

    els.submitCreateTable.disabled = true;
    try {
      // Creating a table never changes which database is active.
      const data = await api('/table/create', {
        json: { db_id: state.dbId, table_name: tableName, columns },
      });
      els.createTableModal.close();
      renderSchema(data.schema);
      await loadDatabases();
      toast(`Created table ${tableName}`, 'ok');
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      els.submitCreateTable.disabled = false;
    }
  }

  function openImportModal() {
    if (!state.dbId) return;
    els.importForm.reset();
    els.importModal.showModal();
  }

  async function importFile(event) {
    event.preventDefault();

    const file = els.importFileInput.files[0];
    const tableName = els.importTableName.value.trim();
    if (!file || !tableName) return;

    const body = new FormData();
    body.append('db_id', state.dbId);
    body.append('table_name', tableName);
    body.append('mode', els.importMode.value);
    body.append('file', file);

    els.submitImport.disabled = true;
    try {
      const data = await api('/import/table', { body });
      els.importModal.close();
      toast(`Imported ${plural(data.rows, 'row')} into ${data.table}`, 'ok');
      await afterDataChange(state.dbId);
    } catch (e) {
      toast(e.message, 'error');
    } finally {
      els.submitImport.disabled = false;
    }
  }

  // ==================== AI chat ====================

  function autoGrowQuestion() {
    els.question.style.height = 'auto';
    els.question.style.height = `${Math.min(els.question.scrollHeight, 160)}px`;
  }

  function startEntry(text) {
    els.emptyState.remove();

    const entry = document.createElement('div');
    entry.className = 'entry';

    const question = document.createElement('div');
    question.className = 'entry-question';
    question.textContent = text;

    const card = document.createElement('div');
    card.className = 'card';
    card.innerHTML = loadingHtml('Writing SQL…');

    entry.append(question, card);
    els.log.appendChild(entry);
    els.log.scrollTop = els.log.scrollHeight;
    return { entry, card };
  }

  async function ask() {
    const text = els.question.value.trim();
    if (!state.dbId || !text || state.asking) return;

    state.asking = true;
    els.question.value = '';
    autoGrowQuestion();
    els.askBtn.disabled = true;

    const { entry, card } = startEntry(text);
    const ctx = { dbId: state.dbId, source: 'chat', question: text };

    try {
      const data = await api('/query', {
        json: { db_id: ctx.dbId, question: text, session_id: state.sessionId },
      });

      // Keep whatever session id the server settled on.
      if (data.session_id && data.session_id !== state.sessionId) {
        state.sessionId = data.session_id;
        store.set(SESSION_KEY, data.session_id);
      }

      renderResult(card, data, ctx);
    } catch (e) {
      messageCard(card, 'error', 'Error', e.message);
    } finally {
      state.asking = false;
      els.askBtn.disabled = false;
      entry.scrollIntoView({ block: 'start', behavior: 'smooth' });
      els.question.focus();
      if (ctx.dbId === state.dbId) loadHistory();
    }
  }

  // Starts a fresh conversation: the AI stops resolving follow-ups against
  // earlier questions. History is kept — it lives in the History tab.
  function newChat() {
    state.sessionId = newSessionId();
    charts.forEach((chart) => chart.destroy());
    charts.clear();
    els.log.innerHTML = '';
    els.log.appendChild(els.emptyState);
    setMode('chat');
    toast('New conversation started', 'ok');
  }

  // ==================== SQL workspace ====================
  //
  // Manual SQL editor. Talks only to /sql/execute, and to the shared
  // /confirm endpoint for anything that needs confirmation.

  function setMode(mode) {
    const workspace = mode === 'workspace';

    els.tabChat.classList.toggle('active', !workspace);
    els.tabWorkspace.classList.toggle('active', workspace);
    els.tabChat.setAttribute('aria-selected', !workspace);
    els.tabWorkspace.setAttribute('aria-selected', workspace);

    els.chatView.hidden = workspace;
    els.sqlWorkspace.hidden = !workspace;

    if (state.dbId) (workspace ? els.sqlEditor : els.question).focus();
  }

  function setWorkspaceStatus(text, kind = '') {
    els.workspaceStatus.textContent = text;
    els.workspaceStatus.className = `status-pill ${kind}`;
  }

  // Fills the editor and switches to the Workspace tab — never executes.
  function openInSqlWorkspace(sqlText) {
    if (!state.dbId) {
      toast('Open or select a database first.', 'error');
      return;
    }
    setMode('workspace');
    els.sqlEditor.value = sqlText;
    els.sqlEditor.focus();
    els.sqlEditor.setSelectionRange(sqlText.length, sqlText.length);
    setWorkspaceStatus('Ready');
  }

  async function formatSqlEditor() {
    const sql = els.sqlEditor.value.trim();
    if (!sql) return;
    try {
      els.sqlEditor.value = (await api('/sql/format', { json: { sql } })).sql;
      setWorkspaceStatus('Formatted');
    } catch (e) {
      setWorkspaceStatus('Could not format', 'error');
      toast(e.message, 'error');
    }
  }

  async function runSqlWorkspace() {
    const sqlText = els.sqlEditor.value.trim();
    if (!state.dbId || els.runSqlBtn.disabled) return;
    if (!sqlText) {
      setWorkspaceStatus('Enter a SQL statement first', 'error');
      return;
    }

    // Snapshot the database this run was issued against. If the user
    // switches databases mid-request, the response is dropped rather
    // than rendered under the wrong database.
    const ctx = { dbId: state.dbId, source: 'workspace' };
    const stillActive = () => ctx.dbId === state.dbId;

    els.runSqlBtn.disabled = true;
    setWorkspaceStatus('Running…', 'loading');

    const card = document.createElement('div');
    card.className = 'card';

    try {
      const data = await api('/sql/execute', { json: { db_id: ctx.dbId, sql: sqlText } });
      if (!stillActive()) return;

      renderResult(card, data, ctx);

      if (data.status === 'executed') {
        const rows = (data.results || []).reduce((sum, r) => sum + (r.rows ? r.rows.length : 0), 0);
        setWorkspaceStatus(`Success — ${plural(rows, 'row')} · ${data.execution_time_ms} ms`, 'success');
      } else if (data.status === 'confirmation_required') {
        setWorkspaceStatus('Confirmation required', 'pending');
      } else if (data.status === 'rejected') {
        setWorkspaceStatus('Blocked by safety policy', 'error');
      } else {
        setWorkspaceStatus('Execution failed', 'error');
      }
    } catch (e) {
      if (!stillActive()) return;
      messageCard(card, 'error', 'Error', e.message);
      setWorkspaceStatus('Request failed', 'error');
    } finally {
      els.runSqlBtn.disabled = !state.dbId;
    }

    els.workspaceResults.prepend(card);
    loadHistory();
  }

  // ==================== query history ====================

  const STATUS_LABELS = {
    executed: 'Executed',
    confirmation_required: 'Awaiting confirmation',
    cancelled: 'Cancelled',
    rejected: 'Rejected',
    error: 'Error',
  };

  async function loadHistory() {
    const dbId = state.dbId;
    if (!dbId) return;

    const { status, search, offset } = state.history;
    const request = ++state.history.request;

    const params = new URLSearchParams({ limit: HISTORY_PAGE_SIZE, offset });
    if (search) params.set('search', search);
    if (status === 'starred') params.set('starred', 'true');
    else if (status !== 'all') params.set('status', status);

    let data;
    try {
      data = await api(`/history/${enc(dbId)}?${params}`);
    } catch (e) {
      if (request === state.history.request) {
        els.historyList.innerHTML = `<p class="empty-note">${esc(e.message)}</p>`;
      }
      return;
    }

    // A newer request (fast typing, database switch) already superseded this one.
    if (request !== state.history.request || dbId !== state.dbId) return;

    // The page we were on no longer exists (e.g. the filter changed).
    if (!data.items.length && offset > 0) {
      state.history.offset = 0;
      loadHistory();
      return;
    }

    renderHistory(data.items, data.total);
  }

  function renderHistory(items, total) {
    const filtered = state.history.search || state.history.status !== 'all';
    els.historyTotalCount.textContent = filtered ? '' : (total || '');
    els.historyList.innerHTML = '';

    if (!items.length) {
      els.historyList.innerHTML = `<p class="empty-note">${filtered ? 'Nothing matches.' : 'No queries yet.'}</p>`;
      els.historyPagination.innerHTML = '';
      return;
    }

    for (const item of items) {
      const sql = (item.sql || [])[0];
      const status = STATUS_LABELS[item.status] || item.status || '';

      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'history-item';
      button.innerHTML = `
        <span class="status-dot ${esc(item.status || '')}"></span>
        <span class="history-body">
          <span class="history-title ${item.question ? '' : 'is-sql'}">${esc(item.question || sql || 'Untitled query')}</span>
          <span class="history-meta">${esc([formatWhen(item.timestamp), item.question ? 'AI' : 'SQL', status].filter(Boolean).join(' · '))}</span>
        </span>
        ${item.starred ? icon('star', 'icon-fill starred') : ''}`;
      button.addEventListener('click', () => showHistoryDetail(item.history_id));
      els.historyList.appendChild(button);
    }

    const { offset } = state.history;
    renderPager(els.historyPagination, {
      from: offset + 1,
      to: offset + items.length,
      total,
      onPrev: () => { state.history.offset = Math.max(0, offset - HISTORY_PAGE_SIZE); loadHistory(); },
      onNext: () => { state.history.offset = offset + HISTORY_PAGE_SIZE; loadHistory(); },
    });
  }

  async function showHistoryDetail(historyId) {
    let record;
    try {
      record = await api(`/history/detail/${enc(historyId)}`);
    } catch (e) {
      toast(e.message, 'error');
      return;
    }

    state.historyDetail = record;
    const sql = record.sql || [];

    const rows = [
      ['Status', `<span class="badge ${esc(record.status)}">${esc(STATUS_LABELS[record.status] || record.status)}</span>`],
      ['When', esc(new Date(record.timestamp).toLocaleString())],
      ['Database', esc(record.database_name || '')],
      ['Source', record.question ? 'AI Chat' : 'SQL Workspace'],
    ];
    if (record.question) rows.push(['Question', esc(record.question)]);
    if (record.standalone_question && record.standalone_question !== record.question) {
      rows.push(['Interpreted as', esc(record.standalone_question)]);
    }
    if (sql.length) rows.push(['SQL', sqlBlocks(sql)]);
    if (record.row_count !== null && record.row_count !== undefined) rows.push(['Rows', esc(record.row_count)]);
    if (record.execution_time_ms !== null && record.execution_time_ms !== undefined) {
      rows.push(['Time', `${esc(record.execution_time_ms)} ms`]);
    }
    if (record.chart_type) rows.push(['Chart', esc(record.chart_type)]);
    if (record.explanation) rows.push([record.status === 'executed' ? 'Answer' : 'Details', esc(record.explanation)]);

    els.historyDetailBody.innerHTML =
      `<dl class="detail-grid">${rows.map(([label, value]) => `<dt>${label}</dt><dd>${value}</dd>`).join('')}</dl>`;
    els.historyDetailOpenSql.disabled = !sql.length;
    els.historyDetailReuse.disabled = !record.question;
    showStarState();
    els.historyDetailModal.showModal();
  }

  function showStarState() {
    const starred = Boolean(state.historyDetail && state.historyDetail.starred);
    els.historyDetailStar.classList.toggle('is-starred', starred);
    els.historyDetailStarLabel.textContent = starred ? 'Starred' : 'Star';
  }

  async function toggleStar() {
    const record = state.historyDetail;
    if (!record) return;
    try {
      const data = await api(`/history/star/${enc(record.history_id)}`, { json: { starred: !record.starred } });
      record.starred = data.starred;
      showStarState();
      loadHistory();
    } catch (e) {
      toast(e.message, 'error');
    }
  }

  // ==================== sidebar / theme / dialogs ====================

  function setSidePane(pane) {
    const history = pane === 'history';
    els.sideTabExplorer.classList.toggle('active', !history);
    els.sideTabHistory.classList.toggle('active', history);
    els.sideTabExplorer.setAttribute('aria-selected', !history);
    els.sideTabHistory.setAttribute('aria-selected', history);
    els.paneExplorer.hidden = history;
    els.paneHistory.hidden = !history;
  }

  function openSidebar() {
    els.sidebar.classList.add('open');
    els.scrim.hidden = false;
  }

  function closeSidebar() {
    els.sidebar.classList.remove('open');
    els.scrim.hidden = true;
  }

  function toggleTheme() {
    const theme = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    document.documentElement.dataset.theme = theme;
    store.set(THEME_KEY, theme);
    charts.forEach((chart) => {
      paintChart(chart.config.type, chart.data, chart.options);
      chart.update();
    });
  }

  // ---- floating menu (account menu, database actions) ----

  let menuAnchor = null;

  function closeMenu() {
    if (menuAnchor) menuAnchor.setAttribute('aria-expanded', 'false');
    menuAnchor = null;
    els.menu.hidden = true;
    els.menu.innerHTML = '';
  }

  // items: [{ label, icon, danger?, onSelect }, 'divider', …]
  function openMenu(anchor, items) {
    const reopening = menuAnchor === anchor;
    closeMenu();
    if (reopening) return;

    for (const item of items) {
      if (item === 'divider') {
        const divider = document.createElement('div');
        divider.className = 'menu-divider';
        els.menu.appendChild(divider);
        continue;
      }
      const button = document.createElement('button');
      button.type = 'button';
      button.className = `menu-item${item.danger ? ' danger' : ''}`;
      button.setAttribute('role', 'menuitem');
      button.innerHTML = `${icon(item.icon)}<span>${esc(item.label)}</span>`;
      button.addEventListener('click', () => {
        closeMenu();
        item.onSelect();
      });
      els.menu.appendChild(button);
    }

    menuAnchor = anchor;
    anchor.setAttribute('aria-expanded', 'true');
    els.menu.hidden = false;

    // Below the anchor if it fits, otherwise above; never off-screen.
    const a = anchor.getBoundingClientRect();
    const m = els.menu.getBoundingClientRect();
    const top = a.bottom + 6 + m.height > window.innerHeight - 8 ? a.top - m.height - 6 : a.bottom + 6;
    els.menu.style.top = `${Math.max(8, top)}px`;
    els.menu.style.left = `${Math.max(8, Math.min(a.left, window.innerWidth - m.width - 8))}px`;
    els.menu.querySelector('.menu-item').focus();
  }

  // ---- promise-based dialogs ----

  // Shows a modal and resolves once it is answered (via `wire`) or
  // dismissed (null). Dismissal is read from the user's own action —
  // Escape, the backdrop, a [data-close] button — rather than from the
  // dialog's "close" event: browsers deliver that with the next painted
  // frame, which never comes while the tab is in the background.
  function awaitDialog(dialog, wire) {
    return new Promise((resolve) => {
      const listeners = new AbortController();
      const { signal } = listeners;
      const settle = (value) => {
        listeners.abort();
        resolve(value);
      };

      dialog.addEventListener('cancel', () => settle(null), { signal });
      dialog.addEventListener('click', (event) => {
        if (event.target === dialog || event.target.closest('[data-close]')) settle(null);
      }, { signal });
      wire(settle, signal);

      dialog.showModal();
    });
  }

  async function confirmDialog({ title, message, confirmLabel, danger = false }) {
    els.confirmTitle.textContent = title;
    els.confirmMessage.textContent = message;
    els.confirmAccept.textContent = confirmLabel;
    els.confirmAccept.className = `btn ${danger ? 'btn-danger' : 'btn-primary'}`;

    const answer = await awaitDialog(els.confirmModal, (settle, signal) => {
      els.confirmAccept.addEventListener('click', () => {
        els.confirmModal.close();
        settle(true);
      }, { signal });
    });
    return answer === true;
  }

  // Resolves with the trimmed text, or null if the dialog was dismissed.
  function promptDialog({ title, label, value = '', placeholder = '', hint = '', submitLabel }) {
    els.promptTitle.textContent = title;
    els.promptLabel.textContent = label;
    els.promptInput.value = value;
    els.promptInput.placeholder = placeholder;
    els.promptHint.textContent = hint;
    els.promptSubmit.textContent = submitLabel;

    const answer = awaitDialog(els.promptModal, (settle, signal) => {
      els.promptForm.addEventListener('submit', (event) => {
        event.preventDefault();
        els.promptModal.close();
        settle(els.promptInput.value.trim() || null);
      }, { signal });
    });
    els.promptInput.select();
    return answer;
  }

  // ---- account ----

  function openUserMenu() {
    const light = document.documentElement.dataset.theme === 'light';
    openMenu(els.userMenuBtn, [
      { label: light ? 'Dark theme' : 'Light theme', icon: light ? 'moon' : 'sun', onSelect: toggleTheme },
      { label: 'Change password', icon: 'lock', onSelect: openPasswordModal },
      'divider',
      { label: 'Sign out', icon: 'logout', onSelect: signOut },
    ]);
  }

  function openPasswordModal() {
    els.passwordForm.reset();
    showFormError(els.passwordError, '');
    els.passwordModal.showModal();
  }

  async function changePassword(event) {
    event.preventDefault();
    if (els.newPassword.value.length < 8) {
      showFormError(els.passwordError, 'The new password must be at least 8 characters.');
      return;
    }

    els.submitPassword.disabled = true;
    try {
      await api('/auth/password', {
        json: { current_password: els.currentPassword.value, new_password: els.newPassword.value },
      });
      els.passwordModal.close();
      els.passwordForm.reset();
      toast('Password updated', 'ok');
    } catch (e) {
      showFormError(els.passwordError, e.message);
    } finally {
      els.submitPassword.disabled = false;
    }
  }

  async function signOut() {
    try {
      await api('/auth/signout', { method: 'POST' });
    } catch (e) {
      toast(e.message, 'error');
      return;
    }
    // A full reload drops everything this page was holding for the user —
    // chat log, results, charts — and lands on the sign-in screen.
    window.location.reload();
  }

  // ---- sign in / create account ----

  const auth = { mode: 'signin', signupEnabled: true, firstRun: false };

  function setAuthMode(mode) {
    auth.mode = mode;
    const signup = mode === 'signup';

    els.authTitle.textContent = signup ? 'Create your account' : 'Sign in';
    els.authSubtitle.textContent = !signup
      ? 'Welcome back. Enter your details to continue.'
      : auth.firstRun
        ? 'Set up the first account on this server. Any databases already here will belong to it.'
        : 'Your databases, history and backups stay private to your account.';
    els.authNameField.hidden = !signup;
    els.authPasswordHint.hidden = !signup;
    els.authPassword.autocomplete = signup ? 'new-password' : 'current-password';
    els.authSubmit.textContent = signup ? 'Create account' : 'Sign in';
    els.authSwitchText.textContent = signup ? 'Already have an account?' : 'New here?';
    els.authSwitch.textContent = signup ? 'Sign in' : 'Create an account';
    // With sign-ups closed there is nothing to switch to from "Sign in".
    els.authSwitchRow.hidden = !auth.signupEnabled || auth.firstRun;
    showFormError(els.authError, '');
  }

  function showAuth(status) {
    auth.signupEnabled = status.signup_enabled;
    auth.firstRun = status.first_run;
    els.app.hidden = true;
    els.authView.hidden = false;
    setAuthMode(status.first_run ? 'signup' : 'signin');
    (els.authNameField.hidden ? els.authEmail : els.authName).focus();
  }

  async function submitAuth(event) {
    event.preventDefault();
    const signup = auth.mode === 'signup';
    const name = els.authName.value.trim();
    const email = els.authEmail.value.trim();
    const password = els.authPassword.value;

    if (signup && !name) return showFormError(els.authError, 'Enter your name.');
    if (!email || !password) return showFormError(els.authError, 'Enter your email and password.');
    if (signup && password.length < 8) return showFormError(els.authError, 'Password must be at least 8 characters.');

    els.authSubmit.disabled = true;
    showFormError(els.authError, '');
    try {
      const data = await api(signup ? '/auth/signup' : '/auth/signin', {
        json: signup ? { name, email, password } : { email, password },
      });
      els.authForm.reset();
      enterApp(data.user);
    } catch (e) {
      showFormError(els.authError, e.message);
    } finally {
      els.authSubmit.disabled = false;
    }
  }

  function enterApp(user) {
    state.user = user;

    // A different account on this browser must not inherit the previous
    // one's active database or conversation.
    if (store.get(USER_KEY) !== user.id) {
      store.set(USER_KEY, user.id);
      store.remove(ACTIVE_DB_KEY);
      state.sessionId = newSessionId();
    }

    const initials = user.name.split(/\s+/).filter(Boolean).slice(0, 2).map((part) => part[0]).join('');
    els.userAvatar.textContent = (initials || user.email[0]).toUpperCase();
    els.userName.textContent = user.name;
    els.userEmail.textContent = user.email;

    els.authView.hidden = true;
    els.app.hidden = false;

    // The backend remembers databases across restarts — list them and
    // re-select the one this browser used last.
    loadDatabases({ restore: true });
  }

  async function boot() {
    try {
      const status = await api('/auth/status');
      if (status.authenticated) enterApp(status.user);
      else showAuth(status);
    } catch (e) {
      showAuth({ signup_enabled: false, first_run: false });
      showFormError(els.authError, e.message);
    }
  }

  // ---- click-to-sort result tables ----

  // Sorts the rows currently on screen (a preview page, or the rows a
  // query returned) — it does not re-run the query.
  function sortTable(header) {
    const table = header.closest('table');
    const body = table.tBodies[0];
    const index = [...header.parentNode.children].indexOf(header);
    const direction = header.dataset.sort === 'asc' ? 'desc' : 'asc';
    const numeric = header.classList.contains('num');

    table.querySelectorAll('th').forEach((th) => { delete th.dataset.sort; });
    header.dataset.sort = direction;

    const key = (row) => {
      const cell = row.cells[index];
      if (!cell || cell.classList.contains('null')) return null;
      return numeric ? Number(cell.textContent) : cell.textContent;
    };

    const rows = [...body.rows].sort((a, b) => {
      const x = key(a);
      const y = key(b);
      if (x === null || y === null) return (x === null) - (y === null);   // NULLs last
      const order = numeric ? x - y : x.localeCompare(y, undefined, { numeric: true, sensitivity: 'base' });
      return direction === 'asc' ? order : -order;
    });
    body.append(...rows);
  }

  // ==================== wiring ====================

  els.authForm.addEventListener('submit', submitAuth);
  els.authSwitch.addEventListener('click', () => {
    setAuthMode(auth.mode === 'signup' ? 'signin' : 'signup');
  });
  els.userMenuBtn.addEventListener('click', openUserMenu);
  els.passwordForm.addEventListener('submit', changePassword);
  els.newChatBtn.addEventListener('click', newChat);
  els.formatSqlBtn.addEventListener('click', formatSqlEditor);
  els.historyDetailStar.addEventListener('click', toggleStar);

  els.menuBtn.addEventListener('click', openSidebar);
  els.sidebarClose.addEventListener('click', closeSidebar);
  els.scrim.addEventListener('click', closeSidebar);
  els.sideTabExplorer.addEventListener('click', () => setSidePane('explorer'));
  els.sideTabHistory.addEventListener('click', () => setSidePane('history'));

  els.uploadBtn.addEventListener('click', () => els.fileInput.click());
  els.fileInput.addEventListener('change', () => {
    if (els.fileInput.files[0]) uploadFile(els.fileInput.files[0]);
  });

  els.newDbBtn.addEventListener('click', createDatabase);

  els.createTableBtn.addEventListener('click', openCreateTableModal);
  els.addColumnBtn.addEventListener('click', () => addColumnRow().querySelector('input').focus());
  els.createTableForm.addEventListener('submit', createTable);

  els.importBtn.addEventListener('click', openImportModal);
  els.importForm.addEventListener('submit', importFile);
  els.importFileInput.addEventListener('change', () => {
    const file = els.importFileInput.files[0];
    if (file && !els.importTableName.value) {
      els.importTableName.value = file.name.replace(/\.(csv|xlsx)$/i, '').replace(/\W+/g, '_').replace(/^(\d)/, 't_$1');
    }
  });

  els.tabChat.addEventListener('click', () => setMode('chat'));
  els.tabWorkspace.addEventListener('click', () => setMode('workspace'));

  els.composer.addEventListener('submit', (event) => {
    event.preventDefault();
    ask();
  });
  els.question.addEventListener('input', autoGrowQuestion);
  els.question.addEventListener('keydown', (event) => {
    // Enter sends; Shift+Enter adds a line. Ignore Enter mid-IME-composition.
    if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      ask();
    }
  });

  els.runSqlBtn.addEventListener('click', runSqlWorkspace);
  els.clearSqlBtn.addEventListener('click', () => {
    els.sqlEditor.value = '';
    els.workspaceResults.innerHTML = '';
    setWorkspaceStatus('Ready');
    els.sqlEditor.focus();
  });
  els.sqlEditor.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault();
      runSqlWorkspace();
    }
  });

  els.closePreview.addEventListener('click', closePreview);
  els.previewExportCsv.addEventListener('click', () => exportPreviewedTable('csv', els.previewExportCsv));
  els.previewExportXlsx.addEventListener('click', () => exportPreviewedTable('xlsx', els.previewExportXlsx));
  els.previewOpenWorkspace.addEventListener('click', () => {
    if (!state.preview) return;
    // Builds the SELECT and opens it in the SQL Workspace WITHOUT running it.
    openInSqlWorkspace(`SELECT *\nFROM ${quoteIdentIfNeeded(state.preview.table)}\nLIMIT 50;`);
  });

  let historySearchTimer;
  els.historySearch.addEventListener('input', () => {
    clearTimeout(historySearchTimer);
    historySearchTimer = setTimeout(() => {
      state.history.search = els.historySearch.value.trim();
      state.history.offset = 0;
      loadHistory();
    }, 250);
  });

  els.historyFilters.addEventListener('click', (event) => {
    const chip = event.target.closest('.chip');
    if (!chip) return;
    els.historyFilters.querySelectorAll('.chip').forEach((c) => c.classList.toggle('active', c === chip));
    state.history.status = chip.dataset.status;
    state.history.offset = 0;
    loadHistory();
  });

  els.historyDetailOpenSql.addEventListener('click', () => {
    const sql = (state.historyDetail && state.historyDetail.sql) || [];
    els.historyDetailModal.close();
    closeSidebar();
    openInSqlWorkspace(sql.join(';\n\n'));
  });

  els.historyDetailReuse.addEventListener('click', () => {
    const question = (state.historyDetail && state.historyDetail.question) || '';
    els.historyDetailModal.close();
    closeSidebar();
    setMode('chat');
    els.question.value = question;
    autoGrowQuestion();
    els.question.focus();
  });

  // Dialogs: any [data-close] button and a click on the dimmed backdrop
  // close them (Escape is handled natively by <dialog>).
  document.querySelectorAll('dialog').forEach((dialog) => {
    dialog.addEventListener('click', (event) => {
      if (event.target === dialog || event.target.closest('[data-close]')) dialog.close();
    });
  });

  document.addEventListener('click', (event) => {
    const target = event.target;

    if (menuAnchor && !els.menu.contains(target) && !menuAnchor.contains(target)) closeMenu();

    const copy = target.closest('[data-copy]');
    if (copy) copyText(copy.dataset.copy, copy);

    const header = target.closest('.result-table th');
    if (header) sortTable(header);
  });

  document.addEventListener('keydown', (event) => {
    if (event.key !== 'Escape') return;
    if (menuAnchor) closeMenu();
    else if (!document.querySelector('dialog[open]')) closeSidebar();
  });

  window.addEventListener('resize', closeMenu);

  boot();
})();
