(() => {
  'use strict';
  const { api, notify, renderMarkdown, formatDate } = App;
  const form = document.getElementById('note-form');
  const content = document.getElementById('note-content');
  const title = document.getElementById('note-title');
  const course = document.getElementById('note-course');
  const preview = document.getElementById('note-preview');
  const saveButton = document.getElementById('note-save');
  const deleteButton = document.getElementById('note-delete');
  const status = document.getElementById('note-save-status');
  const quick = form.dataset.mode === 'quick';
  let noteId = location.pathname.match(/^\/notes\/(\d+)/)?.[1] || null;
  let dirty = false;
  let ready = false;
  let savedNote = null;
  let previewTimer;
  function values() { return { title: title.value.trim(), content: content.value, course_id: course.value ? Number(course.value) : null }; }
  function markDirty() {
    dirty = true;
    status.textContent = '有未保存的修改';
    document.dispatchEvent(new CustomEvent('note:changed', { detail: { noteId, ...values() } }));
  }
  function updatePreview() { renderMarkdown(content.value, preview); }
  form.addEventListener('input', () => { markDirty(); clearTimeout(previewTimer); previewTimer = setTimeout(updatePreview, 200); });
  course.addEventListener('change', markDirty);
  async function save() {
    if (!ready || saveButton.disabled || !form.reportValidity()) return null;
    saveButton.disabled = true;
    status.textContent = '正在保存…';
    const submitted = values();
    try {
      const previousId = noteId;
      const note = await api(noteId ? `/api/notes/${noteId}` : '/api/notes', { method: noteId ? 'PUT' : 'POST', body: submitted });
      savedNote = note;
      noteId = String(note.id);
      history.replaceState(null, '', `/notes/${noteId}`);
      document.getElementById('editor-heading').textContent = '编辑笔记';
      deleteButton.hidden = false;
      dirty = JSON.stringify(submitted) !== JSON.stringify(values());
      status.textContent = dirty ? '已保存，有新的未保存修改' : `已保存 · ${formatDate(note.updated_at)}`;
      document.dispatchEvent(new CustomEvent('note:saved', { detail: { note, previousId, dirty } }));
      notify('笔记已保存，待办与复习卡已更新');
      return note;
    } catch (error) { status.textContent = '保存失败，内容仍在编辑器中'; notify(error.message, true); return null; }
    finally { saveButton.disabled = false; }
  }
  form.addEventListener('submit', event => { event.preventDefault(); save(); });
  document.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') { event.preventDefault(); save(); }
  });
  deleteButton.addEventListener('click', async () => {
    if (!noteId || !window.confirm('删除这篇笔记及其关联待办、复习卡？此操作无法撤销。')) return;
    deleteButton.disabled = true;
    try { await api(`/api/notes/${noteId}`, { method: 'DELETE' }); dirty = false; document.dispatchEvent(new CustomEvent('note:deleted', { detail: { noteId } })); location.assign('/notes'); }
    catch (error) { notify(error.message, true); deleteButton.disabled = false; }
  });
  window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
  window.NoteEditor = {
    get quick() { return quick; },
    get noteId() { return noteId; },
    get note() { return savedNote; },
    get ready() { return ready; },
    get dirty() { return dirty; },
    values, save,
    setValues(data) {
      if (data.title !== undefined) title.value = data.title;
      if (data.content !== undefined) content.value = data.content;
      if (data.course_id !== undefined) course.value = data.course_id ?? '';
      markDirty(); updatePreview();
    },
  };
  async function initialize() {
    saveButton.disabled = true;
    if (quick) content.focus();
    if (noteId) { title.disabled = true; content.disabled = true; course.disabled = true; }
    try {
      const [courses, initial] = await Promise.all([api('/api/courses'), noteId ? api(`/api/notes/${noteId}`) : (quick ? Promise.resolve({ course_id: null }) : api('/api/current-course'))]);
      courses.forEach(item => course.add(new Option(item.name, item.id)));
      if (noteId) {
        savedNote = initial;
        title.value = initial.title;
        content.value = initial.content;
        course.value = initial.course_id ?? '';
        status.textContent = `上次保存 · ${formatDate(initial.updated_at)}`;
        document.getElementById('editor-heading').textContent = '编辑笔记';
        document.getElementById('editor-course-hint').textContent = '保存后，笔记中的任务与重点会自动更新到待办和复习卡。';
        deleteButton.hidden = false;
      } else {
        if (!dirty) course.value = initial.course_id ?? '';
        document.getElementById('editor-course-hint').textContent = quick ? '随手记默认保存为未归类，随时可以选择课程。' : (initial.course_name ? `当前课程：${initial.course_name}，已为你自动选择。` : '现在没有课程，笔记将保存为未归类，也可以手动选择课程。');
      }
      updatePreview();
      ready = true;
      saveButton.disabled = false;
      document.dispatchEvent(new CustomEvent('note:ready', { detail: { noteId, note: savedNote } }));
    } catch (error) {
      status.textContent = '加载失败，可继续编辑本地草稿；恢复网络并刷新后再保存';
      notify(error.message, true);
      document.dispatchEvent(new CustomEvent('note:ready', { detail: { noteId, note: null, loadFailed: true } }));
    } finally { title.disabled = false; content.disabled = false; course.disabled = false; }
  }
  initialize();
})();
