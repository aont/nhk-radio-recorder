let seriesCache = [];
let selectedSeries = null;

async function api(url, opts) {
  const res = await fetch(url, opts);
  if (!res.ok) throw new Error(await res.text());
  return res;
}

function fmt(v) {
  return v ? new Date(v).toLocaleString() : "";
}

async function loadSeries() {
  const list = await (await api('/api/series')).json();
  seriesCache = list.sort((a, b) => (a.areaName || '').localeCompare(b.areaName || '') || a.title.localeCompare(b.title));
  renderSeries();
}

function renderSeries() {
  const keyword = document.querySelector('#keyword').value.toLowerCase();
  const area = document.querySelector('#areaFilter').value.toLowerCase();
  const broadcast = document.querySelector('#broadcastFilter').value;
  const ul = document.querySelector('#seriesList');
  ul.innerHTML = '';
  seriesCache
    .filter(s => (!keyword || s.title.toLowerCase().includes(keyword)) && (!area || (s.areaName || '').toLowerCase().includes(area)) && (!broadcast || s.broadcasts.includes(broadcast)))
    .forEach(s => {
      const li = document.createElement('li');
      li.innerHTML = `<b>${s.title}</b> <span class="small">[${(s.areaName || 'N/A')} / ${(s.broadcasts || []).join(',')}]</span>
        <div class="small">${s.scheduleText || ''}</div>
        <div class="actions">
          <button data-sid="${s.id}" class="show-events">Show events</button>
          <button data-sid="${s.id}" class="watch-series">Watch series</button>
        </div>`;
      ul.appendChild(li);
    });
}

async function showEvents(seriesId) {
  selectedSeries = seriesId;
  document.querySelector('#eventTarget').textContent = `Series ID: ${seriesId}`;
  const events = await (await api(`/api/events?series_id=${seriesId}`)).json();
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
  await api('/api/reservations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: 'single_event', payload: { series_id: selectedSeries, event } })
  });
  await loadReservations();
}

async function reserveSeries(seriesId) {
  const areaId = prompt('Optional area_id filter for watcher (blank for all):', '') || '';
  await api('/api/reservations', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: 'series_watch', payload: { series_id: Number(seriesId), area_id: areaId || null, seen_broadcast_event_ids: [] } })
  });
  await loadReservations();
}

async function loadReservations() {
  const rows = await (await api('/api/reservations')).json();
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
  const video = document.querySelector('#player');
  const src = `/recordings/${id}/recording.m3u8`;
  if (video.canPlayType('application/vnd.apple.mpegurl')) {
    video.src = src;
  } else if (window.Hls && Hls.isSupported()) {
    const hls = new Hls();
    hls.loadSource(src);
    hls.attachMedia(video);
  } else {
    alert('HLS playback is not supported in this browser.');
  }
  video.play();
}

async function editMetadata(id) {
  const title = prompt('Set metadata.title (blank to skip):', '');
  const description = prompt('Set metadata.description (blank to skip):', '');
  const payload = {};
  if (title) payload.title = title;
  if (description) payload.description = description;
  await api(`/api/recordings/${id}/metadata`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  await loadRecordings();
}

async function bulkDownload() {
  const ids = [...document.querySelectorAll('.bulk:checked')].map(x => x.value);
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
document.querySelector('#areaFilter').oninput = renderSeries;
document.querySelector('#broadcastFilter').onchange = renderSeries;
document.querySelector('#refreshReservations').onclick = loadReservations;
document.querySelector('#refreshRecordings').onclick = loadRecordings;
document.querySelector('#bulkDownload').onclick = bulkDownload;

document.addEventListener('click', async (e) => {
  if (e.target.matches('.show-events')) await showEvents(e.target.dataset.sid);
  if (e.target.matches('.watch-series')) await reserveSeries(e.target.dataset.sid);
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
