(() => {
  'use strict';
  const userId = document.body.dataset.userId;
  const storageKey = userId ? `notebook-ai:${userId}` : null;
  function validate(config) {
    if (!config || typeof config.baseUrl !== 'string' || typeof config.apiKey !== 'string' || typeof config.model !== 'string') throw new Error('请完整填写 AI 地址、API Key 和模型名。');
    let url;
    try { url = new URL(config.baseUrl); } catch (_) { throw new Error('请填写有效的 HTTPS Base URL。'); }
    if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash || config.baseUrl.length > 500) throw new Error('Base URL 需要使用 HTTPS，且不能包含账号、密码、查询参数或片段。');
    if (!config.apiKey.trim() || config.apiKey.length > 4096 || /[\r\n]/.test(config.apiKey)) throw new Error('API Key 不能为空、不能超过 4096 个字符或包含换行。');
    if (!config.model.trim() || config.model.length > 200 || /[\r\n]/.test(config.model)) throw new Error('模型名不能为空、不能超过 200 个字符或包含换行。');
    return { baseUrl: url.href.replace(/\/+$/, ''), apiKey: config.apiKey.trim(), model: config.model.trim() };
  }
  function get() {
    if (!storageKey) return null;
    const value = App.storage.get(storageKey);
    if (!value) return null;
    try { return validate(JSON.parse(value)); } catch (_) { return null; }
  }
  function configured() { return Boolean(get()); }
  function headers() {
    const config = get();
    if (!config) throw new Error('请先前往设置，填写并保存 AI 连接信息。');
    return { 'X-AI-Key': config.apiKey, 'X-AI-Base-URL': config.baseUrl, 'X-AI-Model': config.model };
  }
  function changed() { document.dispatchEvent(new CustomEvent('ai:settings-changed')); }
  function save(config) {
    if (!storageKey) throw new Error('请先登录，再配置 AI。');
    const validated = validate(config);
    if (!App.storage.set(storageKey, JSON.stringify(validated))) throw new Error('浏览器未能保存设置，请检查是否禁用了本地存储，或清理存储空间。');
    changed();
  }
  function remove() {
    if (!storageKey) return;
    App.storage.remove(storageKey);
    if (App.storage.get(storageKey)) throw new Error('浏览器未能删除设置，请在浏览器设置中清理本站存储。');
    changed();
  }
  window.addEventListener('storage', event => { if (event.key === storageKey || event.key === null) changed(); });
  window.AISettings = { get, configured, headers, save, remove };
})();
