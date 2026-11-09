// DeepBell dashboard: plain JavaScript, no framework and no build step.
// It polls the API, renders each view, drives the guided demo panel and lights up the
// "How it works" pipeline from the server's own pipeline state.
'use strict';

const CONFIG = JSON.parse(document.getElementById('deepbell-config').textContent);
const INITIAL_REPORT = JSON.parse(document.getElementById('deepbell-report').textContent);

const VIEWS = {
    overview: ['Overview', 'Self-monitoring incident response lab'],
    incidents: ['Incidents', 'Detect, report, recover'],
    alerts: ['Alerts', 'Rules, states and notifications'],
    telemetry: ['Live Telemetry', 'CPU and memory of this process'],
    chaos: ['Chaos Lab', 'Capped fault injection'],
    timemachine: ['Time Machine', 'Snapshots and rollback'],
    about: ['About', 'Why and how it is built'],
    settings: ['Settings', 'Local run only'],
};

const KIND_ICONS = { cpu_spike: 'cpu', memory_leak: 'memory', db_corruption: 'layers' };
const STATUS_TONES = { open: 'bad', resolved: 'ok', interrupted: 'neutral' };
const STATUS_LABELS = { open: 'Open', resolved: 'Resolved', interrupted: 'Interrupted' };
const EVENT_LABELS = {
    fault: 'Fault', flagged: 'Flagged', detected: 'Detected', report: 'Report',
    action: 'Action', alert: 'Alert', restore: 'Restore', resolved: 'Resolved', interrupted: 'Restart',
};
const CONFIDENCE_TONES = { high: 'ok', medium: 'warn', low: 'info', unknown: 'neutral' };
const SOURCE_LABELS = {
    gemini: ['Gemini via LangChain', 'violet'],
    template: ['Built-in template', 'info'],
    'template-fallback': ['Template fallback (AI call failed)', 'warn'],
    example: ['Example, synthetic data', 'neutral'],
};
const ALERT_TONES = { ok: 'ok', pending: 'warn', firing: 'bad', resolved: 'info' };
const ALERT_LABELS = { ok: 'OK', pending: 'Pending', firing: 'Firing', resolved: 'Resolved' };
const SEVERITY_TONES = { info: 'info', warning: 'warn', critical: 'bad' };
const STAGE_NAMES = { sample: 'Sample', detect: 'Detect', report: 'Report', recover: 'Recover' };
// Short enough for the sidebar's status box.
const SHORT_NAMES = { cpu_spike: 'CPU spike', memory_leak: 'memory leak', db_corruption: 'DB corruption' };

const state = {
    view: 'overview',
    targetIncident: null, // incident id from a #incidents/<id> link
    status: null,
    eventSeq: 0,
    incidentsRev: null,
    incidents: [],
    incidentsLoaded: false,
    alerts: null,
    causeKey: null, // the incident, status and timeline length the cause on screen was computed for
    causeFor: null,
    filter: 'all',
    selectedIncident: null,
    reports: [],
    latestReport: undefined,
    reportName: null, // report shown in the Incidents viewer
    reportPinned: false, // true once the visitor picks a report; otherwise follow the newest
    awaitManualReport: false, // "Write a report now" was pressed: show the next new report
    myDemoId: null, // the run this browser started, if any
    announcedSteps: new Set(),
    demoState: undefined,
    pipelineKey: null,
    bannerTitle: null,
    telemetryRange: 120, // seconds shown on Live Telemetry
    telemetrySeq: 0,
    lastSampleAt: null,
    drawnAt: 0,
    rows: [],
    serverOffsetMs: 0,
    offline: false,
    offlineAttempts: 0,
    lastSnapshot: undefined,
    lastDbOk: true,
    snapshots: [],
    history: null,
    stripIndex: null,
    knownTx: new Set(),
    resizeTimer: null,
};

// ---------- Helpers ----------

const $ = id => document.getElementById(id);

function esc(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({
        '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
}

const icon = name => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;

function parseTime(iso) {
    if (!iso) return null; // new Date(null) would be 1970, not "no time"
    const d = new Date(iso);
    return isNaN(d) ? null : d;
}

// The API sends UTC (ISO-8601); the UI shows the viewer's local time, 24-hour.
function fmtTime(iso) {
    const d = parseTime(iso);
    return d ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) : '–';
}

function fmtDateTime(iso) {
    const d = parseTime(iso);
    return d ? d.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) : '–';
}

function fmtDuration(sec) {
    if (sec === null || sec === undefined) return '–';
    sec = Math.round(sec);
    if (sec < 60) return `${sec} s`;
    const m = Math.floor(sec / 60);
    return sec % 60 ? `${m} min ${sec % 60} s` : `${m} min`;
}

function fmtUptime(sec) {
    if (sec < 60) return `${sec} s`;
    if (sec < 3600) return `${Math.floor(sec / 60)} min`;
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    if (h < 48) return m ? `${h} h ${m} min` : `${h} h`;
    return `${Math.floor(h / 24)} days`;
}

function ago(iso) {
    const d = parseTime(iso);
    if (!d) return '';
    const s = Math.max(0, Math.round((Date.now() + state.serverOffsetMs - d.getTime()) / 1000));
    if (s < 60) return `${s} s ago`;
    if (s < 3600) return `${Math.floor(s / 60)} min ago`;
    return `${Math.floor(s / 3600)} h ago`;
}

async function api(method, url, body) {
    try {
        const res = await fetch(url, { method, body, cache: 'no-store' });
        let data = null;
        try { data = await res.json(); } catch (e) { data = null; }
        const detail = data && (typeof data.detail === 'string' ? data.detail : data.message);
        return { ok: res.ok, status: res.status, data, detail: detail || `Request failed (${res.status}).` };
    } catch (err) {
        return { ok: false, status: 0, data: null, detail: 'Could not reach the server. Check the connection and try again.' };
    }
}

function setLoading(btn, loading) {
    if (!btn) return;
    btn.classList.toggle('is-loading', loading);
    btn.disabled = loading;
    btn.setAttribute('aria-busy', String(loading));
}

const reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// Lists re-render while the page polls. setHTML replaces an element's markup only when it
// changed, and keeps keyboard focus: a focused link or button that carries a data-focus key is
// focused again in the new markup, so a poll never drops focus to the page.
const renderedHTML = new WeakMap();

function setHTML(el, html) {
    if (renderedHTML.get(el) === html) return false;
    const active = document.activeElement;
    const key = active && active !== el && el.contains(active) ? active.dataset.focus : undefined;
    el.innerHTML = html;
    renderedHTML.set(el, html);
    if (key !== undefined) el.querySelector(`[data-focus="${CSS.escape(key)}"]`)?.focus({ preventScroll: true });
    return true;
}

// One polite live region for results that arrive on their own (pipeline steps, demo steps,
// the connection). Messages that arrive together are read as one.
let announceTimer = null;

function announce(text) {
    const el = $('sr-announce');
    el.textContent = announceTimer ? `${el.textContent} ${text}` : text;
    clearTimeout(announceTimer);
    announceTimer = setTimeout(() => { announceTimer = null; }, 400);
}

// ---------- Theme ----------

function applyTheme(theme) {
    document.documentElement.dataset.theme = theme;
    const toggle = $('theme-toggle');
    toggle.setAttribute('aria-pressed', String(theme === 'light'));
    $('theme-label').textContent = theme === 'light' ? 'Dark theme' : 'Light theme';
    document.querySelector('meta[name="theme-color"]').content = theme === 'light' ? '#eef2f8' : '#03050c';
    restyleCharts();
}

function toggleTheme() {
    const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    try { localStorage.setItem('deepbell.theme', next); } catch (e) { /* storage unavailable */ }
    applyTheme(next);
}

// ---------- Toasts and confirmation ----------

const TOAST_ICONS = { success: 'check', warn: 'incident', error: 'incident', info: 'info' };

// Toasts never block the page: they stack in a corner, can be dismissed and expire.
function toast(message, type = 'info', timeout = 6000) {
    const stack = $('toasts');
    const el = document.createElement('div');
    el.className = 'toast';
    el.dataset.type = type;
    if (type === 'error') el.setAttribute('role', 'alert');
    el.innerHTML = `${icon(TOAST_ICONS[type] || 'info')}<p></p><button type="button" class="toast-close" aria-label="Dismiss">${icon('x')}</button>`;
    el.querySelector('p').textContent = message;
    const remove = () => {
        el.classList.add('leaving');
        setTimeout(() => el.remove(), 200);
    };
    el.querySelector('button').addEventListener('click', remove);
    stack.append(el);
    while (stack.children.length > 3) stack.firstElementChild.remove();
    setTimeout(remove, timeout);
}

function confirmAction({ title, body, confirmLabel = 'Confirm', danger = false }) {
    const dialog = $('confirm');
    $('confirm-title').textContent = title;
    $('confirm-body').textContent = body;
    const ok = $('confirm-ok');
    ok.textContent = confirmLabel;
    ok.classList.toggle('btn-danger', danger);
    ok.classList.toggle('btn-primary', !danger);
    dialog.toggleAttribute('data-danger', danger);
    dialog.returnValue = '';
    dialog.showModal();
    (danger ? $('confirm-cancel') : ok).focus();
    return new Promise(resolve => {
        dialog.addEventListener('close', () => resolve(dialog.returnValue === 'ok'), { once: true });
    });
}

// ---------- Navigation ----------

function routeFromHash() {
    const [view, id] = location.hash.slice(1).split('/');
    return { view: VIEWS[view] && $(`view-${view}`) ? view : 'overview', id: id ? Number(id) : null };
}

function showView({ view, id }, moveFocus = false) {
    const changed = view !== state.view;
    state.view = view;
    state.targetIncident = id;
    document.querySelectorAll('.view').forEach(v => { v.hidden = v.id !== `view-${view}`; });
    document.querySelectorAll('.nav-item').forEach(a => {
        if (a.dataset.view === view) {
            a.setAttribute('aria-current', 'page');
            if (window.innerWidth <= 900) a.scrollIntoView({ block: 'nearest', inline: 'center' });
        } else {
            a.removeAttribute('aria-current');
        }
    });
    $('view-title').textContent = VIEWS[view][0];
    $('view-eyebrow').textContent = VIEWS[view][1];
    document.title = view === 'overview'
        ? 'DeepBell · Self-monitoring incident response lab'
        : `${VIEWS[view][0]} · DeepBell`;
    if (changed) window.scrollTo(0, 0);
    if (moveFocus) $('view-title').focus({ preventScroll: true });
    if (state.status) renderDemo(state.status.demo, state.status.faults.demo.cooldown);
    refreshView();
    resizeCharts();
}

// Load whatever the visible view needs.
function refreshView() {
    const v = state.view;
    if (state.offline) {
        // Nothing would answer: show the last data on the new view, and reload on reconnect.
        drawTelemetry(state.rows);
        if (v === 'overview') renderIncidentViews();
        return;
    }
    loadTelemetry();
    if (v === 'overview') { loadHistory(); renderIncidentViews(); }
    if (v === 'incidents') { loadIncidents(); loadReports(); }
    if (v === 'alerts') loadAlerts();
    if (v === 'timemachine') { loadTransactions(); loadSnapshots(); }
}

// ---------- Live status ----------

function healthOf(data) {
    if (data.incidents.length) {
        const inc = data.incidents[0];
        const extra = data.incidents.length > 1 ? ` +${data.incidents.length - 1}` : '';
        return { tone: 'bad', text: `Incident: ${SHORT_NAMES[inc.kind] || inc.name.toLowerCase()}${extra}` };
    }
    if (data.state === 'warming') return { tone: 'neutral', text: 'Learning baseline' };
    if (data.anomaly_score > 0) return { tone: 'warn', text: 'Anomalous sample' };
    return { tone: 'ok', text: 'System normal' };
}

function setBar(bar, pct, level) {
    bar.style.width = `${Math.max(0, Math.min(pct, 100))}%`;
    bar.dataset.level = level;
}

function renderStatus(data) {
    state.status = data;
    state.serverOffsetMs = (parseTime(data.server_time)?.getTime() ?? Date.now()) - Date.now();
    const base = data.baseline;

    // Right after a restart there is no sample yet: show dashes rather than zeros.
    const sampled = !!data.last_sample_at;
    $('header-cpu').textContent = sampled ? data.cpu.toFixed(1) : '--';
    $('header-ram').textContent = sampled ? data.ram.toFixed(1) : '--';
    // Every reading on the page shows this same sample, and the charts reload when a new one
    // arrives, so the header, the chart headings and the charts always agree.
    if (sampled) {
        $('ov-cpu-now').innerHTML = `${data.cpu.toFixed(1)}<small>%</small>`;
        $('ov-ram-now').innerHTML = `${data.ram.toFixed(1)}<small>MB</small>`;
        $('mini-cpu-now').textContent = `${data.cpu.toFixed(1)} %`;
        $('mini-ram-now').textContent = `${data.ram.toFixed(1)} MB`;
    }
    if (data.last_sample_at !== state.lastSampleAt) {
        state.lastSampleAt = data.last_sample_at;
        loadTelemetry();
    } else if (Date.now() - state.drawnAt > 3 * CONFIG.sampleIntervalSec * 1000) {
        drawTelemetry(state.rows); // no new sample: still slide the time axis, so a gap shows
    }
    const cpuAlert = base ? base.cpu + CONFIG.cpuDeltaPct : null;
    setBar($('cpu-bar'), data.cpu, cpuAlert && data.cpu > cpuAlert ? 'high' : cpuAlert && data.cpu > cpuAlert * 0.7 ? 'warn' : 'ok');
    $('cpu-note').textContent = data.cpu_capacity < 1 ? `of the ${data.cpu_capacity} vCPU quota` : 'of one core';
    if (base) {
        // Colours are relative to the learned baseline, not fixed thresholds.
        const over = data.ram - base.ram;
        const scale = base.ram + CONFIG.leakLimitMb + CONFIG.ramDeltaMb;
        setBar($('ram-bar'), (data.ram / scale) * 100,
            over >= CONFIG.ramDeltaMb ? 'high' : over >= CONFIG.ramDeltaMb / 2 ? 'warn' : 'ok');
        $('ram-note').innerHTML = `baseline ${esc(base.ram.toFixed(0))} MB<span class="hide-sm">, alert above ${esc((base.ram + CONFIG.ramDeltaMb).toFixed(0))}</span>`;
    } else {
        setBar($('ram-bar'), 0, 'ok');
        $('ram-note').textContent = 'learning baseline';
    }

    const health = healthOf(data);
    document.querySelectorAll('[data-status-box]').forEach(box => {
        box.dataset.tone = health.tone;
        box.title = data.status;
    });
    document.querySelectorAll('[data-health-text]').forEach(el => { el.textContent = health.text; });
    const chaosHealth = $('chaos-health');
    chaosHealth.dataset.tone = health.tone;
    chaosHealth.textContent = health.text;

    const navOpen = $('nav-open');
    navOpen.hidden = !data.incidents.length;
    navOpen.textContent = String(data.incidents.length);
    navOpen.title = `${data.incidents.length} open`;

    const navAlerts = $('nav-alerts');
    navAlerts.hidden = !data.alerts.firing;
    navAlerts.textContent = String(data.alerts.firing);
    navAlerts.title = `${data.alerts.firing} firing`;

    renderBanner(data);
    renderWatchdog(data);
    renderDetector(data);
    renderPipeline(data.pipeline);
    renderFaults(data.faults);
    renderDatabase(data.database);
    renderDemo(data.demo, data.faults.demo.cooldown);
    renderReportButton(data);
    renderUptime(data);
    if (state.view === 'incidents') {
        // An open incident's lifecycle bar grows while you watch.
        const open = state.incidents.find(i => i.id === state.selectedIncident && i.status === 'open');
        if (open) renderLifecycle(open);
    }

    if (data.incidents_rev !== state.incidentsRev) {
        const first = state.incidentsRev === null;
        state.incidentsRev = data.incidents_rev;
        if (!first) { loadIncidents(); loadHistory(); }
    }
    if (data.latest_report !== state.latestReport) {
        const first = state.latestReport === undefined;
        state.latestReport = data.latest_report;
        loadReports();
        if (data.latest_report) loadLatestReport(data.latest_report);
        if (data.latest_report && (state.awaitManualReport || (!state.reportPinned && !state.selectedIncident))) {
            state.awaitManualReport = false;
            showReport(data.latest_report);
        }
        const demoRunning = data.demo?.state === 'running';
        if (!first && data.latest_report && state.view !== 'incidents' && !demoRunning) {
            toast('A new incident report was written. Read it on the Incidents view.', 'success', 4500);
        }
    }
    // Snapshots are taken only when the data changes, so an old one can still be up to date.
    const snap = data.latest_snapshot;
    const snapEl = $('banner-snapshot');
    snapEl.textContent = snap ? `${ago(snap.timestamp)}${snap.current ? ' · up to date' : ''}` : 'none yet';
    snapEl.title = snap ? (snap.current ? 'The newest snapshot holds exactly the data in the live database.' : 'The live database has changes that no snapshot holds yet.') : '';
    $('snapshot-foot').textContent = snap
        ? `Last snapshot ${fmtTime(snap.timestamp)} (${ago(snap.timestamp)}); `
            + (snap.current ? 'it holds the current data. ' : 'newer changes go into the next one. ')
            + `The data is copied every ${CONFIG.snapshotIntervalSec} s when it changed.`
        : 'No snapshot yet.';
    if ((snap?.filename ?? null) !== state.lastSnapshot) {
        state.lastSnapshot = snap?.filename ?? null;
        if (state.view === 'timemachine') loadSnapshots();
    }
}

function renderBanner(data) {
    if (state.offline) return; // setOffline owns the banner until the server answers again
    const base = data.baseline;
    $('banner-baseline').textContent = !base ? 'learning'
        : base.source === 'live' ? `${base.samples} live samples`
        : `warm-up, live ${Math.min(base.live_samples, base.live_needed)}/${base.live_needed}`;
    const recover = data.pipeline?.stages.find(s => s.key === 'recover');
    let tone, title, detail;
    if (data.incidents.length) {
        const inc = data.incidents[0];
        const more = data.incidents.length > 1 ? ` and ${data.incidents.length - 1} more` : '';
        tone = 'bad';
        title = `Incident open: ${inc.name.toLowerCase()} #${inc.id ?? ''}${more}`;
        detail = `${esc(recover?.detail || inc.detail)} <a href="#incidents/${esc(inc.id)}" data-focus="banner-incident">View incident</a>`;
    } else if (data.state === 'warming') {
        tone = 'neutral';
        title = 'Learning the baseline';
        detail = esc('The watchdog collects samples before it scores them (about 30 s after a restart).');
    } else if (data.anomaly_score > 0) {
        tone = 'warn';
        title = 'Anomalous sample';
        detail = esc(data.pipeline?.stages.find(s => s.key === 'detect')?.detail
            || `An incident opens after ${CONFIG.openAfter} anomalous samples in a row.`);
    } else {
        tone = 'ok';
        title = 'All systems normal';
        const pct = state.history?.summary?.healthy_pct;
        detail = esc('No open incidents. CPU and memory are within the learned baseline'
            + (pct !== null && pct !== undefined ? `; ${pct}% of live samples were healthy in the last ${CONFIG.historyMinutes} minutes.` : '.'));
    }
    setBanner(tone, title, detail);
}

// The banner's details change with every sample, so only a new headline is read out.
function setBanner(tone, title, detailHtml) {
    $('status-banner').dataset.tone = tone;
    setHTML($('banner-detail'), detailHtml);
    if (title === state.bannerTitle) return;
    if (state.bannerTitle !== null) announce(title);
    state.bannerTitle = title;
    $('banner-title').textContent = title;
}

function renderWatchdog(data) {
    const base = data.baseline;
    const box = $('agent-box');
    const stateEl = $('agent-state');
    const sub = $('agent-sub');
    if (data.is_generating_rca) {
        box.dataset.tone = 'warn';
        stateEl.textContent = 'Writing incident report…';
        sub.textContent = data.api_key_configured ? 'Gemini is analysing the last 15 samples.' : 'Building a template report from the last 15 samples.';
    } else if (data.incidents.length) {
        const inc = data.incidents[0];
        box.dataset.tone = 'bad';
        stateEl.textContent = `Incident: ${inc.name.toLowerCase()}`;
        sub.textContent = `${inc.detail} Since ${fmtTime(inc.opened_at)}.`;
    } else if (data.state === 'warming') {
        box.dataset.tone = 'neutral';
        stateEl.textContent = 'Learning baseline';
        sub.textContent = 'Collecting samples before scoring them (about 30 s).';
    } else {
        box.dataset.tone = 'ok';
        stateEl.textContent = 'Watching';
        sub.textContent = `Sampling CPU and memory every ${CONFIG.sampleIntervalSec} s. No open incidents.`;
    }
    $('w-source').textContent = !base ? 'learning…'
        : base.source === 'live' ? `${base.samples} live samples`
        : `warm-up, live ${Math.min(base.live_samples, base.live_needed)}/${base.live_needed}`;
    $('w-source').title = base ? `Fitted ${fmtDateTime(base.fitted_at)}` : '';
    $('w-cpu').textContent = base ? `${(base.cpu + CONFIG.cpuDeltaPct).toFixed(1)} %` : '–';
    $('w-ram').textContent = base ? `${(base.ram + CONFIG.ramDeltaMb).toFixed(0)} MB` : '–';
}

function renderDetector(data) {
    const base = data.baseline;
    const det = data.detection;
    $('f-source').textContent = !base ? 'learning…'
        : base.source === 'live' ? `${base.samples} live samples`
        : `synthetic warm-up (live ${Math.min(base.live_samples, base.live_needed)} of ${base.live_needed})`;
    $('facts-meta').textContent = base ? `Baseline fitted ${fmtTime(base.fitted_at)}; refitted every 5 minutes.` : 'Learning the baseline.';
    $('f-cpu').textContent = base ? `${base.cpu.toFixed(1)} %, alert above ${(base.cpu + CONFIG.cpuDeltaPct).toFixed(1)} %` : '–';
    $('f-ram').textContent = base ? `${base.ram.toFixed(1)} MB, alert above ${(base.ram + CONFIG.ramDeltaMb).toFixed(0)} MB` : '–';
    $('f-streak').textContent = `CPU ${det.streak.cpu_spike} of ${det.open_after}, memory ${det.streak.memory_leak} of ${det.open_after}`;
    $('f-fits').textContent = `${det.forest_fits} (new candidates only); baseline fits ${det.fits}`;
    const unit = data.cpu_capacity < 1 ? `% of the ${data.cpu_capacity} vCPU quota` : '% of one core';
    $('cpu-unit').textContent = unit;
    $('ov-cpu-sub').textContent = `${unit}, last 2 minutes`;
}

function renderDatabase(db) {
    document.querySelectorAll('[data-db-alert]').forEach(el => { el.hidden = db.ok; });
    document.querySelectorAll('[data-db-error]').forEach(el => { el.textContent = db.error || ''; });
    $('btn-add-tx').disabled = !db.ok;
    if (db.ok !== state.lastDbOk) {
        state.lastDbOk = db.ok;
        if (state.view === 'timemachine') { loadTransactions(); loadSnapshots(); }
    }
}

function renderUptime(data) {
    $('kpi-uptime').textContent = fmtUptime(data.uptime_sec);
    const pct = state.history?.summary?.healthy_pct;
    $('kpi-uptime-note').textContent = pct !== null && pct !== undefined
        ? `${pct}% healthy samples, ${CONFIG.historyMinutes} min`
        : `since ${fmtTime(data.started_at)}`;
    $('kpi-uptime-note').title = `Process started ${fmtDateTime(data.started_at)}`;
    $('kpi-healthy-bar').style.width = `${pct ?? 0}%`;
    const open = data.incidents.length;
    const openEl = $('kpi-open');
    openEl.textContent = String(open);
    openEl.dataset.tone = open ? 'bad' : 'ok';
}

// Every control that sends a request. They are paused while the server is unreachable.
const SERVER_CONTROLS = '#demo-run, [data-fault], #btn-release, #btn-add-tx, [data-generate-report], #restore-newest, #snap-body button';

function setOffline(offline) {
    if (offline) {
        state.offlineAttempts += 1;
        $('conn-attempt').textContent = ` Attempt ${state.offlineAttempts}.`;
        document.querySelectorAll('[data-status-box]').forEach(box => { box.dataset.tone = 'bad'; });
        document.querySelectorAll('[data-health-text]').forEach(el => { el.textContent = 'Offline, reconnecting'; });
    }
    if (offline === state.offline) return;
    state.offline = offline;
    $('conn-banner').hidden = !offline;
    document.documentElement.toggleAttribute('data-offline', offline);
    if (offline) {
        // Say plainly that the numbers on screen are frozen, and pause every action.
        document.querySelectorAll(SERVER_CONTROLS).forEach(btn => {
            if (btn.disabled) return;
            btn.disabled = true;
            btn.dataset.offlineDisabled = '';
        });
        const last = state.status?.server_time;
        setBanner('neutral', 'Connection lost', esc(last
            ? `Showing data from ${fmtTime(last)}. Actions are paused; retrying every ${CONFIG.sampleIntervalSec} s.`
            : `Actions are paused; retrying every ${CONFIG.sampleIntervalSec} s.`));
        return;
    }
    // Back online: undo the pause; the status render that follows sets each control's real state.
    document.querySelectorAll('[data-offline-disabled]').forEach(btn => {
        btn.disabled = false;
        delete btn.dataset.offlineDisabled;
    });
    state.offlineAttempts = 0;
    toast('Connection restored.', 'success', 3000);
    refreshView();
    loadIncidents();
}

// One status request at a time: an action's refresh and the scheduled poll share it, so
// the same events are never rendered twice.
let statusRequest = null;

function pollStatus() {
    if (!statusRequest) statusRequest = fetchStatus().finally(() => { statusRequest = null; });
    return statusRequest;
}

async function fetchStatus() {
    let data;
    try {
        const res = await fetch(`/api/status?events_after=${state.eventSeq}`, { cache: 'no-store' });
        if (!res.ok) throw new Error(String(res.status));
        data = await res.json();
    } catch (err) {
        setOffline(true);
        return;
    }
    setOffline(false);
    if (data.event_seq < state.eventSeq) {
        // The server restarted, and its event counter with it.
        state.eventSeq = 0;
        return fetchStatus();
    }
    state.eventSeq = data.event_seq;
    renderEvents(data.events);
    renderStatus(data);
}

// Poll every 2 s (every 1 s while the guided demo runs), and only while the tab is visible.
function schedulePoll() {
    const running = state.status?.demo?.state === 'running';
    setTimeout(async () => {
        if (!document.hidden) await pollStatus();
        schedulePoll();
    }, running ? 1000 : 2000);
}

// ---------- Pipeline ("How it works", lit by the server's pipeline state) ----------

function renderPipeline(pipeline) {
    if (!pipeline) return;
    for (const list of [$('pipeline'), $('pipeline-chaos')]) {
        for (const stage of pipeline.stages) {
            const li = list.querySelector(`[data-key="${stage.key}"]`);
            li.dataset.state = stage.state;
            li.toggleAttribute('data-current', stage.key === pipeline.current && stage.key !== 'sample');
            li.querySelector('[data-detail]').textContent = stage.detail;
        }
    }
    const caption = $('pipeline-chaos-caption');
    const current = pipeline.stages.find(s => s.key === pipeline.current) || pipeline.stages[0];
    caption.dataset.state = current.state;
    caption.innerHTML = `<b>${esc(STAGE_NAMES[current.key])}:</b> ${esc(current.detail)}`;

    // Details change with every sample; screen readers hear only a stage that changes state.
    const key = pipeline.stages.map(s => `${s.key}:${s.state}`).join(' ');
    if (state.pipelineKey !== null && key !== state.pipelineKey) {
        const before = Object.fromEntries(state.pipelineKey.split(' ').map(p => p.split(':')));
        pipeline.stages
            .filter(s => s.key !== 'sample' && s.state !== 'idle' && before[s.key] !== s.state)
            .forEach(s => announce(`${STAGE_NAMES[s.key]}: ${s.detail}`));
    }
    state.pipelineKey = key;
}

// ---------- Server events ----------

const LEVEL_LABELS = { info: 'info', warn: 'warn', error: 'error', success: 'ok' };

function renderEvents(events) {
    if (!events.length) return;
    for (const id of ['log-overview', 'log-chaos']) {
        const box = $(id);
        box.querySelector('.log-empty')?.remove();
        for (const e of events) {
            const li = document.createElement('li');
            li.innerHTML = `<time datetime="${esc(e.time)}">${esc(fmtTime(e.time))}</time>`
                + `<span class="lvl" data-level="${esc(e.level)}">${esc(LEVEL_LABELS[e.level] || e.level)}</span>`
                + `<span class="msg">${esc(e.message)}</span>`;
            box.prepend(li);
        }
        while (box.children.length > 60) box.lastElementChild.remove();
    }
}

// ---------- Guided demo ----------

async function startDemo() {
    const btn = $('demo-run');
    setLoading(btn, true);
    const r = await api('POST', '/api/demo/start');
    setLoading(btn, false);
    if (r.ok) {
        state.myDemoId = r.data.id;
        state.announcedSteps.clear();
        toast('Guided demo started. Watch the steps and the pipeline light up.', 'info', 4000);
        // On narrow screens the panel can be below the fold: bring it into view.
        const panel = $('demo-panel');
        if (panel.getBoundingClientRect().top > window.innerHeight * 0.6) {
            panel.scrollIntoView({ behavior: reducedMotion() ? 'auto' : 'smooth', block: 'start' });
        }
    } else if (r.status === 409 || r.status === 429) {
        toast(r.detail, 'info');
    } else {
        toast(r.detail, 'error');
    }
    pollStatus();
}

const DEMO_DEFAULTS = {
    meta: document.getElementById('demo-meta').textContent,
    foot: document.getElementById('demo-foot').textContent,
};

function renderDemo(run, cooldown) {
    const running = run?.state === 'running';
    // Steps that were already finished when the page loaded are not read out.
    const pageLoad = state.demoState === undefined;
    if ((run?.state ?? null) !== state.demoState) {
        // The incidents' empty states offer to run the demo, or to watch the one running.
        state.demoState = run?.state ?? null;
        if (!pageLoad) renderIncidentViews();
    }
    const btn = $('demo-run');
    if (!btn.classList.contains('is-loading')) btn.disabled = running || cooldown > 0;
    btn.querySelector('span').textContent = running ? 'Demo running…'
        : cooldown > 0 ? `Run again in ${cooldown} s`
        : run ? 'Run the demo again' : 'Run the guided demo';

    let done = 0;
    // While a run is in progress the newest finished step keeps its detail visible; older
    // details collapse (they stay available as a tooltip) so the panel does not keep growing.
    const latestDone = running ? run.steps.map(st => st.state).lastIndexOf('done') : -1;
    document.querySelectorAll('#demo-steps .step').forEach((li, i) => {
        const step = run?.steps[i];
        const stepState = step ? step.state : 'pending';
        if (stepState === 'done') done += 1;
        li.dataset.state = stepState;
        li.classList.toggle('is-latest', i === latestDone);
        li.title = step?.detail || '';
        li.querySelector('.step-detail').textContent = step?.detail || '';
        const time = li.querySelector('.step-time');
        time.textContent = step?.done_at ? fmtTime(step.done_at) : '';
        time.dateTime = step?.done_at || '';
        li.querySelector('.step-sr').textContent = stepState === 'pending' ? '' : ` (${stepState})`;
        // Announce each finished step once, for screen readers.
        const key = run && `${run.id}:${step.key}:${stepState}`;
        if (run && (stepState === 'done' || stepState === 'failed') && !state.announcedSteps.has(key)) {
            state.announcedSteps.add(key);
            if (!pageLoad) announce(`Guided demo, ${step.title}: ${step.detail}`);
        }
    });
    const total = document.querySelectorAll('#demo-steps .step').length;
    $('demo-progress').style.width = run ? `${(done / total) * 100}%` : '0%';

    const meta = $('demo-meta');
    const foot = $('demo-foot');
    const chip = $('demo-chip');
    const badge = $('demo-badge');
    if (!run) {
        // No run on this server yet (or it restarted): back to the initial copy.
        chip.hidden = true;
        badge.dataset.tone = cooldown > 0 ? 'neutral' : 'accent';
        badge.textContent = 'Ready';
        meta.textContent = DEMO_DEFAULTS.meta;
        foot.dataset.tone = '';
        setHTML(foot, esc(DEMO_DEFAULTS.foot));
        return;
    }
    const mine = run.id === state.myDemoId;
    if (running) {
        const index = Math.max(run.steps.findIndex(s => s.state !== 'done'), 0);
        badge.dataset.tone = 'accent';
        badge.textContent = `Step ${index + 1} of ${run.steps.length}`;
        meta.textContent = `Running since ${fmtTime(run.started_at)}${mine ? '' : ', started by another visitor'}`;
        foot.dataset.tone = '';
        setHTML(foot, esc(mine
            ? 'This runs on the server, so closing the tab will not leave a fault behind.'
            : 'Someone else started this run. The server is shared, so you are watching the same one.'));
        chip.hidden = state.view === 'overview';
        chip.textContent = `Guided demo running: step ${index + 1} of ${run.steps.length}, ${run.steps[index].title.toLowerCase()}`;
        return;
    }
    chip.hidden = true;
    badge.dataset.tone = run.state === 'succeeded' ? 'ok' : 'bad';
    badge.textContent = run.state === 'succeeded' ? 'Succeeded' : 'Stopped';
    meta.textContent = `Last run ${run.state} at ${fmtTime(run.finished_at)}`;
    const links = (run.incident_ids || []).map(id => `<a href="#incidents/${id}" data-focus="demo-${id}">#${id}</a>`).join(', ');
    if (run.state === 'succeeded') {
        foot.dataset.tone = 'ok';
        setHTML(foot, `${esc(run.summary)} Demo measurements from this server's clock.`
            + (links ? ` Incident timelines: ${links}.` : ''));
    } else {
        foot.dataset.tone = 'bad';
        setHTML(foot, `Stopped: ${esc(run.summary)}`
            + (cooldown > 0 ? ` You can retry in ${cooldown} s.` : ' You can retry now.')
            + (links ? ` Incidents: ${links}.` : ''));
    }
}

// ---------- Status strip ----------

const STRIP_LABELS = {
    healthy: 'Healthy',
    degraded: 'Flagged samples, no incident',
    incident: 'Incident open',
    synthetic: 'Synthetic warm-up only',
    nodata: 'No data',
};

async function loadHistory() {
    const bars = window.innerWidth < 640 ? 30 : CONFIG.historyBars;
    const r = await api('GET', `/api/status/history?minutes=${CONFIG.historyMinutes}&bars=${bars}`);
    if (!r.ok) {
        $('strip-summary').textContent = 'Could not load the status history. It retries every 15 s.';
        return;
    }
    state.history = r.data;
    renderStrip(r.data);
    if (state.status) { renderUptime(state.status); renderBanner(state.status); }
}

function barText(bar) {
    let text = `${fmtTime(bar.start)} to ${fmtTime(bar.end)}: ${STRIP_LABELS[bar.state]}`;
    if (bar.incidents.length) text += ` (${bar.incidents.map(i => `#${i}`).join(', ')})`;
    if (bar.samples) text += `. ${bar.samples} live samples, ${bar.flagged} flagged.`;
    return text;
}

function renderStrip(history) {
    const strip = $('strip');
    strip.innerHTML = history.bars.map((b, i) => `<span class="bar" data-state="${b.state}" data-i="${i}"></span>`).join('');
    const s = history.summary;
    const counts = {};
    history.bars.forEach(b => { counts[b.state] = (counts[b.state] || 0) + 1; });
    $('strip-summary').textContent = s.live_samples
        ? `${s.healthy_pct}% of ${s.live_samples} live samples healthy, ${s.incidents} incident${s.incidents === 1 ? '' : 's'} opened`
        : 'No live samples yet';
    strip.setAttribute('aria-label', `Status for the last ${history.minutes} minutes in ${history.bar_sec}-second bars: `
        + Object.entries(counts).map(([k, n]) => `${n} ${STRIP_LABELS[k].toLowerCase()}`).join(', ') + '.');
    // Keep the selection; its description (a live region) changes only while the strip has focus.
    if (state.stripIndex !== null) selectBar(state.stripIndex, document.activeElement === strip);
}

function selectBar(i, describe = true) {
    const bars = state.history?.bars;
    if (!bars?.length) return;
    state.stripIndex = Math.max(0, Math.min(i, bars.length - 1));
    document.querySelectorAll('#strip .bar').forEach((el, j) => el.classList.toggle('is-selected', j === state.stripIndex));
    if (describe) $('strip-detail').textContent = barText(bars[state.stripIndex]);
}

function initStrip() {
    const strip = $('strip');
    strip.addEventListener('mouseover', e => { if (e.target.dataset.i) selectBar(Number(e.target.dataset.i)); });
    strip.addEventListener('click', e => {
        const bar = state.history?.bars[Number(e.target.dataset.i)];
        if (bar?.incidents.length) location.hash = `#incidents/${bar.incidents[0]}`;
    });
    strip.addEventListener('focus', () => selectBar(state.stripIndex ?? Infinity));
    strip.addEventListener('keydown', e => {
        const step = { ArrowLeft: -1, ArrowRight: 1 }[e.key];
        const bar = state.history?.bars[state.stripIndex];
        if (step) selectBar((state.stripIndex ?? 0) + step);
        else if (e.key === 'Home') selectBar(0);
        else if (e.key === 'End') selectBar(Infinity);
        else if (e.key === 'Enter' && bar?.incidents.length) location.hash = `#incidents/${bar.incidents[0]}`;
        else return;
        e.preventDefault();
    });
}

// ---------- Incidents ----------

async function loadIncidents() {
    const [r, stats] = await Promise.all([api('GET', '/api/incidents?limit=50'), api('GET', '/api/incidents/stats')]);
    if (stats.ok) renderKpis(stats.data);
    if (!r.ok) {
        const msg = `<li class="empty-box">${icon('incident')}<span>Could not load incidents: ${esc(r.detail)} It retries when the server answers.</span></li>`;
        setHTML($('incident-list'), msg);
        setHTML($('recent-incidents'), msg);
        return;
    }
    state.incidents = r.data;
    state.incidentsLoaded = true;
    renderIncidentViews();
    renderBands();
}

function renderIncidentViews() {
    if (!state.incidentsLoaded) return; // the skeletons stay until the first answer
    renderRecent(state.incidents.slice(0, 5));
    renderIncidentList();
}

function badge(status) {
    return `<span class="badge" data-tone="${STATUS_TONES[status]}">${STATUS_LABELS[status]}</span>`;
}

function kindIcon(kind) {
    return `<span class="kind-icon" data-kind="${esc(kind)}" aria-hidden="true">${icon(KIND_ICONS[kind] || 'incident')}</span>`;
}

function incidentMeta(i) {
    return `${fmtTime(i.detected_at)} · detect ${fmtDuration(i.ttd_sec)} · `
        + (i.status === 'open' ? 'still open' : i.status === 'interrupted' ? 'interrupted' : `recover ${fmtDuration(i.ttr_sec)}`);
}

function emptyIncidents() {
    // While a demo runs, starting another would only get a 409: offer to watch it instead.
    if (state.status?.demo?.state === 'running') {
        return `<li class="empty-box">${icon('shield')}<span>No incidents yet. The guided demo is running: its first incident opens in a few seconds.</span>
            <span class="btn-row"><a class="btn btn-small" href="#overview" data-focus="empty-demo">Watch the guided demo</a></span></li>`;
    }
    return `<li class="empty-box">${icon('shield')}<span>No incidents yet. Run the guided demo, or inject a fault in the Chaos Lab: the watchdog opens an incident within a few seconds.</span>
        <span class="btn-row"><a class="btn btn-small" href="#overview" data-run-demo data-focus="empty-demo">Run the guided demo</a><a class="btn btn-secondary btn-small" href="#chaos" data-focus="empty-chaos">Open the Chaos Lab</a></span></li>`;
}

function renderRecent(incidents) {
    const list = $('recent-incidents');
    if (!incidents.length) {
        setHTML(list, emptyIncidents());
        return;
    }
    setHTML(list, incidents.map(i => `
        <li><a class="recent-item" href="#incidents/${i.id}" data-focus="recent-${i.id}">
            ${kindIcon(i.kind)}
            <span class="row-main"><span class="recent-name">${esc(i.name)} <span class="incident-id">#${i.id}</span></span>
            <span class="recent-meta">${esc(incidentMeta(i))}</span></span>
            ${badge(i.status)}
        </a></li>`).join(''));
}

function renderIncidentList() {
    const box = $('incident-list');
    const all = state.incidents;
    const counts = { all: all.length, open: 0, resolved: 0 };
    all.forEach(i => { if (counts[i.status] !== undefined) counts[i.status] += 1; });
    Object.entries(counts).forEach(([k, n]) => { $(`count-${k}`).textContent = n; });
    const shown = state.filter === 'all' ? all : all.filter(i => i.status === state.filter);
    if (!all.length) {
        setHTML(box, emptyIncidents());
        renderDetail(null);
        return;
    }
    if (!shown.length) {
        setHTML(box, `<li class="empty-box">${icon('check')}<span>No ${state.filter} incidents.</span></li>`);
    } else {
        setHTML(box, shown.map(i => `
            <li><button type="button" class="incident-row" data-id="${i.id}" data-focus="inc-${i.id}"${i.id === state.selectedIncident ? ' aria-current="true"' : ''}>
                ${kindIcon(i.kind)}
                <span class="row-main"><span class="row-title">${esc(i.name)} <span class="incident-id">#${i.id}</span></span>
                <span class="row-meta">${esc(incidentMeta(i))}</span></span>
                ${badge(i.status)}
            </button></li>`).join(''));
    }
    // A #incidents/<id> link wins; otherwise keep the selection, or follow the newest.
    if (state.view === 'incidents' && state.targetIncident) {
        const id = state.targetIncident;
        state.targetIncident = null;
        if (all.some(i => i.id === id)) selectIncident(id, true);
        else toast(`Incident #${id} is not in the history any more.`, 'info');
    } else if (!all.some(i => i.id === state.selectedIncident)) {
        if (!state.reportPinned) selectIncident(all[0].id, false);
    } else {
        renderDetail(all.find(i => i.id === state.selectedIncident));
    }
}

function selectIncident(id, pinned) {
    const incident = state.incidents.find(i => i.id === id);
    if (!incident) return;
    const changed = state.selectedIncident !== id;
    state.selectedIncident = id;
    if (pinned) state.reportPinned = true;
    if (pinned && changed) announce(`Showing incident #${id}, ${incident.name.toLowerCase()}, ${STATUS_LABELS[incident.status].toLowerCase()}.`);
    document.querySelectorAll('.incident-row').forEach(b => {
        if (Number(b.dataset.id) === id) b.setAttribute('aria-current', 'true');
        else b.removeAttribute('aria-current');
    });
    renderDetail(incident);
    if (incident.report && incident.report_available && (changed || state.reportName !== incident.report)) {
        showReport(incident.report);
    } else if (!incident.report_available) {
        showReportPlaceholder(incident);
    }
}

function renderDetail(incident) {
    const wrap = $('detail-incident');
    if (!incident) {
        wrap.hidden = true;
        return;
    }
    wrap.hidden = false;
    $('detail-icon').dataset.kind = incident.kind;
    $('detail-icon').className = 'kind-icon';
    $('detail-icon').innerHTML = icon(KIND_ICONS[incident.kind] || 'incident');
    $('detail-title').innerHTML = `${esc(incident.name)} <span class="incident-id">#${incident.id}</span>`;
    const cause = incident.timeline.find(e => e.type === 'fault');
    $('detail-sub').textContent = cause ? `Caused by: ${cause.text}` : 'No injected fault on record; started at the first anomalous sample.';
    const status = $('detail-status');
    status.dataset.tone = STATUS_TONES[incident.status];
    status.textContent = STATUS_LABELS[incident.status];
    const measure = (label, value, cls = '') => `<div class="${cls}"><dt>${label}</dt><dd>${esc(value)}</dd></div>`;
    $('detail-measures').innerHTML = measure('Started', fmtTime(incident.started_at))
        + measure('Detected', fmtTime(incident.detected_at))
        + measure('Resolved', incident.resolved_at ? fmtTime(incident.resolved_at) : incident.status === 'open' ? 'open' : '–')
        + measure('Time to detect', fmtDuration(incident.ttd_sec), 'is-measure')
        + measure('Time to recover', incident.status === 'open' ? 'open' : fmtDuration(incident.ttr_sec), 'is-measure');
    renderLifecycle(incident);
    $('detail-timeline').innerHTML = incident.timeline.map(e => `
        <li data-type="${esc(e.type)}"><time datetime="${esc(e.at)}">${esc(fmtTime(e.at))}<small>+${esc(fmtDuration(e.offset_sec ?? 0))}</small></time><span class="tl-type">${esc(EVENT_LABELS[e.type] || e.type)}</span><span class="tl-text">${esc(e.text)}</span></li>`).join('');
    loadCause(incident);
}

// The likely cause is worked out by the server (a heuristic over the merged timeline and the
// telemetry). It is fetched when an incident is selected and again whenever its timeline grows
// or its status changes, never on every poll.
async function loadCause(incident) {
    const key = `${incident.id}:${incident.status}:${incident.timeline.length}`;
    if (state.causeKey === key) return;
    state.causeKey = key;
    if (state.causeFor !== incident.id) {
        $('cause-confidence').hidden = true;
        setHTML($('cause-body'), '<p class="cause-note">Working out the likely cause…</p>');
    }
    const r = await api('GET', `/api/incidents/${incident.id}`);
    if (state.selectedIncident !== incident.id) return; // another incident was selected meanwhile
    if (!r.ok) {
        state.causeKey = null; // try again on the next change
        setHTML($('cause-body'), `<p class="cause-note">Could not work out the likely cause: ${esc(r.detail)}</p>`);
        return;
    }
    state.causeFor = incident.id;
    renderCause(r.data.likely_cause);
}

function renderCause(cause) {
    const badge = $('cause-confidence');
    badge.hidden = false;
    badge.dataset.tone = CONFIDENCE_TONES[cause.confidence];
    badge.textContent = `${cause.confidence} confidence`;
    const list = items => `<ul class="cause-list">${items.map(i => `<li>${esc(i)}</li>`).join('')}</ul>`;
    setHTML($('cause-body'), `
        <p class="cause-summary">${esc(cause.summary)}</p>
        <p class="cause-note">${esc(cause.confidence_reason)}</p>
        ${cause.confidence === 'low' ? '<p class="cause-note">Low confidence means no fault was recorded for this incident, so the telemetry shows what moved and when, but not why.</p>' : ''}
        ${cause.evidence.length ? `<h4 class="cause-label">Evidence</h4>${list(cause.evidence)}` : ''}
        ${cause.not_found.length ? `<h4 class="cause-label">Checked, not found</h4>${list(cause.not_found)}` : ''}
        <p class="cause-note cause-disclaimer">${esc(cause.disclaimer)}</p>`);
}

// A to-scale bar: started -> detected (time to detect), detected -> resolved (time to recover).
// Each segment grows by its real seconds; a zero-length one still shows as a thin tick.
function renderLifecycle(incident) {
    const box = $('detail-lifecycle');
    const start = parseTime(incident.started_at);
    const detected = parseTime(incident.detected_at);
    if (!start || !detected) { box.innerHTML = ''; return; }
    const now = new Date(Date.now() + state.serverOffsetMs);
    const end = parseTime(incident.resolved_at) || (incident.status === 'open' ? now : detected);
    const ttd = Math.max(0, (detected - start) / 1000);
    const open = Math.max(0, (end - detected) / 1000);
    box.innerHTML = `<div class="lc-track">
            <span class="lc-seg" data-part="detect" style="flex-grow:${ttd}" title="Time to detect"></span>
            <span class="lc-seg" data-part="open" style="flex-grow:${open}"${incident.status === 'open' ? ' data-live' : ''} title="Open"></span>
        </div>
        <div class="lc-labels"><span><b>Started</b> ${esc(fmtTime(incident.started_at))}</span><span><b>Detected</b> after ${esc(fmtDuration(incident.ttd_sec))}</span><span><b>${incident.status === 'open' ? 'Still open' : incident.status === 'interrupted' ? 'Interrupted' : 'Resolved'}</b>${incident.ttr_sec !== null ? ` after ${esc(fmtDuration(incident.ttr_sec))}` : ''}</span></div>`;
}

// ---------- Alerts ----------

async function loadAlerts() {
    const r = await api('GET', '/api/alerts');
    if (!r.ok) {
        // Keep the last good data on screen; only say so when there is none yet.
        if (!state.alerts) setHTML($('alert-list'), `<li class="empty-box">${icon('bell')}<span>Could not load the alert rules: ${esc(r.detail)} It retries when the server answers.</span></li>`);
        return;
    }
    state.alerts = r.data;
    renderAlerts(r.data);
}

function alertRow(rule) {
    const since = rule.state === 'pending' || rule.state === 'firing' ? `since ${fmtTime(rule.since)}`
        : rule.state === 'resolved' ? `cleared ${fmtTime(rule.changed_at)}` : 'not triggered';
    const value = rule.value === null ? 'no data yet' : `now ${rule.value}`;
    return `<li class="alert-row" data-state="${esc(rule.state)}">
        <div class="row-main">
            <span class="row-title">${esc(rule.name)} <span class="badge" data-tone="${SEVERITY_TONES[rule.severity]}">${esc(rule.severity)}</span></span>
            <span class="alert-cond">${esc(rule.condition)}</span>
        </div>
        <div class="alert-state"><span class="badge" data-tone="${ALERT_TONES[rule.state]}">${ALERT_LABELS[rule.state]}</span><span>${esc(value)}</span><span>${esc(since)}</span></div>
        ${rule.description ? `<span class="alert-desc">${esc(rule.description)}</span>` : ''}
    </li>`;
}

function renderAlerts(data) {
    $('rule-count').textContent = String(data.rules.length);
    if (!data.rules.length) {
        setHTML($('alert-list'), `<li class="empty-box">${icon('bell')}<span>No alert rules are loaded, so nothing can alert. Add rules to <code>target_app/alert_rules.json</code> and restart.</span></li>`);
    } else {
        setHTML($('alert-list'), data.rules.map(alertRow).join(''));
    }
    const errors = $('rule-errors');
    errors.hidden = !data.errors.length;
    errors.textContent = data.errors.join(' ');
    const mode = $('notify-mode');
    mode.dataset.tone = data.webhook_configured ? 'ok' : 'neutral';
    mode.textContent = data.webhook_configured ? 'Webhook on' : 'Log only';
    $('notify-note').textContent = data.webhook_configured
        ? `Firing and resolved alerts are posted to the configured webhook (Slack and Discord compatible), at most ${CONFIG.notifyMaxPerMin} a minute, and the same rule at most once every ${CONFIG.notifyMinIntervalSec} s.`
        : 'No webhook is set, so alerts only appear here and in the server log. To send them to Slack or Discord, set DEEPBELL_ALERT_WEBHOOK_URL.';
    if (!data.history.length) {
        setHTML($('alert-history'), `<p class="empty-box">${icon('check')}<span>No alert has changed state since the server started. Inject a fault in the Chaos Lab and watch a rule go pending, then firing.</span></p>`);
        return;
    }
    setHTML($('alert-history'), `<ol class="timeline">${data.history.map(e => `
        <li data-type="${esc(e.state)}"><time datetime="${esc(e.at)}">${esc(fmtTime(e.at))}</time><span class="tl-type">${esc(ALERT_LABELS[e.state])}</span><span class="tl-text">${esc(e.text)}</span></li>`).join('')}</ol>`);
}

// ---------- KPI tiles ----------

// Small bars for the KPI tiles, one per incident (oldest first). Each bar links to its incident
// and names it on hover; a null value draws as a stub, and a fixed height draws tokens. The
// links stay out of the tab order: the incident lists are the keyboard route to the same pages.
function miniBars(svg, incidents, { value, tone, title, fixed = null }) {
    const n = incidents.length;
    const values = incidents.map(value);
    const max = Math.max(...values.map(v => v ?? 0), 1);
    const slots = Math.max(n, 12);
    const w = 120 / slots;
    setHTML(svg, incidents.map((inc, i) => {
        const v = values[i];
        const h = fixed ?? (v === null ? 4 : Math.max(3, (v / max) * 28));
        const x = (slots - n + i) * w + 1;
        const t = tone(inc);
        return `<a href="#incidents/${inc.id}" tabindex="-1"><title>${esc(title(inc))}</title>`
            + `<rect x="${x.toFixed(1)}" y="${(28 - h).toFixed(1)}" width="${(w - 2).toFixed(1)}" height="${h.toFixed(1)}" rx="1.5"${t ? ` data-tone="${t}"` : ''}></rect></a>`;
    }).join(''));
}

function spread(values) {
    if (!values.length) return 'none yet';
    const sorted = [...values].sort((a, b) => a - b);
    const mid = sorted.length / 2;
    const median = sorted.length % 2 ? sorted[Math.floor(mid)] : (sorted[mid - 1] + sorted[mid]) / 2;
    return `fastest ${sorted[0]} s, median ${Math.round(median)} s, slowest ${sorted[sorted.length - 1]} s`;
}

function renderKpis(stats) {
    const recent = stats.recent.slice(-12);
    const name = i => `#${i.id} ${i.name}`;
    $('kpi-open-note').textContent = stats.total ? `${stats.total} recorded in total` : 'none recorded yet';
    miniBars($('kpi-open-bars'), recent, {
        value: () => 1,
        fixed: 8,
        tone: i => (i.status === 'open' ? 'bad' : i.status === 'resolved' ? '' : 'muted'),
        title: i => `${name(i)}: ${STATUS_LABELS[i.status].toLowerCase()}`,
    });
    const open = recent.filter(i => i.status === 'open').length;
    $('kpi-open-bars').setAttribute('aria-label', recent.length
        ? `Last ${recent.length} incidents: ${open} open, ${recent.length - open} closed.` : 'No incidents yet.');
    const value = (sec, el) => {
        el.innerHTML = sec === null ? '–' : `${esc(Math.round(sec))}<small>s</small>`;
    };
    value(stats.median_ttd_sec, $('kpi-ttd'));
    value(stats.median_ttr_sec, $('kpi-ttr'));
    const ttds = recent.map(i => i.ttd_sec).filter(v => v !== null);
    const ttrs = recent.map(i => i.ttr_sec).filter(v => v !== null);
    const ttrCount = stats.recent.filter(i => i.ttr_sec !== null).length;
    $('kpi-ttd-note').textContent = ttds.length ? `median of the last ${Math.min(stats.window, stats.recent.length)} incidents` : 'no incidents yet';
    $('kpi-ttr-note').textContent = ttrCount ? `median of ${ttrCount} resolved incidents` : 'nothing resolved yet';
    miniBars($('kpi-ttd-bars'), recent, {
        value: i => i.ttd_sec,
        tone: () => '',
        title: i => `${name(i)}: detected in ${fmtDuration(i.ttd_sec)}`,
    });
    miniBars($('kpi-ttr-bars'), recent, {
        value: i => i.ttr_sec,
        tone: i => (i.ttr_sec === null ? 'muted' : 'ram'),
        title: i => `${name(i)}: ${i.ttr_sec === null ? (i.status === 'open' ? 'still open' : 'not resolved') : `recovered in ${fmtDuration(i.ttr_sec)}`}`,
    });
    $('kpi-ttd-bars').setAttribute('aria-label', `Time to detect, last ${ttds.length} incidents: ${spread(ttds)}.`);
    $('kpi-ttr-bars').setAttribute('aria-label', `Time to recover, last ${ttrs.length} resolved incidents: ${spread(ttrs)}.`);
}

// ---------- Reports ----------

async function loadReports() {
    const r = await api('GET', '/api/reports');
    if (!r.ok) {
        setHTML($('report-list'), `<li class="error-text">Could not load reports: ${esc(r.detail)}</li>`);
        return;
    }
    state.reports = r.data;
    $('report-count').textContent = r.data.length;
    setHTML($('report-list'), r.data.map(rep => `
        <li><button type="button" data-name="${esc(rep.filename)}" data-focus="rep-${esc(rep.filename)}"${rep.filename === state.reportName ? ' aria-current="true"' : ''}>
            <span class="rl-title">${esc(rep.title)}${rep.incident_id ? ` <span class="incident-id">#${rep.incident_id}</span>` : ''}</span>
            <time datetime="${esc(rep.generated_at)}">${esc(fmtTime(rep.generated_at))}</time>
        </button></li>`).join('')
        || '<li class="empty-state">No reports yet.</li>');
}

function openReportFromList(name) {
    state.reportPinned = true;
    const rep = state.reports.find(x => x.filename === name);
    if (rep?.incident_id && state.incidents.some(i => i.id === rep.incident_id)) {
        selectIncident(rep.incident_id, true);
        showReport(rep.filename);
    } else {
        state.selectedIncident = null;
        document.querySelectorAll('.incident-row').forEach(b => b.removeAttribute('aria-current'));
        renderDetail(null);
        showReport(name);
    }
}

async function showReport(name) {
    state.reportName = name;
    $('report-meta').textContent = 'Loading the report…';
    const r = await api('GET', `/api/reports/${encodeURIComponent(name)}`);
    if (state.reportName !== name) return; // the visitor picked another one meanwhile
    if (!r.ok) {
        $('report-meta').textContent = '';
        $('report-body').innerHTML = `<p class="error-text">${esc(r.detail)}</p>`;
        $('report-download').hidden = true;
        $('report-source').hidden = true;
        return;
    }
    renderReport(r.data);
}

function showReportPlaceholder(incident) {
    state.reportName = null;
    $('report-title').textContent = 'Report';
    $('report-download').hidden = true;
    $('report-source').hidden = true;
    $('report-meta').textContent = '';
    $('report-body').innerHTML = incident.report
        ? '<p class="empty-state">This report was pruned: only the newest 20 are kept. The timeline above is still complete.</p>'
        : incident.status === 'open'
            ? '<p class="empty-state">Writing the report from the last 15 samples…</p>'
            : '<p class="empty-state">No report was written for this incident.</p>';
}

function renderMarkdown(body, content) {
    if (window.marked) {
        body.classList.remove('raw');
        body.innerHTML = marked.parse(content);
        // Wide tables scroll inside the report instead of widening the page.
        body.querySelectorAll('table').forEach(t => {
            const wrap = document.createElement('div');
            wrap.className = 'table-wrap';
            t.replaceWith(wrap);
            wrap.append(t);
        });
    } else {
        body.classList.add('raw');
        body.textContent = content;
    }
}

function sourceBadge(el, source) {
    const [label, tone] = SOURCE_LABELS[source] || [source, 'neutral'];
    el.hidden = false;
    el.dataset.tone = tone;
    el.textContent = label;
}

function renderReport(report) {
    const body = $('report-body');
    if (!report) {
        body.innerHTML = '<p class="empty-state">No incident reports yet. Run the guided demo or inject a fault in the Chaos Lab to create one.</p>';
        $('report-download').hidden = true;
        $('report-source').hidden = true;
        $('report-meta').textContent = '';
        return;
    }
    state.reportName = report.filename;
    renderMarkdown(body, report.content);
    $('report-title').textContent = report.incident_id ? `Report for incident #${report.incident_id}` : 'Report';
    $('report-meta').textContent = `Written ${fmtDateTime(report.generated_at)}`
        + (report.incident_id ? '' : ', not linked to an incident')
        + '. Times inside are UTC.';
    sourceBadge($('report-source'), report.source);
    const link = $('report-download');
    link.href = `/api/reports/${encodeURIComponent(report.filename)}/download`;
    link.hidden = false;
    $('report-list').querySelectorAll('button').forEach(b => {
        if (b.dataset.name === report.filename) b.setAttribute('aria-current', 'true');
        else b.removeAttribute('aria-current');
    });
}

async function loadLatestReport(name) {
    const r = await api('GET', `/api/reports/${encodeURIComponent(name)}`);
    if (!r.ok) return;
    renderLatest(r.data);
}

function renderLatest(report) {
    const body = $('latest-body');
    const open = $('latest-open');
    if (!report) {
        body.innerHTML = '<p class="empty-state">No incident reports yet. Run the guided demo or inject a fault in the Chaos Lab to create one.</p>';
        $('latest-meta').textContent = '';
        open.hidden = true;
        $('chaos-report-title').textContent = 'None yet.';
        return;
    }
    renderMarkdown(body, report.content);
    const [label] = SOURCE_LABELS[report.source] || [report.source];
    $('latest-meta').textContent = `Written ${fmtDateTime(report.generated_at)} (${ago(report.generated_at)}) · ${label}`
        + (report.incident_id ? ` · incident #${report.incident_id}` : '');
    open.hidden = false;
    open.href = report.incident_id ? `#incidents/${report.incident_id}` : '#incidents';
    open.dataset.report = report.filename;
    const title = (report.content.match(/^#\s+(.+)$/m) || [])[1] || report.filename;
    $('chaos-report-title').textContent = `${title.replace(/^Incident Report:\s*/, '')} · ${fmtTime(report.generated_at)}`;
}

// "Write a report now" sits on the Overview's report panel and on the Incidents view.
function renderReportButton(data) {
    const wait = data.faults.report.cooldown;
    document.querySelectorAll('[data-generate-report]').forEach(btn => {
        btn.disabled = data.is_generating_rca || wait > 0;
        btn.classList.toggle('is-loading', data.is_generating_rca);
        btn.querySelector('span').textContent = data.is_generating_rca ? 'Writing…' : wait > 0 ? `Write again in ${wait} s` : 'Write a report now';
    });
}

async function generateReport(event) {
    const btn = event.currentTarget;
    setLoading(btn, true);
    const r = await api('POST', '/api/rca/generate');
    setLoading(btn, false);
    if (r.ok) {
        toast(r.data.status === 'busy' ? r.data.message : 'Writing a report from the last 15 samples. It appears when it is done.', 'info', 4000);
        if (r.data.status !== 'busy') {
            // Show the new report on its own (it is not linked to an incident) once it exists.
            state.reportPinned = true;
            state.awaitManualReport = true;
            state.selectedIncident = null;
            document.querySelectorAll('.incident-row').forEach(b => b.removeAttribute('aria-current'));
            renderDetail(null);
        }
    } else {
        toast(r.detail, r.status === 429 ? 'info' : 'error');
    }
    pollStatus();
}

// ---------- Charts ----------

const charts = {};

function chartColors() {
    return {
        cpu: cssVar('--chart-cpu'),
        ram: cssVar('--chart-ram'),
        grid: cssVar('--chart-grid'),
        tick: cssVar('--chart-tick'),
        base: cssVar('--chart-base'),
        alert: cssVar('--chart-alert'),
        flag: cssVar('--chart-flag'),
        band: cssVar('--chart-band'),
        synthetic: cssVar('--chart-synthetic'),
        gap: cssVar('--chart-gap'),
        tooltip: cssVar('--chart-tooltip'),
        surface: cssVar('--surface-solid'),
    };
}

const isFlagged = (status, word) => (status || '').startsWith('Anomalous') && status.includes(word);
const isSynthetic = status => (status || '').startsWith('Warm-up');

function withAlpha(colour, alpha) {
    if (colour.startsWith('#') && colour.length === 7) {
        const n = parseInt(colour.slice(1), 16);
        return `rgba(${n >> 16}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
    }
    return colour;
}

// Drawn under the line: shaded spans while an incident was open (labelled with its id), gaps
// where no sample was taken, and the learned baseline and alert lines.
const overlayPlugin = {
    id: 'deepbellOverlay',
    beforeDatasetsDraw(chart) {
        const { ctx, chartArea: a, scales: { x, y } } = chart;
        if (!a) return;
        const c = chartColors();
        ctx.save();
        ctx.font = '500 10px "Fira Code", monospace';
        for (const b of chart.$bands || []) {
            const from = x.getPixelForValue(b.from);
            const to = Math.max(x.getPixelForValue(b.to), from + 2); // an instant still shows
            if (to <= a.left || from >= a.right) continue; // outside the window
            const x0 = Math.max(a.left, from);
            const x1 = Math.min(a.right, to);
            ctx.fillStyle = c.band;
            ctx.fillRect(x0, a.top, x1 - x0, a.bottom - a.top);
            ctx.fillStyle = c.flag;
            ctx.globalAlpha = 0.7;
            ctx.fillRect(x0, a.top, x1 - x0, 2);
            ctx.globalAlpha = 1;
            if (x1 - x0 > 30 && !chart.$small) ctx.fillText(b.label, x0 + 4, a.bottom - 6);
        }
        for (const g of chart.$gaps || []) {
            const x0 = Math.max(a.left, x.getPixelForValue(g.from));
            const x1 = Math.min(a.right, x.getPixelForValue(g.to));
            if (x1 - x0 < 2) continue;
            ctx.fillStyle = c.gap;
            ctx.fillRect(x0, a.top, x1 - x0, a.bottom - a.top);
            if (x1 - x0 > 70 && !chart.$small) {
                ctx.fillStyle = c.tick;
                ctx.textAlign = 'center';
                ctx.fillText('no samples', (x0 + x1) / 2, a.top + 14);
                ctx.textAlign = 'start';
            }
        }
        for (const [value, colour, dash] of [[chart.$base, c.base, [2, 4]], [chart.$alert, c.alert, [6, 4]]]) {
            if (value === null || value === undefined) continue;
            const py = y.getPixelForValue(value);
            if (py < a.top || py > a.bottom) continue;
            ctx.strokeStyle = colour;
            ctx.lineWidth = 1.25;
            ctx.setLineDash(dash);
            ctx.beginPath();
            ctx.moveTo(a.left, py);
            ctx.lineTo(a.right, py);
            ctx.stroke();
        }
        ctx.restore();
    },
    afterDatasetsDraw(chart) {
        if (chart.$small || !chart.chartArea) return;
        const { ctx, chartArea: a, scales: { y } } = chart;
        const c = chartColors();
        ctx.save();
        ctx.font = '500 10px "Fira Code", monospace';
        const labels = [[chart.$alert, 'alert', c.alert], [chart.$base, 'baseline', c.base]];
        let lastY = null;
        for (const [value, label, colour] of labels) {
            if (value === null || value === undefined) continue;
            const py = y.getPixelForValue(value);
            // Skip a label that would fall outside the plot or on top of the previous one.
            if (py < a.top + 10 || py > a.bottom - 2 || (lastY !== null && Math.abs(lastY - py) < 14)) continue;
            lastY = py;
            const w = ctx.measureText(label).width + 8;
            ctx.fillStyle = c.surface;
            ctx.globalAlpha = 0.85;
            ctx.fillRect(a.left + 2, py - 15, w, 13);
            ctx.globalAlpha = 1;
            ctx.fillStyle = colour;
            ctx.fillText(label, a.left + 6, py - 5);
        }
        ctx.restore();
    },
};

// Wall-clock tick labels for a time axis in milliseconds (seconds only on short ranges).
function clockLabel(ms, rangeSec) {
    const opts = { hour: '2-digit', minute: '2-digit', hour12: false };
    if (rangeSec <= 180) opts.second = '2-digit';
    return new Date(ms).toLocaleTimeString([], opts);
}

// A time series: x is the sample's time (ms), so gaps and uneven sampling are drawn to scale.
function lineChart(canvasId, { series, word, unit, small }) {
    const c = chartColors();
    const statusAt = (ctx, i) => (ctx.chart.$statuses || [])[i];
    const tickFont = { family: 'Fira Code', size: small ? 10 : 11 };
    const chart = new Chart($(canvasId), {
        type: 'line',
        data: {
            datasets: [
                {
                    label: series === 'cpu' ? 'CPU' : 'Memory',
                    data: [],
                    borderColor: c[series],
                    backgroundColor: ctx => {
                        const { chartArea, ctx: g } = ctx.chart;
                        if (!chartArea) return 'transparent';
                        const fill = g.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
                        const cc = chartColors()[series];
                        fill.addColorStop(0, withAlpha(cc, 0.22));
                        fill.addColorStop(1, withAlpha(cc, 0));
                        return fill;
                    },
                    fill: 'start',
                    spanGaps: false,
                    borderWidth: 2,
                    tension: 0.25,
                    pointRadius: ctx => (isFlagged(statusAt(ctx, ctx.dataIndex), word) ? (small ? 2.5 : 3.5) : 0),
                    pointHoverRadius: 4,
                    // Red marks a flagged sample only; a hovered normal sample keeps the series colour.
                    pointBackgroundColor: ctx => (isFlagged(statusAt(ctx, ctx.dataIndex), word) ? chartColors().flag : chartColors()[series]),
                    pointBorderColor: () => chartColors().surface,
                    pointBorderWidth: 1.5,
                    segment: {
                        borderColor: ctx => (isSynthetic(statusAt(ctx, ctx.p1DataIndex)) ? chartColors().synthetic : undefined),
                        borderDash: ctx => (isSynthetic(statusAt(ctx, ctx.p1DataIndex)) ? [4, 4] : undefined),
                    },
                },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            interaction: { mode: 'nearest', axis: 'x', intersect: false },
            scales: {
                x: {
                    type: 'linear',
                    grid: { display: false },
                    border: { color: c.grid },
                    ticks: {
                        color: c.tick,
                        includeBounds: false,
                        maxRotation: 0,
                        autoSkipPadding: 16,
                        font: tickFont,
                        callback(value) { return clockLabel(value, this.chart.$rangeSec); },
                    },
                },
                y: {
                    grid: { color: c.grid },
                    border: { display: false },
                    ticks: { color: c.tick, maxTicksLimit: small ? 3 : 5, font: tickFont, callback: v => `${v}${series === 'cpu' ? '%' : ''}` },
                    title: { display: !small, text: series === 'cpu' ? 'CPU %' : 'MB', color: c.tick, font: { family: 'Plus Jakarta Sans', size: 11, weight: '600' } },
                },
            },
            plugins: {
                legend: { display: false },
                tooltip: {
                    backgroundColor: c.tooltip,
                    borderColor: 'rgba(255,255,255,0.12)',
                    borderWidth: 1,
                    titleFont: { family: 'Fira Code', size: 11, weight: '500' },
                    bodyFont: { family: 'Plus Jakarta Sans', size: 12 },
                    footerFont: { family: 'Plus Jakarta Sans', size: 11, weight: '400' },
                    padding: 10,
                    displayColors: false,
                    callbacks: {
                        title: items => (items.length ? fmtTime(new Date(items[0].parsed.x).toISOString()) : ''),
                        label: item => `${item.dataset.label}: ${item.parsed.y.toFixed(1)} ${unit}`,
                        footer: items => {
                            if (!items.length) return '';
                            const ch = items[0].chart;
                            const alert = ch.$alert === null || ch.$alert === undefined ? '' : `Alert above ${ch.$alert.toFixed(series === 'cpu' ? 1 : 0)} ${unit}\n`;
                            return alert + (statusAt(items[0], items[0].dataIndex) || '');
                        },
                    },
                },
            },
        },
        plugins: [overlayPlugin],
    });
    chart.$series = series;
    chart.$small = !!small;
    chart.$rangeSec = 120;
    return chart;
}

// What the detector sees: each sample as (memory, CPU), with both alert lines.
const spacePlugin = {
    id: 'deepbellSpace',
    beforeDatasetsDraw(chart) {
        const { ctx, chartArea: a, scales: { x, y } } = chart;
        const limits = chart.$limits;
        if (!limits || !a) return;
        const c = chartColors();
        const px = x.getPixelForValue(limits.ram);
        const py = y.getPixelForValue(limits.cpu);
        ctx.save();
        // Candidate region: beyond either alert line.
        ctx.fillStyle = c.band;
        if (px < a.right) ctx.fillRect(Math.max(px, a.left), a.top, a.right - Math.max(px, a.left), a.bottom - a.top);
        if (py > a.top) ctx.fillRect(a.left, a.top, Math.min(px, a.right) - a.left, Math.min(py, a.bottom) - a.top);
        ctx.strokeStyle = c.alert;
        ctx.setLineDash([6, 4]);
        ctx.lineWidth = 1.25;
        ctx.beginPath();
        if (px >= a.left && px <= a.right) { ctx.moveTo(px, a.top); ctx.lineTo(px, a.bottom); }
        if (py >= a.top && py <= a.bottom) { ctx.moveTo(a.left, py); ctx.lineTo(a.right, py); }
        ctx.stroke();
        ctx.setLineDash([]);
        ctx.fillStyle = c.alert;
        ctx.font = '500 10px "Fira Code", monospace';
        if (px >= a.left && px <= a.right) {
            // Beside the vertical line, on whichever side has room, never across it.
            const label = `memory alert ${limits.ram.toFixed(0)} MB`;
            const left = px + 6 + ctx.measureText(label).width > a.right;
            ctx.textAlign = left ? 'right' : 'left';
            ctx.fillText(label, left ? px - 6 : px + 6, a.top + 12);
            ctx.textAlign = 'left';
        }
        if (py >= a.top && py <= a.bottom) ctx.fillText(`CPU alert ${limits.cpu.toFixed(1)}%`, a.left + 6, py - 5);
        ctx.restore();
    },
};

function spaceChart() {
    const c = chartColors();
    const chart = new Chart($('chart-space'), {
        type: 'scatter',
        data: {
            datasets: [
                { label: 'Normal', data: [], pointRadius: 3, pointHoverRadius: 5, backgroundColor: withAlpha(c.cpu, 0.55), borderWidth: 0 },
                { label: 'Flagged', data: [], pointRadius: 4.5, pointHoverRadius: 6, backgroundColor: c.flag, borderColor: c.surface, borderWidth: 1.5 },
                { label: 'Warm-up (synthetic)', data: [], pointRadius: 2.5, pointHoverRadius: 4, backgroundColor: withAlpha(c.synthetic, 0.7), borderWidth: 0 },
            ],
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            scales: {
                x: {
                    title: { display: true, text: 'Memory (RSS), MB', color: c.tick, font: { family: 'Plus Jakarta Sans', size: 11, weight: '600' } },
                    grid: { color: c.grid },
                    border: { display: false },
                    ticks: { color: c.tick, font: { family: 'Fira Code', size: 11 }, maxTicksLimit: 6 },
                },
                y: {
                    title: { display: true, text: 'CPU %', color: c.tick, font: { family: 'Plus Jakarta Sans', size: 11, weight: '600' } },
                    min: 0,
                    grid: { color: c.grid },
                    border: { display: false },
                    ticks: { color: c.tick, font: { family: 'Fira Code', size: 11 }, maxTicksLimit: 6 },
                },
            },
            plugins: {
                legend: { position: 'bottom', labels: { color: c.tick, boxWidth: 8, boxHeight: 8, usePointStyle: true, font: { family: 'Plus Jakarta Sans', size: 11 } } },
                tooltip: {
                    backgroundColor: c.tooltip,
                    titleFont: { family: 'Fira Code', size: 11 },
                    bodyFont: { family: 'Plus Jakarta Sans', size: 12 },
                    callbacks: {
                        title: items => (items.length ? fmtTime(items[0].raw.t) : ''),
                        label: item => `${item.dataset.label}: ${item.raw.y.toFixed(1)} % CPU, ${item.raw.x.toFixed(1)} MB`,
                    },
                },
            },
        },
        plugins: [spacePlugin],
    });
    chart.$space = true;
    return chart;
}

function initCharts() {
    if (typeof Chart === 'undefined') {
        document.querySelectorAll('[data-chart-fallback]').forEach(el => { el.hidden = false; });
        document.querySelectorAll('.chart-box').forEach(el => { el.hidden = true; });
        return;
    }
    Chart.defaults.font.family = 'Plus Jakarta Sans';
    const cpu = { series: 'cpu', word: 'CPU', unit: '%' };
    const ram = { series: 'ram', word: 'Memory', unit: 'MB' };
    charts.ovCpu = lineChart('chart-ov-cpu', cpu);
    charts.ovRam = lineChart('chart-ov-ram', ram);
    charts.cpu = lineChart('chart-cpu', cpu);
    charts.ram = lineChart('chart-ram', ram);
    charts.miniCpu = lineChart('chart-mini-cpu', { ...cpu, small: true });
    charts.miniRam = lineChart('chart-mini-ram', { ...ram, small: true });
    charts.space = spaceChart();
}

// Re-read the colour tokens after a theme switch.
function restyleCharts() {
    if (typeof Chart === 'undefined' || !charts.cpu) return;
    const c = chartColors();
    for (const chart of Object.values(charts)) {
        const scales = chart.options.scales;
        for (const axis of Object.values(scales)) {
            if (axis.grid) axis.grid.color = c.grid;
            if (axis.ticks) axis.ticks.color = c.tick;
            if (axis.title) axis.title.color = c.tick;
            if (axis.border && axis.border.color) axis.border.color = c.grid;
        }
        chart.options.plugins.tooltip.backgroundColor = c.tooltip;
        if (chart.$space) {
            chart.data.datasets[0].backgroundColor = withAlpha(c.cpu, 0.55);
            chart.data.datasets[1].backgroundColor = c.flag;
            chart.data.datasets[1].borderColor = c.surface;
            chart.data.datasets[2].backgroundColor = withAlpha(c.synthetic, 0.7);
            chart.options.plugins.legend.labels.color = c.tick;
        } else {
            chart.data.datasets[0].borderColor = c[chart.$series];
        }
        chart.update('none');
    }
}

function resizeCharts() {
    requestAnimationFrame(() => Object.values(charts).forEach(c => c.resize()));
}

// One series, oldest first. A break (y: null) goes where no sample was taken for more than
// three sample intervals, so a restart or a starved sampler shows as a gap, not a smooth line.
function seriesFrom(rows, value) {
    const gapMs = 3 * CONFIG.sampleIntervalSec * 1000;
    const points = [];
    const statuses = [];
    const gaps = [];
    let prev = null;
    for (const r of rows) {
        const t = parseTime(r.timestamp)?.getTime();
        if (t === undefined) continue;
        if (prev !== null && t - prev > gapMs) {
            points.push({ x: prev + 1, y: null });
            statuses.push(null);
            gaps.push({ from: prev, to: t });
        }
        points.push({ x: t, y: value(r) });
        statuses.push(r.status);
        prev = t;
    }
    return { points, statuses, gaps };
}

// When each incident was open, in ms; an open one runs until now.
function incidentBands() {
    const now = Date.now() + state.serverOffsetMs;
    return state.incidents.flatMap(inc => {
        const from = parseTime(inc.detected_at)?.getTime();
        if (!from) return [];
        const to = parseTime(inc.resolved_at)?.getTime() ?? (inc.status === 'open' ? now : from);
        return [{ from, to, label: `#${inc.id}` }];
    });
}

// The axis always ends at the server's "now", so the window slides even when samples stop.
function updateLine(chart, series, base, delta, bands, rangeSec) {
    if (!chart) return;
    const now = Date.now() + state.serverOffsetMs;
    const min = now - rangeSec * 1000;
    chart.$rangeSec = rangeSec;
    chart.$statuses = series.statuses;
    chart.$gaps = series.gaps;
    chart.$bands = bands;
    chart.$base = base;
    chart.$alert = base === null ? null : base + delta;
    chart.data.datasets[0].data = series.points;
    const x = chart.options.scales.x;
    x.min = min;
    x.max = now;
    x.ticks.stepSize = (chart.$small ? 2 : 1) * (rangeSec <= 180 ? 30000 : 120000);
    const values = series.points.filter(p => p.x >= min && p.y !== null).map(p => p.y);
    const y = chart.options.scales.y;
    if (chart.$series === 'cpu') {
        y.min = 0;
        y.suggestedMax = Math.max(30, (chart.$alert ?? 0) + 10, ...values);
    } else if (base !== null) {
        y.suggestedMin = Math.min(base - 8, ...values);
        y.suggestedMax = Math.max(base + delta + 10, ...values);
    }
    chart.update('none');
}

function updateSpace(rows, base) {
    const chart = charts.space;
    if (!chart) return;
    const point = r => ({ x: r.ram, y: r.cpu, t: r.timestamp });
    chart.data.datasets[0].data = rows.filter(r => !isSynthetic(r.status) && !(r.status || '').startsWith('Anomalous')).map(point);
    chart.data.datasets[1].data = rows.filter(r => (r.status || '').startsWith('Anomalous')).map(point);
    chart.data.datasets[2].data = rows.filter(r => isSynthetic(r.status)).map(point);
    chart.$limits = base ? { cpu: base.cpu + CONFIG.cpuDeltaPct, ram: base.ram + CONFIG.ramDeltaMb } : null;
    const rams = rows.map(r => r.ram);
    if (base && rams.length) {
        chart.options.scales.x.suggestedMin = Math.min(base.ram - 6, ...rams);
        chart.options.scales.x.suggestedMax = Math.max(base.ram + CONFIG.ramDeltaMb + 12, ...rams);
        chart.options.scales.y.suggestedMax = Math.max(base.cpu + CONFIG.cpuDeltaPct + 10, ...rows.map(r => r.cpu));
    }
    chart.update('none');
}

function renderBands() {
    if (state.rows.length) drawTelemetry(state.rows);
}

// ---------- Telemetry ----------

// A small trend line. minSpan keeps tiny jitter from looking like a spike.
function spark(svg, values, minSpan) {
    if (values.length < 2) { svg.innerHTML = ''; return; }
    const lo = Math.min(...values);
    const hi = Math.max(...values);
    const span = Math.max(hi - lo, minSpan);
    const min = (lo + hi) / 2 - span / 2;
    const pts = values.map((v, i) => [(i / (values.length - 1)) * 80, 22 - ((v - min) / span) * 20]);
    const line = pts.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`).join(' ');
    svg.innerHTML = `<path class="spark-area" d="${line} L80 24 L0 24 Z"></path><path d="${line}"></path>`;
}

function statusBadge(status) {
    if ((status || '').startsWith('Anomalous')) return `<span class="badge" data-tone="bad">${esc(status)}</span>`;
    if (status === 'Normal') return '<span class="badge" data-tone="ok">Normal</span>';
    return `<span class="badge">${esc(status)}</span>`;
}

// Seconds of telemetry the visible view plots.
const chartRange = () => (state.view === 'telemetry' ? state.telemetryRange : 120);

// Called when the status poll sees a new sample, and when the view or range changes.
async function loadTelemetry() {
    const seq = ++state.telemetrySeq;
    const limit = Math.ceil(chartRange() / CONFIG.sampleIntervalSec) + 10;
    const r = await api('GET', `/api/metrics?limit=${limit}`);
    if (seq !== state.telemetrySeq) return; // a newer request is on its way
    const tbody = $('samples-body');
    if (!r.ok || !Array.isArray(r.data)) {
        setHTML(tbody, `<tr><td colspan="5" class="error-text">Could not load samples: ${esc(r.detail)}</td></tr>`);
        return;
    }
    state.rows = [...r.data].reverse(); // oldest first, left to right
    drawTelemetry(state.rows);
    if (!r.data.length) {
        setHTML(tbody, '<tr><td colspan="5" class="table-empty">No samples recorded yet. The first arrives within 2 s of startup.</td></tr>');
        return;
    }
    const base = state.status?.baseline;
    setHTML(tbody, r.data.slice(0, 15).map(m => {
        const over = base ? m.ram - base.ram : null;
        const delta = over === null ? '–' : `${over >= 0 ? '+' : ''}${over.toFixed(1)} MB`;
        return `<tr>
            <td class="time">${esc(fmtTime(m.timestamp))}</td>
            <td class="num">${m.cpu.toFixed(1)}</td>
            <td class="num">${m.ram.toFixed(1)}</td>
            <td class="num hide-sm ${over !== null && over >= CONFIG.ramDeltaMb ? 'delta-up' : 'delta-flat'}">${esc(delta)}</td>
            <td>${statusBadge(m.status)}</td>
        </tr>`;
    }).join(''));
}

const VIEW_CHARTS = { overview: ['ovCpu', 'ovRam'], telemetry: ['cpu', 'ram'], chaos: ['miniCpu', 'miniRam'] };

function drawTelemetry(rows) {
    state.drawnAt = Date.now();
    const base = state.status?.baseline ?? null;
    const recent = rows.slice(-30);
    spark($('spark-cpu'), recent.map(r => r.cpu), 10);
    spark($('spark-ram'), recent.map(r => r.ram), 20);
    const visible = VIEW_CHARTS[state.view];
    if (!visible) return;
    const bands = incidentBands();
    updateLine(charts[visible[0]], seriesFrom(rows, r => r.cpu), base ? base.cpu : null, CONFIG.cpuDeltaPct, bands, chartRange());
    updateLine(charts[visible[1]], seriesFrom(rows, r => r.ram), base ? base.ram : null, CONFIG.ramDeltaMb, bands, chartRange());
    if (state.view === 'telemetry') updateSpace(rows, base);
}

// ---------- Chaos Lab ----------

const SHARED_NOTE = CONFIG.publicDemo ? ' Everyone viewing this demo will see it.' : '';
const FAULTS = {
    cpu_spike: {
        url: () => `/fault/cpu_spike?duration_sec=${CONFIG.maxSpikeSec}`,
        title: 'Inject a CPU spike?',
        body: () => `A busy loop runs on this server for ${CONFIG.maxSpikeSec} seconds.${SHARED_NOTE}`,
        confirm: 'Inject spike',
        done: 'CPU spike injected. Watch the pipeline in Live results: the watchdog should open an incident within a few seconds.',
    },
    memory_leak: {
        url: () => `/fault/memory_leak?size_mb=${CONFIG.leakStepMb}`,
        title: 'Leak memory?',
        body: () => `The server allocates ${CONFIG.leakStepMb} MB and holds it until someone releases it (or for ${CONFIG.leakAutoReleaseSec / 60} minutes at most). The incident stays open while it is held.${SHARED_NOTE}`,
        confirm: 'Leak memory',
        done: 'Memory leaked. The incident stays open until the memory is released.',
    },
    corrupt_db: {
        url: () => '/fault/corrupt_db',
        title: 'Corrupt the database?',
        body: () => `Garbage is written over the SQLite file header. Transactions fail until a snapshot is restored in the Time Machine.${SHARED_NOTE}`,
        confirm: 'Corrupt database',
        done: 'Database corrupted. Restore a snapshot in the Time Machine to recover.',
    },
    fatal_crash: {
        url: () => '/fault/fatal_crash',
        title: 'Crash the server?',
        body: () => 'The process exits immediately. Under run_all.py, the supervisor restarts it after 2 seconds and this page reconnects.',
        confirm: 'Crash server',
        done: 'Server is exiting. This page reconnects when it is back.',
    },
};

async function injectFault(kind, btn) {
    const f = FAULTS[kind];
    if (!await confirmAction({ title: f.title, body: f.body(), confirmLabel: f.confirm, danger: true })) return;
    setLoading(btn, true);
    const r = await api('POST', f.url());
    setLoading(btn, false);
    if (r.ok) {
        toast(f.done, 'warn', 8000);
        showResults();
    } else {
        toast(r.detail, r.status === 429 || r.status === 409 ? 'info' : 'error');
    }
    await pollStatus();
}

// On a phone the Live results panel sits below the fault cards: after an action, bring it
// into view so the visitor sees detection happen.
function showResults() {
    if (window.innerWidth > 900) return;
    document.querySelector('.results').scrollIntoView({ behavior: reducedMotion() ? 'auto' : 'smooth', block: 'start' });
}

async function releaseMemory() {
    const btn = $('btn-release');
    setLoading(btn, true);
    const r = await api('POST', '/fault/memory_release');
    setLoading(btn, false);
    if (r.ok) {
        toast(r.data.released_mb
            ? `Released ${r.data.released_mb} MB. The incident closes after ${CONFIG.closeAfter} normal samples.`
            : 'Nothing to release: the memory was already freed.', r.data.released_mb ? 'success' : 'info');
        showResults();
    } else {
        toast(r.detail, 'error');
    }
    pollStatus();
}

function renderFaults(f) {
    // A cap that blocks the fault wins over the cooldown: waiting would not help.
    const setButton = (name, wait, label, blocked = '', blockedNote = '') => {
        const btn = $(`btn-${name}`);
        if (!btn) return;
        if (!btn.classList.contains('is-loading')) btn.disabled = !!blocked || wait > 0;
        btn.querySelector('span').textContent = blocked || (wait > 0 ? `Cooling down, ${wait} s` : label);
        const stateEl = $(`state-${name}`);
        if (stateEl) stateEl.textContent = blocked ? blockedNote : wait > 0 ? 'shared cooldown' : '';
        const cool = $(`cool-${name}`);
        if (cool) cool.style.width = `${blocked ? 0 : (wait / CONFIG.cooldownSec) * 100}%`;
    };
    setButton('cpu_spike', f.cpu_spike.cooldown, 'Inject spike');
    const leak = f.memory_leak;
    const full = leak.held_mb + leak.step_mb > leak.limit_mb;
    setButton('memory_leak', leak.cooldown, `Inject ${leak.step_mb} MB leak`, full ? 'Limit reached' : '',
        'release memory first');
    $('leak-fill').style.width = `${(leak.held_mb / leak.limit_mb) * 100}%`;
    $('leak-meter').setAttribute('aria-valuenow', String(leak.held_mb));
    $('leak-held').textContent = `${leak.held_mb} of ${leak.limit_mb} MB held`
        + (leak.auto_release_in !== null ? `, auto-release in ${fmtDuration(leak.auto_release_in)}` : '');
    const release = $('btn-release');
    if (!release.classList.contains('is-loading')) release.disabled = leak.held_mb === 0;
    setButton('corrupt_db', f.corrupt_db.cooldown, 'Corrupt database');
}

// ---------- Time Machine: snapshots and transactions ----------

async function loadTransactions() {
    const tbody = $('tx-body');
    const db = state.status?.database;
    if (db && !db.ok) {
        // The status already says the database is unreadable; a query would only return 503.
        setHTML(tbody, `<tr><td colspan="3" class="error-text">The database is unreadable (${esc(db.error)}). Restore a snapshot to see the transactions again.</td></tr>`);
        return;
    }
    const r = await api('GET', '/transactions');
    if (!r.ok) {
        setHTML(tbody, `<tr><td colspan="3" class="error-text">${esc(r.detail)}</td></tr>`);
        return;
    }
    if (!r.data.length) {
        setHTML(tbody, '<tr><td colspan="3" class="table-empty">No transactions. Add one above.</td></tr>');
        return;
    }
    const first = state.knownTx.size === 0;
    setHTML(tbody, r.data.map(t => `
        <tr${!first && !state.knownTx.has(t.id) ? ' class="is-new"' : ''}>
            <td class="time">${esc(fmtDateTime(t.timestamp))}</td>
            <td class="num">${Number(t.amount).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</td>
            <td class="mono hide-sm" title="${esc(t.id)}">${esc(String(t.id).slice(0, 8))}</td>
        </tr>`).join(''));
    r.data.forEach(t => state.knownTx.add(t.id));
}

function validateAmount(input) {
    const raw = input.value.trim();
    const value = Number(raw);
    if (!raw) return 'Enter an amount.';
    if (!Number.isFinite(value)) return 'Enter a number, for example 25.00.';
    if (value <= 0) return 'The amount must be greater than 0.';
    if (value > 1000000) return 'The amount must be at most 1,000,000.';
    return '';
}

async function addTransaction(event) {
    event.preventDefault();
    const input = $('tx-amount');
    const error = validateAmount(input);
    $('tx-error').textContent = error;
    input.setAttribute('aria-invalid', String(!!error));
    if (error) { input.focus(); return; }
    const btn = $('btn-add-tx');
    setLoading(btn, true);
    const r = await api('POST', `/transaction?amount=${encodeURIComponent(input.value)}`);
    setLoading(btn, false);
    btn.disabled = !state.lastDbOk;
    if (r.ok) {
        toast(`Added a transaction of ${r.data.amount.toFixed(2)}. The next snapshot (within ${CONFIG.snapshotIntervalSec} s) will include it.`, 'success', 4000);
        input.value = (5 + Math.random() * 495).toFixed(2);
    } else {
        $('tx-error').textContent = r.detail;
        input.setAttribute('aria-invalid', 'true');
    }
    loadTransactions();
}

async function loadSnapshots() {
    const tbody = $('snap-body');
    const r = await api('GET', '/api/backups');
    if (!r.ok) {
        setHTML(tbody, `<tr><td colspan="4" class="error-text">Could not list snapshots: ${esc(r.detail)}</td></tr>`);
        return;
    }
    state.snapshots = r.data;
    renderSnapTrack(r.data);
    $('snap-sub').textContent = r.data.length
        ? `${r.data.length} of ${CONFIG.snapshotsKeep} kept, newest first. Each is checked with PRAGMA quick_check.`
        : 'newest first';
    if (!r.data.length) {
        setHTML(tbody, '<tr><td colspan="4" class="table-empty">No snapshots yet. The first is taken when the app starts.</td></tr>');
        return;
    }
    // The rows' "x s ago" text is filled in separately, so the markup (and a focused Restore
    // button) only changes when the snapshots do.
    const current = state.status?.latest_snapshot?.current;
    setHTML(tbody, r.data.map((b, i) => `
        <tr>
            <td class="time">${esc(fmtTime(b.timestamp))} <span class="cell-ago" data-ago="${esc(b.timestamp)}" data-suffix="${i === 0 ? (current ? ', newest, same as live data' : ', newest') : ''}"></span><span class="cell-sub mono hide-sm">${esc(b.filename)}</span></td>
            <td class="num">${b.readable ? esc(b.transactions) : '<span class="tag-bad">unreadable</span>'}</td>
            <td class="num hide-sm">${esc(b.size_kb)} KB</td>
            <td class="num"><button class="btn ${i === 0 && !state.lastDbOk ? 'btn-danger' : 'btn-secondary'} btn-small" type="button" data-snapshot="${esc(b.filename)}" data-taken="${esc(b.timestamp)}" data-rows="${esc(b.transactions)}" data-focus="snap-${esc(b.filename)}"${b.readable ? '' : ' disabled'}>${icon('restore')}Restore<span class="sr-only"> the snapshot from ${esc(fmtTime(b.timestamp))}</span></button></td>
        </tr>`).join(''));
    tbody.querySelectorAll('[data-ago]').forEach(el => { el.textContent = ago(el.dataset.ago) + el.dataset.suffix; });
}

function renderSnapTrack(snapshots) {
    const track = $('snap-track');
    if (!snapshots.length) { track.innerHTML = ''; return; }
    const now = Date.now() + state.serverOffsetMs;
    const times = snapshots.map(s => parseTime(s.timestamp).getTime());
    const oldest = Math.min(...times);
    const span = Math.max(now - oldest, 60000);
    track.innerHTML = '<span class="snap-axis"></span><span class="snap-now" title="now"></span>'
        + snapshots.map((s, i) => {
            const left = ((parseTime(s.timestamp).getTime() - oldest) / span) * 97;
            return `<span class="snap-dot" style="left:${left.toFixed(2)}%"${i === 0 ? ' data-newest' : ''}${s.readable ? '' : ' data-bad'} title="${esc(fmtTime(s.timestamp))}"></span>`;
        }).join('')
        + `<span class="snap-label snap-label-start">${esc(fmtTime(new Date(oldest).toISOString()))}</span><span class="snap-label snap-label-end">now</span>`;
}

async function rollback(filename, taken, rows, btn) {
    const ok = await confirmAction({
        title: 'Roll back the database?',
        body: `The database is restored to the snapshot taken at ${fmtDateTime(taken)}`
            + (rows !== undefined && rows !== 'null' ? `, which holds ${rows} transactions` : '')
            + '. Anything added after that is lost.'
            + (CONFIG.publicDemo ? ' This is a shared demo, so everyone sees the change.' : ''),
        confirmLabel: 'Restore snapshot',
    });
    if (!ok) return;
    setLoading(btn, true);
    const form = new FormData();
    form.append('filename', filename);
    const r = await api('POST', '/api/rollback', form);
    setLoading(btn, false);
    if (r.ok && r.data.status === 'success') toast(`Database restored to the snapshot from ${fmtTime(taken)}. Any database incident resolves now.`, 'success');
    else if (r.ok) toast('The snapshot was restored, but the database still fails its check. Try an older snapshot.', 'warn', 8000);
    else toast(`Rollback failed: ${r.detail}`, 'error');
    loadSnapshots();
    loadTransactions();
    pollStatus();
}

function restoreNewest() {
    const good = state.snapshots.find(s => s.readable);
    if (!good) {
        toast('No readable snapshot is available yet.', 'error');
        return;
    }
    rollback(good.filename, good.timestamp, String(good.transactions), $('restore-newest'));
}

// ---------- Settings (local runs only) ----------

async function saveApiKey(event) {
    event.preventDefault();
    const input = $('settings-api-key');
    const feedback = $('settings-feedback');
    if (!input.value.trim()) {
        feedback.textContent = 'The key cannot be empty.';
        feedback.dataset.tone = 'bad';
        input.setAttribute('aria-invalid', 'true');
        input.focus();
        return;
    }
    input.setAttribute('aria-invalid', 'false');
    const form = new FormData();
    form.append('api_key', input.value);
    const btn = $('key-save');
    setLoading(btn, true);
    feedback.textContent = 'Saving…';
    feedback.dataset.tone = '';
    const r = await api('POST', '/api/settings/apikey', form);
    setLoading(btn, false);
    feedback.textContent = r.ok ? r.data.message : r.detail;
    feedback.dataset.tone = r.ok ? 'ok' : 'bad';
    if (r.ok) input.value = '';
}

// ---------- Start-up ----------

function every(ms, fn) {
    setInterval(() => { if (!document.hidden && !state.offline) fn(); }, ms);
}

function initMarkdown() {
    if (!window.marked) return;
    // Reports are Markdown. Raw HTML inside them is shown as text, links are limited to
    // http(s) and mailto, and images are reduced to their alt text, so a report can never
    // run script or load remote content.
    const text = value => (typeof value === 'string' ? value : value?.text ?? '');
    marked.use({
        renderer: {
            html: html => esc(text(html)),
            link(href, title, label) {
                const url = typeof href === 'object' ? href.href : href;
                const inner = typeof href === 'object' ? esc(href.text) : label;
                return /^(https?:|mailto:)/i.test(url || '')
                    ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${inner}</a>`
                    : inner;
            },
            image(href, title, alt) {
                return esc(typeof href === 'object' ? href.text : alt);
            },
        },
    });
}

// On narrow screens the nav scrolls sideways; a CSS fade on its right edge shows while more items remain.
function initNavHint() {
    const nav = $('nav');
    const update = () => nav.toggleAttribute('data-more', nav.scrollWidth - nav.clientWidth - nav.scrollLeft > 4);
    nav.addEventListener('scroll', update, { passive: true });
    window.addEventListener('resize', update);
    update();
}

document.addEventListener('DOMContentLoaded', () => {
    initMarkdown();
    initNavHint();
    initCharts();
    initStrip();
    applyTheme(document.documentElement.dataset.theme === 'light' ? 'light' : 'dark');
    renderReport(INITIAL_REPORT);
    renderLatest(INITIAL_REPORT);

    // Views are hash routes, so the skip link moves focus itself instead of changing the hash.
    document.querySelector('.skip-link').addEventListener('click', e => {
        e.preventDefault();
        $('view-title').focus();
    });
    window.addEventListener('hashchange', () => showView(routeFromHash(), true));
    $('theme-toggle').addEventListener('click', toggleTheme);
    $('demo-run').addEventListener('click', startDemo);
    document.querySelectorAll('[data-generate-report]').forEach(btn => btn.addEventListener('click', generateReport));
    // Lists are re-rendered while polling, so their clicks are handled once, here.
    $('incident-list').addEventListener('click', e => {
        const row = e.target.closest('.incident-row');
        if (!row) return;
        selectIncident(Number(row.dataset.id), true);
        if (window.innerWidth <= 1200) $('incident-detail').scrollIntoView({ behavior: reducedMotion() ? 'auto' : 'smooth', block: 'start' });
    });
    $('report-list').addEventListener('click', e => {
        const btn = e.target.closest('button[data-name]');
        if (btn) openReportFromList(btn.dataset.name);
    });
    $('snap-body').addEventListener('click', e => {
        const btn = e.target.closest('button[data-snapshot]');
        if (btn) rollback(btn.dataset.snapshot, btn.dataset.taken, btn.dataset.rows, btn);
    });
    document.querySelectorAll('[data-fault]').forEach(btn => btn.addEventListener('click', () => injectFault(btn.dataset.fault, btn)));
    $('btn-release').addEventListener('click', releaseMemory);
    $('tx-form').addEventListener('submit', addTransaction);
    $('tx-amount').addEventListener('input', () => {
        if ($('tx-amount').getAttribute('aria-invalid') === 'true') {
            const error = validateAmount($('tx-amount'));
            $('tx-error').textContent = error;
            $('tx-amount').setAttribute('aria-invalid', String(!!error));
        }
    });
    $('restore-newest').addEventListener('click', restoreNewest);
    $('key-form')?.addEventListener('submit', saveApiKey);
    $('latest-open').addEventListener('click', e => {
        const name = e.currentTarget.dataset.report;
        const rep = state.reports.find(x => x.filename === name);
        if (!rep?.incident_id && name) {
            state.reportPinned = true;
            state.selectedIncident = null;
            showReport(name);
        }
    });
    // "Run the guided demo" buttons inside empty states go to the overview and start it.
    document.addEventListener('click', e => {
        const runner = e.target.closest('[data-run-demo]');
        if (runner) setTimeout(startDemo, 50);
        const scroller = e.target.closest('[data-scroll-to]');
        if (scroller) {
            setTimeout(() => $(scroller.dataset.scrollTo)?.scrollIntoView({ behavior: reducedMotion() ? 'auto' : 'smooth', block: 'center' }), 60);
        }
    });
    document.querySelectorAll('[data-range]').forEach(btn => btn.addEventListener('click', () => {
        state.telemetryRange = Number(btn.dataset.range);
        document.querySelectorAll('[data-range]').forEach(b => b.setAttribute('aria-pressed', String(b === btn)));
        $('tl-sub').textContent = `CPU and resident memory of this process, last ${btn.textContent}. Shaded spans are open incidents; blank spans had no samples.`;
        loadTelemetry();
    }));
    document.querySelectorAll('[data-filter]').forEach(btn => btn.addEventListener('click', () => {
        state.filter = btn.dataset.filter;
        document.querySelectorAll('[data-filter]').forEach(b => b.setAttribute('aria-pressed', String(b === btn)));
        renderIncidentList();
    }));
    $('confirm').addEventListener('click', e => { if (e.target === $('confirm')) $('confirm').close('cancel'); });

    showView(routeFromHash());
    pollStatus();
    loadIncidents();
    loadReports();
    schedulePoll();
    every(15000, () => { if (state.view === 'overview') loadHistory(); });
    every(2000, () => { if (state.view === 'alerts') loadAlerts(); });
    every(10000, () => { if (state.view === 'timemachine') { loadTransactions(); loadSnapshots(); } });
    window.addEventListener('resize', () => {
        clearTimeout(state.resizeTimer);
        state.resizeTimer = setTimeout(() => { if (state.view === 'overview') loadHistory(); }, 300);
    });
    document.addEventListener('visibilitychange', () => { if (!document.hidden) { pollStatus(); refreshView(); } });
});
