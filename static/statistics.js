(() => {
  'use strict';
  const { api, element, notify } = App;
  const heatmap = document.getElementById('learning-heatmap');
  const detail = document.getElementById('heatmap-detail');
  const status = document.getElementById('statistics-status');
  const timezone = document.getElementById('statistics-timezone');
  const todoRate = document.getElementById('todo-rate');
  const courses = document.getElementById('course-statistics');
  const reload = document.getElementById('statistics-reload');
  const percentage = value => typeof value !== 'number' || !Number.isFinite(value) ? '暂无' : `${Math.round(value * 100)}%`;
  let selectedCell = null;
  let loading = false;
  function describeDay(button, day) {
    selectedCell?.setAttribute('aria-pressed', 'false');
    selectedCell = button;
    selectedCell.setAttribute('aria-pressed', 'true');
    detail.textContent = `${day.date}：${day.count} 次学习活动${day.count === 0 ? '，当天尚无记录' : ''}`;
  }
  async function initialize() {
    if (loading) return;
    loading = true;
    reload.disabled = true;
    status.classList.remove('error-message');
    status.textContent = '正在加载学习记录…';
    try {
      const stats = await api('/api/statistics');
      if (!stats || !Array.isArray(stats.heatmap) || !Array.isArray(stats.courses)) throw new Error('统计数据暂时不可用，请稍后重试。');
      todoRate.textContent = `${percentage(stats.todo_completion_rate)} · ${stats.todo_completed} / ${stats.todo_total} 项已完成`;
      timezone.textContent = stats.timezone ? `统计时区：${stats.timezone}` : '统计时区：服务端未提供';
      selectedCell = null;
      heatmap.replaceChildren();
      let activityCount = 0;
      for (const day of stats.heatmap) {
        const level = day.count === 0 ? 0 : day.count === 1 ? 1 : day.count <= 3 ? 2 : 3;
        const item = element('li', 'learning-heatmap__item');
        const button = element('button', `learning-heatmap__day learning-heatmap__day--${level}`);
        button.type = 'button';
        button.title = `${day.date}：${day.count} 次学习活动`;
        button.setAttribute('aria-label', button.title);
        button.setAttribute('aria-pressed', 'false');
        button.dataset.date = day.date;
        button.append(element('span', 'learning-heatmap__count', String(day.count)));
        for (const event of ['focus', 'pointerenter', 'click']) button.addEventListener(event, () => describeDay(button, day));
        item.append(button);
        heatmap.append(item);
        activityCount += day.count;
      }
      if (stats.heatmap.length) {
        const first = stats.heatmap[0].date;
        const last = stats.heatmap.at(-1).date;
        status.textContent = `${first} 至 ${last}，共 ${activityCount} 次学习活动。`;
        detail.textContent = activityCount ? '聚焦、悬停或点击日期方格，查看当天学习次数。' : '这段时间暂无学习记录。创建笔记、完成待办或复习卡片后，这里会记录你的进步。';
      } else {
        status.textContent = '学习记录已加载，暂无热力图数据。';
        detail.textContent = '暂无学习记录。创建笔记、完成待办或复习卡片后再来看看。';
      }
      courses.replaceChildren();
      if (!stats.courses.length) courses.append(element('p', 'empty-state', '记录课程与笔记后，这里会显示学习情况。'));
      for (const course of stats.courses) {
        const card = element('article', 'review-card course-statistics');
        card.append(element('h3', 'course-statistics__name', course.course_name));
        card.append(element('p', 'course-statistics__accuracy', `复习正确率：${percentage(course.review_accuracy)}（${course.review_correct} / ${course.review_attempts} 次）`));
        card.append(element('p', 'course-statistics__completion', `待办完成率：${percentage(course.todo_completion_rate)}（${course.todo_completed} / ${course.todo_total} 项）`));
        courses.append(card);
      }
    } catch (error) {
      status.textContent = `加载失败：${error.message} 可点击“刷新统计”重试。`;
      status.classList.add('error-message');
      timezone.textContent = '统计时区暂时不可用';
      todoRate.textContent = '暂时无法加载';
      heatmap.replaceChildren();
      selectedCell = null;
      detail.textContent = '热力图暂时无法加载，请重试。';
      courses.replaceChildren(element('p', 'error-message', '课程统计暂时无法加载，请重试。'));
      notify(error.message, true);
    } finally {
      loading = false;
      reload.disabled = false;
    }
  }
  reload.addEventListener('click', initialize);
  initialize();
})();
