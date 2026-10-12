(() => {
  'use strict';
  const { api } = App;
  const form = document.getElementById('auth-form');
  const password = document.getElementById('auth-password');
  const confirmPassword = document.getElementById('auth-confirm-password');
  const confirmField = document.getElementById('auth-confirm-field');
  const submit = document.getElementById('auth-submit');
  const modeSwitch = document.getElementById('auth-switch');
  const errorText = document.getElementById('auth-error');
  let registering = false;
  function clearError() { errorText.hidden = true; errorText.textContent = ''; }
  modeSwitch?.addEventListener('click', () => {
    if (form.dataset.registrationEnabled !== 'true') return;
    registering = !registering;
    clearError();
    document.getElementById('auth-heading').textContent = registering ? '注册笔记课间' : '登录笔记课间';
    document.getElementById('auth-form-heading').textContent = registering ? '创建你的账号' : '欢迎回来';
    submit.textContent = registering ? '注册并登录' : '登录';
    modeSwitch.textContent = registering ? '已有账号？登录' : '还没有账号？注册';
    password.autocomplete = registering ? 'new-password' : 'current-password';
    password.value = '';
    confirmPassword.value = '';
    confirmPassword.required = registering;
    confirmPassword.disabled = !registering;
    confirmField.hidden = !registering;
    password.focus();
  });
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (submit.disabled || !form.reportValidity()) return;
    clearError();
    if (registering && password.value !== confirmPassword.value) {
      errorText.textContent = '两次输入的密码不一致，请重新输入。';
      errorText.hidden = false;
      confirmPassword.focus();
      return;
    }
    submit.disabled = true;
    if (modeSwitch) modeSwitch.disabled = true;
    submit.textContent = registering ? '正在创建账号…' : '正在登录…';
    try {
      const data = await api(`/api/auth/${registering ? 'register' : 'login'}`, {
        method: 'POST',
        body: { username: form.elements.username.value.trim(), password: password.value, next: form.dataset.next || '/' },
      });
      if (data.csrf_token) document.querySelector('meta[name="csrf-token"]').content = data.csrf_token;
      password.value = '';
      confirmPassword.value = '';
      let destination = '/';
      if (typeof data.next === 'string' && data.next.startsWith('/') && !data.next.startsWith('//') && !/[\\\u0000-\u001f\u007f]/.test(data.next)) destination = data.next;
      location.assign(destination);
    } catch (error) {
      errorText.textContent = error.message;
      errorText.hidden = false;
      password.focus();
    } finally {
      submit.disabled = false;
      if (modeSwitch) modeSwitch.disabled = false;
      submit.textContent = registering ? '注册并登录' : '登录';
    }
  });
})();
