(() => {
  'use strict';
  const panel = document.getElementById('vision-panel');
  if (!panel || !window.NoteEditor) return;
  const { api, notify, element, renderMarkdown } = App;
  const fileInput = document.getElementById('vision-file');
  const imagePreview = document.getElementById('vision-image');
  const status = document.getElementById('vision-status');
  const recognize = document.getElementById('vision-recognize');
  const resultSection = document.getElementById('vision-result');
  const resultPreview = document.getElementById('vision-result-preview');
  const appendButton = document.getElementById('vision-append');
  const replaceButton = document.getElementById('vision-replace');
  const discardButton = document.getElementById('vision-discard');
  const pendingSection = document.getElementById('vision-pending');
  const pendingList = document.getElementById('vision-pending-list');
  const uploadButton = document.getElementById('vision-upload');
  const attachmentList = document.getElementById('attachment-list');
  const attachmentStatus = document.getElementById('attachment-status');
  const reloadButton = document.getElementById('attachment-reload');
  let selectedFile = null;
  let selectedUrl = null;
  let result = '';
  let recognizing = false;
  let uploading = false;
  let savingForUpload = false;
  let attachmentRequest = 0;
  let pending = [];

  function configured() { return Boolean(window.AISettings?.configured()); }
  function updateControls() {
    const enabled = configured();
    document.getElementById('vision-settings-hint').hidden = enabled;
    recognize.disabled = !enabled || !selectedFile || recognizing || !NoteEditor.ready;
    fileInput.disabled = recognizing;
    appendButton.disabled = !result || recognizing || !NoteEditor.ready;
    replaceButton.disabled = appendButton.disabled;
    discardButton.disabled = recognizing;
    uploadButton.disabled = !pending.length || uploading || savingForUpload || !NoteEditor.ready;
    reloadButton.disabled = !NoteEditor.noteId;
  }
  function clearSelected() {
    if (selectedUrl) URL.revokeObjectURL(selectedUrl);
    selectedUrl = null;
    selectedFile = null;
    result = '';
    fileInput.value = '';
    imagePreview.removeAttribute('src');
    imagePreview.hidden = true;
    resultSection.hidden = true;
    resultPreview.replaceChildren();
    updateControls();
  }
  function renderPending() {
    pendingList.replaceChildren();
    pendingSection.hidden = pending.length === 0;
    for (const item of pending) {
      const row = element('li', 'list-item vision-pending__item');
      const description = element('div', 'vision-pending__name', item.file.name);
      description.append(element('div', 'vision-pending__status', item.message || '等待保存笔记后上传'));
      const remove = element('button', 'button button--quiet button--small', '取消上传');
      remove.type = 'button';
      remove.disabled = uploading;
      remove.addEventListener('click', () => {
        pending = pending.filter(candidate => candidate !== item);
        renderPending();
      });
      row.append(description, remove);
      pendingList.append(row);
    }
    updateControls();
  }
  function readImage(url) {
    return new Promise((resolve, reject) => {
      const image = new Image();
      image.onload = () => resolve(image);
      image.onerror = () => reject(new Error('无法读取图片，请重新选择 JPEG、PNG 或 WebP 图片。'));
      image.src = url;
    });
  }
  async function compressImage(url) {
    const image = await readImage(url);
    const scale = Math.min(1, 2000 / Math.max(image.naturalWidth, image.naturalHeight));
    if (!image.naturalWidth || !image.naturalHeight) throw new Error('图片尺寸无效，请更换图片。');
    const canvas = document.createElement('canvas');
    canvas.width = Math.max(1, Math.round(image.naturalWidth * scale));
    canvas.height = Math.max(1, Math.round(image.naturalHeight * scale));
    const context = canvas.getContext('2d', { alpha: false });
    if (!context) throw new Error('当前浏览器无法压缩图片，请更换浏览器重试。');
    context.drawImage(image, 0, 0, canvas.width, canvas.height);
    const dataUrl = canvas.toDataURL('image/jpeg', 0.85);
    canvas.width = 0;
    canvas.height = 0;
    if (!dataUrl.startsWith('data:image/jpeg;base64,') || dataUrl.length > 8 * 1024 * 1024) {
      throw new Error('压缩后的图片仍然过大，请裁剪板书区域后重试。');
    }
    return dataUrl;
  }
  fileInput.addEventListener('change', () => {
    const file = fileInput.files?.[0];
    clearSelected();
    if (!file) return;
    if (!['image/jpeg', 'image/png', 'image/webp'].includes(file.type)) {
      status.textContent = '仅支持 JPEG、PNG、WebP 图片，请更换格式后重试。';
      return;
    }
    if (!file.size || file.size > 12 * 1024 * 1024) {
      status.textContent = '请选择小于或等于 12 MiB 的有效图片。';
      return;
    }
    selectedFile = file;
    selectedUrl = URL.createObjectURL(file);
    imagePreview.src = selectedUrl;
    imagePreview.hidden = false;
    status.textContent = `已选择 ${file.name}，点击“识别照片”继续。`;
    updateControls();
  });
  recognize.addEventListener('click', async () => {
    if (!selectedFile || !configured() || recognizing) return;
    recognizing = true;
    result = '';
    resultSection.hidden = true;
    updateControls();
    status.textContent = '正在压缩照片并识别，请稍候…';
    try {
      const image = await compressImage(selectedUrl);
      const response = await api('/api/ai/vision', { method: 'POST', headers: AISettings.headers(), body: { image } });
      if (typeof response?.content !== 'string' || !response.content.trim()) throw new Error('AI 没有返回可用的文字，请更换照片或支持图片输入的模型后重试。');
      result = response.content;
      renderMarkdown(result, resultPreview);
      resultSection.hidden = false;
      status.textContent = '识别完成，请核对文字、公式与作业要求。内容尚未写入笔记。';
    } catch (error) {
      status.textContent = `识别失败：${error.message} 原图仍保留在本页，可重试。`;
      notify(error.message, true);
    } finally { recognizing = false; updateControls(); }
  });
  function useResult(replace) {
    if (!result || !selectedFile || !NoteEditor.ready) return;
    const current = NoteEditor.values().content;
    if (replace && current.trim() && !window.confirm('使用识别结果替换当前正文？尚未保存的正文也会被替换。')) return;
    const content = replace || !current.trim() ? result : `${current.trimEnd()}\n\n${result}`;
    NoteEditor.setValues({ content });
    pending.push({ file: selectedFile, message: '识别内容已加入编辑器，等待保存笔记后上传原图' });
    clearSelected();
    renderPending();
    status.textContent = '识别结果已加入编辑器，尚未保存。保存笔记后会上传原图。';
    notify('已加入编辑器，请保存笔记以保留内容和原图。');
  }
  appendButton.addEventListener('click', () => useResult(false));
  replaceButton.addEventListener('click', () => useResult(true));
  discardButton.addEventListener('click', () => {
    result = '';
    resultSection.hidden = true;
    resultPreview.replaceChildren();
    status.textContent = '识别结果已丢弃，可重新识别或更换照片。';
    updateControls();
  });
  function safeAttachmentUrl(value) {
    if (typeof value !== 'string' || !value.trim()) return null;
    try {
      const url = new URL(value, location.origin);
      return url.origin === location.origin && ['http:', 'https:'].includes(url.protocol) ? url.href : null;
    } catch (_) { return null; }
  }
  function attachmentCard(item) {
    const card = element('figure', 'attachment-card');
    const url = safeAttachmentUrl(item.url);
    if (url) {
      const link = element('a', 'attachment-card__link');
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      const image = element('img', 'attachment-card__image');
      image.src = url;
      image.alt = item.original_name || '板书原图';
      image.loading = 'lazy';
      link.append(image);
      card.append(link);
    }
    card.append(element('figcaption', 'attachment-card__caption', item.original_name || '板书原图'));
    const remove = element('button', 'button button--danger button--small', '删除原图');
    remove.type = 'button';
    remove.addEventListener('click', async () => {
      if (!window.confirm('删除这张原图附件？笔记正文会保留。')) return;
      remove.disabled = true;
      try { await api(`/api/attachments/${encodeURIComponent(item.id)}`, { method: 'DELETE' }); await loadAttachments(); }
      catch (error) { notify(error.message, true); remove.disabled = false; }
    });
    card.append(remove);
    return card;
  }
  async function loadAttachments() {
    const request = ++attachmentRequest;
    const noteId = NoteEditor.noteId;
    if (!noteId) {
      attachmentList.replaceChildren();
      attachmentStatus.textContent = '保存笔记后，板书原图会显示在这里。';
      updateControls();
      return;
    }
    attachmentStatus.textContent = '正在加载附件…';
    try {
      const items = await api(`/api/notes/${encodeURIComponent(noteId)}/attachments`);
      if (request !== attachmentRequest || noteId !== NoteEditor.noteId) return;
      if (!Array.isArray(items)) throw new Error('附件列表返回异常，请稍后刷新。');
      attachmentList.replaceChildren(...items.map(attachmentCard));
      attachmentStatus.textContent = items.length ? `已保存 ${items.length} 张原图` : '这篇笔记还没有原图附件。';
    } catch (error) {
      if (request === attachmentRequest) attachmentStatus.textContent = `附件加载失败：${error.message}`;
    }
    updateControls();
  }
  async function uploadPending() {
    if (uploading || !pending.length || !NoteEditor.noteId || NoteEditor.dirty) return;
    const noteId = NoteEditor.noteId;
    const batch = [...pending];
    uploading = true;
    renderPending();
    let succeeded = 0;
    try {
      for (const item of batch) {
        item.message = '正在上传原图…';
        renderPending();
        const form = new FormData();
        form.append('file', item.file, item.file.name);
        try {
          await api(`/api/notes/${encodeURIComponent(noteId)}/attachments`, { method: 'POST', body: form });
          pending = pending.filter(candidate => candidate !== item);
          succeeded += 1;
        } catch (error) { item.message = `上传失败：${error.message} 原图仍在本页，可重试。`; }
        renderPending();
      }
      if (succeeded) { await loadAttachments(); notify(`已保存 ${succeeded} 张原图附件。`); }
      if (NoteEditor.dirty) status.textContent = '正文有新的未保存修改，请再次保存笔记；待上传的原图会保留在本页。';
      else if (pending.length) status.textContent = '笔记内容已保存，但仍有原图尚未上传。请重试上传并保持本页打开。';
      else status.textContent = '笔记内容与已确认的原图均已保存。';
    } finally { uploading = false; renderPending(); }
  }
  uploadButton.addEventListener('click', async () => {
    if (!pending.length || uploading || savingForUpload) return;
    if (NoteEditor.noteId && !NoteEditor.dirty) { await uploadPending(); return; }
    savingForUpload = true;
    updateControls();
    try {
      const saved = await NoteEditor.save();
      if (!saved) status.textContent = '笔记尚未保存，原图仍在本页。请先完成笔记保存后重试。';
      else if (NoteEditor.dirty) status.textContent = '保存期间正文有新的修改，请再次保存后上传原图。';
    } finally { savingForUpload = false; updateControls(); }
  });
  reloadButton.addEventListener('click', loadAttachments);
  document.addEventListener('note:ready', () => { updateControls(); loadAttachments(); });
  document.addEventListener('note:saved', event => {
    updateControls();
    if (!event.detail?.dirty && pending.length) uploadPending();
    else loadAttachments();
  });
  document.addEventListener('note:deleted', () => { pending = []; clearSelected(); });
  document.addEventListener('ai:settings-changed', updateControls);
  window.addEventListener('storage', updateControls);
  window.addEventListener('beforeunload', event => {
    if (pending.length) { event.preventDefault(); event.returnValue = ''; }
  });
  updateControls();
  if (NoteEditor.ready) loadAttachments();
})();
