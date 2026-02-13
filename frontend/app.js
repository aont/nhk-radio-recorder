let seriesCache = [];
let selectedSeries = null;
const DEBUG_LOG = ['1', 'true', 'yes', 'on'].includes((new URLSearchParams(window.location.search).get('debug') || localStorage.getItem('debugLog') || '').toLowerCase());

function debugLog(...args) {
  if (!DEBUG_LOG) return;
  console.log('[debug]', ...args);
}

async function api(url, opts) {
  debugLog('api request', { url, opts });
  const res = await fetch(url, opts);
  debugLog('api response', { url, status: res.status, ok: res.ok });
  if (!res.ok) throw new Error(await res.text());
  return res;
}

function fmt(v) {
  return v ? new Date(v).toLocaleString() : "";
}

async function loadSeries() {
  const list = await (await api('/api/series')).json();
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
          <button data-sid="${s.id}" data-scode="${s.seriesCode || ''}" class="show-events">Show events</button>
          <button data-sid="${s.id}" data-scode="${s.seriesCode || ''}" class="watch-series">Watch series</button>
        </div>`;
      ul.appendChild(li);
      rendered += 1;
    });
  debugLog('renderSeries rendered', rendered);
}

async function showEvents(seriesId, seriesCode) {
  selectedSeries = { seriesId, seriesCode };
  document.querySelector('#eventTarget').textContent = `Series: ${seriesCode || seriesId}`;
  debugLog('showEvents start', { seriesId, seriesCode });
  const key = encodeURIComponent(seriesCode || String(seriesId));
  const events = await (await api(`/api/events?series_code=${key}&to_days=7`)).json();
  debugLog('showEvents events count', events.length, events.slice(0, 3));
  const ul = document.querySelector('#eventsList');
  ul.innerHTML = '';
  for (const ev of events) {
    const li = document.createElement('li');
    li.innerHTML = `<b>${ev.name}</b>
      <div class="small">${fmt(ev.startDate)} - ${fmt(ev.endDate)} / ${ev.serviceId} / area:${ev.areaId}</div>`;
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
  await api('/api/reservations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      type: 'single_event',
      payload: { series_id: selectedSeries.seriesId, series_code: selectedSeries.seriesCode || null, event }
    })
  });
  await loadReservations();
}

async function reserveSeries(seriesId, seriesCode) {
  const areaId = '';
  debugLog('reserveSeries', { seriesId, seriesCode, areaId });
  await api('/api/reservations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      type: 'series_watch',
      payload: {
        series_id: Number(seriesId),
        series_code: seriesCode || null,
        area_id: areaId || null,
        seen_broadcast_event_ids: []
      }
    })
  });
  await loadReservations();
}

async function loadReservations() {
  const rows = await (await api('/api/reservations')).json();
  debugLog('loadReservations count', rows.length);
  const ul = document.querySelector('#reservationList');
  ul.innerHTML = '';
  rows.forEach(r => {
    const li = document.createElement('li');
    li.innerHTML = `<b>${r.type}</b> <span class="small">${r.status} / ${r.id}</span>
      <div class="small">${JSON.stringify(r.payload)}</div>
      <div class="actions"><button data-rid="${r.id}" class="delete-reservation">Delete</button></div>`;
    ul.appendChild(li);
  });
}

async function loadRecordings() {
  const rows = await (await api('/api/recordings')).json();
  debugLog('loadRecordings count', rows.length);
  const ul = document.querySelector('#recordingList');
  ul.innerHTML = '';
  rows.forEach(r => {
    const li = document.createElement('li');
    li.innerHTML = `<label><input type="checkbox" class="bulk" value="${r.id}"/> </label>
      <b>${r.title}</b> <span class="small">${fmt(r.start_date)} / ${r.id}</span>
      <div class="small">${JSON.stringify(r.metadata)}</div>
      <div class="actions">
        <button data-rec="${r.id}" class="play">Play</button>
        <a href="/api/recordings/${r.id}/download"><button>Download m4a</button></a>
        <button data-rec="${r.id}" class="edit-meta">Edit metadata</button>
        <button data-rec="${r.id}" class="delete-recording">Delete</button>
      </div>`;
    ul.appendChild(li);
  });
}

function playRecording(id) {
  const player = document.querySelector('#player');
  const src = `/recordings/${id}/recording.m3u8`;
  if (player.canPlayType('application/vnd.apple.mpegurl')) {
    player.src = src;
  } else if (window.Hls && Hls.isSupported()) {
    const hls = new Hls();
    hls.loadSource(src);
    hls.attachMedia(player);
  } else {
    alert('HLS playback is not supported in this browser.');
  }
  player.play();
}

async function editMetadata(id) {
  const title = prompt('Set metadata.title (blank to skip):', '');
  const description = prompt('Set metadata.description (blank to skip):', '');
  const payload = {};
  if (title) payload.title = title;
  if (description) payload.description = description;
  debugLog('editMetadata', { id, payload });
  await api(`/api/recordings/${id}/metadata`, {
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
  const res = await api('/api/recordings/bulk-download', {
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

document.querySelector('#loadSeries').onclick = loadSeries;
document.querySelector('#keyword').oninput = renderSeries;
document.querySelector('#broadcastFilter').onchange = renderSeries;
document.querySelector('#refreshReservations').onclick = loadReservations;
document.querySelector('#refreshRecordings').onclick = loadRecordings;
document.querySelector('#bulkDownload').onclick = bulkDownload;

document.addEventListener('click', async (e) => {
  if (e.target.matches('.show-events')) await showEvents(e.target.dataset.sid, e.target.dataset.scode);
  if (e.target.matches('.watch-series')) await reserveSeries(e.target.dataset.sid, e.target.dataset.scode);
  if (e.target.matches('.delete-reservation')) {
    await api(`/api/reservations/${e.target.dataset.rid}`, { method: 'DELETE' });
    await loadReservations();
  }
  if (e.target.matches('.play')) playRecording(e.target.dataset.rec);
  if (e.target.matches('.edit-meta')) await editMetadata(e.target.dataset.rec);
  if (e.target.matches('.delete-recording')) {
    await api(`/api/recordings/${e.target.dataset.rec}`, { method: 'DELETE' });
    await loadRecordings();
  }
});

loadReservations();
loadRecordings();
debugLog('frontend debug enabled', { DEBUG_LOG, query: window.location.search });
