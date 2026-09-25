/**
 * KenauShorts Studio — Frontend Application Logic
 */

let currentPage = 'overview';
let activeJobData = null;
let statusInterval = null;
let csrfToken = '';

// Every mutating request needs the server's per-run CSRF token (learned from
// /api/status) or it's rejected — this is what stops a page from another
// origin/tab from driving the Studio even if it can reach 127.0.0.1.
async function postJSON(path, body) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Studio-Token': csrfToken },
    body: JSON.stringify(body || {}),
  });
  return res;
}

// Initialize on load
document.addEventListener('DOMContentLoaded', () => {
  setupNavigation();
  setupQuickActions();
  setupWizard();
  pollStatus();
  statusInterval = setInterval(pollStatus, 3000);
  renderPage(currentPage);
});

// --------------------------------------------------------------------------
// Navigation & Router
// --------------------------------------------------------------------------

function setupNavigation() {
  const buttons = document.querySelectorAll('.nav-btn');
  buttons.forEach(btn => {
    btn.addEventListener('click', () => {
      buttons.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentPage = btn.dataset.page;
      renderPage(currentPage);
    });
  });
}

function renderPage(page) {
  const view = document.getElementById('content-view');
  const title = document.getElementById('page-title');
  const eyebrow = document.getElementById('page-eyebrow');

  switch (page) {
    case 'overview':
      title.textContent = 'Workspace Overview';
      eyebrow.textContent = 'DASHBOARD';
      loadOverview(view);
      break;
    case 'queue':
      title.textContent = 'Pipeline & Job Queue';
      eyebrow.textContent = 'QUEUE';
      loadQueue(view);
      break;
    case 'library':
      title.textContent = 'Drafts & Video Library';
      eyebrow.textContent = 'MEDIA';
      loadLibrary(view);
      break;
    case 'sources':
      title.textContent = 'Content Sources';
      eyebrow.textContent = 'DISCOVERY';
      loadSources(view);
      break;
    case 'style':
      title.textContent = 'Channel Identity & Card Style';
      eyebrow.textContent = 'BRANDING';
      loadStyle(view);
      break;
    case 'connections':
      title.textContent = 'API Keys & YouTube OAuth';
      eyebrow.textContent = 'CONNECTIONS';
      loadConnections(view);
      break;
    case 'automation':
      title.textContent = 'Background Automation';
      eyebrow.textContent = 'SCHEDULER';
      loadAutomation(view);
      break;
    case 'logs':
      title.textContent = 'Engine Execution Logs';
      eyebrow.textContent = 'TERMINAL';
      loadLogs(view);
      break;
    default:
      view.innerHTML = '<p>Page not found.</p>';
  }
}

// --------------------------------------------------------------------------
// Polling & Status
// --------------------------------------------------------------------------

async function pollStatus() {
  try {
    const res = await fetch('/api/status');
    if (!res.ok) return;
    const data = await res.json();
    if (data.csrf) csrfToken = data.csrf;

    const dot = document.getElementById('status-dot');
    const statusText = document.getElementById('connection-status');
    const badgeMode = document.getElementById('badge-mode');

    if (data.online) {
      dot.className = 'status-indicator online';
      statusText.textContent = `Online • ${data.platform}`;
    } else {
      dot.className = 'status-indicator';
      statusText.textContent = 'Offline (Check connection)';
    }

    activeJobData = data.active_job;
    const cancelBtn = document.getElementById('btn-cancel-job');
    if (cancelBtn) {
      cancelBtn.style.display = activeJobData ? 'inline-flex' : 'none';
    }

    if (activeJobData) {
      badgeMode.className = 'badge running';
      const queueSuffix = data.queue_count > 0 ? ` (${data.queue_count} queued)` : '';
      badgeMode.textContent = `Running: ${activeJobData.stage || 'In progress'}${queueSuffix}`;
    } else if (data.queue_count > 0) {
      badgeMode.className = 'badge running';
      badgeMode.textContent = `Queued (${data.queue_count})`;
    } else {
      badgeMode.className = 'badge';
      badgeMode.textContent = data.busy ? 'Busy' : 'Idle';
    }

    // Refresh live logs if on logs page
    if (currentPage === 'logs') {
      const term = document.getElementById('terminal-view');
      if (term && activeJobData) {
        term.textContent = activeJobData.log || 'Waiting for log output...';
        term.scrollTop = term.scrollHeight;
      }
    }

    // Refresh the queue view live if on queue page
    if (currentPage === 'queue') {
      refreshQueueView();
    }

    // Refresh the library grid so the "in progress" placeholder updates/clears live
    if (currentPage === 'library' && document.getElementById('library-grid')) {
      renderLibraryGrid();
    }
  } catch (e) {
    console.error('Status poll error:', e);
  }
}

function showNotification(msg, type = 'success') {
  const bar = document.getElementById('notification-bar');
  bar.innerHTML = `<div style="padding: 12px 20px; margin-bottom: 20px; border-radius: 8px; background: ${type === 'success' ? 'rgba(0,186,124,0.15)' : 'rgba(244,33,46,0.15)'}; border: 1px solid ${type === 'success' ? '#00ba7c' : '#f4212e'}; color: ${type === 'success' ? '#00ba7c' : '#f4212e'}; font-size: 14px; font-weight: 500;">${msg}</div>`;
  setTimeout(() => { bar.innerHTML = ''; }, 5000);
}

// --------------------------------------------------------------------------
// Quick Actions & Cancellation
// --------------------------------------------------------------------------

async function cancelCurrentJob() {
  if (!confirm('Are you sure you want to cancel the active job?')) return;
  try {
    const res = await postJSON('/api/job/cancel', {});
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    showNotification('Job cancelled successfully.');
    pollStatus();
  } catch (e) {
    showNotification(e.message, 'error');
  }
}

function setupQuickActions() {
  document.getElementById('btn-quick-preview').addEventListener('click', async () => {
    try {
      const res = await postJSON('/api/job', { action: 'preview' });
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      if (data.status === 'pending' && data.queue_position > 0) {
        showNotification(`Preview job enqueued! (Position in queue: ${data.queue_position})`);
      } else {
        showNotification('Preview draft job started! Check the Library or Logs tab.');
      }
      pollStatus();
    } catch (e) {
      showNotification(e.message, 'error');
    }
  });

  document.getElementById('btn-quick-run').addEventListener('click', async () => {
    if (!confirm('Run pipeline and publish to YouTube now?')) return;
    try {
      const res = await postJSON('/api/job', { action: 'run' });
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      if (data.status === 'pending' && data.queue_position > 0) {
        showNotification(`Run & publish job enqueued! (Position in queue: ${data.queue_position})`);
      } else {
        showNotification('Run & publish job launched! Monitoring progress in Logs.');
      }
      pollStatus();
    } catch (e) {
      showNotification(e.message, 'error');
    }
  });
}

function openManualUrlModal() {
  document.getElementById('manual-url').value = '';
  document.getElementById('manual-headline').value = '';
  document.getElementById('manual-description').value = '';
  const modal = document.getElementById('manual-url-modal');
  document.getElementById('btn-close-manual-modal').onclick = () => modal.close();
  modal.showModal();
}

async function submitManualUrl() {
  const url = document.getElementById('manual-url').value.trim();
  const headline = document.getElementById('manual-headline').value.trim();
  const description = document.getElementById('manual-description').value.trim();

  if (!url) {
    alert('Please paste a video URL.');
    return;
  }

  try {
    const res = await postJSON('/api/job', { action: 'manual', url, headline, description });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    showNotification('Rendering started — check the Library or Logs tab.');
    document.getElementById('manual-url-modal').close();
    pollStatus();
  } catch (e) {
    showNotification(e.message, 'error');
  }
}
// --------------------------------------------------------------------------
// Page: Overview
// --------------------------------------------------------------------------

async function loadOverview(container) {
  container.innerHTML = '<p>Loading statistics...</p>';
  try {
    const [statusRes, videosRes, failuresRes] = await Promise.all([
      fetch('/api/status').then(r => r.json()),
      fetch('/api/videos').then(r => r.json()),
      fetch('/api/failures').then(r => r.json())
    ]);

    const total = videosRes.length;
    const ready = videosRes.filter(v => v.status === 'ready').length;
    const uploaded = videosRes.filter(v => v.status === 'uploaded').length;

    container.innerHTML = `
      <div class="stats-grid">
        <div class="stat-card">
          <div class="label">Ready for Review</div>
          <div class="value" style="color: var(--accent)">${ready}</div>
        </div>
        <div class="stat-card">
          <div class="label">Uploaded Shorts</div>
          <div class="value" style="color: var(--success)">${uploaded}</div>
        </div>
        <div class="stat-card">
          <div class="label">Total Generated</div>
          <div class="value">${total}</div>
        </div>
        <div class="stat-card">
          <div class="label">Free Disk Space</div>
          <div class="value">${statusRes.free_space_mb} MB</div>
        </div>
      </div>
        <div class="stat-card">
          <div class="label">Free Disk Space</div>
          <div class="value">${statusRes.free_space_mb} MB</div>
        </div>
      </div>

      ${failuresRes.length > 0 ? `
      <div style="background: var(--bg-card); border: 1px solid var(--danger); border-radius: var(--radius-md); padding: 24px; margin-bottom: 24px;">
        <h3 style="font-size: 18px; margin-bottom: 12px; color: var(--danger);">Recent Failures (${failuresRes.length})</h3>
        <table class="sub-table">
          <thead><tr><th>Candidate</th><th>Attempts</th><th>Last Reason</th><th>When</th></tr></thead>
          <tbody>
            ${failuresRes.slice(0, 10).map(f => `
              <tr>
                <td style="font-family: monospace; font-size: 12px;">${escapeHtml(f.key)}</td>
                <td>${f.attempts}</td>
                <td>${escapeHtml(f.last_reason)}</td>
                <td>${f.last_time ? new Date(f.last_time * 1000).toLocaleString() : 'â€”'}</td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
      ` : ''}

      <div class="panel" style="margin-bottom: 24px;">
        <h3 class="panel-title" style="font-size: 18px;">Active Pipeline Engine</h3>
      <div style="background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 24px; margin-bottom: 24px;">
        <h3 style="font-size: 18px; margin-bottom: 12px;">Active Pipeline Engine</h3>
        <p style="color: var(--text-muted); font-size: 14px; margin-bottom: 20px;">
          KenauShorts runs locally in the background on your PC. It monitors configured subreddits & YouTube channels, performs AI editorial selection, and composites 1080x1920 shorts automatically.
        </p>
        <div style="display: flex; gap: 12px;">
          <button class="btn btn-secondary" onclick="document.querySelector('[data-page=library]').click()">Browse Drafts (${ready})</button>
          <button class="btn btn-secondary" onclick="document.querySelector('[data-page=sources]').click()">Manage Sources</button>         
          <button class="btn btn-secondary" onclick="document.querySelector('[data-page=logs]').click()">View Terminal</button>
          <button class="btn btn-secondary" onclick="openManualUrlModal()">+ Add Video by Link</button>
        </div>
      </div>
    `;
  } catch (e) {
    container.innerHTML = `<p style="color: var(--danger)">Failed to load overview: ${e.message}</p>`;
  }
}

// --------------------------------------------------------------------------
// Page: Drafts & Library
// --------------------------------------------------------------------------

// --------------------------------------------------------------------------
// Page: Drafts & Library (Stage 5 Enhanced)
// --------------------------------------------------------------------------

let libraryVideos = [];
let librarySearch = '';
let libraryFilterStatus = 'all';
let libraryFilterReview = 'all';
let libraryFilterSource = 'all';
let libraryFilterPreset = 'all';
let libraryFilterHealth = 'all';
let librarySort = 'newest';
let librarySelected = new Set();

async function loadLibrary(container) {
  container.innerHTML = '<p>Loading drafts & video library...</p>';
  try {
    const res = await fetch('/api/videos');
    libraryVideos = await res.json();

    if (!libraryVideos || libraryVideos.length === 0) {
      container.innerHTML = `
        <div style="text-align: center; padding: 60px 20px; background: var(--bg-card); border-radius: var(--radius-md); border: 1px dashed var(--border);">
          <h3 style="font-size: 18px; margin-bottom: 8px;">No Videos Generated Yet</h3>
          <p style="color: var(--text-muted); font-size: 14px; margin-bottom: 20px;">Create your first preview draft to see how your cards render.</p>
          <button class="btn btn-primary" onclick="document.getElementById('btn-quick-preview').click()">+ Create Preview Draft</button>
        </div>
      `;
      return;
    }

    container.innerHTML = `
      <div style="background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 16px; margin-bottom: 20px;">
        <div style="display: flex; gap: 10px; margin-bottom: 12px; align-items: center; flex-wrap: wrap;">
          <input type="text" class="form-input" id="library-search" placeholder="Search title, headline, channel, URL, ID..." style="flex: 1; min-width: 220px;" value="${escapeHtml(librarySearch)}">
          <button class="btn btn-secondary" id="library-reset-filters" style="padding: 8px 14px; font-size: 13px;" onclick="resetLibraryFilters()">Reset Filters</button>
        </div>
        <div style="display: flex; gap: 10px; align-items: center; flex-wrap: wrap;">
          <select class="form-input" id="library-filter-status" style="width: auto; min-width: 130px;">
            <option value="all">All Statuses</option>
            <option value="ready">Ready</option>
            <option value="uploaded">Uploaded</option>
            <option value="failed">Failed</option>
            <option value="rendering">Rendering</option>
          </select>
          <select class="form-input" id="library-filter-review" style="width: auto; min-width: 140px;">
            <option value="all">All Reviews</option>
            <option value="unreviewed">⏳ Unreviewed</option>
            <option value="approved">✓ Approved</option>
            <option value="rejected">✕ Rejected</option>
          </select>
          <select class="form-input" id="library-filter-source" style="width: auto; min-width: 130px;">
            <option value="all">All Sources</option>
            <option value="youtube">YouTube</option>
            <option value="reddit">Reddit</option>
            <option value="manual">Manual Link</option>
          </select>
          <select class="form-input" id="library-filter-preset" style="width: auto; min-width: 140px;">
            <option value="all">All Styles</option>
            <option value="classic_blue">Classic Blue (16:9)</option>
            <option value="warm_amber">Warm Amber (4:3)</option>
            <option value="emerald_compact">Emerald Compact (1:1)</option>
            <option value="cyber_violet">Cyber Violet (4:5)</option>
            <option value="unset">Auto / Unset</option>
          </select>
          <select class="form-input" id="library-filter-health" style="width: auto; min-width: 130px;">
            <option value="all">All Media</option>
            <option value="intact">Files Intact</option>
            <option value="missing">Missing Files</option>
          </select>
          <select class="form-input" id="library-sort" style="width: auto; min-width: 130px;">
            <option value="newest">Newest First</option>
            <option value="oldest">Oldest First</option>
            <option value="title">Title (A-Z)</option>
            <option value="status">Status</option>
          </select>
          <span style="color: var(--text-muted); font-size: 13px; margin-left: auto;" id="library-count"></span>
        </div>
      </div>

      <div id="library-bulk-bar" style="display: none; gap: 12px; margin-bottom: 16px; align-items: center; background: var(--bg-card); border: 1px solid var(--danger); border-radius: var(--radius-sm); padding: 12px 16px;">
        <span id="library-bulk-count" style="font-size: 13px;"></span>
        <button class="btn btn-danger" style="padding: 6px 14px; font-size: 13px;" onclick="deleteSelectedVideos()">Delete Selected</button>
        <button class="btn btn-secondary" style="padding: 6px 14px; font-size: 13px;" onclick="librarySelected.clear(); renderLibraryGrid();">Clear Selection</button>
      </div>

      <div class="video-grid" id="library-grid"></div>
    `;

    // Bind event listeners
    const searchInput = document.getElementById('library-search');
    searchInput.addEventListener('input', (e) => {
      librarySearch = e.target.value;
      renderLibraryGrid();
    });

    const statusSel = document.getElementById('library-filter-status');
    statusSel.value = libraryFilterStatus;
    statusSel.addEventListener('change', (e) => {
      libraryFilterStatus = e.target.value;
      renderLibraryGrid();
    });

    const reviewSel = document.getElementById('library-filter-review');
    reviewSel.value = libraryFilterReview;
    reviewSel.addEventListener('change', (e) => {
      libraryFilterReview = e.target.value;
      renderLibraryGrid();
    });

    const sourceSel = document.getElementById('library-filter-source');
    sourceSel.value = libraryFilterSource;
    sourceSel.addEventListener('change', (e) => {
      libraryFilterSource = e.target.value;
      renderLibraryGrid();
    });

    const presetSel = document.getElementById('library-filter-preset');
    presetSel.value = libraryFilterPreset;
    presetSel.addEventListener('change', (e) => {
      libraryFilterPreset = e.target.value;
      renderLibraryGrid();
    });

    const healthSel = document.getElementById('library-filter-health');
    healthSel.value = libraryFilterHealth;
    healthSel.addEventListener('change', (e) => {
      libraryFilterHealth = e.target.value;
      renderLibraryGrid();
    });

    const sortSel = document.getElementById('library-sort');
    sortSel.value = librarySort;
    sortSel.addEventListener('change', (e) => {
      librarySort = e.target.value;
      renderLibraryGrid();
    });

    renderLibraryGrid();
  } catch (e) {
    container.innerHTML = `<p style="color: var(--danger)">Failed to load library: ${e.message}</p>`;
  }
}

function resetLibraryFilters() {
  librarySearch = '';
  libraryFilterStatus = 'all';
  libraryFilterReview = 'all';
  libraryFilterSource = 'all';
  libraryFilterPreset = 'all';
  libraryFilterHealth = 'all';
  librarySort = 'newest';

  const sInput = document.getElementById('library-search');
  if (sInput) sInput.value = '';
  const fs = document.getElementById('library-filter-status');
  if (fs) fs.value = 'all';
  const fr = document.getElementById('library-filter-review');
  if (fr) fr.value = 'all';
  const fsrc = document.getElementById('library-filter-source');
  if (fsrc) fsrc.value = 'all';
  const fp = document.getElementById('library-filter-preset');
  if (fp) fp.value = 'all';
  const fh = document.getElementById('library-filter-health');
  if (fh) fh.value = 'all';
  const st = document.getElementById('library-sort');
  if (st) st.value = 'newest';

  renderLibraryGrid();
}

function renderLibraryGrid() {
  const grid = document.getElementById('library-grid');
  const countEl = document.getElementById('library-count');
  if (!grid) return;

  const query = (librarySearch || '').toLowerCase().trim();

  let filtered = libraryVideos.filter(v => {
    // 1. Text search across title, headline, channel, source, url, id
    if (query) {
      const title = (v.title || '').toLowerCase();
      const headline = (v.headline || '').toLowerCase();
      const channel = (v.candidate?.channel || '').toLowerCase();
      const source = (v.candidate?.source || '').toLowerCase();
      const url = (v.candidate?.url || '').toLowerCase();
      const vidId = (v.id || '').toLowerCase();
      if (!title.includes(query) && !headline.includes(query) && !channel.includes(query) &&
          !source.includes(query) && !url.includes(query) && !vidId.includes(query)) {
        return false;
      }
    }

    // 2. Processing status filter
    if (libraryFilterStatus !== 'all') {
      if (libraryFilterStatus === 'failed') {
        if (v.status !== 'failed' && v.status !== 'render_failed') return false;
      } else if (libraryFilterStatus === 'rendering') {
        if (v.status !== 'rendering' && v.status !== 'uploading') return false;
      } else {
        if (v.status !== libraryFilterStatus) return false;
      }
    }

    // 3. Editorial review status filter
    const rev = v.review_status || 'unreviewed';
    if (libraryFilterReview !== 'all' && rev !== libraryFilterReview) {
      return false;
    }

    // 4. Source platform filter
    if (libraryFilterSource !== 'all') {
      const s = (v.candidate?.source || '').toLowerCase();
      if (libraryFilterSource === 'youtube') {
        if (!s.includes('youtube')) return false;
      } else if (libraryFilterSource === 'reddit') {
        if (s !== 'reddit') return false;
      } else if (libraryFilterSource === 'manual') {
        if (s !== 'manual') return false;
      }
    }

    // 5. Style preset filter
    if (libraryFilterPreset !== 'all') {
      const p = v.style_preset || '';
      if (libraryFilterPreset === 'unset') {
        if (p !== '') return false;
      } else {
        if (p !== libraryFilterPreset) return false;
      }
    }

    // 6. Media health filter
    if (libraryFilterHealth !== 'all') {
      const intact = v.video_exists !== false;
      if (libraryFilterHealth === 'intact' && !intact) return false;
      if (libraryFilterHealth === 'missing' && intact) return false;
    }

    return true;
  });

  // Sorting
  filtered = [...filtered].sort((a, b) => {
    if (librarySort === 'newest') {
      return new Date(b.created_at) - new Date(a.created_at);
    } else if (librarySort === 'oldest') {
      return new Date(a.created_at) - new Date(b.created_at);
    } else if (librarySort === 'title') {
      const ta = (a.title || a.headline || a.id).toLowerCase();
      const tb = (b.title || b.headline || b.id).toLowerCase();
      return ta.localeCompare(tb);
    } else if (librarySort === 'status') {
      return (a.status || '').localeCompare(b.status || '');
    }
    return 0;
  });

  const unreviewedCount = libraryVideos.filter(v => (v.review_status || 'unreviewed') === 'unreviewed').length;
  countEl.textContent = `Showing ${filtered.length} of ${libraryVideos.length} videos (${unreviewedCount} unreviewed)`;

  const bulkBar = document.getElementById('library-bulk-bar');
  if (bulkBar) {
    bulkBar.style.display = librarySelected.size > 0 ? 'flex' : 'none';
    const label = document.getElementById('library-bulk-count');
    if (label) label.textContent = `${librarySelected.size} selected`;
  }

  const activeCard = (activeJobData && libraryFilterStatus === 'all' && !query) ? `
    <div class="video-card" style="cursor: default;">
      <div class="video-thumb" style="display: flex; align-items: center; justify-content: center; background: #11141b;">
        <span style="color: var(--accent); font-size: 13px; text-align: center; padding: 12px;">⚙ ${escapeHtml(activeJobData.stage || 'Rendering...')}</span>
      </div>
      <div class="video-info">
        <h4>In Progress</h4>
        <div class="video-meta"><span>Pipeline job active</span></div>
      </div>
    </div>
  ` : '';

  if (filtered.length === 0) {
    grid.innerHTML = activeCard + `<p style="color: var(--text-muted); grid-column: 1 / -1; padding: 40px; text-align: center;">No videos match the selected filters or search query.</p>`;
    return;
  }

  grid.innerHTML = activeCard + filtered.map(v => {
    const posterUrl = v.poster ? `/media/${v.poster.replace(/\\/g, '/')}` : '';
    const checked = librarySelected.has(v.id) ? 'checked' : '';
    const rev = v.review_status || 'unreviewed';
    const revBadgeClass = `badge-review-${rev}`;
    const revLabel = rev === 'approved' ? '✓ Approved' : rev === 'rejected' ? '✕ Rejected' : 'Unreviewed';
    const missingBadge = (v.video_exists === false) ? `<span class="badge badge-missing" style="position: absolute; bottom: 8px; right: 8px; z-index: 2;">⚠ Missing File</span>` : '';
    const presetLabel = v.style_preset ? `<span class="preset-tag" style="position: absolute; top: 10px; right: 10px; z-index: 2;">${escapeHtml(v.style_preset.replace('_', ' '))}</span>` : '';

    return `
      <div class="video-card">
        <input type="checkbox" class="video-select-checkbox" ${checked} onclick="event.stopPropagation(); toggleLibrarySelect('${v.id}')"
               style="position: absolute; top: 10px; left: 10px; z-index: 2;">
        ${presetLabel}
        <div onclick="openVideoModal('${v.id}')">
          <div class="video-thumb" style="position: relative;">
            ${posterUrl ? `<img src="${posterUrl}" alt="Preview" onerror="this.style.display='none'">` : ''}
            <span class="status-pill ${v.status}" style="position: absolute; bottom: 8px; left: 8px; z-index: 2;">${v.status}</span>
            ${missingBadge}
          </div>
          <div class="video-info">
            <div style="display: flex; justify-content: space-between; align-items: flex-start; gap: 8px; margin-bottom: 4px;">
              <h4 style="margin: 0; flex: 1;">${escapeHtml(v.title || v.headline || 'Untitled Short')}</h4>
              <span class="${revBadgeClass}">${revLabel}</span>
            </div>
            <div class="video-meta">
              <span>${escapeHtml(v.candidate?.channel || 'Local')}</span> •
              <span>${new Date(v.created_at).toLocaleDateString()}</span>
            </div>
          </div>
        </div>
      </div>
    `;
  }).join('');
}

function toggleLibrarySelect(id) {
  if (librarySelected.has(id)) {
    librarySelected.delete(id);
  } else {
    librarySelected.add(id);
  }
  renderLibraryGrid();
}

async function deleteSelectedVideos() {
  if (librarySelected.size === 0) return;
  if (!confirm(`Permanently delete ${librarySelected.size} video(s) and their files? This cannot be undone.`)) return;

  const ids = [...librarySelected];
  let failures = 0;
  let failureReasons = [];
  for (const id of ids) {
    try {
      const res = await postJSON('/api/video', { id, action: 'delete' });
      const data = await res.json();
      if (!res.ok || data.error) {
        failures++;
        if (data.error) failureReasons.push(data.error);
      }
    } catch (e) {
      failures++;
      failureReasons.push(e.message);
    }
  }

  librarySelected.clear();
  if (failures > 0) {
    showNotification(`Deleted with ${failures} failure(s): ${failureReasons.slice(0, 2).join('; ')}`, 'error');
  } else {
    showNotification('Selected videos deleted.', 'success');
  }
  loadLibrary(document.getElementById('content-view'));
}

async function openVideoModal(id) {
  try {
    const res = await fetch(`/api/video?id=${id}`);
    const v = await res.json();
    if (!res.ok || v.error) throw new Error(v.error || 'Video not found');

    const modal = document.getElementById('video-modal');
    const body = document.getElementById('modal-body');
    const videoUrl = `/media/${v.video.replace(/\\/g, '/')}`;

    const cand = v.candidate || {};
    const rev = v.review_status || 'unreviewed';

    // Build Licensing technical evidence badge
    const lic = (cand.licence || cand.license || '').toLowerCase();
    let licenseBadge = '';
    if (lic === 'public-domain') {
      licenseBadge = `<span class="badge-license public-domain">Source metadata: Public Domain (e.g. NASA / NOAA / LoC)</span>`;
    } else if (lic === 'creative-commons' || cand.youtube_cc_only) {
      licenseBadge = `<span class="badge-license creative-commons">Source metadata: Creative Commons detected</span>`;
    } else if (lic === 'manual') {
      licenseBadge = `<span class="badge-license manual">Source metadata: Manual ingestion (unverified license)</span>`;
    } else if (lic === 'standard') {
      licenseBadge = `<span class="badge-license standard">Source metadata: Standard platform license</span>`;
    } else {
      licenseBadge = `<span class="badge-license unknown">Source metadata: Unspecified / Unknown</span>`;
    }

    // Build Artifact Health badges
    const videoHealthPill = v.video_exists !== false
      ? `<span class="badge" style="color: var(--success);">✓ Video: Available (${v.file_size_mb || 0} MB)</span>`
      : `<span class="badge" style="color: var(--danger);">✕ Video: Missing on disk</span>`;

    const posterHealthPill = v.poster_exists !== false
      ? `<span class="badge" style="color: var(--success);">✓ Poster: Available</span>`
      : `<span class="badge" style="color: var(--danger);">✕ Poster: Missing</span>`;

    const rawHealthPill = v.raw_exists
      ? `<span class="badge" style="color: var(--success);">✓ Raw Source: Available</span>`
      : `<span class="badge" style="color: var(--text-muted);">✕ Raw Source: Not available</span>`;

    // Build Preview column (video player or graceful missing-artifact state)
    let previewHtml = '';
    if (v.video_exists !== false) {
      previewHtml = `
        <div style="aspect-ratio: 9/16; background: #000; border-radius: var(--radius-md); overflow: hidden; position: relative;">
          <video controls autoplay loop src="${videoUrl}" style="width: 100%; height: 100%; object-fit: contain;"></video>
        </div>
      `;
    } else {
      previewHtml = `
        <div class="video-missing-placeholder" style="aspect-ratio: 9/16;">
          <span style="font-size: 36px; margin-bottom: 8px;">⚠</span>
          <h4 style="color: var(--danger); font-size: 15px; margin-bottom: 6px;">Rendered Video File Missing</h4>
          <p style="font-size: 12px; margin-bottom: 12px; word-break: break-all;"><code>${escapeHtml(v.video || '')}</code></p>
          ${v.raw_exists ? '<p style="color: var(--success); font-size: 12px; margin: 0;">Raw source clip is available. You can re-render this card below.</p>' : '<p style="color: var(--text-muted); font-size: 12px; margin: 0;">No raw source clip found for re-rendering.</p>'}
        </div>
      `;
    }

    // Build Stage 4 Pipeline Activity card
    let jobHtml = '';
    if (v.latest_job) {
      const j = v.latest_job;
      const statusClass = `badge ${j.status}`;
      const isRetryable = ['failed', 'cancelled', 'interrupted'].includes(j.status);
      jobHtml = `
        <div style="background: rgba(255,255,255,0.02); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 14px; margin-top: 16px;">
          <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px;">
            <span style="font-weight: 700; font-size: 13px;">Stage 4 Pipeline Activity</span>
            <span class="${statusClass}">${j.status}</span>
          </div>
          <div style="font-size: 12px; color: var(--text-muted); line-height: 1.6;">
            <div>Action: <strong>${escapeHtml(j.action || '')}</strong> • Stage: <em>${escapeHtml(j.stage || '')}</em></div>
            ${j.duration_seconds != null ? `<div>Duration: <strong>${j.duration_seconds}s</strong></div>` : ''}
            ${j.finished_at ? `<div>Finished: ${new Date(j.finished_at).toLocaleString()}</div>` : ''}
          </div>
          ${j.error ? `
            <div style="background: rgba(239, 68, 68, 0.1); border: 1px solid rgba(239, 68, 68, 0.3); border-radius: 4px; padding: 8px 12px; margin-top: 8px; font-family: var(--font-mono); font-size: 12px; color: #fca5a5; word-break: break-all;">
              <strong>Diagnostic:</strong> ${escapeHtml(j.error)}
            </div>
          ` : ''}
          <div style="display: flex; gap: 8px; margin-top: 10px;">
            ${isRetryable ? `<button class="btn btn-secondary btn-sm" onclick="retryFromVideoModal('${j.id}')">↻ Retry Job</button>` : ''}
            <button class="btn btn-secondary btn-sm" onclick="document.getElementById('video-modal').close(); openJobModal('${j.id}')">View Full Job Log ↗</button>
          </div>
        </div>
      `;
    } else {
      jobHtml = `
        <div style="background: rgba(255,255,255,0.02); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 12px; margin-top: 16px; font-size: 12px; color: var(--text-muted);">
          No recent pipeline execution jobs recorded for this video.
        </div>
      `;
    }

    body.innerHTML = `
      <div style="display: grid; grid-template-columns: 320px 1fr; gap: 24px;">
        <!-- Left Column: Media Preview & Artifact Health -->
        <div>
          ${previewHtml}
          <div style="margin-top: 14px; display: flex; flex-direction: column; gap: 6px;">
            ${videoHealthPill}
            ${posterHealthPill}
            ${rawHealthPill}
          </div>
        </div>

        <!-- Right Column: Editorial, Review, Source, Pipeline -->
        <div>
          <!-- Editorial Review Section -->
          <div style="background: rgba(255,255,255,0.02); border: 1px solid var(--border); padding: 14px 16px; border-radius: var(--radius-sm); margin-bottom: 18px;">
            <label style="font-weight: 700; font-size: 13px; display: block; margin-bottom: 8px;">Editorial Review Decision</label>
            <div style="display: flex; gap: 12px; align-items: center;">
              <select class="form-input" id="edit-review-status" style="max-width: 220px; font-weight: 600;" onchange="updateReviewPill(this.value)">
                <option value="unreviewed" ${rev === 'unreviewed' ? 'selected' : ''}>⏳ Unreviewed</option>
                <option value="approved" ${rev === 'approved' ? 'selected' : ''}>✓ Approved for Publishing</option>
                <option value="rejected" ${rev === 'rejected' ? 'selected' : ''}>✕ Rejected</option>
              </select>
              <span id="review-pill-preview" class="badge-review-${rev}">${rev === 'approved' ? '✓ Approved' : rev === 'rejected' ? '✕ Rejected' : 'Unreviewed'}</span>
            </div>
          </div>

          <!-- Metadata Form -->
          <div class="form-group">
            <label>Short Title</label>
            <input type="text" class="form-input" id="edit-title" value="${escapeHtml(v.title || '')}">
            <p class="form-help">Title displayed on YouTube Shorts.</p>
          </div>

          <div class="form-group">
            <label>Card Headline</label>
            <input type="text" class="form-input" id="edit-headline" value="${escapeHtml(v.headline || '')}">
            <p class="form-help">Headline text rendered in bold monospace on top of the card.</p>
          </div>

          <div class="form-group">
            <label>Card Style</label>
            <select class="form-input" id="edit-style-preset">
              <option value="" ${!v.style_preset ? 'selected' : ''}>Auto (match video shape)</option>
              <option value="classic_blue" ${v.style_preset === 'classic_blue' ? 'selected' : ''}>Classic Blue (16:9)</option>
              <option value="warm_amber" ${v.style_preset === 'warm_amber' ? 'selected' : ''}>Warm Amber (4:3)</option>
              <option value="emerald_compact" ${v.style_preset === 'emerald_compact' ? 'selected' : ''}>Emerald Compact (1:1)</option>
              <option value="cyber_violet" ${v.style_preset === 'cyber_violet' ? 'selected' : ''}>Cyber Violet (4:5)</option>
            </select>
            <p class="form-help">Border color and aspect ratio preset. Auto matches the source video's shape.</p>
          </div>

          <div class="form-group">
            <label>YouTube Description</label>
            <textarea class="form-input" id="edit-desc" rows="3">${escapeHtml(v.description || '')}</textarea>
          </div>

          <!-- Source & Licensing Evidence Panel -->
          <div style="background: rgba(255,255,255,0.02); border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 14px; margin-top: 16px;">
            <div style="font-weight: 700; font-size: 13px; margin-bottom: 8px;">Technical Source & Licensing Evidence</div>
            <div style="font-size: 12px; color: var(--text-muted); line-height: 1.7;">
              <div>Platform: <strong>${escapeHtml(cand.source || 'Local')}</strong> • Channel: <strong>${escapeHtml(cand.channel || 'Unknown')}</strong></div>
              ${cand.url ? `<div>Source URL: <a href="${escapeHtml(cand.url)}" target="_blank" rel="noopener noreferrer" class="source-link">${escapeHtml(cand.url)} ↗</a></div>` : ''}
              <div>Candidate Key: <code style="font-size: 11px;">${escapeHtml(cand.key || 'N/A')}</code></div>
              ${cand.published_at ? `<div>Published: ${new Date(cand.published_at * 1000).toLocaleString()}</div>` : ''}
              <div style="margin-top: 6px;">${licenseBadge}</div>
            </div>
          </div>

          <!-- Stage 4 Pipeline Activity -->
          ${jobHtml}

          <!-- Action Buttons -->
          <div style="display: flex; gap: 12px; margin-top: 24px; flex-wrap: wrap;">
            <button class="btn btn-secondary" onclick="saveVideoEdits('${v.id}')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:4px;"><path d="M19 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11l5 5v11a2 2 0 0 1-2 2Z"></path><polyline points="17 21 17 13 7 13 7 21"></polyline><polyline points="7 3 7 8 15 8"></polyline></svg>Save Details</button>
            <button class="btn btn-secondary" onclick="reRenderDraft('${v.id}')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:4px;"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>Re-render Card</button>
            ${v.status !== 'uploaded' ? `<button class="btn btn-primary" onclick="uploadDraft('${v.id}')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:4px;"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="17 8 12 3 7 8"></polyline><line x1="12" y1="3" x2="12" y2="15"></line></svg>Upload to YouTube</button>` : `<span class="badge" style="color: var(--success); padding: 10px 16px;">✓ Uploaded to YouTube</span>`}
            <button class="btn btn-danger" onclick="deleteVideo('${v.id}')"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:4px;"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path><line x1="10" y1="11" x2="10" y2="17"></line><line x1="14" y1="11" x2="14" y2="17"></line></svg>Delete</button>
          </div>
        </div>
      </div>
    `;

    document.getElementById('btn-close-modal').onclick = () => modal.close();
    modal.showModal();

    modal.addEventListener('close', () => {
      const vid = body.querySelector('video');
      if (vid) {
        vid.pause();
        vid.currentTime = 0;
      }
    }, { once: true });
  } catch (e) {
    showNotification(`Could not open draft: ${e.message}`, 'error');
  }
}

function updateReviewPill(val) {
  const pill = document.getElementById('review-pill-preview');
  if (!pill) return;
  pill.className = `badge-review-${val}`;
  pill.textContent = val === 'approved' ? '✓ Approved' : val === 'rejected' ? '✕ Rejected' : 'Unreviewed';
}

async function retryFromVideoModal(jobId) {
  try {
    const res = await postJSON('/api/job/retry', { id: jobId });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Retry failed');
    showNotification('Retry job enqueued successfully.', 'success');
    document.getElementById('video-modal').close();
    pollStatus();
  } catch (e) {
    showNotification(`Retry failed: ${e.message}`, 'error');
  }
}

async function deleteVideo(id) {
  if (!confirm('Permanently delete this video and its files? This cannot be undone.')) return;
  try {
    const res = await postJSON('/api/video', { id, action: 'delete' });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Delete failed');
    showNotification('Video deleted.', 'success');
    document.getElementById('video-modal').close();
    loadLibrary(document.getElementById('content-view'));
  } catch (e) {
    showNotification(e.message, 'error');
  }
}

async function saveVideoEdits(id) {
  const title = document.getElementById('edit-title').value;
  const headline = document.getElementById('edit-headline').value;
  const description = document.getElementById('edit-desc').value;
  const presetEl = document.getElementById('edit-style-preset');
  const style_preset = presetEl ? presetEl.value : '';
  const reviewEl = document.getElementById('edit-review-status');
  const review_status = reviewEl ? reviewEl.value : 'unreviewed';

  try {
    const res = await postJSON('/api/video', { id, title, headline, description, style_preset, review_status });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Save failed');
    showNotification('Video details and review status saved.', 'success');

    // Update in-memory cache
    const item = libraryVideos.find(x => x.id === id);
    if (item) {
      item.title = title;
      item.headline = headline;
      item.description = description;
      item.style_preset = style_preset;
      item.review_status = review_status;
      renderLibraryGrid();
    }
  } catch (e) {
    showNotification(e.message, 'error');
  }
}

async function reRenderDraft(id) {
  await saveVideoEdits(id);
  try {
    const res = await postJSON('/api/job', { action: 'render', key: id });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Re-render failed');
    showNotification('Re-render job started! Check Pipeline & Queue for progress.', 'success');
    document.getElementById('video-modal').close();
    pollStatus();
  } catch (e) {
    showNotification(e.message, 'error');
  }
}

async function uploadDraft(id) {
  await saveVideoEdits(id);
  if (!confirm('Upload this short to your YouTube channel now?')) return;
  try {
    const res = await postJSON('/api/job', { action: 'upload', key: id });
    const data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Upload failed');
    showNotification('Upload job started! Monitoring progress in Pipeline & Queue.', 'success');
    document.getElementById('video-modal').close();
    pollStatus();
  } catch (e) {
    showNotification(e.message, 'error');
  }
}


// --------------------------------------------------------------------------
// Page: Sources (Subreddits, YouTube Channels, Search Queries)
// --------------------------------------------------------------------------

let sourcesTab = 'subreddits';

async function loadSources(container) {
  container.innerHTML = '<p>Loading sources...</p>';
  try {
    const [subreddits, channels, queries] = await Promise.all([
      fetch('/api/subreddits').then(r => r.json()),
      fetch('/api/youtube_channels').then(r => r.json()),
      fetch('/api/youtube_queries').then(r => r.json()),
    ]);

    container.innerHTML = `
      <div class="sources-tabs" style="display: flex; gap: 8px; margin-bottom: 20px;">
        <button class="btn ${sourcesTab === 'subreddits' ? 'btn-primary' : 'btn-secondary'}" onclick="switchSourcesTab('subreddits')">Subreddits (${subreddits.length})</button>
        <button class="btn ${sourcesTab === 'channels' ? 'btn-primary' : 'btn-secondary'}" onclick="switchSourcesTab('channels')">YouTube Channels (${channels.length})</button>
        <button class="btn ${sourcesTab === 'queries' ? 'btn-primary' : 'btn-secondary'}" onclick="switchSourcesTab('queries')">Search Queries (${queries.length})</button>
      </div>
      <div id="sources-tab-body"></div>
    `;

    renderSourcesTab(subreddits, channels, queries);
  } catch (e) {
    container.innerHTML = `<p style="color: var(--danger)">Failed to load sources: ${e.message}</p>`;
  }
}

function switchSourcesTab(tab) {
  sourcesTab = tab;
  loadSources(document.getElementById('content-view'));
}

function renderSourcesTab(subreddits, channels, queries) {
  const body = document.getElementById('sources-tab-body');

  if (sourcesTab === 'subreddits') {
    body.innerHTML = `
      <div class="subreddits-header">
        <div>
          <h3 style="font-size: 18px; margin-bottom: 4px;">Target Subreddits</h3>
          <p style="color: var(--text-muted); font-size: 13px;">Add any subreddit you wish to scrape for viral content, trending tech, or stories.</p>
        </div>
      </div>

      <div class="add-sub-bar">
        <input type="text" class="form-input" id="new-sub-name" placeholder="Subreddit name (e.g. artificial, pcgaming, space)">
        <select class="form-input" id="new-sub-cat" style="max-width: 180px;">
          <option value="Tech">Tech</option>
          <option value="AI">AI & LLMs</option>
          <option value="Gaming">Gaming</option>
          <option value="Science">Science</option>
          <option value="Pop Culture">Pop Culture</option>
          <option value="General">General</option>
        </select>
        <input type="number" class="form-input" id="new-sub-score" value="200" style="max-width: 130px;" placeholder="Min upvotes">
        <button class="btn btn-secondary" onclick="testNewSubreddit()"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:4px;"><circle cx="11" cy="11" r="8"></circle><line x1="21" y1="21" x2="16.65" y2="16.65"></line></svg>Test Scrape</button>
        <button class="btn btn-primary" onclick="addNewSubreddit()">+ Add Subreddit</button>
      </div>

      <div id="sub-test-results" style="margin-bottom: 20px;"></div>

      <table class="sub-table">
        <thead><tr><th>Subreddit</th><th>Category</th><th>Min Upvotes</th><th>Actions</th></tr></thead>
        <tbody>
          ${subreddits.map(s => {
            const name = typeof s === 'object' ? s.name : s;
            const cat = typeof s === 'object' ? (s.category || 'General') : 'General';
            const score = typeof s === 'object' ? (s.min_score || 200) : 200;
            return `
              <tr>
                <td><strong>r/${escapeHtml(name)}</strong></td>
                <td><span class="category-tag">${escapeHtml(cat)}</span></td>
                <td>${score} upvotes</td>
                <td><button class="btn btn-danger" style="padding: 4px 10px; font-size: 12px;" onclick="deleteSubreddit('${escapeHtml(name)}')">Remove</button></td>
              </tr>
            `;
          }).join('')}
        </tbody>
      </table>
    `;
  } else if (sourcesTab === 'channels') {
    body.innerHTML = `
      <div class="subreddits-header">
        <div>
          <h3 style="font-size: 18px; margin-bottom: 4px;">YouTube Channels</h3>
          <p style="color: var(--text-muted); font-size: 13px;">Channels whose recent uploads are pulled in as video candidates via RSS.</p>
        </div>
      </div>

      <div class="add-sub-bar">
        <input type="text" class="form-input" id="new-ch-name" placeholder="Display name (e.g. NASA)">
        <input type="text" class="form-input" id="new-ch-id" placeholder="Channel ID (e.g. UCLA_DiR1FfKNvjuUpBHmylQ)">
        <select class="form-input" id="new-ch-licence" style="max-width: 180px;">
          <option value="public-domain">Public Domain</option>
          <option value="cc">Creative Commons</option>
          <option value="unknown">Unknown</option>
        </select>
        <button class="btn btn-primary" onclick="addNewChannel()">+ Add Channel</button>
      </div>

      <table class="sub-table">
        <thead><tr><th>Name</th><th>Channel ID</th><th>Licence</th><th>Actions</th></tr></thead>
        <tbody>
          ${channels.map(c => `
            <tr>
              <td><strong>${escapeHtml(c.name || c.channel)}</strong></td>
              <td style="font-family: monospace; font-size: 12px;">${escapeHtml(c.channel)}</td>
              <td><span class="category-tag">${escapeHtml(c.licence || 'unknown')}</span></td>
              <td><button class="btn btn-danger" style="padding: 4px 10px; font-size: 12px;" onclick="deleteChannel('${escapeHtml(c.channel)}')">Remove</button></td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
  } else if (sourcesTab === 'queries') {
    body.innerHTML = `
      <div class="subreddits-header">
        <div>
          <h3 style="font-size: 18px; margin-bottom: 4px;">YouTube Search Queries</h3>
          <p style="color: var(--text-muted); font-size: 13px;">Search terms used to discover video candidates across all of YouTube, not just configured channels.</p>
        </div>
      </div>

      <div class="add-sub-bar">
        <input type="text" class="form-input" id="new-query-text" placeholder="Search query (e.g. tech news shorts)">
        <button class="btn btn-primary" onclick="addNewQuery()">+ Add Query</button>
      </div>

      <table class="sub-table">
        <thead><tr><th>Query</th><th>Actions</th></tr></thead>
        <tbody>
          ${queries.map(q => `
            <tr>
              <td><strong>${escapeHtml(q)}</strong></td>
              <td><button class="btn btn-danger" style="padding: 4px 10px; font-size: 12px;" onclick="deleteQuery('${escapeHtml(q)}')">Remove</button></td>
            </tr>
          `).join('')}
        </tbody>
      </table>
    `;
  }
}

async function testNewSubreddit() {
  const name = document.getElementById('new-sub-name').value.trim();
  const box = document.getElementById('sub-test-results');
  if (!name) { alert('Please enter a subreddit name to test.'); return; }
  box.innerHTML = `<p style="color: var(--accent); font-size: 13px;">Testing scraper on r/${name}...</p>`;
  try {
    const res = await postJSON('/api/subreddits/test', { name });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    box.innerHTML = `
      <div style="background: #11141b; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 16px;">
        <h4 style="font-size: 14px; color: var(--success); margin-bottom: 8px;">âœ“ Scrape successful! Found ${data.count} posts from r/${data.subreddit}</h4>
        <ul style="font-size: 13px; color: var(--text-muted); list-style: none;">
          ${data.sample.map(p => `<li style="margin-bottom: 4px;">â€¢ <strong>[${p.score} pts]</strong> ${escapeHtml(p.title)}</li>`).join('')}
        </ul>
      </div>
    `;
  } catch (e) {
    box.innerHTML = `<div style="color: var(--danger); font-size: 13px;">Scrape test failed: ${e.message}</div>`;
  }
}

async function addNewSubreddit() {
  const name = document.getElementById('new-sub-name').value.trim();
  const category = document.getElementById('new-sub-cat').value;
  const min_score = parseInt(document.getElementById('new-sub-score').value, 10) || 200;
  if (!name) { alert('Please enter a subreddit name.'); return; }
  try {
    const res = await postJSON('/api/subreddits', { action: 'add', name, category, min_score });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    showNotification(`Added r/${name} to scraping list.`);
    loadSources(document.getElementById('content-view'));
  } catch (e) {
    showNotification(e.message, 'error');
  }
}

async function deleteSubreddit(name) {
  if (!confirm(`Remove r/${name} from discovery?`)) return;
  try {
    const res = await postJSON('/api/subreddits', { action: 'delete', name });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    showNotification(`Removed r/${name}.`);
    loadSources(document.getElementById('content-view'));
  } catch (e) {
    showNotification(e.message, 'error');
  }
}

async function addNewChannel() {
  const name = document.getElementById('new-ch-name').value.trim();
  const channel = document.getElementById('new-ch-id').value.trim();
  const licence = document.getElementById('new-ch-licence').value;
  if (!name || !channel) { alert('Please enter both a name and a channel ID.'); return; }
  try {
    const res = await postJSON('/api/youtube_channels', { action: 'add', name, channel, licence });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    showNotification(`Added channel "${name}".`);
    loadSources(document.getElementById('content-view'));
  } catch (e) {
    showNotification(e.message, 'error');
  }
}

async function deleteChannel(channel) {
  if (!confirm('Remove this channel from discovery?')) return;
  try {
    const res = await postJSON('/api/youtube_channels', { action: 'delete', channel });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    showNotification('Removed channel.');
    loadSources(document.getElementById('content-view'));
  } catch (e) {
    showNotification(e.message, 'error');
  }
}

async function addNewQuery() {
  const query = document.getElementById('new-query-text').value.trim();
  if (!query) { alert('Please enter a search query.'); return; }
  try {
    const res = await postJSON('/api/youtube_queries', { action: 'add', query });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    showNotification(`Added query "${query}".`);
    loadSources(document.getElementById('content-view'));
  } catch (e) {
    showNotification(e.message, 'error');
  }
}

async function deleteQuery(query) {
  if (!confirm('Remove this search query?')) return;
  try {
    const res = await postJSON('/api/youtube_queries', { action: 'delete', query });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    showNotification('Removed query.');
    loadSources(document.getElementById('content-view'));
  } catch (e) {
    showNotification(e.message, 'error');
  }
}

// --------------------------------------------------------------------------
// Page: Connections & Keys
// --------------------------------------------------------------------------

async function loadConnections(container) {
  container.innerHTML = '<p>Loading connection settings...</p>';
  try {
    const conn = await fetch('/api/connections').then(r => r.json());

    container.innerHTML = `
      <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 24px;">
        <!-- AI Keys -->
        <div class="panel">
          <h3 class="panel-title">AI Editorial Keys</h3>
          <p style="color: var(--text-muted); font-size: 13px; margin-bottom: 20px;">
            At least one key is needed for Claude, Gemini, or OpenAI to evaluate viral candidates and generate punchy headlines.
          </p>

          <div class="form-group">
            <label>Google Gemini API Key ${conn.gemini ? '<span style="color:var(--success); font-size:12px;">(âœ“ Configured)</span>' : ''}</label>
            <div style="display: flex; gap: 8px;">
              <input type="password" class="form-input" id="key-gemini" placeholder="${conn.gemini_preview || 'AIzaSy...'}">
              ${conn.gemini ? `<button class="btn btn-danger" style="white-space: nowrap;" onclick="removeApiKey('GEMINI_API_KEY')">Remove</button>` : ''}
            </div>
            <p class="form-help">Free tier available at <a href="https://aistudio.google.com/app/apikey" target="_blank" style="color:var(--accent);">aistudio.google.com</a>.</p>
          </div>

          <div class="form-group">
            <label>Anthropic (Claude) API Key ${conn.anthropic ? '<span style="color:var(--success); font-size:12px;">(âœ“ Configured)</span>' : ''}</label>
            <div style="display: flex; gap: 8px;">
              <input type="password" class="form-input" id="key-anthropic" placeholder="sk-ant-...">
              ${conn.anthropic ? `<button class="btn btn-danger" style="white-space: nowrap;" onclick="removeApiKey('ANTHROPIC_API_KEY')">Remove</button>` : ''}
            </div>
          </div>

          <div class="form-group">
            <label>OpenAI API Key ${conn.openai ? '<span style="color:var(--success); font-size:12px;">(âœ“ Configured)</span>' : ''}</label>
            <div style="display: flex; gap: 8px;">
              <input type="password" class="form-input" id="key-openai" placeholder="sk-...">
              ${conn.openai ? `<button class="btn btn-danger" style="white-space: nowrap;" onclick="removeApiKey('OPENAI_API_KEY')">Remove</button>` : ''}
            </div>
          </div>

          <button class="btn btn-primary" onclick="saveApiKeys()">Save API Keys</button>
        </div>

        <!-- YouTube OAuth -->
        <div class="panel">
          <h3 class="panel-title">YouTube Publishing OAuth</h3>
          <p style="color: var(--text-muted); font-size: 13px; margin-bottom: 12px;">
            Uploading a video needs your explicit consent, so this can't be a pasted API key like the ones on the
            left — Google only allows it through a one-time OAuth sign-in. It's also a different Google product:
            the AI keys above come from <a href="https://aistudio.google.com/app/apikey" target="_blank" style="color:var(--accent);">Google AI Studio</a>,
            while this comes from <a href="https://console.cloud.google.com/apis/credentials" target="_blank" style="color:var(--accent);">Google Cloud Console</a>.
          </p>
          <p style="color: var(--text-muted); font-size: 13px; margin-bottom: 20px;">
            Full walkthrough: <code>docs/YOUTUBE_API_SETUP.md</code> in your installation.
          </p>

          <div style="margin-bottom: 20px; padding: 16px; border-radius: var(--radius-sm); background: #101318; border: 1px solid var(--border);">
            <div style="font-size: 14px; font-weight: 600; margin-bottom: 6px;">
              OAuth Status: ${conn.youtube_oauth_ready ? '<span style="color:var(--success)">Connected & Ready</span>' : '<span style="color:var(--warning)">Not Authorized</span>'}
            </div>
            <p style="color: var(--text-muted); font-size: 12px;">
              ${conn.youtube_client_secret_present
                ? 'client_secret.json detected in root folder.'
                : 'No client_secret.json yet — follow docs/YOUTUBE_API_SETUP.md to create a Desktop OAuth client in Google Cloud Console and download it there first.'}
            </p>
          </div>

          ${conn.youtube_client_secret_present
            ? `<button class="btn btn-secondary" onclick="connectYouTube()">🔑 Connect / Authorize YouTube</button>
               <p class="form-help" style="margin-top: 10px;">This opens a real Google sign-in window in your browser — that's expected, not an error. Approve access, then come back here.</p>`
            : `<button class="btn btn-secondary" disabled title="Add client_secret.json first — see docs/YOUTUBE_API_SETUP.md">🔑 Connect / Authorize YouTube</button>
               <p class="form-help" style="margin-top: 10px;">This button unlocks once client_secret.json is in place.</p>`}
        </div>
      </div>
    `;
  } catch (e) {
    container.innerHTML = `<p style="color: var(--danger)">Failed to load connections: ${e.message}</p>`;
  }
}

async function saveApiKeys() {
  const gemini = document.getElementById('key-gemini').value.trim();
  const anthropic = document.getElementById('key-anthropic').value.trim();
  const openai = document.getElementById('key-openai').value.trim();

  const payload = {};
  if (gemini) payload.GEMINI_API_KEY = gemini;
  if (anthropic) payload.ANTHROPIC_API_KEY = anthropic;
  if (openai) payload.OPENAI_API_KEY = openai;

  try {
    const res = await postJSON('/api/connections', payload);
    if (!res.ok) throw new Error('Failed to save keys');
    showNotification('API keys securely saved.');
    loadConnections(document.getElementById('content-view'));
  } catch (e) {
    showNotification(e.message, 'error');
  }
}

async function removeApiKey(keyName) {
  if (!confirm('Remove this API key? You can add a new one anytime.')) return;
  try {
    const res = await postJSON('/api/connections', { [keyName]: '' });
    if (!res.ok) throw new Error('Failed to remove key');
    showNotification('API key removed.');
    loadConnections(document.getElementById('content-view'));
  } catch (e) {
    showNotification(e.message, 'error');
  }
}

async function connectYouTube() {
  try {
    const res = await postJSON('/api/job', { action: 'youtube_connect' });
    const job = await res.json();
    if (job.error) throw new Error(job.error);
    showNotification('Starting YouTube authorization — a Google sign-in window should open shortly.');
    watchYoutubeConnectJob(job.id);
  } catch (e) {
    showNotification(e.message, 'error');
  }
}

async function watchYoutubeConnectJob(jobId, attempt = 0) {
  // The initial POST only confirms the background job started, not that the
  // browser window actually opened — a missing dependency, an occupied
  // port, or a bad client_secret.json all fail silently after that point
  // unless something actually checks back on the job and its real error.
  if (attempt > 40) return; // ~60s, then give up quietly
  try {
    const res = await fetch(`/api/job?id=${jobId}`);
    if (!res.ok) return;
    const job = await res.json();
    if (job.status === 'running') {
      setTimeout(() => watchYoutubeConnectJob(jobId, attempt + 1), 1500);
      return;
    }
    if (job.status === 'completed') {
      const conn = await fetch('/api/connections').then(r => r.json());
      if (conn.youtube_oauth_ready) {
        showNotification('YouTube connected and ready to publish!');
      } else {
        showNotification('The authorization window closed without connecting. Check the Logs tab for details.', 'error');
      }
    } else {
      const lastLogLine = (job.log || '').trim().split('\n').filter(Boolean).pop();
      showNotification(`YouTube authorization failed${lastLogLine ? ': ' + lastLogLine : ''} — see the Logs tab for the full error.`, 'error');
    }
    if (currentPage === 'connections') loadConnections(document.getElementById('content-view'));
  } catch (e) {
    // Network hiccup on one poll shouldn't stop watching the job.
    setTimeout(() => watchYoutubeConnectJob(jobId, attempt + 1), 1500);
  }
}

// --------------------------------------------------------------------------
// Page: Automation
// --------------------------------------------------------------------------

async function loadAutomation(container) {
  container.innerHTML = '<p>Loading automation settings...</p>';
  try {
    const data = await fetch('/api/settings').then(r => r.json());
    const auto = data.automation;
    const editorial = data.config.editorial || {};

    const nextRunText = auto.enabled && auto.next_run
      ? new Date(auto.next_run * 1000).toLocaleString()
      : 'Not scheduled (automation is paused)';

    container.innerHTML = `
      <div class="panel" style="max-width: 600px; margin-bottom: 24px;">
        <h3 class="panel-title" style="margin-bottom: 20px;">Always-On Background Automation</h3>

        <div style="background: #11141b; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 14px 16px; margin-bottom: 20px;">
          <div style="font-size: 12px; color: var(--text-muted); margin-bottom: 4px;">NEXT SCHEDULED RUN</div>
          <div style="font-size: 15px; font-weight: 600;">${nextRunText}</div>
        </div>

        <div class="form-group">
          <label>Automation Status</label>
          <select class="form-input" id="auto-enabled">
            <option value="true" ${auto.enabled ? 'selected' : ''}>Enabled (Runs periodically)</option>
            <option value="false" ${!auto.enabled ? 'selected' : ''}>Paused</option>
          </select>
        </div>

        <div class="form-group">
          <label>Run Interval</label>
          <select class="form-input" id="auto-interval">
            <option value="3" ${auto.interval_hours == 3 ? 'selected' : ''}>Every 3 hours</option>
            <option value="4" ${auto.interval_hours == 4 ? 'selected' : ''}>Every 4 hours</option>
            <option value="5" ${auto.interval_hours == 5 ? 'selected' : ''}>Every 5 hours (Default)</option>
            <option value="8" ${auto.interval_hours == 8 ? 'selected' : ''}>Every 8 hours</option>
            <option value="12" ${auto.interval_hours == 12 ? 'selected' : ''}>Every 12 hours</option>
            <option value="24" ${auto.interval_hours == 24 ? 'selected' : ''}>Once a day</option>
          </select>
        </div>

        <div class="form-group">
          <label>Automation Mode</label>
          <select class="form-input" id="auto-mode">
            <option value="preview" ${auto.mode === 'preview' ? 'selected' : ''}>Create Previews (Wait for manual review)</option>
            <option value="publish" ${auto.mode === 'publish' ? 'selected' : ''}>Auto-Publish to YouTube</option>
          </select>
        </div>

        <button class="btn btn-primary" onclick="saveAutomationSettings()">Save Automation Settings</button>
      </div>

      <div class="panel" style="max-width: 600px;">
        <h3 class="panel-title" style="margin-bottom: 8px;">AI Editorial Instructions</h3>
        <p style="color: var(--text-muted); font-size: 13px; margin-bottom: 16px;">This prompt tells the AI how to pick and write up the best candidate each run.</p>
        <div class="form-group">
          <textarea class="form-input" id="editorial-prompt" rows="14" style="font-family: 'JetBrains Mono', monospace; font-size: 13px; line-height: 1.5;">${escapeHtml(editorial.system_prompt || '')}</textarea>
        </div>
        <button class="btn btn-primary" onclick="saveEditorialPrompt()">Save Editorial Prompt</button>
      </div>
    `;
  } catch (e) {
    container.innerHTML = `<p style="color: var(--danger)">Failed to load automation: ${e.message}</p>`;
  }
}

async function saveEditorialPrompt() {
  const system_prompt = document.getElementById('editorial-prompt').value;
  try {
    const data = await fetch('/api/settings').then(r => r.json());
    const cfg = data.config;
    cfg.editorial = { ...cfg.editorial, system_prompt };
    const res = await postJSON('/api/settings', { config: cfg });
    if (!res.ok) {
      const errData = await res.json();
      throw new Error(errData.error || 'Save failed');
    }
    showNotification('Editorial prompt saved.');
  } catch (e) {
    showNotification(e.message, 'error');
  }
}

async function saveAutomationSettings() {
  const enabled = document.getElementById('auto-enabled').value === 'true';
  const interval_hours = parseFloat(document.getElementById('auto-interval').value);
  const mode = document.getElementById('auto-mode').value;

  try {
    const res = await postJSON('/api/settings', { automation: { enabled, interval_hours, mode } });
    if (!res.ok) throw new Error('Update failed');
    showNotification('Automation preferences saved.');
  } catch (e) {
    showNotification(e.message, 'error');
  }
}

// --------------------------------------------------------------------------
// Page: Style & Layout
// --------------------------------------------------------------------------

async function loadStyle(container) {
  try {
    const data = await fetch('/api/settings').then(r => r.json());
    const acct = data.config.account || {};
    const layout = data.config.layout || {};

    container.innerHTML = `
      <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 24px;">
        <div class="panel">
          <h3 class="panel-title">Channel Identity</h3>
          <div class="form-group">
            <label>Channel Name</label>
            <input type="text" class="form-input" id="style-name" value="${escapeHtml(acct.name || '')}">
          </div>
          <div class="form-group">
            <label>Channel Handle</label>
            <input type="text" class="form-input" id="style-handle" value="${escapeHtml(acct.handle || '')}">
          </div>
          <div class="form-group">
            <label>Verified Badge</label>
            <select class="form-input" id="style-verified">
              <option value="true" ${acct.verified ? 'selected' : ''}>Enabled (Show blue check)</option>
              <option value="false" ${!acct.verified ? 'selected' : ''}>Disabled</option>
            </select>
          </div>
          <button class="btn btn-primary" onclick="saveStyleSettings()">Save Branding</button>
        </div>

        <div class="panel">
          <h3 class="panel-title">Card Styling</h3>
          <div class="form-group">
            <label>Border Color</label>
            <input type="color" class="form-input" id="style-border-color" value="${layout.border_color || '#1D9BF0'}" style="height: 44px; padding: 4px;">
          </div>
          <div class="form-group">
            <label>Corner Radius (px)</label>
            <input type="number" class="form-input" id="style-radius" value="${layout.corner_radius || 40}">
          </div>
          <div class="form-group">
            <label>Headline Size (px)</label>
            <input type="number" class="form-input" id="style-headline-size" value="${layout.headline_size || 76}">
          </div>

          <h3 style="font-size: 14px; margin: 20px 0 12px; color: var(--text-muted);">Live Preview</h3>
          <div style="background: #000; border-radius: 12px; padding: 20px; display: flex; justify-content: center;">
            <div id="style-preview-card" style="width: 260px; background: #15181f; overflow: hidden;">
              <div style="padding: 16px;">
                <div id="style-preview-headline" style="color: #fff; font-family: 'JetBrains Mono', monospace; font-weight: 700; line-height: 1.15;">
                  SAMPLE HEADLINE TEXT GOES HERE
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    `;

    setupStylePreview();
  } catch (e) {
    container.innerHTML = `<p style="color: var(--danger)">Failed to load style: ${e.message}</p>`;
  }
}

function setupStylePreview() {
  const colorInput = document.getElementById('style-border-color');
  const radiusInput = document.getElementById('style-radius');
  const headlineInput = document.getElementById('style-headline-size');

  function updatePreview() {
    const card = document.getElementById('style-preview-card');
    const headline = document.getElementById('style-preview-headline');
    if (!card || !headline) return;

    const borderColor = colorInput.value || '#1D9BF0';
    const radius = parseInt(radiusInput.value, 10) || 40;
    // Preview card is a fixed 260px wide mock, while real cards render at
    // 1080px â€” scale the px values down proportionally so the preview
    // actually looks like what will render, not just uses the raw numbers.
    const scale = 260 / 1080;
    const scaledRadius = Math.round(radius * scale);
    const scaledHeadline = Math.round((parseInt(headlineInput.value, 10) || 76) * scale);

    card.style.border = `3px solid ${borderColor}`;
    card.style.borderRadius = `${scaledRadius}px`;
    headline.style.fontSize = `${scaledHeadline}px`;
  }

  [colorInput, radiusInput, headlineInput].forEach(el => {
    el.addEventListener('input', updatePreview);
  });
  updatePreview();
}

async function saveStyleSettings() {
  const name = document.getElementById('style-name').value;
  const handle = document.getElementById('style-handle').value;
  const verified = document.getElementById('style-verified').value === 'true';
  const border_color = document.getElementById('style-border-color').value;
  const corner_radius = parseInt(document.getElementById('style-radius').value, 10);
  const headline_size = parseInt(document.getElementById('style-headline-size').value, 10);

  try {
    const data = await fetch('/api/settings').then(r => r.json());
    const cfg = data.config;
    cfg.account = { ...cfg.account, name, handle, verified };
    cfg.layout = { ...cfg.layout, border_color, corner_radius, headline_size };

    await postJSON('/api/settings', { config: cfg });
    showNotification('Channel & style settings saved.');
  } catch (e) {
    showNotification(e.message, 'error');
  }
}

// --------------------------------------------------------------------------
// Page: Pipeline & Queue
// --------------------------------------------------------------------------

let currentQueueFilter = 'all';

async function loadQueue(container) {
  container.innerHTML = `
    <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px;">
      <div>
        <p style="color: var(--text-muted); font-size: 14px;">Monitor running pipelines, inspect queued tasks, and trace execution history.</p>
      </div>
      <div style="display: flex; gap: 8px;">
        <button class="btn btn-secondary" onclick="refreshQueueView()"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-2px;margin-right:4px;"><polyline points="23 4 23 10 17 10"></polyline><polyline points="1 20 1 14 7 14"></polyline><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path></svg>Refresh</button>
      </div>
    </div>

    <!-- Active Job Section -->
    <div id="queue-active-section" style="margin-bottom: 28px;">
      <h3 style="font-size: 16px; margin-bottom: 12px; display: flex; align-items: center; gap: 8px;">
        Active Execution
      </h3>
      <div id="queue-active-container"><p style="color: var(--text-muted); font-size: 13px;">Checking active status...</p></div>
    </div>

    <!-- Pending Queue Section -->
    <div id="queue-pending-section" style="margin-bottom: 28px;">
      <h3 style="font-size: 16px; margin-bottom: 12px; display: flex; align-items: center; gap: 8px;">
        Queued Tasks <span id="queue-count-badge" class="badge" style="font-size: 12px; display: inline-block;">0</span>
      </h3>
      <div id="queue-pending-container"><p style="color: var(--text-muted); font-size: 13px;">Checking queue...</p></div>
    </div>

    <!-- History Section -->
    <div id="queue-history-section">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px;">
        <h3 style="font-size: 16px;">Execution History</h3>
        <div class="filter-group" style="display: flex; gap: 6px;">
          <button class="btn btn-sm btn-filter ${currentQueueFilter === 'all' ? 'btn-primary' : 'btn-secondary'}" onclick="setQueueFilter('all')">All</button>
          <button class="btn btn-sm btn-filter ${currentQueueFilter === 'completed' ? 'btn-primary' : 'btn-secondary'}" onclick="setQueueFilter('completed')">Completed</button>
          <button class="btn btn-sm btn-filter ${currentQueueFilter === 'failed' ? 'btn-primary' : 'btn-secondary'}" onclick="setQueueFilter('failed')">Failed</button>
          <button class="btn btn-sm btn-filter ${currentQueueFilter === 'cancelled' ? 'btn-primary' : 'btn-secondary'}" onclick="setQueueFilter('cancelled')">Cancelled</button>
        </div>
      </div>
      <div id="queue-history-container"><p style="color: var(--text-muted); font-size: 13px;">Loading history...</p></div>
    </div>
  `;

  await refreshQueueView();
}

function setQueueFilter(filter) {
  currentQueueFilter = filter;
  const buttons = document.querySelectorAll('.filter-group .btn-filter');
  buttons.forEach(btn => {
    const isCurrent = btn.textContent.toLowerCase() === filter;
    btn.classList.toggle('btn-primary', isCurrent);
    btn.classList.toggle('btn-secondary', !isCurrent);
  });
  refreshQueueView();
}

async function refreshQueueView() {
  if (currentPage !== 'queue') return;

  try {
    const statusUrl = '/api/status';
    const filterParam = currentQueueFilter !== 'all' ? `&status=${encodeURIComponent(currentQueueFilter)}` : '';
    const jobsUrl = `/api/jobs?limit=50${filterParam}`;

    const [statusRes, jobsRes] = await Promise.all([
      fetch(statusUrl).then(r => r.json()).catch(() => null),
      fetch(jobsUrl).then(r => r.json()).catch(() => [])
    ]);

    if (!statusRes) return;

    // 1. Render Active Job Section
    const activeCont = document.getElementById('queue-active-container');
    if (activeCont) {
      const active = statusRes.active_job;
      if (active) {
        activeCont.innerHTML = `
          <div class="stat-card" style="display: flex; justify-content: space-between; align-items: center; border-left: 4px solid var(--accent); padding: 18px 20px;">
            <div>
              <div style="display: flex; align-items: center; gap: 10px; margin-bottom: 6px;">
                <span class="status-pill running">RUNNING</span>
                <strong style="font-size: 15px;">${escapeHtml((active.action || '').toUpperCase())}</strong>
                <span style="font-family: monospace; font-size: 12px; color: var(--text-muted);">${escapeHtml((active.id || '').slice(0, 8))}</span>
              </div>
              <div style="font-size: 13px; color: var(--text); margin-bottom: 4px;">
                Stage: <strong>${escapeHtml(active.stage || 'Executing...')}</strong>
              </div>
              <div style="font-size: 12px; color: var(--text-muted);">
                Started: ${active.started_at ? active.started_at.replace('T', ' ').slice(0, 19) : 'Just now'}
              </div>
            </div>
            <div style="display: flex; gap: 8px;">
              <button class="btn btn-secondary btn-sm" onclick="openJobModal('${escapeHtml(active.id)}')">View Pipeline</button>
              <button class="btn btn-danger btn-sm" onclick="cancelJobById('${escapeHtml(active.id)}')">Cancel</button>
            </div>
          </div>
        `;
      } else {
        activeCont.innerHTML = `
          <div style="background: var(--bg-card); border: 1px dashed var(--border); border-radius: var(--radius-md); padding: 20px; text-align: center; color: var(--text-muted); font-size: 13px;">
            Engine Idle — No active job running.
          </div>
        `;
      }
    }

    // 2. Render Queued Tasks Section
    const pendingCont = document.getElementById('queue-pending-container');
    const countBadge = document.getElementById('queue-count-badge');
    const pendingList = statusRes.pending || [];
    if (countBadge) countBadge.textContent = pendingList.length;

    if (pendingCont) {
      if (pendingList.length > 0) {
        pendingCont.innerHTML = `
          <table class="sub-table">
            <thead>
              <tr>
                <th style="width: 50px;">Pos</th>
                <th>Job ID</th>
                <th>Action</th>
                <th>Queued At</th>
                <th style="text-align: right;">Actions</th>
              </tr>
            </thead>
            <tbody>
              ${pendingList.map((pj, idx) => `
                <tr>
                  <td><strong>#${idx + 1}</strong></td>
                  <td style="font-family: monospace; font-size: 12px;">${escapeHtml(pj.id ? pj.id.slice(0, 8) : '')}</td>
                  <td><span class="job-badge">${escapeHtml(pj.action || '')}</span></td>
                  <td style="font-size: 12px; color: var(--text-muted);">${pj.created_at ? pj.created_at.replace('T', ' ').slice(0, 19) : '—'}</td>
                  <td style="text-align: right;">
                    <button class="btn btn-secondary btn-sm" onclick="openJobModal('${escapeHtml(pj.id)}')">Details</button>
                    <button class="btn btn-danger btn-sm" style="margin-left: 4px;" onclick="cancelJobById('${escapeHtml(pj.id)}')">Cancel</button>
                  </td>
                </tr>
              `).join('')}
            </tbody>
          </table>
        `;
      } else {
        pendingCont.innerHTML = `
          <div style="background: var(--bg-card); border: 1px dashed var(--border); border-radius: var(--radius-md); padding: 20px; text-align: center; color: var(--text-muted); font-size: 13px;">
            No jobs waiting in queue.
          </div>
        `;
      }
    }

    // 3. Render Execution History Section
    const historyCont = document.getElementById('queue-history-container');
    if (historyCont) {
      const jobs = Array.isArray(jobsRes) ? jobsRes : [];
      if (jobs.length > 0) {
        historyCont.innerHTML = `
          <table class="sub-table">
            <thead>
              <tr>
                <th>Status</th>
                <th>Action</th>
                <th>Job ID</th>
                <th>Started</th>
                <th>Duration</th>
                <th>Summary / Error</th>
                <th style="text-align: right;">Actions</th>
              </tr>
            </thead>
            <tbody>
              ${jobs.map(j => {
                const canRetry = ['failed', 'cancelled', 'interrupted'].includes(j.status);
                const isRetried = !!j.retried_by;
                const durationText = (j.duration_seconds !== null && j.duration_seconds !== undefined) ? `${j.duration_seconds}s` : '—';
                const timeText = j.started_at ? j.started_at.replace('T', ' ').slice(0, 19) : (j.created_at ? j.created_at.replace('T', ' ').slice(0, 19) : '—');
                let summaryHtml = '';
                if (j.error) {
                  summaryHtml = `<span style="color: var(--danger); font-family: monospace;" title="${escapeHtml(j.error)}">${escapeHtml(j.error.length > 45 ? j.error.slice(0, 45) + '...' : j.error)}</span>`;
                } else {
                  summaryHtml = `<span style="color: var(--text-muted);">${escapeHtml(j.stage || 'Completed')}</span>`;
                }
                return `
                  <tr>
                    <td><span class="status-pill ${escapeHtml(j.status)}">${escapeHtml(j.status)}</span></td>
                    <td><span class="job-badge">${escapeHtml(j.action || '')}</span></td>
                    <td style="font-family: monospace; font-size: 12px;">
                      <a href="#" onclick="openJobModal('${escapeHtml(j.id)}'); return false;" style="color: var(--accent);">${escapeHtml((j.id || '').slice(0, 8))}</a>
                    </td>
                    <td style="font-size: 12px; color: var(--text-muted);">${timeText}</td>
                    <td style="font-size: 12px; font-weight: 500;">${durationText}</td>
                    <td style="font-size: 12px; max-width: 250px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${summaryHtml}</td>
                    <td style="text-align: right; white-space: nowrap;">
                      <button class="btn btn-secondary btn-sm" onclick="openJobModal('${escapeHtml(j.id)}')">Details</button>
                      ${canRetry ? `<button class="btn btn-primary btn-sm" style="margin-left: 4px;" onclick="retryJob('${escapeHtml(j.id)}')">${isRetried ? 'Retry Again' : 'Retry'}</button>` : ''}
                    </td>
                  </tr>
                `;
              }).join('')}
            </tbody>
          </table>
        `;
      } else {
        historyCont.innerHTML = `
          <div style="background: var(--bg-card); border: 1px dashed var(--border); border-radius: var(--radius-md); padding: 30px; text-align: center; color: var(--text-muted); font-size: 13px;">
            No job history matching current filter.
          </div>
        `;
      }
    }
  } catch (e) {
    console.error('Queue refresh error:', e);
  }
}

async function cancelJobById(id) {
  if (!id) return;
  if (!confirm(`Cancel job ${id.slice(0, 8)}?`)) return;
  try {
    const res = await postJSON('/api/job/cancel', { id });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    showNotification(`Job ${id.slice(0, 8)} cancelled.`);
    pollStatus();
    if (currentPage === 'queue') refreshQueueView();
    const modal = document.getElementById('job-detail-modal');
    if (modal && modal.open) modal.close();
  } catch (e) {
    showNotification(e.message, 'error');
  }
}

async function retryJob(id) {
  if (!id) return;
  try {
    const res = await postJSON('/api/job/retry', { id });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    const posMsg = data.queue_position > 0 ? ` (Queue pos: ${data.queue_position})` : '';
    showNotification(`Job retried! New job: ${data.id.slice(0, 8)}${posMsg}`);
    pollStatus();
    if (currentPage === 'queue') refreshQueueView();
    const modal = document.getElementById('job-detail-modal');
    if (modal && modal.open) modal.close();
  } catch (e) {
    showNotification(e.message, 'error');
  }
}

async function openJobModal(id) {
  try {
    const res = await fetch(`/api/job?id=${encodeURIComponent(id)}`);
    const job = await res.json();
    if (job.error) throw new Error(job.error);

    const modal = document.getElementById('job-detail-modal');
    const title = document.getElementById('job-modal-title');
    const body = document.getElementById('job-modal-body');
    const closeBtn = document.getElementById('btn-close-job-modal');

    title.textContent = `Job Details — ${(job.action || '').toUpperCase()} (${(job.id || '').slice(0, 8)})`;
    closeBtn.onclick = () => modal.close();

    const stages = Array.isArray(job.stages) ? job.stages : [];
    const canRetry = ['failed', 'cancelled', 'interrupted'].includes(job.status);
    const durationText = (job.duration_seconds !== null && job.duration_seconds !== undefined) ? `${job.duration_seconds}s` : '—';

    let html = `
      <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-bottom: 20px; background: var(--bg-card); padding: 16px; border-radius: var(--radius-md); border: 1px solid var(--border);">
        <div><strong style="color: var(--text-muted); font-size: 11px; text-transform: uppercase;">Job ID</strong><div style="font-family: monospace; font-size: 12px; margin-top: 2px;">${escapeHtml(job.id)}</div></div>
        <div><strong style="color: var(--text-muted); font-size: 11px; text-transform: uppercase;">Status</strong><div style="margin-top: 2px;"><span class="status-pill ${escapeHtml(job.status)}">${escapeHtml(job.status)}</span></div></div>
        <div><strong style="color: var(--text-muted); font-size: 11px; text-transform: uppercase;">Action</strong><div style="font-weight: 600; margin-top: 2px;">${escapeHtml(job.action)}</div></div>
        <div><strong style="color: var(--text-muted); font-size: 11px; text-transform: uppercase;">Duration</strong><div style="margin-top: 2px;">${durationText}</div></div>
        <div><strong style="color: var(--text-muted); font-size: 11px; text-transform: uppercase;">Created</strong><div style="font-size: 12px; color: var(--text-muted); margin-top: 2px;">${job.created_at ? job.created_at.replace('T', ' ').slice(0, 19) : '—'}</div></div>
        <div><strong style="color: var(--text-muted); font-size: 11px; text-transform: uppercase;">Started</strong><div style="font-size: 12px; color: var(--text-muted); margin-top: 2px;">${job.started_at ? job.started_at.replace('T', ' ').slice(0, 19) : '—'}</div></div>
        <div><strong style="color: var(--text-muted); font-size: 11px; text-transform: uppercase;">Finished</strong><div style="font-size: 12px; color: var(--text-muted); margin-top: 2px;">${job.finished_at ? job.finished_at.replace('T', ' ').slice(0, 19) : '—'}</div></div>
        <div><strong style="color: var(--text-muted); font-size: 11px; text-transform: uppercase;">Trigger</strong><div style="font-size: 12px; color: var(--text-muted); margin-top: 2px;">${job.automatic ? 'Automatic (Scheduler)' : 'Manual (User)'}</div></div>
      </div>
    `;

    if (job.retry_of || job.retried_by) {
      html += `
        <div style="background: var(--bg-hover); padding: 10px 14px; border-radius: var(--radius-sm); margin-bottom: 16px; font-size: 13px;">
          ${job.retry_of ? `<div style="margin-bottom: 4px;">↳ <strong>Retry of:</strong> <a href="#" onclick="openJobModal('${escapeHtml(job.retry_of)}'); return false;" style="color: var(--accent); font-family: monospace;">${escapeHtml(job.retry_of.slice(0, 10))}...</a></div>` : ''}
          ${job.retried_by ? `<div>↳ <strong>Retried by:</strong> <a href="#" onclick="openJobModal('${escapeHtml(job.retried_by)}'); return false;" style="color: var(--accent); font-family: monospace;">${escapeHtml(job.retried_by.slice(0, 10))}...</a></div>` : ''}
        </div>
      `;
    }

    if (job.error) {
      html += `
        <div style="background: rgba(244, 33, 46, 0.08); border: 1px solid var(--danger); border-radius: var(--radius-md); padding: 14px; margin-bottom: 20px;">
          <div style="font-weight: 600; color: var(--danger); font-size: 13px; margin-bottom: 6px;">Failure Diagnostic</div>
          <div style="font-family: monospace; font-size: 12px; color: var(--text); white-space: pre-wrap; word-break: break-all;">${escapeHtml(job.error)}</div>
        </div>
      `;
    }

    html += `
      <div style="margin-bottom: 24px;">
        <h4 style="font-size: 14px; font-weight: 600; margin-bottom: 12px;">Pipeline Stage Progression</h4>
        ${stages.length > 0 ? `
          <div class="timeline-stepper">
            ${stages.map((st, idx) => {
              const isLast = idx === stages.length - 1;
              let dotClass = 'timeline-dot';
              if (isLast && (job.status === 'failed' || job.status === 'cancelled')) {
                dotClass += ' failed';
              } else if (isLast && job.status === 'running') {
                dotClass += ' active';
              } else {
                dotClass += ' completed';
              }
              const stageName = typeof st === 'string' ? st : (st.stage || 'Unknown');
              const stageTime = typeof st === 'object' && st.at ? st.at.replace('T', ' ').slice(11, 19) : '';
              return `
                <div class="timeline-step">
                  <div class="${dotClass}"></div>
                  <div class="timeline-content">
                    <div class="timeline-title">${escapeHtml(stageName)}</div>
                    ${stageTime ? `<div class="timeline-time">${escapeHtml(stageTime)}</div>` : ''}
                  </div>
                </div>
              `;
            }).join('')}
          </div>
        ` : '<p style="color: var(--text-muted); font-size: 13px;">No stage events recorded.</p>'}
      </div>
    `;

    html += `
      <div style="display: flex; gap: 10px; justify-content: flex-end; align-items: center; border-top: 1px solid var(--border); padding-top: 16px;">
        ${(job.status === 'pending' || job.status === 'running') ? `
          <button class="btn btn-danger" onclick="cancelJobById('${escapeHtml(job.id)}')">Cancel Job</button>
        ` : ''}
        ${canRetry ? `
          <button class="btn btn-primary" onclick="retryJob('${escapeHtml(job.id)}')">Retry Job</button>
        ` : ''}
        <button class="btn btn-secondary" onclick="viewJobLogDirect('${escapeHtml(job.id)}')">View Full Log</button>
        <button class="btn btn-secondary" onclick="document.getElementById('job-detail-modal').close()">Close</button>
      </div>
    `;

    body.innerHTML = html;
    modal.showModal();
  } catch (e) {
    showNotification(`Could not load job details: ${e.message}`, 'error');
  }
}

function viewJobLogDirect(id) {
  const modal = document.getElementById('job-detail-modal');
  if (modal && modal.open) modal.close();
  const navBtns = document.querySelectorAll('.nav-btn');
  navBtns.forEach(b => b.classList.remove('active'));
  const logsBtn = document.querySelector('.nav-btn[data-page="logs"]');
  if (logsBtn) logsBtn.classList.add('active');
  currentPage = 'logs';
  renderPage('logs');
  setTimeout(() => viewPastLog(id), 100);
}

// --------------------------------------------------------------------------
// Page: Logs
// --------------------------------------------------------------------------

async function loadLogs(container) {
  const logText = (activeJobData && activeJobData.log) ? activeJobData.log : 'No active job running. Logs appear live as jobs execute.';
  container.innerHTML = `
    <div style="margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center;">
      <span style="font-size: 13px; color: var(--text-muted);">Real-time stream from Python engine</span>
      <button class="btn btn-secondary" onclick="pollStatus()">Refresh</button>
    </div>
    <div class="terminal-box" id="terminal-view">${escapeHtml(logText)}</div>

    <h3 style="font-size: 15px; margin: 24px 0 12px;">Past Job Logs</h3>
    <div id="past-logs-list"><p style="color: var(--text-muted); font-size: 13px;">Loading...</p></div>
  `;

  try {
    const res = await fetch('/api/logs');
    const ids = await res.json();
    const listEl = document.getElementById('past-logs-list');
    if (!ids.length) {
      listEl.innerHTML = '<p style="color: var(--text-muted); font-size: 13px;">No past job logs yet.</p>';
      return;
    }
    listEl.innerHTML = `
      <div style="display: flex; flex-direction: column; gap: 6px;">
        ${ids.map(id => `
          <button class="btn btn-secondary" style="text-align: left; font-family: monospace; font-size: 12px;" onclick="viewPastLog('${id}')">${escapeHtml(id)}</button>
        `).join('')}
      </div>
    `;
  } catch (e) {
    document.getElementById('past-logs-list').innerHTML = `<p style="color: var(--danger); font-size: 13px;">Failed to load past logs: ${e.message}</p>`;
  }
}

async function viewPastLog(id) {
  try {
    const res = await fetch(`/api/logs/file?id=${encodeURIComponent(id)}`);
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    const term = document.getElementById('terminal-view');
    term.textContent = data.log;
    term.scrollTop = 0;
  } catch (e) {
    showNotification(e.message, 'error');
  }
}

// --------------------------------------------------------------------------
// First-Run Wizard
// --------------------------------------------------------------------------

function setupWizard() {
  const modal = document.getElementById('wizard-modal');
  const btn = document.getElementById('btn-open-wizard');
  const close = document.getElementById('btn-close-wizard');

  btn.addEventListener('click', () => {
    loadWizardStep(1);
    modal.showModal();
  });

  close.addEventListener('click', () => modal.close());
}

let wizardData = { name: '', handle: '', provider: 'gemini', api_key: '', subreddits: [] };

function loadWizardStep(step) {
  const body = document.getElementById('wizard-body');
  document.querySelectorAll('.wizard-step').forEach((s, idx) => {
    s.classList.toggle('active', idx + 1 === step);
  });

  if (step === 1) {
    body.innerHTML = `
      <div class="form-group">
        <label>Your Channel Name</label>
        <input type="text" class="form-input" id="wiz-name" value="${escapeHtml(wizardData.name || 'Tech Pulse')}" placeholder="e.g. Daily Curiosities">
      </div>
      <div class="form-group">
        <label>Your Channel Handle</label>
        <input type="text" class="form-input" id="wiz-handle" value="${escapeHtml(wizardData.handle || '@TechPulse')}" placeholder="e.g. @DailyCuriosities">
      </div>
      <div style="display: flex; justify-content: flex-end; margin-top: 24px;">
        <button class="btn btn-primary" onclick="wizardNext(1)">Next: AI Provider →</button>
      </div>
    `;
  } else if (step === 2) {
    body.innerHTML = `
      <div class="form-group">
        <label>Select Your AI Editorial Provider</label>
        <select class="form-input" id="wiz-provider">
          <option value="gemini">Google Gemini (Recommended — Generous Free Tier)</option>
          <option value="anthropic">Anthropic Claude</option>
          <option value="openai">OpenAI GPT-4o</option>
        </select>
      </div>
      <div class="form-group">
        <label>Paste API Key</label>
        <input type="password" class="form-input" id="wiz-key" value="${escapeHtml(wizardData.api_key)}" placeholder="AIzaSy... or sk-...">
        <p class="form-help">Keys are stored locally only on your PC in studio-secrets.json.</p>
      </div>
      <div style="display: flex; justify-content: space-between; margin-top: 24px;">
        <button class="btn btn-secondary" onclick="loadWizardStep(1)">← Back</button>
        <button class="btn btn-primary" onclick="wizardNext(2)">Next: Subreddits →</button>
      </div>
    `;
  } else if (step === 3) {
    body.innerHTML = `
      <div class="form-group">
        <label>Choose Starting Subreddits for Viral Discovery</label>
        <p class="form-help" style="margin-bottom: 12px;">You can add and customize more in the Subreddits tab anytime.</p>
        <div style="display: flex; flex-direction: column; gap: 8px;">
          <label><input type="checkbox" id="wiz-sub-tech" checked> r/technology & r/gadgets (Tech news)</label>
          <label><input type="checkbox" id="wiz-sub-ai" checked> r/artificial & r/ChatGPT (AI developments)</label>
          <label><input type="checkbox" id="wiz-sub-space" checked> r/space (Astronomy & rocket launches)</label>
          <label><input type="checkbox" id="wiz-sub-gaming" checked> r/pcgaming (Gaming)</label>
        </div>
      </div>
      <div style="display: flex; justify-content: space-between; margin-top: 24px;">
        <button class="btn btn-secondary" onclick="loadWizardStep(2)">← Back</button>
        <button class="btn btn-primary" onclick="wizardFinish()">Complete Setup & Launch 🚀</button>
      </div>
    `;
  }
}

function wizardNext(currentStep) {
  if (currentStep === 1) {
    wizardData.name = document.getElementById('wiz-name').value.trim();
    wizardData.handle = document.getElementById('wiz-handle').value.trim();
    loadWizardStep(2);
  } else if (currentStep === 2) {
    wizardData.provider = document.getElementById('wiz-provider').value;
    wizardData.api_key = document.getElementById('wiz-key').value.trim();
    loadWizardStep(3);
  }
}

async function wizardFinish() {
  const subs = [];
  if (document.getElementById('wiz-sub-tech').checked) {
    subs.push({ name: 'technology', category: 'Tech', min_score: 250 });
    subs.push({ name: 'gadgets', category: 'Tech', min_score: 200 });
  }
  if (document.getElementById('wiz-sub-ai').checked) {
    subs.push({ name: 'artificial', category: 'AI', min_score: 150 });
  }
  if (document.getElementById('wiz-sub-space').checked) {
    subs.push({ name: 'space', category: 'Science', min_score: 200 });
  }
  if (document.getElementById('wiz-sub-gaming').checked) {
    subs.push({ name: 'pcgaming', category: 'Gaming', min_score: 250 });
  }
  wizardData.subreddits = subs;

  try {
    const res = await postJSON('/api/wizard', wizardData);
    if (!res.ok) throw new Error('Setup failed');
    showNotification('Setup complete! Welcome to KenauShorts.');
    document.getElementById('wizard-modal').close();
    renderPage('overview');
  } catch (e) {
    showNotification(e.message, 'error');
  }
}

function escapeHtml(str) {
  return String(str || '').replace(/[&<>'"]/g, tag => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    "'": '&#39;',
    '"': '&quot;'
  }[tag] || tag));
}
