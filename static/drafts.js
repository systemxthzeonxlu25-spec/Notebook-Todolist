(() => {
  'use strict';
  const { notify, storage, formatDate } = App;
  const status = document.getElementById('draft-status');
  const banner = document.getElementById('draft-recovery');
  const restoreButton = document.getElementById('draft-restore');
  const discardButton = document.getElementById('draft-discard');
  const userScope = document.body.dataset.userId || 'local';
  const newScope = NoteEditor.quick ? 'quick' : 'new';
  const keyFor = noteId => `notebook-draft:${userScope}:${noteId || newScope}`;
  let pending = null;
  let timer;
  let warned = false;
  let checked = false;
  function persist() {
    clearTimeout(timer);
    if (pending || !NoteEditor.dirty) return;
    const record = {
      version: 1,
      ...NoteEditor.values(),
      saved_at: new Date().toISOString(),
      base_updated_at: NoteEditor.note?.updated_at || null,
    };
    const success = storage.set(keyFor(NoteEditor.noteId), JSON.stringify(record));
    status.textContent = success ? `草稿已保存在此浏览器 · ${formatDate(record.saved_at)}` : '浏览器无法保存草稿，请复制正文备份，并在联网时保存笔记。';
    if (!success && !warned) { warned = true; notify('本地存储不可用或已满，请及时保存笔记或复制备份。', true); }
  }
  function lockEditor(locked) {
    ['note-title', 'note-content', 'note-course'].forEach(id => { document.getElementById(id).disabled = locked; });
    document.getElementById('note-save').disabled = locked || !NoteEditor.ready;
  }
  function readInitialDraft() {
    const raw = storage.get(keyFor(NoteEditor.noteId));
    if (!raw) return null;
    let draft;
    try { draft = JSON.parse(raw); }
    catch (_) { status.textContent = '本地草稿格式损坏，请保存当前笔记后清理浏览器数据。'; return null; }
    if (!draft || draft.version !== 1 || typeof draft.content !== 'string' || typeof draft.title !== 'string') return null;
    return draft;
  }
  // Capture the previous draft before any input debounce can replace it.
  pending = readInitialDraft();
  if (pending) {
    banner.hidden = false;
    document.getElementById('draft-recovery-message').textContent = '已有本地草稿已保留，正在读取笔记信息，随后可选择恢复或丢弃。';
    restoreButton.disabled = true;
    discardButton.disabled = true;
    lockEditor(true);
  }
  function recover() {
    if (checked) return;
    checked = true;
    if (!pending) return;
    const draft = pending;
    const saved = NoteEditor.note;
    // Only server-confirmed content can make a draft redundant. The editor
    // may already contain unsaved typing from before initialization completed.
    if (saved && !NoteEditor.dirty && saved.title === draft.title && saved.content === draft.content && String(saved.course_id ?? '') === String(draft.course_id ?? '')) {
      storage.remove(keyFor(NoteEditor.noteId));
      pending = null;
      banner.hidden = true;
      lockEditor(false);
      return;
    }
    const newerServer = draft.base_updated_at && NoteEditor.note?.updated_at && new Date(NoteEditor.note.updated_at) > new Date(draft.base_updated_at);
    document.getElementById('draft-recovery-message').textContent = `发现 ${formatDate(draft.saved_at)} 的未提交草稿。${newerServer ? '服务器上的笔记也有更新，恢复草稿会替换当前编辑器内容；确认后再保存。' : '恢复后可继续编辑，手动保存才会更新服务器。'}`;
    banner.hidden = false;
    restoreButton.disabled = false;
    discardButton.disabled = false;
    lockEditor(true);
    restoreButton.focus();
  }
  document.getElementById('draft-restore').addEventListener('click', () => {
    if (!pending) return;
    const draft = pending;
    pending = null;
    banner.hidden = true;
    lockEditor(false);
    NoteEditor.setValues({ title: draft.title, content: draft.content, course_id: draft.course_id });
    persist();
    document.getElementById('note-content').focus();
    notify('已恢复草稿，请检查内容后保存');
  });
  document.getElementById('draft-discard').addEventListener('click', () => {
    storage.remove(keyFor(NoteEditor.noteId));
    pending = null;
    banner.hidden = true;
    lockEditor(false);
    status.textContent = '已丢弃本地草稿，继续编辑会自动暂存新草稿。';
    if (NoteEditor.dirty) persist();
  });
  document.addEventListener('note:ready', () => { queueMicrotask(recover); });
  document.addEventListener('note:changed', () => { clearTimeout(timer); timer = setTimeout(persist, 350); });
  document.addEventListener('note:saved', event => {
    clearTimeout(timer);
    storage.remove(keyFor(event.detail.previousId));
    storage.remove(keyFor(NoteEditor.noteId));
    if (event.detail.dirty) persist();
    else status.textContent = '内容已保存到服务器，本地草稿已清理。';
  });
  document.addEventListener('note:deleted', event => { clearTimeout(timer); storage.remove(keyFor(event.detail.noteId)); });
  window.addEventListener('pagehide', persist);
  document.addEventListener('visibilitychange', () => { if (document.hidden) persist(); });
  if (NoteEditor.ready) recover();
})();
