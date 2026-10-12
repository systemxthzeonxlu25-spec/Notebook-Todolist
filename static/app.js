(() => {
  'use strict';
  let toastTimer;
  const storage = {
    get(key) { try { return localStorage.getItem(key); } catch (_) { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); return true; } catch (_) { return false; } },
    remove(key) { try { localStorage.removeItem(key); } catch (_) { /* Browser storage may be disabled. */ } },
  };
  function notify(message, error = false) {
    const toast = document.getElementById('toast');
    toast.textContent = message;
    toast.classList.toggle('toast--error', error);
    toast.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.hidden = true; }, error ? 7000 : 4000);
  }
  async function api(path, options = {}) {
    const headers = new Headers(options.headers || {});
    headers.set('Accept', 'application/json');
    const method = (options.method || 'GET').toUpperCase();
    if (!['GET', 'HEAD', 'OPTIONS'].includes(method)) {
      headers.set('X-CSRF-Token', document.querySelector('meta[name="csrf-token"]').content);
    }
    let body = options.body;
    if (body !== undefined && !(body instanceof FormData) && typeof body !== 'string') {
      headers.set('Content-Type', 'application/json');
      body = JSON.stringify(body);
    }
    let response;
    try { response = await fetch(path, { ...options, body, headers, credentials: 'same-origin' }); }
    catch (_) { throw new Error('网络连接失败，请检查网络后重试。'); }
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const expired = response.status === 401 && !path.startsWith('/api/auth/');
      if (expired) document.dispatchEvent(new CustomEvent('session:expired'));
      const error = new Error(expired ? '登录状态已失效，请保留本地草稿并前往登录。' : (payload.error || `请求失败（${response.status}），请稍后重试。`));
      error.status = response.status;
      throw error;
    }
    return payload.data;
  }
  function renderMarkdown(content, target) {
    if (!window.marked || !window.DOMPurify) {
      target.textContent = content || '开始输入笔记，即可在这里预览。';
      return;
    }
    const parsed = marked.parse(content || '开始输入笔记，即可在这里预览。', { breaks: true, gfm: true });
    target.innerHTML = DOMPurify.sanitize(parsed, { FORBID_TAGS: ['style', 'iframe'], FORBID_ATTR: ['style'] });
    target.querySelectorAll('a').forEach(link => {
      link.rel = 'noopener noreferrer';
      if (/^https?:/.test(link.getAttribute('href') || '')) link.target = '_blank';
    });
    const walker = document.createTreeWalker(target, NodeFilter.SHOW_TEXT);
    const textNodes = [];
    while (walker.nextNode()) textNodes.push(walker.currentNode);
    textNodes.forEach(node => {
      if (node.parentElement.closest('code, pre') || !/==([^=\n]+)==/.test(node.textContent)) return;
      const fragment = document.createDocumentFragment();
      const parts = node.textContent.split(/==([^=\n]+)==/g);
      parts.forEach((part, index) => {
        if (index % 2 === 0) fragment.append(document.createTextNode(part));
        else { const mark = document.createElement('mark'); mark.textContent = part; fragment.append(mark); }
      });
      node.replaceWith(fragment);
    });
    document.dispatchEvent(new CustomEvent('markdown:rendered', { detail: { target } }));
  }
  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function formatDate(value) {
    if (!value) return '未设置';
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  }
  function localDateTime(value) {
    if (!value) return '';
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return '';
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}T${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
  }
  const themeButton = document.getElementById('theme-toggle');
  let theme = storage.get('notebook-theme') || (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  function applyTheme() {
    document.documentElement.dataset.theme = theme;
    themeButton.textContent = theme === 'dark' ? '浅色模式' : '深色模式';
    themeButton.setAttribute('aria-pressed', String(theme === 'dark'));
  }
  applyTheme();
  themeButton.addEventListener('click', () => { theme = theme === 'dark' ? 'light' : 'dark'; storage.set('notebook-theme', theme); applyTheme(); });
  window.App = { api, notify, renderMarkdown, element, formatDate, localDateTime, storage };
})();
