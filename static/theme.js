// Applied before first paint (loaded synchronously in <head>) so the page
// never flashes the wrong theme. script.js handles the toggle button.
(() => {
  let theme = null;
  try {
    theme = localStorage.getItem('eng2sql_theme');
  } catch (e) {
    // Storage unavailable — fall back to the system preference.
  }
  if (theme !== 'light' && theme !== 'dark') {
    theme = window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  }
  document.documentElement.dataset.theme = theme;
})();
