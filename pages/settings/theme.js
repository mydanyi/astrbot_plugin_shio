
(function () {
  var STYLES = ['native', 'mist', 'midnight', 'cream'];
  var root = document.documentElement;
  var group = document.querySelector('.visual-options');
  var buttons = group ? group.querySelectorAll('.visual-option') : [];
  var sel = document.getElementById('visualStyleSelect');
  var themeBtn = document.getElementById('themeBtn');
  var params = new URLSearchParams(location.search);
  var requested = params.get('style');
  var explicitStyle = STYLES.indexOf(requested) !== -1 ? requested : null;
  var userChose = false;
  function applyMode(mode) {
    root.setAttribute('data-theme', mode);
    if (themeBtn) {
      themeBtn.setAttribute('aria-pressed', mode === 'dark' ? 'true' : 'false');
      themeBtn.textContent = mode === 'dark' ? '浅色模式' : '深色模式';
    }
  }
  function applyStyle(style) {
    if (STYLES.indexOf(style) === -1) { style = 'native'; }
    root.setAttribute('data-visual-style', style);
    applyMode(style === 'midnight' ? 'dark' : 'light');
    for (var i = 0; i < buttons.length; i++) {
      buttons[i].setAttribute('aria-pressed', buttons[i].getAttribute('data-style') === style ? 'true' : 'false');
    }
    if (sel) { sel.value = style; }
  }
  if (group) {
    group.addEventListener('click', function (event) {
      var option = event.target.closest ? event.target.closest('.visual-option') : null;
      if (!option || !group.contains(option)) { return; }
      userChose = true;
      applyStyle(option.getAttribute('data-style'));
    });
  }
  if (sel) {
    sel.addEventListener('change', function () {
      userChose = true;
      applyStyle(sel.value);
    });
  }
  applyStyle(explicitStyle || 'native');
  var bridge = window.AstrBotPluginPage;
  if (!explicitStyle && bridge && typeof bridge.ready === 'function') {
    bridge.ready().then(function (ctx) {
      if (userChose || !ctx || !ctx.isDark) { return; }
      applyMode('dark');
    }).catch(function () {});
  }
})();

