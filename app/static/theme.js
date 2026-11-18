/* Confih — Single signature dark theme ("Acoustic Noir") guardian. */
(function () {
  'use strict';

  var root = document.documentElement;

  function applyDark() {
    root.setAttribute('data-theme', 'dark');
    var meta = document.getElementById('theme-color-meta');
    if (meta) meta.setAttribute('content', '#09090b');
    try {
      localStorage.setItem('bc-theme', 'dark');
    } catch (e) {
      /* no storage */
    }
    if (window.ConfihSpatial && typeof window.ConfihSpatial.setTheme === 'function') {
      window.ConfihSpatial.setTheme('dark');
    }
    return 'dark';
  }

  applyDark();

  window.ConfihTheme = {
    get: function () { return 'dark'; },
    set: applyDark,
    sync: applyDark,
  };
})();
