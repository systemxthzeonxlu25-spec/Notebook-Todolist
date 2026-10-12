(() => {
  'use strict';
  const { api, notify } = App;
  const logout = document.getElementById('session-logout');
  document.addEventListener('session:expired', () => {
    document.getElementById('session-expired').hidden = false;
    const next = location.pathname + location.search;
    document.getElementById('session-login-link').href = `/auth?next=${encodeURIComponent(next)}`;
  });
  logout?.addEventListener('click', async () => {
    if (logout.disabled) return;
    logout.disabled = true;
    try {
      const data = await api('/api/auth/logout', { method: 'POST', body: {} });
      if (data.csrf_token) document.querySelector('meta[name="csrf-token"]').content = data.csrf_token;
      location.assign('/auth');
    } catch (error) { notify(error.message, true); logout.disabled = false; }
  });
})();
