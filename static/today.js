(() => {
  'use strict';
  const { api, notify, element, formatDate, localDateTime } = App;
  const todoList = document.getElementById('today-todos');
  const cardList = document.getElementById('today-cards');
  const allToggle = document.getElementById('todos-show-all');
  let today = null;
  let todoSerial = 0;
  function sourceLink(noteId) {
    const link = element('a', '', '查看来源笔记');
    link.href = `/notes/${noteId}`;
    return link;
  }
  function renderTodos(todos) {
    todoList.replaceChildren();
    document.getElementById('todos-count').textContent = `${todos.filter(todo => !todo.completed).length} 项未完成`;
    if (!todos.length) { todoList.append(element('li', 'empty-state', allToggle.checked ? '还没有待办。在笔记中写下「- [ ] 任务内容」即可生成。' : '到期任务都处理好了。继续记录，或复习一张卡片吧。')); return; }
    for (const todo of todos) {
      const item = element('li', 'list-item todo-item');
      item.classList.toggle('todo-item--completed', Boolean(todo.completed));
      const label = element('label', 'todo-item__check');
      const checkbox = element('input');
      checkbox.type = 'checkbox';
      checkbox.checked = Boolean(todo.completed);
      label.append(checkbox, element('span', 'todo-item__title', todo.title));
      const meta = element('div', 'list-item__meta');
      const due = element('span', '', `截止：${formatDate(todo.due_at)} · `);
      meta.append(due, sourceLink(todo.note_id));
      const details = element('details', 'todo-item__details');
      details.append(element('summary', '', '修改截止时间'));
      const form = element('form', 'form-row todo-item__due-form');
      const dueLabel = element('label', 'field field--grow');
      dueLabel.append(element('span', 'field__label', '截止时间'));
      const dueInput = element('input');
      dueInput.type = 'datetime-local';
      dueInput.required = true;
      dueInput.value = localDateTime(todo.due_at);
      dueLabel.append(dueInput);
      const update = element('button', 'button button--secondary button--small', '更新');
      update.type = 'submit';
      form.append(dueLabel, update);
      details.append(form);
      item.append(label, meta, details);
      todoList.append(item);
      checkbox.addEventListener('change', async () => {
        checkbox.disabled = true;
        try {
          await api(`/api/todos/${todo.id}`, { method: 'PATCH', body: { completed: checkbox.checked } });
          todo.completed = checkbox.checked;
          item.classList.toggle('todo-item--completed', checkbox.checked);
          document.getElementById('todos-count').textContent = `${todos.filter(entry => !entry.completed).length} 项未完成`;
          notify(checkbox.checked ? '已完成，做得好' : '已恢复为待办');
        } catch (error) { checkbox.checked = !checkbox.checked; notify(error.message, true); }
        finally { checkbox.disabled = false; }
      });
      form.addEventListener('submit', async event => {
        event.preventDefault();
        update.disabled = true;
        try {
          const dueAt = new Date(dueInput.value).toISOString();
          await api(`/api/todos/${todo.id}`, { method: 'PATCH', body: { due_at: dueAt } });
          todo.due_at = dueAt;
          due.textContent = `截止：${formatDate(dueAt)} · `;
          details.open = false;
          notify('截止时间已更新');
          await loadTodos();
        } catch (error) { notify(error.message, true); }
        finally { update.disabled = false; }
      });
    }
  }
  function renderCards(cards) {
    cardList.replaceChildren();
    document.getElementById('cards-count').textContent = `${cards.length} 张`;
    if (!cards.length) { cardList.append(element('p', 'empty-state', '今天的复习已完成。在笔记中用 ==重点== 标记内容，可生成新的卡片。')); return; }
    for (const card of cards) {
      const article = element('article', 'review-card');
      article.append(element('p', 'review-card__front', card.front));
      const source = element('div', 'review-card__source');
      source.append(sourceLink(card.note_id));
      article.append(source);
      const reveal = element('button', 'button button--secondary button--small', '显示答案');
      reveal.type = 'button';
      reveal.setAttribute('aria-expanded', 'false');
      const back = element('div', 'review-card__back');
      back.id = `card-answer-${card.id}`;
      reveal.setAttribute('aria-controls', back.id);
      back.hidden = true;
      back.append(element('p', '', card.back));
      const actions = element('div', 'action-row');
      const remembered = element('button', 'button button--small', '记住了');
      remembered.type = 'button';
      const forgotten = element('button', 'button button--quiet button--small', '没记住');
      forgotten.type = 'button';
      actions.append(remembered, forgotten);
      back.append(actions);
      article.append(reveal, back);
      cardList.append(article);
      reveal.addEventListener('click', () => { back.hidden = false; reveal.hidden = true; reveal.setAttribute('aria-expanded', 'true'); remembered.focus(); });
      async function review(success) {
        remembered.disabled = true;
        forgotten.disabled = true;
        try {
          const reviewed = await api(`/api/cards/${card.id}/review`, { method: 'POST', body: { remembered: success } });
          const index = cards.findIndex(entry => entry.id === card.id);
          if (index !== -1) cards.splice(index, 1);
          renderCards(cards);
          notify(success ? `已记录，下次复习：${formatDate(reviewed?.due_at)}` : '已安排稍后复习，再回顾一下来源笔记吧');
        } catch (error) { notify(error.message, true); remembered.disabled = false; forgotten.disabled = false; }
      }
      remembered.addEventListener('click', () => review(true));
      forgotten.addEventListener('click', () => review(false));
    }
  }
  async function loadTodos() {
    const serial = ++todoSerial;
    try {
      const data = await api(allToggle.checked ? '/api/todos' : '/api/today');
      if (serial !== todoSerial) return;
      renderTodos(allToggle.checked ? data : data.todos);
    } catch (error) { notify(error.message, true); }
  }
  allToggle.addEventListener('change', loadTodos);
  async function initialize() {
    try {
      today = await api('/api/today');
      document.getElementById('today-date').textContent = `${today.date} · ${today.tz || '应用时间'} · 从一件小事开始。`;
      renderTodos(today.todos);
      renderCards(today.cards);
      document.dispatchEvent(new CustomEvent('today:ready', { detail: today }));
    } catch (error) {
      document.getElementById('today-date').textContent = '暂时无法加载今天的安排';
      todoList.replaceChildren(element('li', 'error-message', error.message));
      cardList.replaceChildren(element('p', 'error-message', '请刷新页面后重试。'));
      notify(error.message, true);
    }
  }
  initialize();
})();
