/**
 * KenauShorts — Frontend Health UI Unit Tests (Node.js test suite)
 *
 * Verifies pure health telemetry state evaluation, summary messaging,
 * and subsystem card rendering across all specified states:
 * - healthy
 * - degraded
 * - unhealthy (critical)
 * - unavailable (network failure, timeout, daemon offline)
 * - stale (age > 45s)
 * - malformed (invalid payload schema)
 */

const assert = require('assert');
const {
  evaluateHealthState,
  getHealthSummaryMessage,
  renderHealthSubsystemCards,
  pollStatus,
  getPollStatusInFlight,
} = require('../studio/web/app.js');

async function runTests() {
  console.log('Running test_health_ui.js...');
  let passed = 0;

  // Test 1: Null / Undefined input -> unavailable
  {
    const state = evaluateHealthState(null, 0);
    assert.strictEqual(state.state, 'unavailable');
    assert.strictEqual(state.label, 'Unavailable');
    assert.strictEqual(state.severity, 'danger');
    assert.strictEqual(state.stale, false);
    passed++;
  }

  // Test 2: Explicit unavailable payload (e.g. 503 or network abort) -> unavailable
  {
    const payload = {
      status: 'unavailable',
      error: 'HTTP 503: Service Unavailable',
      timestamp: new Date().toISOString(),
      subsystems: {},
    };
    const state = evaluateHealthState(payload, Date.now());
    assert.strictEqual(state.state, 'unavailable');
    assert.strictEqual(state.label, 'Unavailable');
    assert.strictEqual(state.severity, 'danger');
    assert.strictEqual(state.message, 'HTTP 503: Service Unavailable');
    passed++;
  }

  // Test 3: Malformed payload -> malformed
  {
    const payload = {
      status: 'malformed',
      error: 'Invalid telemetry response from server',
      timestamp: new Date().toISOString(),
      subsystems: {},
    };
    const state = evaluateHealthState(payload, Date.now());
    assert.strictEqual(state.state, 'malformed');
    assert.strictEqual(state.label, 'Malformed');
    assert.strictEqual(state.severity, 'danger');
    assert.strictEqual(state.message, 'Invalid telemetry response from server');
    passed++;
  }

  // Test 4: Stale telemetry (timestamp > 45s in the past) -> stale state
  {
    const pastTime = new Date(Date.now() - 60000).toISOString(); // 60s ago
    const payload = {
      status: 'healthy',
      timestamp: pastTime,
      subsystems: { database: { status: 'healthy', latency_sec: 0.001 } },
    };
    const state = evaluateHealthState(payload, Date.now() - 60000);
    assert.strictEqual(state.state, 'stale');
    assert.strictEqual(state.stale, true);
    assert.strictEqual(state.severity, 'warning');
    assert(state.label.includes('Stale (60s ago)'));
    assert(state.message.includes('Telemetry is stale. Last update was 60s ago.'));
    passed++;
  }

  // Test 5: Fresh Healthy status -> healthy
  {
    const freshTime = new Date().toISOString();
    const payload = {
      status: 'healthy',
      timestamp: freshTime,
      subsystems: {
        database: { status: 'healthy', latency_sec: 0.002 },
        storage: { status: 'healthy', free_mb: 25000 },
        scheduler: { stalled: false, enabled: true },
        queue_worker: { stalled: false, thread_alive: false, pending_jobs_count: 0 },
        active_job: { stalled: false, status: 'idle' },
        alerts: { drain_healthy: true, queue_depth: 0 },
      },
    };
    const state = evaluateHealthState(payload, Date.now());
    assert.strictEqual(state.state, 'healthy');
    assert.strictEqual(state.label, 'Healthy');
    assert.strictEqual(state.severity, 'success');
    assert.strictEqual(state.message, 'All subsystems operational.');
    passed++;
  }

  // Test 6: Fresh Degraded status -> degraded
  {
    const freshTime = new Date().toISOString();
    const payload = {
      status: 'degraded',
      timestamp: freshTime,
      subsystems: {
        database: { status: 'healthy', latency_sec: 0.002 },
        storage: { status: 'low', free_mb: 750 },
        scheduler: { stalled: false, enabled: true },
      },
    };
    const state = evaluateHealthState(payload, Date.now());
    assert.strictEqual(state.state, 'degraded');
    assert.strictEqual(state.label, 'Degraded');
    assert.strictEqual(state.severity, 'warning');
    assert(state.message.includes('Low disk space (<1GB free: 750MB)'));
    passed++;
  }

  // Test 7: Fresh Unhealthy status -> unhealthy
  {
    const freshTime = new Date().toISOString();
    const payload = {
      status: 'unhealthy',
      timestamp: freshTime,
      subsystems: {
        database: { status: 'unresponsive', error: 'Database lock timed out' },
        storage: { status: 'critical', free_mb: 200 },
        queue_worker: { stalled: true, thread_alive: false, pending_jobs_count: 3 },
      },
    };
    const state = evaluateHealthState(payload, Date.now());
    assert.strictEqual(state.state, 'unhealthy');
    assert.strictEqual(state.label, 'Critical / Unhealthy');
    assert.strictEqual(state.severity, 'danger');
    assert(state.message.includes('Database probe unresponsive'));
    assert(state.message.includes('Critical disk space (<512MB free: 200MB)'));
    assert(state.message.includes('Queue worker dead (3 pending)'));
    passed++;
  }

  // Test 8: getHealthSummaryMessage detail checks
  {
    const singleIssue = {
      status: 'degraded',
      subsystems: {
        scheduler: { stalled: true, overdue_seconds: 42 },
      },
    };
    const msg = getHealthSummaryMessage(singleIssue);
    assert.strictEqual(msg, 'Scheduler stalled (overdue 42s)');

    const jobStalled = {
      status: 'unhealthy',
      subsystems: {
        active_job: { stalled: true, job_id: 'job_456' },
      },
    };
    const jobMsg = getHealthSummaryMessage(jobStalled);
    assert.strictEqual(jobMsg, 'Job job_456 progress stalled (>300s)');

    const alertFailure = {
      status: 'unhealthy',
      subsystems: {
        alerts: { drain_healthy: false },
      },
    };
    const alertMsg = getHealthSummaryMessage(alertFailure);
    assert.strictEqual(alertMsg, 'Alert engine drain worker failure');
    passed++;
  }

  // Test 9: renderHealthSubsystemCards output and XSS sanitization
  {
    const stateInfo = { state: 'healthy', message: 'All operational' };
    const payload = {
      subsystems: {
        database: { status: 'healthy', latency_sec: 0.005 },
        storage: { status: 'healthy', free_mb: 10240 },
        scheduler: { stalled: false, enabled: true, scheduler_state: 'idle <script>alert(1)</script>' },
        queue_worker: { stalled: false, thread_alive: true, pending_jobs_count: 0 },
        active_job: { stalled: false, status: 'running', stage: 'audio_gen <img src=x onerror=alert(1)>', elapsed_without_progress_sec: 12 },
        alerts: { drain_healthy: true, queue_depth: 0, engine_state: 'ready' },
      },
    };
    const html = renderHealthSubsystemCards(payload, stateInfo);
    assert(html.includes('subsystem-card healthy'));
    assert(html.includes('SQLite Database'));
    assert(html.includes('5ms probe latency'));
    assert(html.includes('10.0 GB Free'));
    assert(html.includes('Audio Gen') || html.includes('audio_gen'));
    // Confirm raw HTML tags were escaped
    assert(!html.includes('<script>alert(1)</script>'));
    assert(html.includes('&lt;script&gt;alert(1)&lt;/script&gt;'));
    assert(!html.includes('<img src=x onerror=alert(1)>'));
    assert(html.includes('&lt;img src=x onerror=alert(1)&gt;'));
    passed++;
  }

  // Test 10: Empty subsystems fallback rendering
  {
    const stateInfo = { state: 'unavailable', message: 'Daemon offline' };
    const emptyHtml = renderHealthSubsystemCards(null, stateInfo);
    assert(emptyHtml.includes('No subsystem metrics available (Daemon offline)'));
    passed++;
  }

  // Test 11: Critical + Stale precedence: critical failure is never downgraded to warning
  {
    const pastTime = new Date(Date.now() - 75000).toISOString(); // 75s ago
    const payload = {
      status: 'unhealthy',
      timestamp: pastTime,
      subsystems: {
        database: { status: 'unresponsive', error: 'Lock timeout' },
      },
    };
    const state = evaluateHealthState(payload, Date.now() - 75000);
    assert.strictEqual(state.state, 'unhealthy');
    assert.strictEqual(state.severity, 'danger');
    assert.strictEqual(state.stale, true);
    assert.strictEqual(state.dataAgeSec, 75);
    assert(state.label.includes('Critical / Stale (75s ago)'));
    assert(state.message.includes('stale: 75s ago'));
    passed++;
  }

  // Test 12: Telemetry recovery: fresh response immediately restores healthy state
  {
    // Step A: System had failure
    const failedPayload = { status: 'unavailable', error: 'Network dropped' };
    const failedState = evaluateHealthState(failedPayload, 0);
    assert.strictEqual(failedState.state, 'unavailable');

    // Step B: Fresh response arrives
    const freshPayload = {
      status: 'healthy',
      timestamp: new Date().toISOString(),
      subsystems: { database: { status: 'healthy', latency_sec: 0.001 } },
    };
    const recoveredState = evaluateHealthState(freshPayload, Date.now());
    assert.strictEqual(recoveredState.state, 'healthy');
    assert.strictEqual(recoveredState.severity, 'success');
    assert.strictEqual(recoveredState.stale, false);
    passed++;
  }

  // Test 13: Polling guard lifecycle and failure independence:
  // - pollStatusInFlight resets via finally on error
  // - Guard recovers immediately for next poll
  // - /api/status updates independently of /api/health failure
  {
    const origFetch = global.fetch;
    const origDoc = global.document;

    const mockElements = {};
    function mockElem(id) {
      if (!mockElements[id]) {
        mockElements[id] = { id, className: '', textContent: '', style: {}, hasAttribute: () => false };
      }
      return mockElements[id];
    }

    global.document = {
      getElementById: (id) => mockElem(id),
      querySelectorAll: () => [],
    };

    try {
      // Step A: Fatal fetch exception during poll
      global.fetch = async () => {
        throw new Error('Connection abruptly closed by peer');
      };

      assert.strictEqual(getPollStatusInFlight(), false);
      await pollStatus();
      // Must be reset to false in finally
      assert.strictEqual(getPollStatusInFlight(), false);
      assert.strictEqual(mockElem('connection-status').textContent, 'Offline • Daemon unreachable');

      // Step B: Next poll immediately allowed to execute (no permanent lock)
      // /api/status succeeds (200 OK) while /api/health fails (500)
      global.fetch = async (url) => {
        if (url === '/api/status') {
          return {
            ok: true,
            json: async () => ({
              online: true,
              platform: 'TestPlatform',
              active_job: { stage: 'Rendering test Short' },
              queue_count: 2,
              csrf: 'csrf_valid_token_123',
            }),
          };
        }
        if (url === '/api/health') {
          return {
            ok: false,
            status: 500,
            statusText: 'Internal Server Error',
          };
        }
        return { ok: false };
      };

      await pollStatus();

      // Guard must be released
      assert.strictEqual(getPollStatusInFlight(), false);
      // Status dot and text must reflect telemetry warning without marking daemon offline
      assert.strictEqual(mockElem('status-dot').className, 'status-indicator degraded');
      assert(mockElem('connection-status').textContent.includes('Online • Telemetry Unavailable'));
      // /api/status fields were successfully applied
      assert.strictEqual(mockElem('badge-mode').textContent, 'Running: Rendering test Short (2 queued)');
      assert.strictEqual(mockElem('btn-cancel-job').style.display, 'inline-flex');

      passed++;
    } finally {
      global.fetch = origFetch;
      global.document = origDoc;
    }
  }

  console.log(`All ${passed} UI health unit tests passed successfully!`);
}

runTests().catch(err => {
  console.error('Test failure:', err);
  process.exit(1);
});
