// Use the browser's monotonic clock for every interval; no cross-host clock math.
(() => {
  const summary = document.getElementById('call-stats-averages');
  const chart = document.getElementById('call-stats-chart');
  const colors = { text: '#6ea8ff', decoded: '#ffb454', ready: '#68d391' };
  let rows, revisions, responses, pendingResponses, batchNumber;
  const key = (chat, revision) => JSON.stringify([chat, revision]);
  function reset() {
    rows = []; revisions = new Map(); responses = new Map(); pendingResponses = new Map(); batchNumber = 0;
    render();
  }
  function render() {
    const average = field => {
      const samples = rows.filter(row => Number.isFinite(row[field]));
      return samples.length ? Math.round(samples.reduce((sum, row) => sum + row[field], 0) / samples.length) + ' ms (n=' + samples.length + ')' : '—';
    };
    summary.textContent = rows.length
      ? `Average batch → text chunk: ${average('text')} · decoded audio: ${average('decoded')} · browser-ready audio: ${average('ready')} · discarded: ${rows.filter(row => row.discarded).length}`
      : 'No completed batch measurements yet.';
    const values = rows.flatMap(row => Object.keys(colors).map(field => row[field])).filter(Number.isFinite);
    const maximum = Math.max(100, ...values) * 1.1;
    const first = rows[0]?.number ?? 1, last = Math.max(first + 1, rows.at(-1)?.number ?? 1);
    const x = number => 48 + (number - first) / (last - first) * 574;
    const y = delay => 162 - delay / maximum * 140;
    let svg = '<g fill="currentColor" font-size="11"><text x="4" y="13">Delay (ms)</text><text x="280" y="195">Prefill batch number</text></g>';
    for (let tick = 0; tick <= 4; tick++) {
      const value = maximum * tick / 4, at = y(value);
      svg += `<path d="M48 ${at} H622" stroke="currentColor" opacity=".15"/><text x="42" y="${at + 4}" text-anchor="end" fill="currentColor" font-size="10">${Math.round(value)}</text>`;
    }
    for (const number of new Set([first, Math.round((first + last) / 2), last]))
      svg += `<text x="${x(number)}" y="179" text-anchor="middle" fill="currentColor" font-size="10">${number}</text>`;
    for (const [field, color] of Object.entries(colors)) {
      // Do not connect across missing/cancelled measurements.
      for (const row of rows) if (Number.isFinite(row[field])) {
        const label = `Batch ${row.number}: ${field} ${Math.round(row[field])} ms${row.discarded ? ' (discarded)' : ''}`;
        svg += `<circle cx="${x(row.number)}" cy="${y(row[field])}" r="3" fill="${color}"><title>${label}</title></circle>`;
      }
    }
    for (const row of rows) if (row.discarded) {
      const at = x(row.number);
      svg += `<path d="M${at - 3} 155 l6 6 m-6 0 l6 -6" stroke="#aaa"><title>Batch ${row.number}: discarded</title></path>`;
    }
    chart.innerHTML = svg;
  }
  function event(message, at) {
    if (message.type === 'llm.input_prefill') {
      if (message.batch_tokens > 0) batchNumber++;
      if (!message.finished) return;
      const revision = key(message.chat_id, message.revision);
      if (revisions.has(revision)) return; // Rollback acknowledgements are not new batches.
      const row = { number: batchNumber, ended: at, revision };
      rows.push(row); revisions.set(revision, row);
      if (pendingResponses.has(revision)) {
        row.responseId = pendingResponses.get(revision);
        responses.set(row.responseId, row);
        pendingResponses.delete(revision);
      }
      if (rows.length > 200) {
        const old = rows.shift(); revisions.delete(old.revision);
        if (old.responseId) responses.delete(old.responseId);
      }
    } else if (message.type === 'response.created') {
      if (message.input_revision == null || !message.llm_chat_id) return;
      const revision = key(message.llm_chat_id, message.input_revision);
      const row = revisions.get(revision);
      if (!row) {
        if (!pendingResponses.has(revision)) pendingResponses.set(revision, message.response.id);
        if (pendingResponses.size > 200) pendingResponses.delete(pendingResponses.keys().next().value);
        return;
      }
      if (row.responseId) return; // Measure the first candidate for a batch.
      row.responseId = message.response.id;
      responses.set(row.responseId, row);
    } else {
      const row = responses.get(message.response_id);
      if (!row) return;
      if (message.type === 'response.chunk_ready') row.text ??= at - row.ended;
      else if (message.type === 'response.audio_decoded') row.decoded ??= at - row.ended;
      else if (message.type === 'response.discarded') row.discarded = true;
      else return;
    }
    render();
  }
  function audioReady(message, at) {
    const row = responses.get(message.response_id);
    if (!row || row.ready !== undefined) return;
    row.ready = at - row.ended;
    render();
  }
  window.callStatistics = { reset, event, audioReady };
  reset();
})();
