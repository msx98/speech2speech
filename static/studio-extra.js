// Health indicator for the unified speech service.
(function () {
  var chip = document.getElementById('studio-status');
  if (!chip) return;
  var health = window.STUDIO_HEALTH || '/health';
  function refresh() {
    fetch(health, { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : Promise.reject(r.status); })
      .then(function () {
        chip.textContent = '● backend';
        chip.className = 'ok';
      })
      .catch(function () {
        chip.textContent = '○ backend offline';
        chip.className = 'bad';
      });
  }
  refresh();
  setInterval(refresh, 10000);
})();
