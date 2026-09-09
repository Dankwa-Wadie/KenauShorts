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
    case 'library':
      title.textContent = 'Drafts & Video Library';
      eyebrow.textContent = 'MEDIA';
      loadLibrary(view);
      break;
    case 'subreddits':
      title.textContent = 'Subreddit Scraper Manager';
      eyebrow.textContent = 'DISCOVERY';
      loadSubreddits(view);
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
    if (activeJobData) {
      badgeMode.className = 'badge running';
      badgeMode.textContent = `Running: ${activeJobData.stage || 'In progress'}`;
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
// Quick Actions
// --------------------------------------------------------------------------

function setupQuickActions() {
  document.getElementById('btn-quick-preview').addEventListener('click', async () => {
    try {
      const res = await postJSON('/api/job', { action: 'preview' });
      const data = await res.json();
      if (data.error) throw new Error(data.error);
      showNotification('Preview draft job started! Check the Library or Logs tab.');
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
      showNotification('Run & publish job launched! Monitoring progress in Logs.');
      pollStatus();
    } catch (e) {
      showNotification(e.message, 'error');
    }
  });
}

// --------------------------------------------------------------------------
// Page: Overview
// --------------------------------------------------------------------------

async function loadOverview(container) {
  container.innerHTML = '<p>Loading statistics...</p>';
  try {
    const [statusRes, videosRes] = await Promise.all([
      fetch('/api/status').then(r => r.json()),
      fetch('/api/videos').then(r => r.json())
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

      <div style="background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 24px; margin-bottom: 24px;">
        <h3 style="font-size: 18px; margin-bottom: 12px;">Active Pipeline Engine</h3>
        <p style="color: var(--text-muted); font-size: 14px; margin-bottom: 20px;">
          KenauShorts runs locally in the background on your PC. It monitors configured subreddits & YouTube channels, performs AI editorial selection, and composites 1080x1920 shorts automatically.
        </p>
        <div style="display: flex; gap: 12px;">
          <button class="btn btn-secondary" onclick="document.querySelector('[data-page=library]').click()">Browse Drafts (${ready})</button>
          <button class="btn btn-secondary" onclick="document.querySelector('[data-page=subreddits]').click()">Manage Subreddits</button>
          <button class="btn btn-secondary" onclick="document.querySelector('[data-page=logs]').click()">View Terminal</button>
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

async function loadLibrary(container) {
  container.innerHTML = '<p>Loading drafts & video library...</p>';
  try {
    const res = await fetch('/api/videos');
    const videos = await res.json();

    if (!videos || videos.length === 0) {
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
      <div class="video-grid">
        ${videos.map(v => {
          const posterUrl = v.poster ? `/media/${v.poster.replace(/\\/g, '/')}` : '';
          return `
            <div class="video-card" onclick="openVideoModal('${v.id}')">
              <div class="video-thumb">
                ${posterUrl ? `<img src="${posterUrl}" alt="Preview" onerror="this.style.display='none'">` : ''}
                <span class="status-pill ${v.status}">${v.status}</span>
              </div>
              <div class="video-info">
                <h4>${escapeHtml(v.title || v.headline || 'Untitled Short')}</h4>
                <div class="video-meta">
                  <span>${v.candidate ? v.candidate.channel : 'Local'}</span> • 
                  <span>${new Date(v.created_at).toLocaleDateString()}</span>
                </div>
              </div>
            </div>
          `;
        }).join('')}
      </div>
    `;
  } catch (e) {
    container.innerHTML = `<p style="color: var(--danger)">Failed to load library: ${e.message}</p>`;
  }
}

async function openVideoModal(id) {
  try {
    const res = await fetch(`/api/video?id=${id}`);
    const v = await res.json();

    const modal = document.getElementById('video-modal');
    const body = document.getElementById('modal-body');
    const videoUrl = `/media/${v.video.replace(/\\/g, '/')}`;

    body.innerHTML = `
      <div style="display: grid; grid-template-columns: 320px 1fr; gap: 24px;">
        <div style="aspect-ratio: 9/16; background: #000; border-radius: var(--radius-md); overflow: hidden;">
          <video controls autoplay loop src="${videoUrl}" style="width: 100%; height: 100%; object-fit: contain;"></video>
        </div>
        <div>
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
            <label>YouTube Description</label>
            <textarea class="form-input" id="edit-desc" rows="4">${escapeHtml(v.description || '')}</textarea>
          </div>

          <div style="display: flex; gap: 12px; margin-top: 24px;">
            <button class="btn btn-secondary" onclick="saveVideoEdits('${v.id}')">💾 Save Details</button>
            <button class="btn btn-secondary" onclick="reRenderDraft('${v.id}')">🔄 Re-render Card</button>
            ${v.status !== 'uploaded' ? `<button class="btn btn-primary" onclick="uploadDraft('${v.id}')">🚀 Upload to YouTube</button>` : `<span class="badge" style="color: var(--success); padding: 10px 16px;">✓ Uploaded to YouTube</span>`}
          </div>
        </div>
      </div>
    `;

    document.getElementById('btn-close-modal').onclick = () => modal.close();
    modal.showModal();
  } catch (e) {
    showNotification(`Could not open draft: ${e.message}`, 'error');
  }
}

async function saveVideoEdits(id) {
  const title = document.getElementById('edit-title').value;
  const headline = document.getElementById('edit-headline').value;
  const description = document.getElementById('edit-desc').value;

  try {
    const res = await postJSON('/api/video', { id, title, headline, description });
    if (!res.ok) throw new Error('Save failed');
    showNotification('Draft details saved.');
  } catch (e) {
    showNotification(e.message, 'error');
  }
}

async function reRenderDraft(id) {
  await saveVideoEdits(id);
  try {
    const res = await postJSON('/api/job', { action: 'render', key: id });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    showNotification('Re-render job started! Check terminal for progress.');
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
    if (data.error) throw new Error(data.error);
    showNotification('Upload job started! Monitoring progress.');
    document.getElementById('video-modal').close();
    pollStatus();
  } catch (e) {
    showNotification(e.message, 'error');
  }
}

// --------------------------------------------------------------------------
// Page: Subreddit Scraper Manager (User-Configured Subreddits)
// --------------------------------------------------------------------------

async function loadSubreddits(container) {
  container.innerHTML = '<p>Loading subreddits...</p>';
  try {
    const res = await fetch('/api/subreddits');
    const subreddits = await res.json();

    container.innerHTML = `
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
        <button class="btn btn-secondary" onclick="testNewSubreddit()">🔍 Test Scrape</button>
        <button class="btn btn-primary" onclick="addNewSubreddit()">+ Add Subreddit</button>
      </div>

      <div id="sub-test-results" style="margin-bottom: 20px;"></div>

      <table class="sub-table">
        <thead>
          <tr>
            <th>Subreddit</th>
            <th>Category</th>
            <th>Min Upvotes</th>
            <th>Actions</th>
          </tr>
        </thead>
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
                <td>
                  <button class="btn btn-danger" style="padding: 4px 10px; font-size: 12px;" onclick="deleteSubreddit('${escapeHtml(name)}')">Remove</button>
                </td>
              </tr>
            `;
          }).join('')}
        </tbody>
      </table>
    `;
  } catch (e) {
    container.innerHTML = `<p style="color: var(--danger)">Failed to load subreddits: ${e.message}</p>`;
  }
}

async function testNewSubreddit() {
  const name = document.getElementById('new-sub-name').value.trim();
  const box = document.getElementById('sub-test-results');
  if (!name) {
    alert('Please enter a subreddit name to test.');
    return;
  }
  box.innerHTML = `<p style="color: var(--accent); font-size: 13px;">Testing scraper on r/${name}...</p>`;

  try {
    const res = await postJSON('/api/subreddits/test', { name });
    const data = await res.json();
    if (data.error) throw new Error(data.error);

    box.innerHTML = `
      <div style="background: #11141b; border: 1px solid var(--border); border-radius: var(--radius-sm); padding: 16px;">
        <h4 style="font-size: 14px; color: var(--success); margin-bottom: 8px;">✓ Scrape successful! Found ${data.count} posts from r/${data.subreddit}</h4>
        <ul style="font-size: 13px; color: var(--text-muted); list-style: none;">
          ${data.sample.map(p => `<li style="margin-bottom: 4px;">• <strong>[${p.score} pts]</strong> ${escapeHtml(p.title)}</li>`).join('')}
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

  if (!name) {
    alert('Please enter a subreddit name.');
    return;
  }

  try {
    const res = await postJSON('/api/subreddits', { action: 'add', name, category, min_score });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    showNotification(`Added r/${name} to scraping list.`);
    loadSubreddits(document.getElementById('content-view'));
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
    loadSubreddits(document.getElementById('content-view'));
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
        <div style="background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 24px;">
          <h3 style="font-size: 16px; margin-bottom: 16px;">AI Editorial Keys</h3>
          <p style="color: var(--text-muted); font-size: 13px; margin-bottom: 20px;">
            At least one key is needed for Claude, Gemini, or OpenAI to evaluate viral candidates and generate punchy headlines.
          </p>

          <div class="form-group">
            <label>Google Gemini API Key ${conn.gemini ? '<span style="color:var(--success); font-size:12px;">(✓ Configured)</span>' : ''}</label>
            <input type="password" class="form-input" id="key-gemini" placeholder="${conn.gemini_preview || 'AIzaSy...'}">
            <p class="form-help">Free tier available at <a href="https://aistudio.google.com/app/apikey" target="_blank" style="color:var(--accent);">aistudio.google.com</a>.</p>
          </div>

          <div class="form-group">
            <label>Anthropic (Claude) API Key ${conn.anthropic ? '<span style="color:var(--success); font-size:12px;">(✓ Configured)</span>' : ''}</label>
            <input type="password" class="form-input" id="key-anthropic" placeholder="sk-ant-...">
          </div>

          <div class="form-group">
            <label>OpenAI API Key ${conn.openai ? '<span style="color:var(--success); font-size:12px;">(✓ Configured)</span>' : ''}</label>
            <input type="password" class="form-input" id="key-openai" placeholder="sk-...">
          </div>

          <button class="btn btn-primary" onclick="saveApiKeys()">Save API Keys</button>
        </div>

        <!-- YouTube OAuth -->
        <div style="background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 24px;">
          <h3 style="font-size: 16px; margin-bottom: 16px;">YouTube Publishing OAuth</h3>
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

    container.innerHTML = `
      <div style="max-width: 600px; background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 24px;">
        <h3 style="font-size: 16px; margin-bottom: 20px;">Always-On Background Automation</h3>

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
    `;
  } catch (e) {
    container.innerHTML = `<p style="color: var(--danger)">Failed to load automation: ${e.message}</p>`;
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
        <div style="background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 24px;">
          <h3 style="font-size: 16px; margin-bottom: 16px;">Channel Identity</h3>
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

        <div style="background: var(--bg-card); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 24px;">
          <h3 style="font-size: 16px; margin-bottom: 16px;">Card Styling</h3>
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
        </div>
      </div>
    `;
  } catch (e) {
    container.innerHTML = `<p style="color: var(--danger)">Failed to load style: ${e.message}</p>`;
  }
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
// Page: Logs
// --------------------------------------------------------------------------

function loadLogs(container) {
  const logText = (activeJobData && activeJobData.log) ? activeJobData.log : 'No active job running. Logs appear live as jobs execute.';
  container.innerHTML = `
    <div style="margin-bottom: 12px; display: flex; justify-content: space-between; align-items: center;">
      <span style="font-size: 13px; color: var(--text-muted);">Real-time stream from Python engine</span>
      <button class="btn btn-secondary" onclick="pollStatus()">Refresh</button>
    </div>
    <div class="terminal-box" id="terminal-view">${escapeHtml(logText)}</div>
  `;
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
