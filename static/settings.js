(() => {
  'use strict';
  const form = document.getElementById('ai-settings-form');
  const status = document.getElementById('ai-settings-status');
  const remove = document.getElementById('ai-settings-delete');
  function load() {
    const config = AISettings.get();
    form.elements.base_url.value = config?.baseUrl || 'https://api.deepseek.com';
    form.elements.api_key.value = config?.apiKey || '';
    form.elements.model.value = config?.model || 'deepseek-v4.1-flash';
    remove.disabled = !config;
    status.textContent = config ? '此账号在当前浏览器中已保存 AI 设置。' : '尚未配置 AI。填写 API Key 并保存后启用。';
  }
  form.addEventListener('submit', event => {
    event.preventDefault();
    if (!form.reportValidity()) return;
    try {
      AISettings.save({ baseUrl: form.elements.base_url.value.trim(), apiKey: form.elements.api_key.value, model: form.elements.model.value.trim() });
      status.textContent = '已保存到当前浏览器。你现在可以返回笔记，使用 AI 功能。';
      remove.disabled = false;
      App.notify('AI 设置已保存');
    } catch (error) { status.textContent = error.message; App.notify(error.message, true); }
  });
  remove.addEventListener('click', () => {
    if (!window.confirm('删除当前账号在此浏览器保存的 AI 设置？删除后相关功能将停用。')) return;
    try { AISettings.remove(); load(); App.notify('已删除此浏览器中的 AI 设置'); }
    catch (error) { status.textContent = error.message; App.notify(error.message, true); }
  });
  load();
})();
