'use strict';
const byId = id => document.getElementById(id);
let selectedFolder = null;
let parentFolder = null;
let scanning = false;
let loadingFolder = false;
const token = document.querySelector('meta[name="scan-token"]').content;
const node = (tag, text, className) => {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
};
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
}
async function openFolder(path) {
  if (loadingFolder || scanning) return;
  loadingFolder = true;
  selectedFolder = null;
  updateButton();
  byId('results').hidden = true;
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
  byId('preview-time').textContent = file.time.replace('T', ' ');
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
  figure.append(button, node('figcaption', file.jpg), node('time', file.time.replace('T', ' '), 'photo-time'));
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
    card.append(node('h3', `セット ${group.number} · ${group.count} 枚`));
    card.append(node('p', `${group.start.replace('T', ' ')} ～ ${group.end.replace('T', ' ')}`, 'set-time'));
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
    for (const title of ['JPG', '撮影日時', 'RAW']) row.append(node('th', title));
    head.append(row);
    const body = node('tbody');
    for (const file of group.files) {
      const row = node('tr');
      row.append(node('td', file.jpg), node('td', file.time.replace('T', ' ')), node('td', file.raw.join(', ') || 'なし'));
      body.append(row);
    }
    table.append(head, body);
    const scroll = node('div', undefined, 'table-scroll');
    scroll.append(table);
    details.append(scroll);
    card.append(details);
    sets.append(card);
  }
}
byId('folder-form').addEventListener('submit', event => { event.preventDefault(); openFolder(byId('folder-path').value); });
byId('parent-folder').addEventListener('click', () => { if (parentFolder !== null) openFolder(parentFolder); });
byId('scan-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (scanning || loadingFolder || selectedFolder === null) return;
  scanning = true;
  updateButton();
  byId('results').hidden = true;
  byId('scan-button').textContent = '解析中…';
  status(`${selectedFolder || '写真ルート'} の Exif を読み取っています…`);
  try {
    const data = await requestJSON('/api/scan', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Scan-Token': token}, body: JSON.stringify({folder: selectedFolder, min_interval: Number(byId('min-interval').value), max_interval: Number(byId('max-interval').value), tolerance: Number(byId('tolerance').value), min_count: Number(byId('min-count').value)})});
    renderResults(data);
    status('解析が完了しました。写真は変更していません。');
  } catch (error) { status(error.message, true); }
  finally { scanning = false; byId('scan-button').textContent = 'このフォルダを解析'; updateButton(); }
});
openFolder('');
