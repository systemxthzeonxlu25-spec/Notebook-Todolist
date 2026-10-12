(() => {
  'use strict';
  const form = document.getElementById('appearance-form');
  if (!form || !window.Appearance) return;
  function sync() {
    form.querySelectorAll('input[name="ui_style"]').forEach(input => { input.checked = input.value === Appearance.style(); });
    form.querySelectorAll('input[name="color_mode"]').forEach(input => { input.checked = input.value === Appearance.mode(); });
  }
  form.addEventListener('change', event => {
    const input = event.target;
    if (input.name === 'ui_style') Appearance.setStyle(input.value);
    if (input.name === 'color_mode') Appearance.setMode(input.value);
  });
  form.addEventListener('submit', event => event.preventDefault());
  document.addEventListener('appearance:change', sync);
  sync();
})();
