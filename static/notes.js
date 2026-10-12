(() => {
  'use strict';
  const { api, notify, element, formatDate } = App;
  const form = document.getElementById('notes-filter');
  const list = document.getElementById('notes-list');
  const count = document.getElementById('notes-count');
  let requestSerial = 0;
  async function loadNotes() {
    const serial = ++requestSerial;
    const params = new URLSearchParams();
    const query = form.elements.q.value.trim();
    const course = form.elements.course_id.value;
    if (query) params.set('q', query);
    if (course === 'unclassified') params.set('unclassified', '1');
    else if (course) params.set('course_id', course);
    count.textContent = '正在搜索…';
    try {
      const notes = await api(`/api/notes?${params}`);
      if (serial !== requestSerial) return;
      history.replaceState(null, '', `/notes${params.size ? `?${params}` : ''}`);
      list.replaceChildren();
      count.textContent = `共 ${notes.length} 篇笔记`;
      if (!notes.length) { list.append(element('p', 'empty-state', query || course ? '没有找到符合条件的笔记。' : '还没有笔记，点击「新建笔记」开始记录。')); return; }
      for (const note of notes) {
        const card = element('a', 'note-card');
        card.href = `/notes/${note.id}`;
        card.append(element('span', 'badge', note.course_name || '未归类'));
        card.append(element('h2', 'note-card__title', note.title || '无标题笔记'));
        card.append(element('p', 'note-card__excerpt', note.content.slice(0, 140) || '空白笔记'));
        card.append(element('div', 'list-item__meta', `更新于 ${formatDate(note.updated_at)}`));
        list.append(card);
      }
    } catch (error) {
      if (serial !== requestSerial) return;
      count.textContent = '加载失败';
      list.replaceChildren(element('p', 'error-message', error.message));
      notify(error.message, true);
    }
  }
  form.addEventListener('submit', event => { event.preventDefault(); loadNotes(); });
  form.elements.course_id.addEventListener('change', loadNotes);
  async function initialize() {
    const params = new URLSearchParams(location.search);
    form.elements.q.value = params.get('q') || '';
    try {
      const courses = await api('/api/courses');
      courses.forEach(course => form.elements.course_id.add(new Option(course.name, course.id)));
      form.elements.course_id.value = params.has('unclassified') ? 'unclassified' : (params.get('course_id') || '');
    } catch (error) { notify(error.message, true); }
    await loadNotes();
  }
  initialize();
})();
