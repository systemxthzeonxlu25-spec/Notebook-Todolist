(() => {
  'use strict';
  const { notify, renderMarkdown } = App;
  const panel = document.getElementById('share-panel');
  const open = document.getElementById('share-open');
  const paper = document.getElementById('share-paper');
  const template = document.getElementById('share-template');
  const download = document.getElementById('share-download');
  const link = document.getElementById('share-download-link');
  const status = document.getElementById('share-status');
  let objectUrl = null;
  let rendering = false;
  function clearDownload() {
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    objectUrl = null;
    link.hidden = true;
    link.removeAttribute('href');
  }
  function updatePreview() {
    if (rendering) return;
    clearDownload();
    const values = NoteEditor.values();
    document.getElementById('share-title').textContent = values.title || '未命名笔记';
    const course = document.getElementById('note-course');
    document.getElementById('share-course').textContent = course.selectedOptions[0]?.textContent || '未归类';
    paper.classList.toggle('share-paper--paper', template.value === 'paper');
    paper.classList.toggle('share-paper--night', template.value === 'night');
    renderMarkdown(values.content, document.getElementById('share-content'));
    status.textContent = '预览已更新，图片仅包含笔记标题、课程和正文。';
  }
  open.addEventListener('click', () => {
    panel.hidden = false;
    open.setAttribute('aria-expanded', 'true');
    updatePreview();
    panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  });
  document.getElementById('share-close').addEventListener('click', () => {
    panel.hidden = true;
    open.setAttribute('aria-expanded', 'false');
    open.focus();
  });
  template.addEventListener('change', updatePreview);
  document.addEventListener('note:changed', () => { if (!panel.hidden) updatePreview(); });
  download.addEventListener('click', async () => {
    if (rendering) return;
    if (!window.html2canvas) { notify('分享图组件加载失败，请刷新后重试。', true); return; }
    updatePreview();
    rendering = true;
    download.disabled = true;
    template.disabled = true;
    status.textContent = '正在生成长图，请稍候…';
    try {
      if (document.fonts?.ready) await document.fonts.ready;
      await Promise.race([
        Promise.all([...paper.querySelectorAll('img')].map(img => img.decode().catch(() => {}))),
        new Promise(resolve => setTimeout(resolve, 4000)),
      ]);
      if (paper.scrollHeight > 12000) throw new Error('笔记超过单张长图的长度限制，请拆分为多篇笔记后导出。');
      const canvas = await html2canvas(paper, {
        scale: paper.scrollHeight <= 4000 ? 2 : 1,
        backgroundColor: null,
        useCORS: true,
        allowTaint: false,
        logging: false,
        imageTimeout: 5000,
        onclone(clone) { clone.getElementById('share-preview').classList.add('share-preview--capture'); },
      });
      const blob = await new Promise((resolve, reject) => canvas.toBlob(value => value ? resolve(value) : reject(new Error('图片生成失败，请缩短笔记后重试。')), 'image/png'));
      clearDownload();
      objectUrl = URL.createObjectURL(blob);
      const filename = (NoteEditor.values().title || '未命名笔记').replace(/[<>:"/\\|?*\u0000-\u001f]/g, '_').slice(0, 80);
      link.href = objectUrl;
      link.download = `${filename}-${template.value}.png`;
      link.hidden = false;
      link.click();
      status.textContent = `长图已生成（${canvas.width} × ${canvas.height}），可点击「再次下载」保存。`;
    } catch (error) { status.textContent = error.message || '生成失败，请稍后重试。'; notify(status.textContent, true); }
    finally { rendering = false; download.disabled = false; template.disabled = false; }
  });
  window.addEventListener('pagehide', clearDownload);
})();
