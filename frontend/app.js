let seriesCache = [];
let selectedSeries = null;
const seriesCodeByUrl = new Map();
const expandedReservationGroups = new Set();
const recordingById = new Map();
const DEBUG_LOG_KEY = 'nhkRadioRecorder.debugLog';
const DEBUG_LOG = ['1', 'true', 'yes', 'on'].includes((new URLSearchParams(window.location.search).get('debug') || localStorage.getItem(DEBUG_LOG_KEY) || '').toLowerCase());

function debugLog(...args) {
  if (!DEBUG_LOG) return;
  console.log('[debug]', ...args);
}

const BACKEND_BASE_URI_KEY = 'nhkRadioRecorder.backendBaseUri';
const DEFAULT_TAB = 'recordings';


function setYoutubeAccountStatus(message) {
  const statusEl = document.querySelector('#youtubeAccountStatus');
  if (!statusEl) return;
  statusEl.textContent = message;
}

function setBulkUploadStatus(message) {
  const statusEl = document.querySelector('#bulkUploadStatus');
  if (!statusEl) return;
  statusEl.textContent = message;
}

function parseYoutubeHeadersInput(rawText) {
  const raw = String(rawText || '').trim();
  if (!raw) throw new Error('HTTP headers JSON is required.');
  let payload;
  try {
    payload = JSON.parse(raw);
  } catch (err) {
    throw new Error('HTTP headers must be valid JSON.');
  }
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    throw new Error('HTTP headers must be a JSON object.');
  }
  return payload;
}

async function loadYoutubeAccounts() {
  const rows = await (await api('/youtube-accounts')).json();
  const ul = document.querySelector('#youtubeAccountList');
  const select = document.querySelector('#bulkUploadYoutubeAccount');
  if (!ul) return;
  ul.innerHTML = '';
  if (select) select.innerHTML = '';
  if (!rows.length) {
    const empty = document.createElement('li');
    empty.className = 'small';
    empty.textContent = 'No YouTube accounts are configured.';
    ul.appendChild(empty);
    if (select) {
      const option = document.createElement('option');
      option.value = '';
      option.textContent = 'No YouTube accounts';
      select.appendChild(option);
    }
    return;
  }

  if (select) {
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = 'Choose YouTube account';
    select.appendChild(placeholder);
  }

  rows.forEach((row) => {
    const li = document.createElement('li');
    li.innerHTML = `<b>${escapeHtml(row.alias || 'Untitled account')}</b>
      <span class="small">${escapeHtml(row.id || '')}</span>
      <div class="small">Created: ${escapeHtml(fmt(row.created_at) || row.created_at || '-')}</div>
      <div class="actions">
        <button data-yid="${escapeHtml(row.id || '')}" class="delete-youtube-account">Delete</button>
      </div>`;
    ul.appendChild(li);
    if (select) {
      const option = document.createElement('option');
      option.value = row.id || '';
      option.textContent = `${row.alias || 'Untitled account'} (${row.id || ''})`;
      select.appendChild(option);
    }
  });
}

async function addYoutubeAccount() {
  const aliasInput = document.querySelector('#youtubeAlias');
  const headersInput = document.querySelector('#youtubeHeaders');
  if (!aliasInput || !headersInput) return;

  const alias = String(aliasInput.value || '').trim();
  if (!alias) {
    setYoutubeAccountStatus('Alias is required.');
    return;
  }

  try {
    const headers = parseYoutubeHeadersInput(headersInput.value);
    await api('/youtube-accounts', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ alias, headers })
    });
    aliasInput.value = '';
    headersInput.value = '';
    setYoutubeAccountStatus(`Added account: ${alias}`);
    await loadYoutubeAccounts();
  } catch (err) {
    setYoutubeAccountStatus(err.message || String(err));
  }
}

function activateTab(tabName) {
  document.querySelectorAll('.tab-button').forEach((button) => {
    const isActive = button.dataset.tab === tabName;
    button.classList.toggle('active', isActive);
    button.setAttribute('aria-selected', String(isActive));
  });

  document.querySelectorAll('.tab-panel').forEach((panel) => {
    panel.hidden = panel.dataset.panel !== tabName;
  });
}

function initTabs() {
  const tabButtons = document.querySelectorAll('.tab-button');
  if (!tabButtons.length) return;
  tabButtons.forEach((button) => {
    button.onclick = () => activateTab(button.dataset.tab);
  });
  activateTab(DEFAULT_TAB);
}

function normalizeBackendBaseUri(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  return raw.replace(/\/$/, '');
}

function getBackendBaseUri() {
  return normalizeBackendBaseUri(localStorage.getItem(BACKEND_BASE_URI_KEY) || '');
}

function toApiUrl(url) {
  const base = getBackendBaseUri();
  if (!base) return url;
  const path = String(url || '');
  if (/^https?:\/\//.test(path)) return path;
  return `${base}${path.startsWith('/') ? path : `/${path}`}`;
}

function setBackendBaseUriStatus(message) {
  const statusEl = document.querySelector('#backendBaseUriStatus');
  if (!statusEl) return;
  statusEl.textContent = message;
}

function initBackendBaseUriSettings() {
  const input = document.querySelector('#backendBaseUri');
  const saveButton = document.querySelector('#saveBackendBaseUri');
  if (!input || !saveButton) return;

  const storedBaseUri = getBackendBaseUri();
  input.value = storedBaseUri;
  setBackendBaseUriStatus(storedBaseUri ? `Using backend: ${storedBaseUri}` : 'Using same-origin backend.');

  saveButton.onclick = () => {
    const normalized = normalizeBackendBaseUri(input.value);
    if (normalized && !/^https?:\/\//.test(normalized)) {
      setBackendBaseUriStatus('Backend URI must start with http:// or https://');
      return;
    }
    localStorage.setItem(BACKEND_BASE_URI_KEY, normalized);
    setBackendBaseUriStatus(normalized ? `Saved backend URI: ${normalized}` : 'Cleared backend URI. Using same-origin backend.');
  };
}

async function api(url, opts) {
  debugLog('api request', { url, opts });
  const apiUrl = toApiUrl(url);
  const res = await fetch(apiUrl, opts);
  debugLog('api response', { url, apiUrl, status: res.status, ok: res.ok });
  if (!res.ok) throw new Error(await res.text());
  return res;
}

function fmt(v) {
  return v ? new Date(v).toLocaleString() : "";
}

function fmtDuration(v) {
  if (!v || typeof v !== 'string' || !v.startsWith('PT')) return v || '';
  const m = v.match(/^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$/);
  if (!m) return v;
  const parts = [];
  if (m[1]) parts.push(`${Number(m[1])}h`);
  if (m[2]) parts.push(`${Number(m[2])}m`);
  if (m[3]) parts.push(`${Number(m[3])}s`);
  return parts.join(' ') || v;
}

function escapeHtml(text) {
  const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  return String(text ?? '').replace(/[&<>"']/g, (ch) => map[ch]);
}

function linkRow(label, url) {
  if (!url) return '';
  const safeUrl = escapeHtml(url);
  return `<div class="small"><b>${escapeHtml(label)}:</b> <a href="${safeUrl}" target="_blank" rel="noopener noreferrer">${safeUrl}</a></div>`;
}

function renderReservationMetadata(meta) {
  if (!meta || typeof meta !== 'object') return '';
  const rows = [
    ['Series ID', meta.series_id],
    ['Series Code', meta.series_code],
    ['Broadcast Event ID', meta.broadcast_event_id],
    ['Radio Episode ID', meta.radio_episode_id]
  ].filter(([, value]) => value);
  const plainRows = rows.map(([label, value]) => `<div class="small"><b>${escapeHtml(label)}:</b> ${escapeHtml(value)}</div>`).join('');
  return `
    ${plainRows}
    ${linkRow('Program URL', meta.program_url)}
  `;
}

function renderSeriesWatchMetadata(payload) {
  const meta = payload?.metadata;
  if (!meta || typeof meta !== 'object') return '';
  const rows = [
    ['Series ID', meta.series_id],
    ['Series Code', meta.series_code],
    ['Series Title', meta.series_title],
    ['Area', meta.series_area],
    ['Schedule', meta.series_schedule]
  ].filter(([, value]) => value);
  const plainRows = rows.map(([label, value]) => `<div class="small"><b>${escapeHtml(label)}:</b> ${escapeHtml(value)}</div>`).join('');
  return `
    ${plainRows}
    ${linkRow('Program URL', meta.program_url)}
  `;
}

async function loadSeries() {
  const list = await (await api('/series')).json();
  debugLog('loadSeries raw count', list.length);
  seriesCache = list.sort((a, b) => (a.areaName || '').localeCompare(b.areaName || '') || a.title.localeCompare(b.title));
  debugLog('loadSeries sorted count', seriesCache.length);
  renderSeries();
}

function renderSeries() {
  const keyword = document.querySelector('#keyword').value.toLowerCase();
  const broadcast = document.querySelector('#broadcastFilter').value;
  const ul = document.querySelector('#seriesList');
  ul.innerHTML = '';
  debugLog('renderSeries filters', { keyword, broadcast, total: seriesCache.length });
  let rendered = 0;
  seriesCache
    .filter(s => (!keyword || s.title.toLowerCase().includes(keyword)) && (!broadcast || s.broadcasts.includes(broadcast)))
    .forEach(s => {
      const li = document.createElement('li');
      li.innerHTML = `<b>${s.title}</b> <span class="small">[${(s.areaName || 'N/A')} / ${(s.broadcasts || []).join(',')}]</span>
        <div class="small">${s.scheduleText || ''}</div>
        <div class="actions">
          <button data-sid="${s.id}" data-scode="${s.seriesCode || ''}" data-surl="${escapeHtml(s.url || '')}" class="show-events">Show events</button>
          <button data-sid="${s.id}" data-scode="${s.seriesCode || ''}" data-surl="${escapeHtml(s.url || '')}" class="watch-series">Watch series</button>
        </div>`;
      ul.appendChild(li);
      rendered += 1;
    });
  debugLog('renderSeries rendered', rendered);
}


async function resolveSeriesCode(seriesCode, seriesUrl) {
  if (seriesCode) return seriesCode;
  if (!seriesUrl) return null;
  if (seriesCodeByUrl.has(seriesUrl)) return seriesCodeByUrl.get(seriesUrl);
  const key = encodeURIComponent(seriesUrl);
  const payload = await (await api(`/series/resolve?series_url=${key}`)).json();
  const resolved = payload?.seriesCode || null;
  seriesCodeByUrl.set(seriesUrl, resolved);
  return resolved;
}

async function showEvents(seriesId, seriesCode, seriesUrl) {
  const resolvedSeriesCode = await resolveSeriesCode(seriesCode, seriesUrl);
  const seriesInfo = seriesCache.find((s) => String(s.id) === String(seriesId));
  selectedSeries = {
    seriesId,
    seriesCode: resolvedSeriesCode,
    seriesTitle: seriesInfo?.title || null
  };
  document.querySelector('#eventTarget').textContent = `Series: ${resolvedSeriesCode || seriesId}`;
  debugLog('showEvents start', { seriesId, seriesCode, seriesUrl, resolvedSeriesCode });
  const key = encodeURIComponent(resolvedSeriesCode || String(seriesId));
  const events = await (await api(`/events?series_code=${key}`)).json();
  debugLog('showEvents events count', events.length, events.slice(0, 3));
  const ul = document.querySelector('#eventsList');
  ul.innerHTML = '';
  for (const ev of events) {
    const li = document.createElement('li');
    li.innerHTML = `<b>${escapeHtml(ev.name)}</b>
      <div class="small">${fmt(ev.startDate)} - ${fmt(ev.endDate)} / ${escapeHtml(ev.serviceDisplayName || ev.serviceName || ev.serviceId || 'N/A')} / area:${escapeHtml(ev.areaId || '-')}</div>
      <div class="small">Duration: ${escapeHtml(fmtDuration(ev.duration) || '-')} / Location: ${escapeHtml(ev.location || '-')}</div>
      <div class="small">Series ID: ${escapeHtml(ev.radioSeriesId || '-')} / Episode ID: ${escapeHtml(ev.radioEpisodeId || '-')}</div>
      ${ev.genres?.length ? `<div class="small">Genres: ${escapeHtml(ev.genres.join(', '))}</div>` : ''}
      ${ev.description ? `<div class="small">${escapeHtml(ev.description)}</div>` : ''}
      ${linkRow('Program URL', ev.episodeUrl || ev.seriesUrl)}`;
    const actions = document.createElement('div');
    actions.className = 'actions';
    const btn = document.createElement('button');
    btn.textContent = 'Reserve this event';
    btn.onclick = () => reserveEvent(ev);
    actions.appendChild(btn);
    li.appendChild(actions);
    ul.appendChild(li);
  }
}

async function reserveEvent(event) {
  debugLog('reserveEvent', { selectedSeries, event });
  await api('/reservation/single-event', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      series_id: selectedSeries.seriesId,
      series_code: selectedSeries.seriesCode || null,
      series_title: selectedSeries.seriesTitle || null,
      event
    })
  });
  await loadReservations();
}

async function reserveSeries(seriesId, seriesCode, seriesUrl) {
  const seriesInfo = seriesCache.find((s) => String(s.id) === String(seriesId));
  const resolvedSeriesCode = await resolveSeriesCode(seriesCode, seriesUrl || seriesInfo?.url);
  const areaId = '';
  debugLog('reserveSeries', { seriesId, seriesCode, seriesUrl, resolvedSeriesCode, areaId, seriesInfo });
  await api('/reservation/watch-series', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      series_id: Number(seriesId),
      series_code: resolvedSeriesCode || null,
      series_title: seriesInfo?.title || null,
      series_area: seriesInfo?.areaName || null,
      series_schedule: seriesInfo?.scheduleText || null,
      program_url: seriesInfo?.url || null,
      series_thumbnail_url: seriesInfo?.thumbnailUrl || null,
      area_id: areaId || null,
      seen_broadcast_event_ids: []
    })
  });
  await loadReservations();
}

async function loadReservations() {
  const rows = await (await api('/reservations')).json();
  debugLog('loadReservations count', rows.length);
  const seriesWatchList = document.querySelector('#seriesWatchReservationList');
  const singleEventList = document.querySelector('#singleEventReservationList');
  seriesWatchList.innerHTML = '';
  singleEventList.innerHTML = '';

  const groupedByType = {
    series_watch: new Map(),
    single_event: new Map()
  };

  rows.forEach((row) => {
    const type = row.type === 'series_watch' ? 'series_watch' : 'single_event';
    const group = buildReservationGroup(row);
    if (!groupedByType[type].has(group.key)) groupedByType[type].set(group.key, { title: group.title, rows: [] });
    groupedByType[type].get(group.key).rows.push(row);
  });

  renderReservationGroups(groupedByType.series_watch, seriesWatchList);
  renderReservationGroups(groupedByType.single_event, singleEventList);
}


function renderReservationGroups(groups, ul) {
  if (!groups.size) {
    const empty = document.createElement('li');
    empty.className = 'small';
    empty.textContent = 'No reservations.';
    ul.appendChild(empty);
    return;
  }

  [...groups.entries()].forEach(([groupKey, group]) => {
    const container = document.createElement('li');
    container.className = 'reservation-group';
    const isExpanded = expandedReservationGroups.has(groupKey);
    const visibleRows = isExpanded ? group.rows : group.rows.slice(0, 1);
    const moreCount = Math.max(0, group.rows.length - visibleRows.length);
    container.innerHTML = `<div class="reservation-group-header">
      <div>
        <b>${escapeHtml(group.title)}</b>
        <span class="small">(${group.rows.length} item${group.rows.length > 1 ? 's' : ''})</span>
        ${!isExpanded && moreCount > 0 ? `<div class="small">+${moreCount} more in this series</div>` : ''}
      </div>
      ${group.rows.length > 1 ? `<button class="toggle-reservation-group" data-group="${escapeHtml(groupKey)}">${isExpanded ? 'Collapse' : 'Expand'}</button>` : ''}
    </div>`;

    const itemList = document.createElement('ul');
    itemList.className = 'reservation-items';
    visibleRows.forEach((row) => itemList.appendChild(renderReservationItem(row)));
    container.appendChild(itemList);
    ul.appendChild(container);
  });
}

function buildReservationGroup(row) {
  const metadata = row.payload?.metadata || {};
  const seriesCode = metadata.series_code || row.payload?.series_code || row.payload?.event?.radioSeriesId || '';
  const seriesId = metadata.series_id || row.payload?.series_id || row.payload?.event?.radioSeriesId || '';
  const seriesTitle = metadata.series_title || row.payload?.series_title || row.payload?.event?.seriesTitle || '';
  const identifier = seriesCode || seriesId || row.id;
  return {
    key: `${row.type || 'reservation'}:series:${identifier}`,
    title: seriesTitle || `Series ${identifier}`
  };
}

function renderReservationItem(row) {
  const li = document.createElement('li');
  const event = row.payload?.event || {};
  const reservationLabel = event.name || row.payload?.series_title || row.payload?.metadata?.series_title || '';
  const metadataHtml = row.type === 'series_watch'
    ? renderSeriesWatchMetadata(row.payload)
    : renderReservationMetadata(row.payload?.metadata);
  li.innerHTML = `<b>${row.type}</b> <span class="small">${row.status} / ${row.id}</span>
    ${event.name ? `<div class="small"><b>${escapeHtml(event.name)}</b> (${fmt(event.startDate)} - ${fmt(event.endDate)})</div>` : ''}
    ${metadataHtml || `<div class="small">${escapeHtml(JSON.stringify(row.payload))}</div>`}
    <div class="actions"><button data-rid="${row.id}" data-rlabel="${escapeHtml(reservationLabel)}" class="delete-reservation">Delete</button></div>`;
  return li;
}


function getRecordingDisplayStatus(recording) {
  const status = String(recording?.status || '').toLowerCase();
  if (status === 'failed') return { label: 'Failed', className: 'status-failed' };
  if (['pending', 'scheduled', 'recording', 'processing', 'in_progress'].includes(status)) {
    return { label: 'In Progress', className: 'status-in-progress' };
  }
  if (status === 'ready' || status === 'done') return { label: 'Ready', className: 'status-ready' };
  return { label: status || 'Unknown', className: 'status-unknown' };
}

async function loadRecordings() {
  const rows = await (await api('/recordings')).json();
  debugLog('loadRecordings count', rows.length);
  const ul = document.querySelector('#recordingList');
  ul.innerHTML = '';
  recordingById.clear();
  rows.forEach(r => {
    recordingById.set(String(r.id), r);
    const li = document.createElement('li');
    const displayStatus = getRecordingDisplayStatus(r);
    const isReady = String(r?.status || '').toLowerCase() === 'ready';
    li.innerHTML = `<label><input type="checkbox" class="bulk" value="${r.id}"/> </label>
      <b>${r.title}</b>
      <span class="recording-status-pill ${displayStatus.className}">${escapeHtml(displayStatus.label)}</span>
      <span class="small">${fmt(r.start_date)} / ${r.id}</span>
      <div class="small">${JSON.stringify(r.metadata)}</div>
      <div class="actions">
        <button data-rec="${r.id}" class="play" ${isReady ? '' : 'disabled'}>Play</button>
        ${isReady ? `<a href="${toApiUrl(`/recordings/${r.id}/download`)}"><button>Download m4a</button></a>` : ''}
        <button data-rec="${r.id}" class="edit-meta">Edit metadata</button>
        <button data-rec="${r.id}" data-rtitle="${escapeHtml(r.title || '')}" class="delete-recording">Delete</button>
      </div>`;
    ul.appendChild(li);
  });
}

function updateNowPlayingLabel(recording) {
  const label = document.querySelector('#nowPlaying');
  if (!label) return;
  if (!recording) {
    label.textContent = 'Now Playing: Not playing';
    return;
  }

  const metadata = recording?.metadata || {};
  const title = recording?.title || metadata.title || 'Unknown title';
  const serviceName = metadata.service_name || metadata.serviceDisplayName;
  const description = metadata.description || recording?.description;
  const duration = fmtDuration(metadata.duration);
  const rows = [
    `<div class="now-playing-title">Now Playing: ${escapeHtml(title)}</div>`,
    serviceName ? `<div class="small"><b>Service:</b> ${escapeHtml(serviceName)}</div>` : '',
    recording?.start_date ? `<div class="small"><b>Start:</b> ${escapeHtml(fmt(recording.start_date))}</div>` : '',
    duration ? `<div class="small"><b>Duration:</b> ${escapeHtml(duration)}</div>` : '',
    recording?.id ? `<div class="small"><b>Recording ID:</b> ${escapeHtml(recording.id)}</div>` : '',
    description ? `<div class="small"><b>Description:</b> ${escapeHtml(description)}</div>` : ''
  ].filter(Boolean);

  label.innerHTML = rows.join('');
}

function playRecording(id) {
  const player = document.querySelector('#player');
  const recording = recordingById.get(String(id));
  const src = toApiUrl(`/recordings/${id}/recording.m3u8`);
  if (player.canPlayType('application/vnd.apple.mpegurl')) {
    player.src = src;
  } else if (window.Hls && Hls.isSupported()) {
    const hls = new Hls();
    hls.loadSource(src);
    hls.attachMedia(player);
  } else {
    alert('HLS playback is not supported in this browser.');
    return;
  }
  updateNowPlayingLabel(recording);
  player.play();
}


function seekPlayerBy(offsetSeconds) {
  const player = document.querySelector('#player');
  if (!player) return;
  const rawNextTime = Math.max(player.currentTime + offsetSeconds, 0);
  const nextTime = Number.isFinite(player.duration) ? Math.min(rawNextTime, player.duration) : rawNextTime;
  player.currentTime = nextTime;
}

async function editMetadata(id) {
  const title = prompt('Set metadata.title (blank to skip):', '');
  const description = prompt('Set metadata.description (blank to skip):', '');
  const payload = {};
  if (title) payload.title = title;
  if (description) payload.description = description;
  debugLog('editMetadata', { id, payload });
  await api(`/recordings/${id}/metadata`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  await loadRecordings();
}

async function bulkDownload() {
  const ids = [...document.querySelectorAll('.bulk:checked')].map(x => x.value);
  debugLog('bulkDownload ids', ids);
  if (!ids.length) return alert('No recordings selected.');
  const res = await api('/recordings/bulk-download', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids })
  });
  const blob = await res.blob();
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'recordings.zip';
  a.click();
}

async function bulkUploadToYoutube() {
  const ids = [...document.querySelectorAll('.bulk:checked')].map(x => x.value);
  if (!ids.length) return alert('No recordings selected.');

  const select = document.querySelector('#bulkUploadYoutubeAccount');
  const accountId = select ? String(select.value || '').trim() : '';
  if (!accountId) {
    setBulkUploadStatus('Please choose a YouTube account first.');
    return;
  }

  setBulkUploadStatus(`Uploading ${ids.length} recording(s)...`);
  const res = await api('/recordings/bulk-upload', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ids, account_id: accountId })
  });
  const payload = await res.json();
  const results = Array.isArray(payload?.results) ? payload.results : [];
  const successCount = results.filter((row) => row.status === 'uploaded').length;
  const failCount = results.length - successCount;
  setBulkUploadStatus(`Upload finished. success=${successCount}, failed=${failCount}`);
  await loadRecordings();
}

document.querySelector('#loadSeries').onclick = loadSeries;
document.querySelector('#keyword').oninput = renderSeries;
document.querySelector('#broadcastFilter').onchange = renderSeries;
document.querySelector('#refreshReservations').onclick = loadReservations;
document.querySelector('#refreshRecordings').onclick = loadRecordings;
document.querySelector('#bulkDownload').onclick = bulkDownload;
document.querySelector('#bulkUploadToYoutube').onclick = bulkUploadToYoutube;
document.querySelector('#seekBack10').onclick = () => seekPlayerBy(-10);
document.querySelector('#seekForward10').onclick = () => seekPlayerBy(10);
document.querySelector('#addYoutubeAccount').onclick = addYoutubeAccount;

document.addEventListener('click', async (e) => {
  if (e.target.matches('.show-events')) await showEvents(e.target.dataset.sid, e.target.dataset.scode, e.target.dataset.surl);
  if (e.target.matches('.watch-series')) await reserveSeries(e.target.dataset.sid, e.target.dataset.scode, e.target.dataset.surl);
  if (e.target.matches('.delete-reservation')) {
    const reservationLabel = e.target.dataset.rlabel ? `\n\nReservation: ${e.target.dataset.rlabel}` : '';
    if (!window.confirm(`Delete this reservation?${reservationLabel}`)) return;
    await api(`/reservations/${e.target.dataset.rid}`, { method: 'DELETE' });
    await loadReservations();
  }
  if (e.target.matches('.toggle-reservation-group')) {
    const group = e.target.dataset.group;
    if (!group) return;
    if (expandedReservationGroups.has(group)) expandedReservationGroups.delete(group);
    else expandedReservationGroups.add(group);
    await loadReservations();
  }
  if (e.target.matches('.play')) playRecording(e.target.dataset.rec);
  if (e.target.matches('.edit-meta')) await editMetadata(e.target.dataset.rec);
  if (e.target.matches('.delete-youtube-account')) {
    const accountId = e.target.dataset.yid;
    if (!accountId) return;
    if (!window.confirm('Delete this YouTube account?')) return;
    await api(`/youtube-accounts/${accountId}`, { method: 'DELETE' });
    setYoutubeAccountStatus('Deleted YouTube account.');
    await loadYoutubeAccounts();
  }
  if (e.target.matches('.delete-recording')) {
    const recordingTitle = e.target.dataset.rtitle ? `\n\nRecording: ${e.target.dataset.rtitle}` : '';
    if (!window.confirm(`Delete this recording?${recordingTitle}`)) return;
    await api(`/recordings/${e.target.dataset.rec}`, { method: 'DELETE' });
    await loadRecordings();
  }
});

initBackendBaseUriSettings();
initTabs();
loadReservations();
loadRecordings();
loadYoutubeAccounts();
debugLog('frontend debug enabled', { DEBUG_LOG, query: window.location.search });
