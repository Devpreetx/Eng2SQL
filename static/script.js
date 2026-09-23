console.log("SCRIPT JS LOADED");

(() => {
  let dbId = null;

  // -------------------- session (conversation memory) --------------------
  // Maintains a session_id for follow-up-question memory. Reused across
  // page refreshes via localStorage; sent with every /query request.
  // The backend generates one if missing, so this is a convenience, not
  // a hard requirement — but reusing it lets the browser stay pinned to
  // the same conversation across reloads.
  const SESSION_STORAGE_KEY = 'eng2sql_session_id';

  function getOrCreateSessionId() {
    let sid = null;
    try {
      sid = localStorage.getItem(SESSION_STORAGE_KEY);
    } catch (e) {
      console.warn("localStorage unavailable, session_id will not persist across reloads:", e);
    }
    if (!sid) {
      sid = (crypto.randomUUID ? crypto.randomUUID() : `sess-${Date.now()}-${Math.random().toString(36).slice(2)}`);
      try {
        localStorage.setItem(SESSION_STORAGE_KEY, sid);
      } catch (e) {
        // Non-fatal — session just won't survive a refresh this time.
      }
    }
    return sid;
  }

  let sessionId = getOrCreateSessionId();
  console.log("SESSION ID:", sessionId);

  // Upload lifecycle is separate from conversation memory. Chat memory keeps
  // using localStorage, while temporary uploaded files are tied to this
  // browser tab and cleaned up when the page is refreshed/unloaded.
  const UPLOAD_SESSION_STORAGE_KEY = 'eng2sql_upload_session_id';

  function getOrCreateUploadSessionId() {
    let sid = null;
    try {
      sid = sessionStorage.getItem(UPLOAD_SESSION_STORAGE_KEY);
    } catch (e) {
      console.warn('sessionStorage unavailable for upload lifecycle:', e);
    }

    if (!sid) {
      sid = (crypto.randomUUID ? crypto.randomUUID() :
        `upload-${Date.now()}-${Math.random().toString(36).slice(2)}`);
      try {
        sessionStorage.setItem(UPLOAD_SESSION_STORAGE_KEY, sid);
      } catch (e) {}
    }
    return sid;
  }

  const uploadSessionId = getOrCreateUploadSessionId();
  console.log('UPLOAD SESSION ID:', uploadSessionId);

  // Fires during a normal browser refresh as well as navigation away.
  // sendBeacon is designed for this last-moment cleanup request.
  function cleanupTemporaryUploads() {
    if (!uploadSessionId) return;

    const payload = JSON.stringify({ upload_session_id: uploadSessionId });
    const blob = new Blob([payload], { type: 'application/json' });

    try {
      if (navigator.sendBeacon('/session/cleanup', blob)) return;
    } catch (e) {}

    try {
      fetch('/session/cleanup', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: payload,
        keepalive: true,
      }).catch(() => {});
    } catch (e) {}
  }

  window.addEventListener('pagehide', cleanupTemporaryUploads);

const uploadBtn   = document.getElementById('uploadBtn');
const newDbBtn    = document.getElementById('newDbBtn');
const fileInput   = document.getElementById('fileInput');
  const importBtn = document.getElementById('importBtn');
  const importFileInput = document.getElementById('importFileInput');

const createTableBtn    = document.getElementById('createTableBtn');
const createTableModal  = document.getElementById('createTableModal');
const closeCreateTable  = document.getElementById('closeCreateTable');
const cancelCreateTable = document.getElementById('cancelCreateTable');
const submitCreateTable = document.getElementById('submitCreateTable');
const addColumnBtn      = document.getElementById('addColumnBtn');
const columnList        = document.getElementById('columnList');
const tableNameInput    = document.getElementById('tableNameInput');
  const uploadStat  = document.getElementById('uploadStatus');
  const schemaPanel = document.getElementById('schemaPanel');
  const schemaTree  = document.getElementById('schemaTree');
  const tableCount  = document.getElementById('tableCount');
  const connDot     = document.getElementById('connDot');
  const connLabel   = document.getElementById('connLabel');
const historyList = document.getElementById('historyList');
const historySearch = document.getElementById('historySearch');
  const databaseList  = document.getElementById('databaseList');
  const databaseCount = document.getElementById('databaseCount');

  const log         = document.getElementById('log');
  const emptyState  = document.getElementById('emptyState');
  const question    = document.getElementById('question');
  const askBtn      = document.getElementById('askBtn');
  const promptBar   = document.querySelector('.prompt-bar');

  const appEl            = document.querySelector('.app');
  const tablePreviewPanel = document.getElementById('tablePreviewPanel');

  // -------------------- SQL Workspace elements --------------------
  const tabChat        = document.getElementById('tabChat');
  const tabWorkspace    = document.getElementById('tabWorkspace');
  const sqlWorkspace     = document.getElementById('sqlWorkspace');
  const workspaceDbTag   = document.getElementById('workspaceDbTag');
  const sqlEditor        = document.getElementById('sqlEditor');
  const runSqlBtn        = document.getElementById('runSqlBtn');
  const clearSqlBtn      = document.getElementById('clearSqlBtn');
  const workspaceStatus  = document.getElementById('workspaceStatus');
  const workspaceResults = document.getElementById('workspaceResults');

  // Null-check every DOM element this script depends on before wiring
  // listeners, so a missing element fails loudly instead of throwing
  // deep inside a click handler.
  const requiredEls = {
    uploadBtn, newDbBtn, fileInput, importBtn, importFileInput, createTableBtn, createTableModal,
    closeCreateTable, cancelCreateTable, submitCreateTable, addColumnBtn,
    columnList, tableNameInput, uploadStat, schemaPanel, schemaTree,
    tableCount, connDot, connLabel, databaseList, log, emptyState,
    tablePreviewPanel,
    question, askBtn,
    tabChat, tabWorkspace, sqlWorkspace, workspaceDbTag, sqlEditor,
    runSqlBtn, clearSqlBtn, workspaceStatus, workspaceResults,
  };
  for (const [name, el] of Object.entries(requiredEls)) {
    if (!el) console.error(`MISSING DOM ELEMENT: ${name}`);
  }

  if (uploadBtn) uploadBtn.addEventListener('click', () => fileInput.click());
if (newDbBtn) newDbBtn.addEventListener('click', createNewDatabase);

if (createTableBtn) createTableBtn.addEventListener('click', openCreateTableModal);

if (closeCreateTable) {
  closeCreateTable.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    closeTableModal();
  });
}

if (cancelCreateTable) {
  cancelCreateTable.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    console.log("CANCEL CREATE TABLE CLICKED");
    closeTableModal();
  });
}

if (historySearch) {
    historySearch.addEventListener('input', () => {
        loadHistory();
    });
}

// Clicking the dimmed backdrop (anywhere outside the modal card itself)
// also closes it. Guarded so a missing modal element can't crash setup.
if (createTableModal) {
  createTableModal.addEventListener('click', (e) => {
    if (e.target === createTableModal) {
      closeTableModal();
    }
  });
}

if (addColumnBtn) addColumnBtn.addEventListener('click', addColumn);
if (submitCreateTable) submitCreateTable.addEventListener('click', createTable);

  if (fileInput) {
    fileInput.addEventListener('change', () => {
      if (fileInput.files[0]) upload(fileInput.files[0]);
    });
  }

  if (importBtn && importFileInput) {
    importBtn.addEventListener('click', () => importFileInput.click());
    importFileInput.addEventListener('change', async () => {
      const file = importFileInput.files[0];
      if (!file || !dbId) return;
      const tableName = prompt('Target table name (letters, numbers, underscores):', file.name.replace(/\.(csv|xlsx)$/i,'').replace(/\W+/g,'_'));
      if (!tableName) { importFileInput.value=''; return; }
      let mode = prompt('Mode: new | append | replace', 'new');
      mode = (mode || 'new').trim().toLowerCase();
      if (!['new','append','replace'].includes(mode)) { alert('Invalid mode.'); importFileInput.value=''; return; }
      const confirmImport = mode === 'new' ? true : window.confirm(`Import will modify the active database using ${mode}. Continue?`);
      if (!confirmImport) { importFileInput.value=''; return; }
      const fd = new FormData();
      fd.append('db_id', dbId); fd.append('table_name', tableName); fd.append('mode', mode); fd.append('confirm', 'true'); fd.append('file', file);
      try {
        const res = await fetch('/import/table', {method:'POST', body:fd});
        const data = await res.json();
        if (!res.ok || data.status !== 'imported') { alert(data.detail || data.message || 'Import failed.'); return; }
        alert(`Imported ${data.rows} rows into ${data.table}. Backup: ${data.backup}`);
        await refreshSchema(); await loadDatabases();
      } catch (e) { alert('Could not reach the server.'); } finally { importFileInput.value=''; }
    });
  }

  if (askBtn) askBtn.addEventListener('click', ask);
  if (question) {
    question.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !askBtn.disabled) ask();
    });
  }

  // -------------------- mode tabs (AI Chat / SQL Workspace) --------------------

  function setMode(mode) {
    const isWorkspace = mode === 'workspace';

    tabChat.classList.toggle('active', !isWorkspace);
    tabWorkspace.classList.toggle('active', isWorkspace);

    log.hidden = isWorkspace;
    if (promptBar) promptBar.hidden = isWorkspace;
    sqlWorkspace.hidden = !isWorkspace;

    if (isWorkspace) {
      updateWorkspaceDbTag();
      if (dbId) sqlEditor.focus();
    }
  }

  if (tabChat) tabChat.addEventListener('click', () => setMode('chat'));
  if (tabWorkspace) tabWorkspace.addEventListener('click', () => setMode('workspace'));

  // Central helper: every place that establishes/changes the active
  // database calls this, so dbId + UI enablement never drift apart.
  // This is the ONE place database-id state changes — upload, new
  // database, and sidebar clicks all route through it.
  function setActiveDatabase(id, label) {
    dbId = id;
    const oldSidebarActions = document.getElementById('sidebarTableActions');
    if (oldSidebarActions) oldSidebarActions.remove();
    console.log("ACTIVE DATABASE:", dbId, label || '');

    createTableBtn.disabled = false;
    question.disabled = false;
    askBtn.disabled = false;
    if (importBtn) importBtn.disabled = false;

    connDot.classList.add('live');
    if (label) connLabel.textContent = label;

    highlightActiveDatabase();

    // SQL Workspace always points at the current active database. Per
    // spec section 2: switching databases must clear any stale results
    // and never allow the editor to execute against a database it
    // wasn't pointed at.
    enableWorkspaceForActiveDb();

    loadHistory();
  }

  // Re-fetches the schema for the active database and re-renders the
  // sidebar. Called after any confirmed CREATE/INSERT/UPDATE/DELETE so
  // the sidebar reflects the change immediately (e.g. new tables show up).
  async function refreshSchema() {
    if (!dbId) return;
    try {
      const res = await fetch(`/schema/${encodeURIComponent(dbId)}`);
      if (!res.ok) {
        console.error("Schema refresh failed:", res.status);
        return;
      }
      const schema = await res.json();
      console.log("SCHEMA REFRESH:", schema);
      renderSchema(schema);
    } catch (e) {
      console.error("SCHEMA REFRESH ERROR:", e);
    }
  }

  // -------------------- databases sidebar (multi-db workspace) --------------------

  // Fetches every known database from the backend and renders the
  // sidebar list. Never touches the active dbId itself — call
  // setActiveDatabase() separately if a switch is needed.
  async function loadDatabases() {
    try {
      const res = await fetch('/databases');
      if (!res.ok) {
        console.error("Failed to load database list:", res.status);
        return;
      }
      const data = await res.json();
      console.log("DATABASE LIST:", data.databases);
      renderDatabaseList(data.databases || []);
    } catch (e) {
      console.error("DATABASE LIST ERROR:", e);
    }
  }

  function renderDatabaseList(databases) {
    databaseList.innerHTML = '';
    databaseCount.textContent = databases.length || '';

    if (!databases.length) {
      databaseList.innerHTML = `<p class="db-list-empty">No databases yet — open a file or create one.</p>`;
      return;
    }

    for (const database of databases) {
      const item = document.createElement('div');
      item.className = 'db-item';
      item.dataset.dbId = database.db_id;
      if (database.db_id === dbId) item.classList.add('active');

      const nameRow = document.createElement('div');
      nameRow.className = 'db-item-name';
      nameRow.textContent = database.name;
      nameRow.tabIndex = 0;
      nameRow.setAttribute('role', 'button');

      nameRow.addEventListener('click', () => switchDatabase(database));
      nameRow.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault();
          switchDatabase(database);
        }
      });

      item.appendChild(nameRow);

      const tablesWrap = document.createElement('div');
      tablesWrap.className = 'db-item-tables';

      const tables = database.tables || [];
      if (tables.length) {
        for (const t of tables) {
          const tEl = document.createElement('div');
          tEl.className = 'db-item-table';
          tEl.textContent = t;
          tablesWrap.appendChild(tEl);
        }
      } else {
        const tEl = document.createElement('div');
        tEl.className = 'db-item-table';
        tEl.textContent = '(empty)';
        tablesWrap.appendChild(tEl);
      }

      item.appendChild(tablesWrap);
      databaseList.appendChild(item);
    }
  }

  // Applies the .active class to whichever sidebar row matches the
  // current dbId, without re-fetching the list.
  function highlightActiveDatabase() {
    const items = databaseList.querySelectorAll('.db-item');
    items.forEach((item) => {
      item.classList.toggle('active', item.dataset.dbId === dbId);
    });
  }

  // The single central switching function. Everything that changes the
  // active database on user click goes through here.
  async function switchDatabase(database) {
    console.log("========== SWITCH DATABASE ==========");
    console.log("Switching to:", database.name, database.db_id);

    // Close the right-side table preview from the previous database —
    // its rows belong to a different db_id now and must not linger.
    closeTablePreview();
    const sidebarActions = document.getElementById('sidebarTableActions');
    if (sidebarActions) sidebarActions.remove();

    setActiveDatabase(database.db_id, `${database.name} connected`);
    setUploadStatus(`Switched to "${database.name}"`, 'ok');

    console.log("QUERY DATABASE ID:", dbId);

    await refreshSchema();
    question.focus();
  }

 // -------------------- Create --------------------

async function createNewDatabase() {

    const name = prompt("Enter database name:");

    if (!name || !name.trim()) {
        return;
    }

    try {

        newDbBtn.disabled = true;

        const res = await fetch(
            `/database/create?name=${encodeURIComponent(name.trim())}`,
            {
                method: 'POST'
            }
        );

        const data = await res.json();

        if (!res.ok) {
            alert(data.detail || "Could not create database.");
            return;
        }

        // Make this the active database
        setActiveDatabase(data.db_id, `${data.name} connected`);

        setUploadStatus(
            `Created "${data.name}"`,
            'ok'
        );

        // Empty database has no tables yet
        schemaTree.innerHTML = '';
        tableCount.textContent = '0';
        schemaPanel.hidden = false;

        // Refresh the sidebar so the new database shows up immediately.
        await loadDatabases();

        question.focus();

        console.log("NEW DATABASE:", data);

    } catch (e) {

        console.error(e);

        setUploadStatus(
            'Could not create database.',
            'error'
        );

    } finally {

        newDbBtn.disabled = false;
    }
}

// -------------------- Create Table --------------------

function openCreateTableModal() {

    if (!dbId) {
        alert("Please create or open a database first.");
        return;
    }

    if (!createTableModal) {
        console.error("openCreateTableModal(): createTableModal element not found.");
        return;
    }

    tableNameInput.value = '';

    // Reset columns
    columnList.innerHTML = '';

    addColumn();

    createTableModal.hidden = false;
    createTableModal.style.setProperty("display", "flex", "important");

    tableNameInput.focus();
}


function closeTableModal() {

    if (!createTableModal) {
        console.error("closeTableModal(): createTableModal element not found.");
        return;
    }

    console.log("CLOSING CREATE TABLE MODAL");

    createTableModal.hidden = true;
    createTableModal.style.setProperty("display", "none", "important");
}


function addColumn() {

    const row = document.createElement('div');

    row.className = 'column-row';

    row.innerHTML = `
        <input
            class="column-name"
            type="text"
            placeholder="Column name"
        >

        <select class="column-type">
            <option value="TEXT">TEXT</option>
            <option value="INTEGER">INTEGER</option>
            <option value="REAL">REAL</option>
            <option value="NUMERIC">NUMERIC</option>
            <option value="BLOB">BLOB</option>
        </select>

        <button
            class="remove-column"
            type="button">
            ×
        </button>
    `;

    row.querySelector('.remove-column')
        .addEventListener('click', () => {

            // Keep at least one column
            if (columnList.children.length > 1) {
                row.remove();
            }

        });

    columnList.appendChild(row);
}


async function createTable() {

    if (!dbId) {
        alert("No database selected.");
        return;
    }

    const tableName = tableNameInput.value.trim();

    if (!tableName) {
        alert("Enter a table name.");
        tableNameInput.focus();
        return;
    }

    const rows = columnList.querySelectorAll('.column-row');

    const columns = [];

    for (const row of rows) {

        const name = row
            .querySelector('.column-name')
            .value
            .trim();

        const type = row
            .querySelector('.column-type')
            .value;

        if (!name) {
            alert("Every column needs a name.");
            return;
        }

        columns.push({
            name: name,
            type: type
        });
    }

    if (columns.length === 0) {
        alert("Add at least one column.");
        return;
    }

    submitCreateTable.disabled = true;
    submitCreateTable.textContent = 'Creating...';

    try {

        const res = await fetch('/table/create', {
            method: 'POST',

            headers: {
                'Content-Type': 'application/json'
            },

            body: JSON.stringify({
                db_id: dbId,
                table_name: tableName,
                columns: columns
            })
        });

        const data = await res.json();

        console.log("CREATE TABLE RESPONSE:", data);

        if (!res.ok) {

            alert(
                data.detail ||
                'Could not create table.'
            );

            return;
        }

        // Keep the existing dbId — creating a table never changes it.
        // Just refresh schema and keep query controls enabled.
        renderSchema(data.schema);

        // Refresh the sidebar so the new table shows up under this db
        // without disturbing which database is active.
        await loadDatabases();

        question.disabled = false;
        askBtn.disabled = false;

        // Close modal
        closeTableModal();

        // Show success message
        setUploadStatus(
            `Created table "${tableName}"`,
            'ok'
        );

        console.log(
            `Table "${tableName}" created successfully. Active dbId:`, dbId
        );

    } catch (e) {

        console.error(
            "CREATE TABLE ERROR:",
            e
        );

        alert(
            "Could not reach the server."
        );

    } finally {

        submitCreateTable.disabled = false;
        submitCreateTable.textContent = 'Create Table';
    }
}
  // -------------------- upload --------------------

  async function upload(file) {
    const fd = new FormData();
    fd.append('file', file);

    setUploadStatus('Reading database…', null);
    uploadBtn.disabled = true;

    try {
      const res = await fetch('/upload', {
        method: 'POST',
        headers: { 'X-Upload-Session-ID': uploadSessionId },
        body: fd,
      });
      const data = await res.json();

      if (!res.ok) {
        setUploadStatus(data.detail || 'Upload failed.', 'error');
        uploadBtn.disabled = false;
        return;
      }

       setActiveDatabase(
         data.db_id,
         `${data.tables.length} table${data.tables.length === 1 ? '' : 's'} connected`
       );

      setUploadStatus(`Loaded "${file.name}"`, 'ok');
      renderSchema(data.schema);

      // Refresh the sidebar so the newly uploaded database appears.
      await loadDatabases();

      question.focus();
    } catch (e) {
      setUploadStatus('Could not reach the server.', 'error');
    } finally {
      uploadBtn.disabled = false;
      fileInput.value = '';
    }
  }

  function setUploadStatus(msg, kind) {
    uploadStat.textContent = msg;
    uploadStat.className = 'upload-status' + (kind ? ' ' + kind : '');
  }

  function renderSchema(schema) {
    schemaTree.innerHTML = '';

    const names = Object.keys(schema);
    tableCount.textContent = names.length;

    for (const name of names) {
        const table = schema[name];

        const wrap = document.createElement('div');
        wrap.className = 'schema-table';

        // Table name
        const title = document.createElement('div');
        title.className = 'schema-table-name';
        title.textContent = `▤ ${name}`;

        // Make table clickable
        title.style.cursor = 'pointer';

        title.addEventListener('click', () => {
    console.log("========== TABLE PREVIEW ==========");
    console.log("Table:", name);
    console.log("DB ID:", dbId);

    previewTable(name);
    renderSidebarTableActions(name);
});

        wrap.appendChild(title);

        // Columns
        const cols = document.createElement('div');
        cols.className = 'schema-columns';

        for (const col of table.columns) {
            const row = document.createElement('div');
            row.className = 'schema-col' + (col.primary_key ? ' pk' : '');

            const colName = document.createElement('span');
            colName.className = 'col-name';
            colName.textContent = col.name;

            const colType = document.createElement('span');
            colType.className = 'col-type';
            colType.textContent = col.type || '';

            row.appendChild(colName);
            row.appendChild(colType);

            cols.appendChild(row);
        }

        wrap.appendChild(cols);
        schemaTree.appendChild(wrap);
    }

    schemaPanel.hidden = false;
}

//----------------History------------

async function loadHistory() {
    if (!dbId) return;

    const search = historySearch
        ? historySearch.value.trim()
        : '';

    try {
        const url =
            `/history/${encodeURIComponent(dbId)}?limit=20` +
            `&search=${encodeURIComponent(search)}`;

        const res = await fetch(url);
        const data = await res.json();

        if (!res.ok) {
            console.error("History error:", data);
            return;
        }

        renderHistory(data.items || []);

    } catch (e) {
        console.error("Could not load history:", e);
    }
}
function renderHistory(items) {
    if (!historyList) {
      console.error("historyList element not found.");
      return;
    }
    historyList.innerHTML = '';

    if (!items.length) {
        historyList.innerHTML =
            `<div class="history-empty">No history yet.</div>`;
        return;
    }

    for (const item of items) {

        const el = document.createElement('div');
        el.className = 'history-item';

        el.innerHTML = `
            <div class="history-question">
                ${esc(item.question || 'Untitled query')}
            </div>

            <div class="history-meta">
                ${new Date(item.timestamp).toLocaleTimeString([], {
                    hour: '2-digit',
                    minute: '2-digit'
                })}

                <span class="history-status ${esc(item.status || '')}">
                    ${esc(item.status || '')}
                </span>
            </div>
        `;

        el.addEventListener('click', () => {
            showHistoryDetail(item.history_id);
        });

        historyList.appendChild(el);
    }
}
async function showHistoryDetail(historyId) {

    try {

        const res = await fetch(
            `/history/detail/${encodeURIComponent(historyId)}`
        );

        const data = await res.json();

        if (!res.ok) {
            alert(data.detail || 'Could not load history.');
            return;
        }

        console.log("HISTORY DETAIL:", data);

        // For now, use a simple dialog.
        const sql = (data.sql || []).join('\n\n');

        alert(
            `Question:\n${data.question}\n\n` +
            `Database:\n${data.database_name}\n\n` +
            `Status:\n${data.status}\n\n` +
            `SQL:\n${sql}`
        );

    } catch (e) {
        alert('Could not load history.');
    }
}
// -------------------- sidebar table export actions --------------------

function renderSidebarTableActions(tableName) {
    if (!schemaPanel) return;

    let panel = document.getElementById('sidebarTableActions');

    if (!panel) {
        panel = document.createElement('div');
        panel.id = 'sidebarTableActions';
        panel.style.cssText = [
            'margin-top:12px',
            'padding-top:12px',
            'border-top:1px solid var(--border, #252b36)'
        ].join(';');
        schemaPanel.appendChild(panel);
    }

    panel.innerHTML = `
        <div style="font-family:'IBM Plex Mono',monospace;font-size:10px;text-transform:uppercase;letter-spacing:.08em;color:var(--text-faint,#6b7280);margin-bottom:7px;">Table actions</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;">
            <button type="button" id="sidebarExportCsv" style="appearance:none !important;background:#10271f !important;color:#3ECF8E !important;border:1px solid #245f49 !important;border-radius:5px !important;padding:6px 9px !important;font-family:'IBM Plex Mono',monospace !important;font-size:10px !important;font-weight:500 !important;cursor:pointer !important;">↓ CSV</button>
            <button type="button" id="sidebarExportXlsx" style="appearance:none !important;background:#111f32 !important;color:#60A5FA !important;border:1px solid #31557d !important;border-radius:5px !important;padding:6px 9px !important;font-family:'IBM Plex Mono',monospace !important;font-size:10px !important;font-weight:500 !important;cursor:pointer !important;">↓ XLSX</button>
        </div>
        <div style="margin-top:6px;font-family:'IBM Plex Mono',monospace;font-size:9px;color:var(--text-faint,#6b7280);">${esc(tableName)} · full dataset</div>
    `;

    const csvBtn = document.getElementById('sidebarExportCsv');
    const xlsxBtn = document.getElementById('sidebarExportXlsx');

    if (csvBtn) csvBtn.addEventListener('click', () => exportTableFile(tableName, 'csv', csvBtn));
    if (xlsxBtn) xlsxBtn.addEventListener('click', () => exportTableFile(tableName, 'xlsx', xlsxBtn));
}

// -------------------- table preview (right-side panel) --------------------
//
// Table previews render exclusively in #tablePreviewPanel, a dedicated
// right-side aside. They are NEVER inserted into #log — the AI/query
// console area is untouched by preview open/close/paginate.

// Hides the right panel and clears its content. Used on close, on
// database switch, and whenever a preview must not linger on screen.
function closeTablePreview() {
    if (!tablePreviewPanel) {
        console.error("closeTablePreview(): tablePreviewPanel element not found.");
        return;
    }

    tablePreviewPanel.hidden = true;
    tablePreviewPanel.innerHTML = '';

    if (appEl) appEl.classList.remove('preview-open');
}

async function previewTable(tableName, page = 1) {

    if (!dbId) {
        console.error("No active database!");
        return;
    }

    if (!tablePreviewPanel) {
        console.error("previewTable(): tablePreviewPanel element not found.");
        return;
    }

    // Capture the dbId this preview was opened for, so a database
    // switch mid-pagination can't accidentally show stale/wrong rows.
    const previewDbId = dbId;

    // Build (or rebuild) the panel content for this table.
    tablePreviewPanel.innerHTML = `
        <div class="preview-header">
            <div class="preview-title">
                <strong>${esc(tableName)}</strong>
                <span id="previewCount"></span>
            </div>

            <button id="closePreview" class="preview-close" aria-label="Close table preview">
                ×
            </button>
        </div>

        <div id="previewContent">
            Loading...
        </div>

        <div id="previewPagination" class="preview-pagination"></div>

        <div class="preview-actions">
          <button type="button" class="preview-open-workspace" id="previewOpenWorkspace">⌗ Open SELECT in SQL Workspace</button>
        </div>
    `;

    tablePreviewPanel.hidden = false;
    if (appEl) appEl.classList.add('preview-open');

    document
        .getElementById('closePreview')
        .addEventListener('click', () => {
            // Closing the preview only touches the right panel — the
            // main AI chat/log area is never affected.
            closeTablePreview();
        });

    // Table preview handoff (spec section 11): builds a plain
    // `SELECT * FROM <table> LIMIT 50;` and opens it in the SQL
    // Workspace WITHOUT executing it automatically.
    const exportCsv = document.getElementById('previewExportCsv');
    const exportXlsx = document.getElementById('previewExportXlsx');
    if (exportCsv) exportCsv.addEventListener('click', () => { location.href = `/export/table/${encodeURIComponent(previewDbId)}/${encodeURIComponent(tableName)}?format=csv`; });
    if (exportXlsx) exportXlsx.addEventListener('click', () => { location.href = `/export/table/${encodeURIComponent(previewDbId)}/${encodeURIComponent(tableName)}?format=xlsx`; });

    const handoffBtn = document.getElementById('previewOpenWorkspace');
    if (handoffBtn) {
        handoffBtn.addEventListener('click', () => {
            const generatedSql = `SELECT *\nFROM ${quoteIdentIfNeeded(tableName)}\nLIMIT 50;`;
            openInSqlWorkspace(generatedSql);
        });
    }

    try {

        console.log("QUERY DATABASE ID:", previewDbId);

        const res = await fetch(
            `/table/${encodeURIComponent(previewDbId)}/${encodeURIComponent(tableName)}?page=${page}&limit=50`
        );

        const data = await res.json();

        console.log("TABLE PREVIEW RESPONSE:", data);

        // If the user switched databases while this request was in
        // flight, don't paint results from the old database.
        if (previewDbId !== dbId) {
            console.log("Preview dropped — active database changed mid-request.");
            return;
        }

        // If the panel was closed while the request was in flight, don't
        // resurrect it.
        if (tablePreviewPanel.hidden) {
            return;
        }

        if (!res.ok) {
            document.getElementById('previewContent').innerHTML =
                `<p class="no-rows">${esc(data.detail || 'Could not load table.')}</p>`;
            return;
        }

        document.getElementById('previewCount').textContent =
            `${data.total_rows} row${data.total_rows === 1 ? '' : 's'}`;

        const content = document.getElementById('previewContent');

        if (!data.rows || data.rows.length === 0) {

            content.innerHTML = `
                <p class="no-rows">No rows in this table.</p>
            `;

        } else {

            const columns = Object.keys(data.rows[0]);

            content.innerHTML = resultTable(
                columns,
                data.rows
            );
        }

        renderPagination(
            tableName,
            data.page,
            data.limit,
            data.total_rows
        );

    } catch (e) {

        console.error("TABLE PREVIEW ERROR:", e);

        const contentEl = document.getElementById('previewContent');
        if (contentEl) {
            contentEl.innerHTML = `
                <p class="no-rows">
                    Could not reach the server.
                </p>
            `;
        }
    }
}

// Quotes a table/column identifier only if it's not a simple
// alnum/underscore token, matching the backend's own quoting style.
function quoteIdentIfNeeded(name) {
    if (/^[A-Za-z_][A-Za-z0-9_]*$/.test(name)) return name;
    return `"${name.replace(/"/g, '""')}"`;
}

function renderPagination(
    tableName,
    page,
    limit,
    totalRows
) {

    const container =
        document.getElementById('previewPagination');

    if (!container) return;

    const totalPages =
        Math.ceil(totalRows / limit);

    if (totalPages <= 1) {
        container.innerHTML = '';
        return;
    }

    container.innerHTML = `
        <button
            class="preview-page-btn"
            ${page <= 1 ? 'disabled' : ''}
            id="prevPage">
            ← Previous
        </button>

        <span>
            Page ${page} of ${totalPages}
        </span>

        <button
            class="preview-page-btn"
            ${page >= totalPages ? 'disabled' : ''}
            id="nextPage">
            Next →
        </button>
    `;

    document
        .getElementById('prevPage')
        .addEventListener('click', () => {
            if (page > 1) {
                previewTable(tableName, page - 1);
            }
        });

    document
        .getElementById('nextPage')
        .addEventListener('click', () => {
            if (page < totalPages) {
                previewTable(tableName, page + 1);
            }
        });
}
  // -------------------- query --------------------

  async function ask() {
    console.log("RUN CLICKED");
    console.log("QUERY DATABASE ID:", dbId);
    console.log("SESSION ID:", sessionId);

    const q = question.value.trim();
    console.log("Current question:", q);

    if (!dbId) {
      console.warn("ask() aborted: no dbId set.");
      alert("No database selected.");
      return;
    }

    if (!q) {
      console.warn("ask() aborted: empty question.");
      return;
    }

    if (emptyState) emptyState.remove();

    question.value = '';
    askBtn.disabled = true;
    question.disabled = true;

    const entry = startEntry(q);
    const askedDbId = dbId;

    try {
      const payload = { db_id: askedDbId, question: q, session_id: sessionId };
      console.log("Sending /query payload:", payload);

      const res = await fetch('/query', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await res.json();

      console.log("/query response:", data);

      // Persist server-confirmed/generated session_id (covers the case
      // where localStorage was empty and the backend minted one, or the
      // backend rotated it for any reason).
      if (data.session_id && data.session_id !== sessionId) {
        sessionId = data.session_id;
        try {
          localStorage.setItem(SESSION_STORAGE_KEY, sessionId);
        } catch (e) {
          // Non-fatal.
        }
        console.log("SESSION ID UPDATED:", sessionId);
      }

      if (!res.ok) {
        renderError(entry, data.detail || `Server returned ${res.status}.`);
      } else {
        renderResponse(entry, data, askedDbId);
      }
    } catch (e) {
      console.error("ask() fetch error:", e);
      renderError(entry, 'Could not reach the server.');
    } finally {
      askBtn.disabled = false;
      question.disabled = false;
      question.focus();
    }
  }

  function startEntry(q) {
    const entry = document.createElement('div');
    entry.className = 'entry';

    const qLine = document.createElement('div');
    qLine.className = 'entry-question';
    qLine.textContent = q;
    entry.appendChild(qLine);

    const card = document.createElement('div');
    card.className = 'entry-card info';
    card.textContent = 'Working…';
    entry.appendChild(card);

    log.appendChild(entry);
    log.scrollTop = log.scrollHeight;
    return { entry, card };
  }

  // Renders one <pre class="sql-block"> per statement in a list.
  function sqlBlocks(statements) {
    if (!statements || !statements.length) return '';
    return statements.map(s => `<pre class="sql-block">${esc(s)}</pre>`).join('');
  }

  // -------------------- chart rendering (Phase 2) --------------------

  const chartInstances = new WeakMap();

  // Returns the HTML markup for a chart panel, or '' if no chart is
  // appropriate for this result. Purely a template — no Chart.js calls
  // happen here, since the <canvas> must exist in the DOM first.
  function chartPanelHtml(chart) {
    if (!chart || !chart.chart_required || chart.chart_type === 'none') return '';

    return `
      <div class="chart-panel">
        <div class="chart-panel-header">
          <span class="chart-panel-title">${esc(chart.chart_title || '')}</span>
          <div class="chart-panel-actions">
            <button type="button" class="chart-toggle-btn active" data-mode="chart">Chart</button>
            <button type="button" class="chart-toggle-btn" data-mode="table">Table</button>
            <button type="button" class="chart-download-btn">Download PNG</button>
          </div>
        </div>
        <div class="chart-canvas-wrap">
          <canvas class="chart-canvas"></canvas>
        </div>
        <div class="chart-kpi-value" style="display:none"></div>
      </div>
    `;
  }

  // Must be called AFTER chartPanelHtml()'s markup has been inserted into
  // the DOM (card.innerHTML = ...), since it looks up the live <canvas>.
  // Never throws out to the caller — a chart failure just removes the
  // chart panel and leaves the rest of the card (explanation + result
  // table) intact, per the "chart rendering failure must not break the
  // query result" requirement.
  function initChartPanel(card, chart) {
    if (!chart || !chart.chart_required || chart.chart_type === 'none') return;
    if (typeof Chart === 'undefined') {
      console.error('Chart.js not loaded — skipping chart render.');
      const panel = card.querySelector('.chart-panel');
      if (panel) panel.remove();
      return;
    }

    const panel = card.querySelector('.chart-panel');
    if (!panel) return;

    try {
      const canvasWrap = panel.querySelector('.chart-canvas-wrap');
      const canvas = panel.querySelector('.chart-canvas');
      const kpiEl = panel.querySelector('.chart-kpi-value');
      const chartBtn = panel.querySelector('.chart-toggle-btn[data-mode="chart"]');
      const tableBtn = panel.querySelector('.chart-toggle-btn[data-mode="table"]');
      const downloadBtn = panel.querySelector('.chart-download-btn');

      // KPI: no canvas — just a big number, built directly from real
      // result data (chart_data[0]).
      if (chart.chart_type === 'kpi') {
        canvasWrap.style.display = 'none';
        kpiEl.style.display = 'block';
        const row = (chart.chart_data && chart.chart_data[0]) || {};
        const value = chart.chart_y_column ? row[chart.chart_y_column] : Object.values(row)[0];
        kpiEl.textContent = (value === undefined || value === null) ? '—' : String(value);
        chartBtn.style.display = 'none';
        tableBtn.style.display = 'none';
        downloadBtn.style.display = 'none';
        return;
      }

      const typeMap = { bar: 'bar', line: 'line', pie: 'pie', scatter: 'scatter' };
      const chartJsType = typeMap[chart.chart_type] || 'bar';

      const labels = chartJsType === 'scatter'
        ? undefined
        : (chart.chart_data || []).map(r => r[chart.chart_x_column]);

      const dataset = chartJsType === 'scatter'
        ? {
            label: chart.chart_y_column || 'value',
            data: (chart.chart_data || []).map(r => ({
              x: r[chart.chart_x_column],
              y: r[chart.chart_y_column],
            })),
            backgroundColor: '#3ECF8E',
          }
        : {
            label: chart.chart_y_column || 'value',
            data: (chart.chart_data || []).map(r => r[chart.chart_y_column]),
            backgroundColor: chartJsType === 'pie'
              ? ['#3ECF8E', '#F2B84B', '#F0596B', '#5C6478', '#9AA3B5', '#1F4F41', '#4A3B1B', '#4A2028']
              : '#3ECF8E',
            borderColor: '#3ECF8E',
          };

      // Destroy any prior instance bound to this canvas (defensive —
      // each entry gets a fresh <canvas>, but this guards re-renders).
      const existing = chartInstances.get(canvas);
      if (existing) existing.destroy();

      const instance = new Chart(canvas, {
        type: chartJsType,
        data: { labels, datasets: [dataset] },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: chartJsType === 'pie' } },
        },
      });
      chartInstances.set(canvas, instance);

      chartBtn.addEventListener('click', () => {
        canvasWrap.style.display = 'block';
        chartBtn.classList.add('active');
        tableBtn.classList.remove('active');
      });

      tableBtn.addEventListener('click', () => {
        canvasWrap.style.display = 'none';
        tableBtn.classList.add('active');
        chartBtn.classList.remove('active');
      });

      downloadBtn.addEventListener('click', () => {
        const url = canvas.toDataURL('image/png');
        const a = document.createElement('a');
        a.href = url;
        a.download = (chart.chart_title || 'chart').trim().replace(/\s+/g, '_') + '.png';
        a.click();
      });
    } catch (e) {
      // Chart rendering must never break the query result — remove the
      // panel and let the SQL block + result table stand on their own.
      console.error('CHART RENDER ERROR:', e);
      panel.remove();
    }
  }

  function renderResponse({ entry, card }, data, askedDbId) {
    if (data.status === 'needs_clarification') {
      card.className = 'entry-card info';
      card.innerHTML = `<p class="entry-explanation" style="margin:0">🤔 ${esc(data.question)}</p>`;
      return;
    }

    if (data.status === 'rejected') {
      card.className = 'entry-card error';
      card.innerHTML = `
        <span class="entry-tag error">blocked</span>
        <p class="entry-explanation">${esc(data.reason)}</p>
        ${sqlBlocks(data.statements)}
      `;
      return;
    }

    if (data.status === 'error') {
      card.className = 'entry-card error';
      card.innerHTML = `
        <span class="entry-tag error">error</span>
        <p class="entry-explanation">${esc(data.error)}</p>
        ${sqlBlocks(data.statements)}
      `;
      return;
    }

    // Pure reads — one or more SELECTs, executed immediately.
    // data.statements: string[]   data.results: [{columns, rows}, ...] (same order)
    // data.explanation: AI-generated insight (Phase 1)
    // data.chart: chart decision payload (Phase 2), or {chart_required: false}
    if (data.status === 'executed' && data.results) {
      card.className = 'entry-card read';

      const blocks = data.statements.map((sql, i) => {
        const r = data.results[i] || { columns: [], rows: [] };
        return `
          <pre class="sql-block">${esc(sql)}</pre>
          ${resultTable(r.columns, r.rows)}
          ${exportButtons(sql)}
        `;
      }).join('<div style="height:10px"></div>');

      const tag = data.statements.length > 1
        ? `read · ${data.statements.length} statements`
        : 'read';

      // Phase 2: chart panel markup, only if the agent decided one is
      // appropriate. Renders in the CENTER conversation card — never in
      // the right-side table preview panel.
      const chartHtml = chartPanelHtml(data.chart);

      // AI → SQL handoff (spec section 10): lets the user open the
      // AI-generated SELECT(s) in the SQL Workspace to edit/re-run
      // manually. Never auto-executes.
      const handoffHtml = data.statements.length
        ? `<button type="button" class="open-in-workspace-btn" data-handoff-idx="${entry.dataset ? '' : ''}">⌗ Open in SQL Workspace</button>`
        : '';

      card.innerHTML = `
        <span class="entry-tag read">${esc(tag)}</span>
        ${data.explanation ? `<p class="entry-explanation">${esc(data.explanation)}</p>` : ''}
        ${handoffHtml}
        ${chartHtml}
        ${blocks}
      `;

      const handoffBtn = card.querySelector('.open-in-workspace-btn');
      if (handoffBtn) {
        handoffBtn.addEventListener('click', () => {
          openInSqlWorkspace(data.statements.join('\n\n'));
        });
      }

      // Chart.js needs the <canvas> to exist in the DOM first, so this
      // runs after the innerHTML assignment above.
      initChartPanel(card, data.chart);
      return;
    }

    // Any batch with insert/update/delete/create — needs confirmation.
    // data.previews: [{ sql, type, affected_count, sample_rows }, ...]
    if (data.status === 'confirmation_required') {
      card.className = 'entry-card write';

      const previews = data.previews || [];
      const previewBlocks = previews.map((p) => {
        const impact = (p.affected_count !== null && p.affected_count !== undefined)
          ? `<p class="impact-line">This will affect ${p.affected_count} row${p.affected_count === 1 ? '' : 's'}.</p>`
          : `<p class="impact-line unknown">Row impact could not be previewed automatically.</p>`;

        const sample = (p.sample_rows && p.sample_rows.length)
          ? `<div class="sample-label">sample of affected rows</div>${resultTable(Object.keys(p.sample_rows[0]), p.sample_rows)}`
          : '';

        return `
          <span class="entry-tag write">${esc(p.type || 'write')}</span>
          <pre class="sql-block">${esc(p.sql)}</pre>
          ${impact}
          ${sample}
        `;
      }).join('<div style="height:14px;border-top:1px solid var(--border);margin-bottom:14px"></div>');

      const runLabel = data.statements.length > 1
        ? `Confirm &amp; run all ${data.statements.length}`
        : 'Confirm &amp; run';

      card.innerHTML = `
        ${data.explanation ? `<p class="entry-explanation">${esc(data.explanation)}</p>` : ''}
        ${previewBlocks}
        <div class="confirm-row" id="confirm-${data.query_id}">
          <button class="btn btn-confirm">${runLabel}</button>
          <button class="btn btn-cancel">Cancel</button>
        </div>
      `;

      const row = card.querySelector(`#confirm-${data.query_id}`);
      row.querySelector('.btn-confirm').addEventListener('click', () => resolve(data.query_id, true, row, askedDbId));
      row.querySelector('.btn-cancel').addEventListener('click', () => resolve(data.query_id, false, row, askedDbId));
      return;
    }

    card.className = 'entry-card info';
    card.textContent = 'Unrecognized response from server.';
  }

  function renderError({ card }, msg) {
    card.className = 'entry-card error';
    card.innerHTML = `<span class="entry-tag error">error</span><p class="entry-explanation">${esc(msg)}</p>`;
  }

  async function resolve(queryId, confirmVal, row, askedDbId) {
    row.innerHTML = `<span style="color:var(--text-faint);font-size:13px">Working…</span>`;
    try {
      // Confirm against whichever database the query was originally asked
      // against, even if the user has since switched the active database.
      const res = await fetch('/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ db_id: askedDbId, query_id: queryId, confirm: confirmVal }),
      });

      const data = await res.json();

      console.log("SERVER RESPONSE:", data);
      console.log("SERVER STATUS:", data.status);

      if (!res.ok) {
        row.outerHTML = `<div class="resolution err">⚠ ${esc(data.detail || `Server returned ${res.status}.`)}</div>`;
        return;
      }

      if (data.status === 'executed') {
        // data.results: [{ sql, rowcount }, ...] — sum rowcounts across the whole batch.
        const results = data.results || [];
        const total = results.reduce((sum, r) => sum + (r.rowcount || 0), 0);
        const stmtWord = results.length === 1 ? 'statement' : 'statements';
        row.outerHTML = `<div class="resolution ok">✓ Done — ${results.length} ${stmtWord} run, ${total} row(s) affected. Backup saved before the change.</div>`;

        // Database was modified (CREATE/INSERT/UPDATE/DELETE) — refresh
        // the sidebar schema (only if that db is still active) and the
        // database list (table names may have changed), keeping the
        // currently active database unchanged either way.
        if (askedDbId === dbId) {
          await refreshSchema();
        }
        await loadDatabases();
      } else if (data.status === 'cancelled') {
        row.outerHTML = `<div class="resolution">Cancelled — no changes made.</div>`;
      } else {
        row.outerHTML = `<div class="resolution err">⚠ ${esc(data.error || 'Something went wrong.')}</div>`;
      }
    } catch (e) {
      row.outerHTML = `<div class="resolution err">⚠ Could not reach the server.</div>`;
    } finally {
      log.scrollTop = log.scrollHeight;
    }
  }

  // ==================== SQL WORKSPACE ====================
  //
  // Manual SQL editor. Talks ONLY to /sql/execute for validation +
  // execution, and to the EXISTING /confirm endpoint for anything that
  // needs confirmation — no separate/duplicate safety path.

  let workspaceDbId = null; // the db_id the editor is currently pointed at

  // Called by setActiveDatabase() every time the active database
  // changes. Per spec section 2: the editor must always point at the
  // CURRENT active db_id, and a switch must clear stale results so a
  // pending result from the old database can never be mistaken for one
  // from the new database.
  function enableWorkspaceForActiveDb() {
    const switchedDb = workspaceDbId !== dbId;
    workspaceDbId = dbId;

    sqlEditor.disabled = !dbId;
    runSqlBtn.disabled = !dbId;
    clearSqlBtn.disabled = !dbId;

    if (switchedDb) {
      // Never leave a previous database's results/status on screen.
      workspaceResults.innerHTML = '';
      setWorkspaceStatus('Ready', 'ready');
    }

    updateWorkspaceDbTag();
  }

  function updateWorkspaceDbTag() {
    if (!workspaceDbTag) return;
    if (!dbId) {
      workspaceDbTag.textContent = 'no database selected';
      workspaceDbTag.classList.remove('live');
      return;
    }
    workspaceDbTag.textContent = connLabel.textContent || dbId;
    workspaceDbTag.classList.add('live');
  }

  function setWorkspaceStatus(text, kind) {
    workspaceStatus.textContent = `Status: ${text}`;
    workspaceStatus.className = 'workspace-status' + (kind ? ' ' + kind : '');
  }

  // AI → SQL handoff and table-preview → SQL handoff both funnel through
  // here: switches to the Workspace tab, fills the editor, but per spec
  // sections 10/11 NEVER executes automatically.
  function openInSqlWorkspace(sqlText) {
    if (!dbId) {
      alert("Open or select a database first.");
      return;
    }
    setMode('workspace');
    sqlEditor.value = sqlText;
    sqlEditor.focus();
    sqlEditor.setSelectionRange(sqlEditor.value.length, sqlEditor.value.length);
    setWorkspaceStatus('Ready', 'ready');
  }

  if (clearSqlBtn) {
    clearSqlBtn.addEventListener('click', () => {
      sqlEditor.value = '';
      workspaceResults.innerHTML = '';
      setWorkspaceStatus('Ready', 'ready');
      sqlEditor.focus();
    });
  }

  if (sqlEditor) {
    sqlEditor.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
        e.preventDefault();
        if (!runSqlBtn.disabled) runSqlWorkspace();
      }
    });
  }

  if (runSqlBtn) runSqlBtn.addEventListener('click', runSqlWorkspace);

  async function runSqlWorkspace() {
    if (!dbId) {
      alert("No database selected.");
      return;
    }

    const sqlText = sqlEditor.value.trim();
    if (!sqlText) {
      setWorkspaceStatus('Enter a SQL statement first.', 'error');
      return;
    }

    // Snapshot the database this run was issued against. If the user
    // switches databases while the request is in flight, the response
    // is dropped rather than rendered against the wrong (now-active) db.
    const ranAgainstDbId = dbId;

    runSqlBtn.disabled = true;
    setWorkspaceStatus('Running…', 'loading');

    const startedAt = performance.now();

    try {
      const res = await fetch('/sql/execute', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ db_id: ranAgainstDbId, sql: sqlText }),
      });

      const data = await res.json();
      console.log("/sql/execute response:", data);

      if (ranAgainstDbId !== dbId) {
        console.log("SQL Workspace result dropped — active database changed mid-request.");
        return;
      }

      const clientElapsedMs = Math.round(performance.now() - startedAt);

      if (!res.ok) {
        setWorkspaceStatus(data.detail || `Server returned ${res.status}.`, 'error');
        renderWorkspaceError(data.detail || `Server returned ${res.status}.`);
        return;
      }

      renderWorkspaceResponse(data, ranAgainstDbId, clientElapsedMs);

    } catch (e) {
      console.error("runSqlWorkspace() fetch error:", e);
      if (ranAgainstDbId === dbId) {
        setWorkspaceStatus('Could not reach the server.', 'error');
        renderWorkspaceError('Could not reach the server.');
      }
    } finally {
      if (ranAgainstDbId === dbId) {
        runSqlBtn.disabled = false;
      }
    }
  }

  function renderWorkspaceError(msg) {
    const card = document.createElement('div');
    card.className = 'workspace-result-card error';
    card.innerHTML = `<p class="entry-explanation" style="margin:0">${esc(msg)}</p>`;
    workspaceResults.prepend(card);
  }

  function renderWorkspaceResponse(data, ranAgainstDbId, clientElapsedMs) {
    if (data.status === 'error') {
      setWorkspaceStatus(data.error || 'Execution failed.', 'error');
      const card = document.createElement('div');
      card.className = 'workspace-result-card error';
      card.innerHTML = `
        <span class="entry-tag error">error</span>
        <p class="entry-explanation">${esc(data.error || 'Execution failed.')}</p>
        ${sqlBlocks(data.statements)}
      `;
      workspaceResults.prepend(card);
      return;
    }

    if (data.status === 'rejected') {
      setWorkspaceStatus(data.reason || 'Blocked by safety policy.', 'error');
      const card = document.createElement('div');
      card.className = 'workspace-result-card error';
      card.innerHTML = `
        <span class="entry-tag error">blocked</span>
        <p class="entry-explanation">${esc(data.reason || 'Statement blocked by safety policy.')}</p>
        ${sqlBlocks(data.statements)}
      `;
      workspaceResults.prepend(card);
      return;
    }

    if (data.status === 'executed' && data.results) {
      const totalRows = data.results.reduce((sum, r) => sum + (r.rows ? r.rows.length : 0), 0);
      setWorkspaceStatus(
        `Success — ${totalRows} row${totalRows === 1 ? '' : 's'} · ${data.execution_time_ms ?? clientElapsedMs}ms`,
        'success'
      );

      const blocks = data.statements.map((sql, i) => {
        const r = data.results[i] || { columns: [], rows: [] };
        return `
          <pre class="sql-block">${esc(sql)}</pre>
          ${resultTable(r.columns, r.rows)}
          ${exportButtons(sql)}
        `;
      }).join('<div style="height:10px"></div>');

      const card = document.createElement('div');
      card.className = 'workspace-result-card read';
      card.innerHTML = `
        <div class="workspace-result-meta">
          <span class="entry-tag read" style="margin-bottom:0">executed</span>
          <span class="meta-chip">${totalRows} row${totalRows === 1 ? '' : 's'}</span>
          <span class="meta-chip">${data.execution_time_ms ?? clientElapsedMs}ms</span>
        </div>
        ${blocks}
      `;
      workspaceResults.prepend(card);
      return;
    }

    if (data.status === 'confirmation_required') {
      setWorkspaceStatus('Confirmation required before this can run.', 'pending');

      const previews = data.previews || [];
      const previewBlocks = previews.map((p) => {
        const impact = (p.affected_count !== null && p.affected_count !== undefined)
          ? `<p class="impact-line">This will affect ${p.affected_count} row${p.affected_count === 1 ? '' : 's'}.</p>`
          : `<p class="impact-line unknown">Row impact could not be previewed automatically.</p>`;

        const sample = (p.sample_rows && p.sample_rows.length)
          ? `<div class="sample-label">sample of affected rows</div>${resultTable(Object.keys(p.sample_rows[0]), p.sample_rows)}`
          : '';

        return `
          <span class="entry-tag write">${esc(p.type || 'write')}</span>
          <pre class="sql-block">${esc(p.sql)}</pre>
          ${impact}
          ${sample}
        `;
      }).join('<div style="height:14px;border-top:1px solid var(--border);margin-bottom:14px"></div>');

      const runLabel = data.statements.length > 1
        ? `Confirm &amp; run all ${data.statements.length}`
        : 'Confirm &amp; run';

      const card = document.createElement('div');
      card.className = 'workspace-result-card write';
      card.innerHTML = `
        ${previewBlocks}
        <div class="confirm-row" id="wsconfirm-${data.query_id}">
          <button class="btn btn-confirm">${runLabel}</button>
          <button class="btn btn-cancel">Cancel</button>
        </div>
      `;
      workspaceResults.prepend(card);

      // Resolution goes through the EXISTING /confirm endpoint — same
      // function used by the AI chat confirm flow. Nothing duplicated.
      const row = card.querySelector(`#wsconfirm-${data.query_id}`);
      row.querySelector('.btn-confirm').addEventListener('click', () =>
        resolveWorkspace(data.query_id, true, row, ranAgainstDbId));
      row.querySelector('.btn-cancel').addEventListener('click', () =>
        resolveWorkspace(data.query_id, false, row, ranAgainstDbId));
      return;
    }

    setWorkspaceStatus('Unrecognized response from server.', 'error');
  }

  async function resolveWorkspace(queryId, confirmVal, row, askedDbId) {
    row.innerHTML = `<span style="color:var(--text-faint);font-size:13px">Working…</span>`;
    try {
      const res = await fetch('/confirm', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ db_id: askedDbId, query_id: queryId, confirm: confirmVal }),
      });

      const data = await res.json();
      console.log("SQL WORKSPACE /confirm response:", data);

      if (!res.ok) {
        row.outerHTML = `<div class="resolution err">⚠ ${esc(data.detail || `Server returned ${res.status}.`)}</div>`;
        if (askedDbId === dbId) setWorkspaceStatus('Confirmation failed.', 'error');
        return;
      }

      if (data.status === 'executed') {
        const results = data.results || [];
        const total = results.reduce((sum, r) => sum + (r.rowcount || 0), 0);
        const stmtWord = results.length === 1 ? 'statement' : 'statements';
        row.outerHTML = `<div class="resolution ok">✓ Done — ${results.length} ${stmtWord} run, ${total} row(s) affected. Backup saved before the change.</div>`;

        if (askedDbId === dbId) {
          setWorkspaceStatus(`Success — ${total} row(s) affected`, 'success');
          await refreshSchema();
        }
        await loadDatabases();
      } else if (data.status === 'cancelled') {
        row.outerHTML = `<div class="resolution">Cancelled — no changes made.</div>`;
        if (askedDbId === dbId) setWorkspaceStatus('Cancelled', 'ready');
      } else {
        row.outerHTML = `<div class="resolution err">⚠ ${esc(data.error || 'Something went wrong.')}</div>`;
        if (askedDbId === dbId) setWorkspaceStatus(data.error || 'Execution failed.', 'error');
      }
    } catch (e) {
      row.outerHTML = `<div class="resolution err">⚠ Could not reach the server.</div>`;
      if (askedDbId === dbId) setWorkspaceStatus('Could not reach the server.', 'error');
    }
  }

  // -------------------- export helpers --------------------

  async function downloadQueryExport(sql, format) {
    try {
      const res = await fetch('/export/query', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({db_id: dbId, sql, format})
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        alert(data.detail || 'Export failed.');
        return;
      }
      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `query_result.${format}`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      alert('Could not export result.');
    }
  }

  function exportButtons(sql, cls='result-export-actions') {
    const safe = encodeURIComponent(sql);
    return `<div class="${cls}">
      <button type="button" class="result-export-btn" data-export-format="csv" data-export-sql="${safe}">CSV</button>
      <button type="button" class="result-export-btn" data-export-format="xlsx" data-export-sql="${safe}">XLSX</button>
    </div>`;
  }

  // -------------------- helpers --------------------

  function resultTable(columns, rows) {
    if (!columns || !rows || rows.length === 0) {
      return `<p class="no-rows">No rows.</p>`;
    }
    let html = '<div class="table-wrap"><table class="result-table"><thead><tr>';
    for (const c of columns) html += `<th>${esc(c)}</th>`;
    html += '</tr></thead><tbody>';
    for (const row of rows) {
      html += '<tr>';
      for (const c of columns) html += `<td>${esc(row[c] === null || row[c] === undefined ? '' : String(row[c]))}</td>`;
      html += '</tr>';
    }
    html += '</tbody></table></div>';
    return html;
  }

  function esc(str) {
    const div = document.createElement('div');
    div.textContent = str === undefined || str === null ? '' : str;
    return div.innerHTML;
  }

  // -------------------- initial load --------------------
  // On page load, populate the sidebar with whatever databases the
  // backend already knows about (e.g. after a server restart with
  // persistence, or simply to reflect the current in-memory state).
  loadDatabases();

  document.addEventListener('click', (e) => {
    const btn = e.target.closest('.result-export-btn');
    if (!btn) return;
    const sql = decodeURIComponent(btn.dataset.exportSql || '');
    downloadQueryExport(sql, btn.dataset.exportFormat || 'csv');
  });

})();