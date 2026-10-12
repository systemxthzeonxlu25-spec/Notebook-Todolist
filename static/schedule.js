(() => {
  'use strict';
  const { api, notify, element } = App;
  const weekdays = ['星期一', '星期二', '星期三', '星期四', '星期五', '星期六', '星期日'];
  let courses = [];
  const courseList = document.getElementById('course-list');
  const courseSelect = document.getElementById('schedule-course');
  const scheduleList = document.getElementById('schedule-list');
  function courseName(id) { return courses.find(course => Number(course.id) === Number(id))?.name || '未命名课程'; }
  async function loadCourses() {
    courses = await api('/api/courses');
    courseList.replaceChildren();
    courseSelect.replaceChildren(new Option(courses.length ? '选择课程' : '请先添加课程', ''));
    for (const course of courses) {
      courseSelect.add(new Option(course.name, course.id));
      const item = element('li', 'list-item list-item__row');
      item.append(element('span', 'list-item__content', course.name));
      const actions = element('div', 'action-row');
      const rename = element('button', 'button button--quiet button--small', '重命名');
      rename.type = 'button';
      rename.addEventListener('click', async () => {
        const name = window.prompt('修改课程名称', course.name);
        if (!name?.trim() || name.trim() === course.name) return;
        try { await api(`/api/courses/${course.id}`, { method: 'PATCH', body: { name: name.trim() } }); await load(); notify('课程已更新'); }
        catch (error) { notify(error.message, true); }
      });
      const remove = element('button', 'button button--danger button--small', '删除');
      remove.type = 'button';
      remove.addEventListener('click', async () => {
        if (!window.confirm(`删除课程「${course.name}」及课表时段？已有笔记将保留并移至未归类。`)) return;
        try { await api(`/api/courses/${course.id}`, { method: 'DELETE' }); await load(); notify('课程已删除'); }
        catch (error) { notify(error.message, true); }
      });
      actions.append(rename, remove);
      item.append(actions);
      courseList.append(item);
    }
    if (!courses.length) courseList.append(element('li', 'empty-state', '还没有课程，添加第一门吧。'));
  }
  async function loadSchedule() {
    const slots = await api('/api/schedule');
    scheduleList.replaceChildren();
    if (!slots.length) { scheduleList.append(element('p', 'empty-state', '课表还是空白。请先添加课程，再设置上课时段。')); return; }
    for (let day = 0; day < 7; day += 1) {
      const daySlots = slots.filter(slot => Number(slot.weekday) === day).sort((a, b) => a.start_time.localeCompare(b.start_time));
      if (!daySlots.length) continue;
      const section = element('section', 'schedule-day');
      section.append(element('h3', 'schedule-day__title', weekdays[day]));
      const list = element('ul', 'item-list');
      for (const slot of daySlots) {
        const item = element('li', 'list-item list-item__row');
        const content = element('div', 'list-item__content');
        content.append(element('strong', '', slot.course_name || courseName(slot.course_id)), element('div', 'list-item__meta', `${slot.start_time} — ${slot.end_time}`));
        const remove = element('button', 'button button--quiet button--small', '删除时段');
        remove.type = 'button';
        remove.setAttribute('aria-label', `删除${weekdays[day]} ${slot.start_time} 的${slot.course_name || courseName(slot.course_id)}时段`);
        remove.addEventListener('click', async () => {
          if (!window.confirm('删除这个上课时段？')) return;
          try { await api(`/api/schedule/${slot.id}`, { method: 'DELETE' }); await loadSchedule(); notify('时段已删除'); }
          catch (error) { notify(error.message, true); }
        });
        item.append(content, remove);
        list.append(item);
      }
      section.append(list);
      scheduleList.append(section);
    }
  }
  async function load() { await loadCourses(); await loadSchedule(); }
  document.getElementById('course-form').addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('button');
    button.disabled = true;
    try { await api('/api/courses', { method: 'POST', body: { name: form.elements.name.value.trim() } }); form.reset(); await load(); notify('课程已添加'); }
    catch (error) { notify(error.message, true); }
    finally { button.disabled = false; }
  });
  document.getElementById('schedule-form').addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const values = Object.fromEntries(new FormData(form));
    if (values.end_time <= values.start_time) { notify('结束时间需要晚于开始时间。', true); return; }
    const button = form.querySelector('button');
    button.disabled = true;
    try {
      await api('/api/schedule', { method: 'POST', body: { ...values, course_id: Number(values.course_id), weekday: Number(values.weekday) } });
      await loadSchedule();
      notify('上课时段已添加');
    } catch (error) { notify(error.message, true); }
    finally { button.disabled = false; }
  });
  load().catch(error => { scheduleList.replaceChildren(element('p', 'error-message', error.message)); notify(error.message, true); });
})();
