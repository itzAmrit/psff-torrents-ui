(() => {
  const byId = (id) => document.getElementById(id);
  const formatBytes = (value) => {
    const bytes = Math.max(0, Number(value || 0));
    if (!bytes) return '0 B';
    const units = ['B', 'KiB', 'MiB', 'GiB', 'TiB'];
    const index = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
    const scaled = bytes / (1024 ** index);
    return `${scaled >= 10 || index === 0 ? scaled.toFixed(0) : scaled.toFixed(1)} ${units[index]}`;
  };
  const showError = (message) => {
    window.__TSFF_BOOT_ERROR__ = String(message || 'Unknown dashboard startup error');
    const element = byId('restoreError');
    if (element) {
      element.textContent = window.__TSFF_BOOT_ERROR__;
      element.classList.remove('hidden');
    }
  };
  window.addEventListener('error', (event) => showError(event.message));
  window.addEventListener('unhandledrejection', (event) => showError(event.reason?.message || event.reason));

  const poll = async () => {
    try {
      const response = await fetch('/api/gcs-sync/status', {
        cache: 'no-store',
        headers: { Accept: 'application/json' }
      });
      if (!response.ok) throw new Error(`Restore status HTTP ${response.status}`);
      const payload = await response.json();
      window.__TSFF_RESTORE_PAYLOAD__ = payload;
      const restore = payload?.restore || {};
      const progress = payload?.runtime?.progress || {};
      const ready = Boolean(restore.ready || restore.data_ready);
      const filesDone = Number(progress.files_done || 0);
      const filesTotal = Number(progress.files_total || 0);
      const bytesDone = Number(progress.bytes_done || 0);
      const bytesTotal = Number(progress.bytes_total || 0);
      const percent = bytesTotal > 0
        ? Math.max(0, Math.min(100, (bytesDone / bytesTotal) * 100))
        : (filesTotal > 0 ? Math.max(0, Math.min(100, (filesDone / filesTotal) * 100)) : 0);
      const phase = String(restore.phase || progress.phase || 'waiting');
      const titles = {
        waiting: 'Waiting for restore worker', restoring: 'Restoring seedbox from GCS',
        planning: 'Scanning bucket and VM', scanning: 'Comparing bucket and VM',
        pull: 'Copying GCS data to this VM', transfer: 'Copying GCS data to this VM',
        checksum: 'Checking file integrity', verifying: 'Verifying restored files',
        error: 'Restore needs attention'
      };
      byId('restoreTitle').textContent = titles[phase] || `Restore: ${phase.replaceAll('-', ' ')}`;
      byId('restoreProgressFill').style.width = `${percent}%`;
      byId('restorePercent').textContent = `${percent.toFixed(1)}%`;
      byId('restoreBytes').textContent = `${formatBytes(bytesDone)} / ${formatBytes(bytesTotal)}`;
      byId('restoreFiles').textContent = `${filesDone} / ${filesTotal}`;
      byId('restorePath').textContent = progress.current_path ? `Current: ${progress.current_path}` : `Stage: ${phase.replaceAll('-', ' ')}`;
      const backendError = String(restore.last_error || payload?.runtime?.last_error || '').trim();
      if (backendError) showError(backendError);
      if (ready) {
        byId('restoreGate').classList.add('hidden');
        byId('mainUi').removeAttribute('inert');
        return;
      }
    } catch (error) {
      showError(`Unable to check restore status: ${error.message}`);
    }
    window.setTimeout(poll, 2000);
  };
  poll();
})();
