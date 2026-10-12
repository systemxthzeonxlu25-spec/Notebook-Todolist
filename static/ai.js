(() => {
  'use strict';
  const { api, notify, renderMarkdown, localDateTime } = App;
  const organize = document.getElementById('ai-organize');
  const generateCards = document.getElementById('ai-generate-cards');
  const extractTasks = document.getElementById('ai-extract-tasks');
  const mainStatus = document.getElementById('ai-main-status');
  const taskStatus = document.getElementById('ai-task-status');
  const cardStatus = document.getElementById('ai-card-status');
  const taskItems = document.getElementById('ai-task-items');
  const cardItems = document.getElementById('ai-card-items');
  const busy = new Set();
  let organized = '';
  let organizeSource = '';
  function syncConfig() {
    const configured = AISettings.configured();
    document.getElementById('ai-config-hint').textContent = configured ? '已配置 AI。生成内容请核对后使用。' : '尚未配置 AI，请前往设置填写并保存 API Key。';
    [organize, generateCards, extractTasks].forEach(button => { button.disabled = !configured || busy.has(button.id); });
  }
  async function generate(button, status, path, body) {
    busy.add(button.id);
    syncConfig();
    status.textContent = 'AI 正在处理，请稍候…';
    try {
      const result = await api(path, { method: 'POST', headers: AISettings.headers(), body });
      status.textContent = '生成完成，请检查下方结果。';
      return result;
    } catch (error) { status.textContent = error.message; notify(error.message, true); return null; }
    finally { busy.delete(button.id); syncConfig(); }
  }
  function requireContent() {
    const content = NoteEditor.values().content;
    if (!content.trim()) { notify('先写一点笔记，再使用这个功能。', true); return null; }
    return content;
  }
  async function sourceNoteId() {
    if (!NoteEditor.ready) throw new Error('笔记尚未加载成功，请保留草稿并刷新后重试。');
    if (NoteEditor.noteId) return Number(NoteEditor.noteId);
    const note = await NoteEditor.save();
    if (!note?.id) throw new Error('新笔记未能保存，生成结果仍保留在这里，请稍后重试。');
    return Number(note.id);
  }
  organize.addEventListener('click', async () => {
    const content = requireContent();
    if (!content) return;
    const result = await generate(organize, mainStatus, '/api/ai/organize', { content });
    if (!result || typeof result.content !== 'string' || !result.content.trim()) return;
    organized = result.content;
    organizeSource = content;
    renderMarkdown(organized, document.getElementById('ai-organize-preview'));
    document.getElementById('ai-organize-result').hidden = false;
  });
  function applyOrganized(replace) {
    if (!organized) return;
    const content = NoteEditor.values().content;
    if (replace && content !== organizeSource && !window.confirm('生成结果期间，编辑器内容已经变化。仍用整理结果替换当前内容？')) return;
    NoteEditor.setValues({ content: replace ? organized : `${content.trimEnd()}\n\n---\n\n## AI 整理版\n\n${organized}` });
    document.getElementById('ai-organize-result').hidden = true;
    mainStatus.textContent = '已放入编辑器，请检查并保存笔记。';
    notify('已应用整理结果，点击保存笔记后生效');
  }
  document.getElementById('ai-organize-replace').addEventListener('click', () => applyOrganized(true));
  document.getElementById('ai-organize-append').addEventListener('click', () => applyOrganized(false));
  document.getElementById('ai-organize-dismiss').addEventListener('click', () => { organized = ''; document.getElementById('ai-organize-result').hidden = true; mainStatus.textContent = '已放弃本次整理结果。'; });
  document.getElementById('ai-task-from-note').addEventListener('click', () => {
    const text = NoteEditor.values().content;
    if (text.length > 20000) { notify('笔记较长，请手动复制需要识别的任务片段（不超过 20000 字符）。', true); return; }
    document.getElementById('ai-task-text').value = text;
    document.getElementById('ai-task-text').focus();
  });
  function selectionControls(row) {
    row.querySelector('.ai-item-selected').addEventListener('change', event => {
      row.querySelectorAll('input:not([type="checkbox"]), textarea').forEach(input => { input.disabled = !event.target.checked; });
    });
  }
  document.getElementById('ai-task-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (extractTasks.disabled) return;
    const text = document.getElementById('ai-task-text').value.trim();
    if (!text) return;
    const result = await generate(extractTasks, taskStatus, '/api/ai/tasks', { text });
    if (!result) return;
    taskItems.replaceChildren();
    const tasks = Array.isArray(result.tasks) ? result.tasks : [];
    for (const task of tasks) {
      if (typeof task.title !== 'string' || !task.title.trim()) continue;
      const row = document.getElementById('ai-task-item-template').content.firstElementChild.cloneNode(true);
      row.querySelector('.ai-task-title').value = task.title;
      row.querySelector('.ai-task-due').value = localDateTime(task.due_at);
      selectionControls(row);
      taskItems.append(row);
    }
    document.getElementById('ai-task-result').hidden = !taskItems.children.length;
    taskStatus.textContent = taskItems.children.length ? `识别出 ${taskItems.children.length} 项任务，请核对截止时间后添加。` : '没有识别出明确任务，可以补充更具体的描述后重试。';
  });
  document.getElementById('ai-task-confirm').addEventListener('submit', async event => {
    event.preventDefault();
    const button = document.getElementById('ai-task-add');
    if (button.disabled) return;
    const items = [...taskItems.children].filter(row => row.querySelector('.ai-item-selected').checked).map(row => {
      const due = row.querySelector('.ai-task-due').value;
      return { title: row.querySelector('.ai-task-title').value.trim(), due_at: due ? new Date(due).toISOString() : null };
    });
    if (!items.length) { notify('请至少选择一项任务。', true); return; }
    button.disabled = true;
    taskStatus.textContent = '正在添加待办…';
    try {
      const noteId = await sourceNoteId();
      const added = await api('/api/todos', { method: 'POST', body: { items, note_id: noteId, course_id: NoteEditor.values().course_id } });
      document.getElementById('ai-task-result').hidden = true;
      taskItems.replaceChildren();
      taskStatus.textContent = `已添加 ${Array.isArray(added) ? added.length : items.length} 项待办，可在今日页的「所有待办」查看。`;
      notify('已添加确认的待办');
    } catch (error) { taskStatus.textContent = error.message; notify(error.message, true); }
    finally { button.disabled = false; }
  });
  document.getElementById('ai-task-dismiss').addEventListener('click', () => { document.getElementById('ai-task-result').hidden = true; taskStatus.textContent = '已放弃本次任务识别结果。'; });
  generateCards.addEventListener('click', async () => {
    const content = requireContent();
    if (!content) return;
    const result = await generate(generateCards, mainStatus, '/api/ai/cards', { content });
    if (!result) return;
    cardItems.replaceChildren();
    const cards = Array.isArray(result.cards) ? result.cards : [];
    for (const card of cards) {
      if (typeof card.front !== 'string' || typeof card.back !== 'string') continue;
      const row = document.getElementById('ai-card-item-template').content.firstElementChild.cloneNode(true);
      row.querySelector('.ai-card-front').value = card.front;
      row.querySelector('.ai-card-back').value = card.back;
      selectionControls(row);
      cardItems.append(row);
    }
    document.getElementById('ai-card-result').hidden = !cardItems.children.length;
    mainStatus.textContent = cardItems.children.length ? `已生成 ${cardItems.children.length} 道题，请核对问答后加入复习。` : '没有生成可用题目，请补充笔记内容后重试。';
    cardStatus.textContent = '';
  });
  document.getElementById('ai-card-confirm').addEventListener('submit', async event => {
    event.preventDefault();
    const button = document.getElementById('ai-card-add');
    if (button.disabled) return;
    const items = [...cardItems.children].filter(row => row.querySelector('.ai-item-selected').checked).map(row => ({ front: row.querySelector('.ai-card-front').value.trim(), back: row.querySelector('.ai-card-back').value.trim() }));
    if (!items.length) { notify('请至少选择一道题目。', true); return; }
    button.disabled = true;
    cardStatus.textContent = '正在加入复习卡…';
    try {
      const noteId = await sourceNoteId();
      const added = await api('/api/cards', { method: 'POST', body: { items, note_id: noteId, course_id: NoteEditor.values().course_id } });
      document.getElementById('ai-card-result').hidden = true;
      cardItems.replaceChildren();
      mainStatus.textContent = `已添加 ${Array.isArray(added) ? added.length : items.length} 张复习卡，可在今日页查看。`;
      notify('已加入确认的复习卡');
    } catch (error) { cardStatus.textContent = error.message; notify(error.message, true); }
    finally { button.disabled = false; }
  });
  document.getElementById('ai-card-dismiss').addEventListener('click', () => { document.getElementById('ai-card-result').hidden = true; mainStatus.textContent = '已放弃本次出题结果。'; });
  document.addEventListener('ai:settings-changed', syncConfig);
  syncConfig();
})();
