'use strict';
const byId = id => document.getElementById(id);
let selectedFolder = null;
let parentFolder = null;
let scanning = false;
let loadingFolder = false;
let currentScan = null;
let copyEnabled = false;
let moveEnabled = false;
let invalidatedMove = null;
let copying = false;
let copySubmitting = false;
let pollTimer = null;
let lastCopyJob = null;
let dismissedCopyJobId = null;
const token = document.querySelector('meta[name="scan-token"]').content;
const node = (tag, text, className) => {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
};
function formatTime(value) {
  // Keep the camera's local clock; do not convert to the browser's timezone.
  return value.replace(/(?:Z|[+-]\d{2}:\d{2})$/, '').replace('T', ' ')
    .replace(/\.(\d+)$/, (_, fraction) => `.${fraction.replace(/0+$/, '').padEnd(3, '0')}`);
}
function formatGap(value) {
  return value === null || value === undefined ? '—' : `${Number(value.toFixed(6))} 秒`;
}
function status(message, error = false) {
  byId('status').textContent = message;
  byId('status').className = error ? 'error' : '';
}
async function requestJSON(url, options) {
  const response = await fetch(url, options);
  let data;
  try { data = await response.json(); }
  catch { throw new Error('応答を読み取れません。接続を確認してください。'); }
  if (!response.ok) throw new Error(data.error || '処理に失敗しました。');
  return data;
}
function updateButton() {
  byId('scan-button').disabled = selectedFolder === null || scanning || loadingFolder;
  updateCopyButtons();
}
async function openFolder(path) {
  if (loadingFolder || scanning) return;
  loadingFolder = true;
  selectedFolder = null;
  updateButton();
  byId('results').hidden = true;
  currentScan = null;
  updateCopyButtons();
  status('フォルダを読み込んでいます…');
  try {
    const data = await requestJSON('/api/folders?path=' + encodeURIComponent(path));
    selectedFolder = data.path;
    parentFolder = data.parent;
    byId('folder-path').value = data.path;
    byId('current-folder').textContent = data.path || '写真ルート';
    byId('parent-folder').hidden = parentFolder === null;
    const list = byId('folders');
    list.replaceChildren();
    for (const folder of data.folders) {
      const button = node('button', folder.name, 'folder-button');
      button.type = 'button';
      button.addEventListener('click', () => openFolder(folder.path));
      list.append(button);
    }
    if (!data.folders.length) list.append(node('p', 'サブフォルダはありません。', 'hint'));
    status('フォルダを選択しました。条件を確認して解析してください。');
  } catch (error) {
    byId('folders').replaceChildren();
    status(error.message, true);
  } finally { loadingFolder = false; updateButton(); }
}
const imageObserver = 'IntersectionObserver' in window ? new IntersectionObserver(entries => {
  for (const entry of entries) {
    if (entry.isIntersecting) {
      const image = entry.target;
      image.src = image.dataset.src;
      delete image.dataset.src;
      imageObserver.unobserve(image);
    }
  }
}, {rootMargin: '160px'}) : null;
let previewFiles = [];
let previewIndex = 0;
let lastPreviewFocus = null;

function closePreview() {
  const dialog = byId('preview-dialog');
  if (dialog.open && typeof dialog.close === 'function') dialog.close();
  else dialog.removeAttribute('open');
  byId('preview-image').removeAttribute('src');
  if (lastPreviewFocus && lastPreviewFocus.isConnected) lastPreviewFocus.focus();
}
function openPreview(files, index) {
  const dialog = byId('preview-dialog');
  if (!dialog.open) lastPreviewFocus = document.activeElement;
  previewFiles = files;
  previewIndex = index;
  const file = files[index];
  byId('preview-title').textContent = file.jpg;
  byId('preview-time').textContent = formatTime(file.time);
  byId('preview-position').textContent = `${index + 1} / ${files.length}`;
  byId('previous-preview').disabled = index === 0;
  byId('next-preview').disabled = index + 1 === files.length;
  const image = byId('preview-image');
  image.hidden = true;
  image.alt = file.jpg;
  byId('preview-status').textContent = 'プレビューを読み込んでいます…';
  image.onload = () => { image.hidden = false; byId('preview-status').textContent = ''; };
  image.onerror = () => { image.hidden = true; byId('preview-status').textContent = 'プレビューを表示できません。写真が変更された場合は再解析してください。'; };
  image.src = file.preview_url;
  if (!dialog.open) {
    if (typeof dialog.showModal === 'function') dialog.showModal();
    else dialog.setAttribute('open', '');
  }
}
function photoTile(file, label, files, index, deferred = false) {
  const figure = node('figure', undefined, 'photo-tile');
  figure.append(node('span', label, 'photo-label'));
  if (!file) {
    figure.append(node('p', '写真なし', 'empty-photo'));
    return figure;
  }
  const button = node('button', undefined, 'photo-button');
  button.type = 'button';
  button.setAttribute('aria-label', `${label}：${file.jpg} を拡大`);
  const image = node('img');
  image.alt = file.jpg;
  image.width = 360;
  image.height = 270;
  image.decoding = 'async';
  image.loading = 'lazy';
  const error = node('span', '画像を表示できません', 'image-error');
  error.hidden = true;
  image.addEventListener('error', () => { image.hidden = true; error.hidden = false; });
  if (deferred) image.dataset.src = file.thumbnail_url;
  else if (imageObserver) { image.dataset.src = file.thumbnail_url; imageObserver.observe(image); }
  else image.src = file.thumbnail_url;
  button.append(image, error);
  button.addEventListener('click', () => openPreview(files, index));
  figure.append(button, node('figcaption', file.jpg), node('time', formatTime(file.time), 'photo-time'));
  if (file.gap !== undefined) figure.append(node('span', `セット端との間隔 ${file.gap.toFixed(2)} 秒`, 'photo-gap'));
  return figure;
}
byId('close-preview').addEventListener('click', closePreview);
byId('preview-dialog').addEventListener('click', event => { if (event.target === byId('preview-dialog')) closePreview(); });
byId('preview-dialog').addEventListener('cancel', event => { event.preventDefault(); closePreview(); });
byId('previous-preview').addEventListener('click', () => openPreview(previewFiles, previewIndex - 1));
byId('next-preview').addEventListener('click', () => openPreview(previewFiles, previewIndex + 1));
byId('preview-dialog').addEventListener('keydown', event => {
  if (event.key === 'ArrowLeft' && previewIndex > 0) { event.preventDefault(); openPreview(previewFiles, previewIndex - 1); }
  if (event.key === 'ArrowRight' && previewIndex + 1 < previewFiles.length) { event.preventDefault(); openPreview(previewFiles, previewIndex + 1); }
});

function renderResults(data) {
  currentScan = data;
  if (imageObserver) imageObserver.disconnect();
  closePreview();
  byId('results').hidden = false;
  byId('summary').replaceChildren();
  for (const [value, label] of [[data.jpg_count, 'JPG 枚数'], [data.dated_count, '日時取得済み'], [data.sets.length, '候補セット']]) {
    const card = node('div', undefined, 'metric');
    card.append(node('strong', String(value)), node('span', label));
    byId('summary').append(card);
  }
  const notices = byId('notices');
  notices.replaceChildren();
  if (data.assumed_zone_count) notices.append(node('p', `タイムゾーン情報のない ${data.assumed_zone_count} 枚には ${data.timezone} を使用しました。`, 'notice'));
  if (data.excluded.length) {
    const details = node('details', undefined, 'notice');
    details.append(node('summary', `撮影日時を取得できない ${data.excluded.length} 枚を除外しました。`));
    const list = node('ul');
    for (const name of data.excluded) list.append(node('li', name));
    details.append(list);
    notices.append(details);
  }
  const sets = byId('sets');
  sets.replaceChildren();
  if (!data.sets.length) sets.append(node('p', '条件に一致するセットはありません。フォルダや分類条件を確認してください。', 'panel'));
  for (const group of data.sets) {
    const card = node('article', undefined, 'panel set-card');
    const selectLabel = node('label', undefined, 'set-selection');
    const checkbox = node('input');
    checkbox.type = 'checkbox';
    checkbox.className = 'set-checkbox';
    checkbox.value = String(group.number);
    checkbox.dataset.copyable = String(!group.copy_error);
    checkbox.disabled = !copyEnabled || copying || Boolean(group.copy_error);
    checkbox.addEventListener('change', updateCopyButtons);
    selectLabel.append(checkbox, node('span', `セット ${group.number} · ${group.count} 枚`));
    card.append(selectLabel);
    if (group.copy_error) card.append(node('p', group.copy_error, 'notice'));
    else card.append(node('p', `保存先：${group.target_folder} / ${group.copy_file_count} ファイル・${formatBytes(group.copy_bytes)}`, 'hint'));
    card.append(node('p', `${formatTime(group.start)} ～ ${formatTime(group.end)}`, 'set-time'));
    card.append(node('p', `約 ${group.interval.toFixed(2)} 秒間隔（${group.gap_min.toFixed(2)} ～ ${group.gap_max.toFixed(2)} 秒） · RAW あり ${group.paired_count} / ${group.count} 枚`));
    const timelineFiles = [group.before, ...group.files, group.after].filter(Boolean);
    const offset = group.before ? 1 : 0;
    const middle = Math.floor((group.files.length - 1) / 2);
    const strip = node('div', undefined, 'photo-strip');
    strip.append(
      photoTile(group.before, '直前 · セット外', timelineFiles, 0),
      photoTile(group.files[0], '先頭', timelineFiles, offset),
      photoTile(group.files[middle], '中央', timelineFiles, offset + middle),
      photoTile(group.files[group.files.length - 1], '末尾', timelineFiles, offset + group.files.length - 1),
      photoTile(group.after, '直後 · セット外', timelineFiles, timelineFiles.length - 1)
    );
    card.append(strip, node('p', '前後の写真は、撮影日時を取得できた JPG の時刻順で表示します。画像をクリックすると拡大できます。', 'hint'));
    const galleryDetails = node('details', undefined, 'gallery-details');
    galleryDetails.append(node('summary', `セットの全 ${group.count} 枚を表示`));
    const gallery = node('div', undefined, 'photo-gallery');
    galleryDetails.append(gallery);
    let galleryCreated = false;
    galleryDetails.addEventListener('toggle', () => {
      if (galleryDetails.open && !galleryCreated) {
        galleryCreated = true;
        group.files.forEach((file, index) => gallery.append(photoTile(file, `${index + 1} 枚目`, timelineFiles, offset + index, true)));
      }
      for (const image of gallery.querySelectorAll('img[data-src]')) {
        if (galleryDetails.open) {
          if (imageObserver) imageObserver.observe(image);
          else { image.src = image.dataset.src; delete image.dataset.src; }
        } else if (imageObserver) imageObserver.unobserve(image);
      }
    });
    card.append(galleryDetails);
    const details = node('details');
    details.append(node('summary', 'ファイル一覧を確認'));
    const table = node('table');
    const head = node('thead');
    const row = node('tr');
    for (const title of ['JPG', '撮影日時', '前の写真との差', 'RAW']) row.append(node('th', title));
    head.append(row);
    const body = node('tbody');
    for (const file of group.files) {
      const row = node('tr');
      row.append(node('td', file.jpg), node('td', formatTime(file.time)), node('td', formatGap(file.previous_gap)), node('td', file.raw.join(', ') || 'なし'));
      body.append(row);
    }
    table.append(head, body);
    const scroll = node('div', undefined, 'table-scroll');
    scroll.append(table);
    details.append(scroll);
    card.append(details);
    sets.append(card);
  }
  updateCopyButtons();
}
byId('folder-form').addEventListener('submit', event => { event.preventDefault(); openFolder(byId('folder-path').value); });
byId('parent-folder').addEventListener('click', () => { if (parentFolder !== null) openFolder(parentFolder); });
byId('scan-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (scanning || loadingFolder || selectedFolder === null) return;
  scanning = true;
  updateButton();
  byId('results').hidden = true;
  currentScan = null;
  updateCopyButtons();
  byId('scan-button').textContent = '解析中…';
  status(`${selectedFolder || '写真ルート'} の Exif を読み取っています…`);
  try {
    const data = await requestJSON('/api/scan', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Scan-Token': token}, body: JSON.stringify({folder: selectedFolder, min_interval: Number(byId('min-interval').value), max_interval: Number(byId('max-interval').value), tolerance: Number(byId('tolerance').value), min_count: Number(byId('min-count').value)})});
    resetSavePanel();
    renderResults(data);
    status('解析が完了しました。写真は変更していません。');
  } catch (error) { status(error.message, true); }
  finally { scanning = false; byId('scan-button').textContent = 'このフォルダを解析'; updateButton(); }
});
function formatBytes(bytes) {
  if (!Number.isFinite(bytes)) return '0 B';
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KiB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GiB`;
}
function selectedGroups() {
  if (!currentScan) return [];
  const checked = new Set([...document.querySelectorAll('.set-checkbox:checked')].map(box => Number(box.value)));
  return currentScan.sets.filter(group => checked.has(group.number) && !group.copy_error);
}
function updateCopyButtons() {
  const blocked = !copyEnabled || copying || copySubmitting || scanning || loadingFolder || !currentScan;
  for (const box of document.querySelectorAll('.set-checkbox')) box.disabled = blocked || box.dataset.copyable !== 'true';
  const groups = selectedGroups();
  byId('operation').disabled = copying || copySubmitting;
  byId('move-option').disabled = !moveEnabled;
  const moving = byId('operation').value === 'move';
  byId('start-copy').textContent = moving ? '選択したセットを移動' : '選択したセットをコピー';
  byId('start-copy').disabled = blocked || !groups.length || (moving && !moveEnabled);
  byId('select-all').disabled = blocked || !currentScan.sets.some(group => !group.copy_error);
  byId('selection-summary').textContent = groups.length ? `${groups.length} セット・${groups.reduce((n,g)=>n+g.copy_file_count,0)} ファイル・${formatBytes(groups.reduce((n,g)=>n+g.copy_bytes,0))}` : 'セットを選択してください。';
}
function resetSavePanel() {
  // An analysis must not hide an ongoing transfer or re-enable its controls.
  if (copying || copySubmitting) return;
  if (lastCopyJob) dismissedCopyJobId = lastCopyJob.id;
  byId('operation').value = 'copy';
  showCopyJob(null);
}
function showCopyJob(job) {
  // Status polling may return the previous result after a new analysis.
  if (job && job.id === dismissedCopyJobId && job.state !== 'running') return;
  lastCopyJob = job;
  copying = Boolean(job && job.state === 'running');
  byId('copy-job').hidden = !job;
  if (!job) {
    byId('copy-progress').value = 0;
    byId('copy-progress-text').textContent = '';
    byId('copy-current').textContent = '';
    byId('copy-error').hidden = true;
    byId('copy-error').textContent = '';
    byId('copy-destinations').replaceChildren();
  }
  if (job) {
    const percent = job.bytes_total ? Math.min(100, (job.bytes_copied + (job.bytes_reused || 0)) / job.bytes_total * 100) : 0;
    byId('copy-progress').value = job.state === 'completed' ? 100 : percent;
    byId('copy-progress-text').textContent = `${job.phase} · 内容検証済み ${job.files_done} / ${job.files_total} ファイル · 転送 ${formatBytes(job.bytes_copied)} / 全体 ${formatBytes(job.bytes_total)}${job.bytes_reused ? ` · 既存データ照合 ${formatBytes(job.bytes_reused)}` : ""}${job.operation === "move" ? ` · 元ファイル削除済み ${job.sources_removed || 0} / ${job.files_total}` : ""}`;
    byId('copy-current').textContent = job.state === 'completed' ? (job.operation === 'move' ? '移動が完了しました。候補を再解析してください。' : 'コピーと内容検証が完了しました。元ファイルは保持しています。') : job.current_file;
    byId('copy-error').hidden = !job.error;
    byId('copy-error').textContent = job.error ? `${job.error} ${job.operation === 'move' ? `元ファイル削除済み ${job.sources_removed || 0} 件。保存先の MOVE_INCOMPLETE と move-log.jsonl、および元フォルダを確認してください。` : '元ファイルは保持しています。保存先の COPY_INCOMPLETE と copy-log.jsonl を確認してください。'}` : '';
    byId('copy-destinations').replaceChildren(...job.destinations.map(name=>node('li', name)));
    if (job.operation === 'move' && (job.state === 'completed' || job.sources_removed > 0) && invalidatedMove !== `${job.id}:${job.sources_removed}:${job.state}`) {
      invalidatedMove = `${job.id}:${job.sources_removed}:${job.state}`;
      currentScan = null;
      document.querySelectorAll('.set-checkbox').forEach(box=>{box.checked=false;});
    }
  }
  updateCopyButtons();
}
async function pollCopy() {
  clearTimeout(pollTimer);
  try {
    const data = await requestJSON('/api/copy/status');
    copyEnabled = data.copy_enabled;
    moveEnabled = Boolean(data.move_enabled);
    byId('copy-config').textContent = copyEnabled ? '保存先は PHOTO_DESTINATION で設定したルートです。移動時は既存コピーも全件照合し、一致した場合だけ元ファイルを削除します。' : 'コピー先が未設定です。NAS の PHOTO_DESTINATION と保存先マウントを設定してください。';
    showCopyJob(data.job);
    if (copying) pollTimer = setTimeout(pollCopy, 1000);
  } catch (error) {
    byId('copy-config').textContent = `コピー状態を取得できません：${error.message}。処理は NAS 側で継続している場合があります。`;
    pollTimer = setTimeout(pollCopy, 3000);
  }
}
byId('operation').addEventListener('change', updateCopyButtons);
byId('select-all').addEventListener('click', () => {
  const boxes = [...document.querySelectorAll('.set-checkbox:not(:disabled)')];
  const select = !boxes.every(box=>box.checked);
  boxes.forEach(box=>{box.checked=select;});
  updateCopyButtons();
});
byId('start-copy').addEventListener('click', async () => {
  const groups = selectedGroups();
  if (!groups.length || copying || copySubmitting || !currentScan) return;
  const summary = groups.map(group=>`${group.target_folder}（${group.copy_file_count} ファイル）`).join('\n');
  const operation = byId('operation').value;
  if (operation === 'move' && !moveEnabled) return;
  const message = operation === 'move' ? '次のセットを移動します。全件の内容照合後、元の JPG / RAW を削除します。既存コピーがあれば一致を確認して利用します。' : '次のセットをコピーします。元ファイルは保持します。';
  if (!window.confirm(`${message}\n\n${summary}`)) return;
  copySubmitting = true;
  updateCopyButtons();
  try {
    const data = await requestJSON('/api/copy', {method:'POST', headers:{'Content-Type':'application/json','X-Scan-Token':token}, body:JSON.stringify({scan_id:currentScan.scan_id,sets:groups.map(group=>group.number),operation})});
    showCopyJob(data.job);
    pollCopy();
  } catch (error) {
    status(error.message, true);
    pollCopy();
  } finally {copySubmitting = false; updateCopyButtons();}
});
pollCopy();
openFolder('');
