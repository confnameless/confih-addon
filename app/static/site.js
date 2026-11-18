/* ============================================================================
   Confih — shared behaviour & client-side QR utility
   ----------------------------------------------------------------------------
   Progressive enhancement only. Nothing here is required for the page to be
   readable: if this file fails to load, content stays visible and controls
   stay usable. Every effect has a prefers-reduced-motion off switch.

   Exports: window.ConfihQR (zero-network ISO/IEC 18004 SVG QR generator).
   Layers: reveals, accordions, sweep & 3D tilt, magnetic buttons, sliding nav
   indicator, number tickers, scroll rail, meter build.
   ========================================================================= */
(function () {
  'use strict';

  var reduceMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  var finePointer = window.matchMedia('(pointer: fine)');

  function reduced() {
    return reduceMotion.matches;
  }

  /* ── 0. Client-side ISO/IEC 18004 QR Code Generator (ECC Level M) ─────── */
  /* Generates crisp SVG QR codes in-browser so addon URLs and wallet addresses
     never leave the visitor's device via a third-party QR image service. */
  window.ConfihQR = (function buildConfihQR() {
    // Version 1..10 ECC-M: [ecPerBlock, g1Blocks, g1DataCw, g2Blocks, g2DataCw, alignCoords]
    var VERSIONS = [
      null,
      [10, 1, 16, 0, 0, []],
      [16, 1, 28, 0, 0, [6, 18]],
      [26, 1, 44, 0, 0, [6, 22]],
      [18, 2, 32, 0, 0, [6, 26]],
      [24, 2, 43, 0, 0, [6, 30]],
      [16, 4, 27, 0, 0, [6, 34]],
      [18, 4, 31, 0, 0, [6, 22, 38]],
      [22, 2, 38, 2, 39, [6, 24, 42]],
      [22, 3, 36, 2, 37, [6, 26, 46]],
      [26, 4, 43, 1, 44, [6, 28, 50]],
    ];

    var EXP = new Uint8Array(512);
    var LOG = new Uint8Array(256);
    (function initGF() {
      var x = 1;
      for (var i = 0; i < 255; i++) {
        EXP[i] = x;
        EXP[i + 255] = x;
        LOG[x] = i;
        x = (x << 1) ^ (x & 128 ? 0x11d : 0);
      }
    })();

    function gfMul(a, b) {
      return a === 0 || b === 0 ? 0 : EXP[LOG[a] + LOG[b]];
    }

    function rsRemainder(data, degree) {
      var gen = [1];
      for (var g = 0; g < degree; g++) {
        var next = new Array(gen.length + 1).fill(0);
        var root = EXP[g];
        for (var j = 0; j < gen.length; j++) {
          next[j] ^= gen[j];
          next[j + 1] ^= gfMul(gen[j], root);
        }
        gen = next;
      }
      var rem = new Uint8Array(degree);
      for (var i = 0; i < data.length; i++) {
        var factor = data[i] ^ rem[0];
        for (var r = 0; r < degree - 1; r++) rem[r] = rem[r + 1];
        rem[degree - 1] = 0;
        if (factor !== 0) {
          for (var k = 0; k < degree; k++) rem[k] ^= gfMul(gen[k + 1], factor);
        }
      }
      return rem;
    }

    function maskBit(mask, r, c) {
      switch (mask) {
        case 0: return (r + c) % 2 === 0;
        case 1: return r % 2 === 0;
        case 2: return c % 3 === 0;
        case 3: return (r + c) % 3 === 0;
        case 4: return (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0;
        case 5: return ((r * c) % 2) + ((r * c) % 3) === 0;
        case 6: return (((r * c) % 2) + ((r * c) % 3)) % 2 === 0;
        default: return (((r + c) % 2) + ((r * c) % 3)) % 2 === 0;
      }
    }

    function drawFormat(grid, size, mask) {
      var data = mask & 7; // ECC Level M = 00
      var rem = data;
      for (var i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
      var bits = ((data << 10) | rem) ^ 0x5412;

      for (var b = 0; b <= 5; b++) grid[b][8] = (bits >>> b) & 1;
      grid[7][8] = (bits >>> 6) & 1;
      grid[8][8] = (bits >>> 7) & 1;
      grid[8][7] = (bits >>> 8) & 1;
      for (var b2 = 9; b2 < 15; b2++) grid[8][14 - b2] = (bits >>> b2) & 1;

      for (var tr = 0; tr < 8; tr++) grid[8][size - 1 - tr] = (bits >>> tr) & 1;
      for (var bl = 8; bl < 15; bl++) grid[size - 15 + bl][8] = (bits >>> bl) & 1;
    }

    function penalty(grid, size) {
      var score = 0;
      for (var r = 0; r < size; r++) {
        var runR = 1;
        var runC = 1;
        for (var c = 1; c < size; c++) {
          if (grid[r][c] === grid[r][c - 1]) {
            runR++;
            if (runR === 5) score += 3;
            else if (runR > 5) score += 1;
          } else runR = 1;

          if (grid[c][r] === grid[c - 1][r]) {
            runC++;
            if (runC === 5) score += 3;
            else if (runC > 5) score += 1;
          } else runC = 1;
        }
      }
      var dark = 0;
      for (var y = 0; y < size; y++) {
        for (var x = 0; x < size; x++) {
          if (grid[y][x]) dark++;
          if (y < size - 1 && x < size - 1) {
            var v = grid[y][x];
            if (v === grid[y][x + 1] && v === grid[y + 1][x] && v === grid[y + 1][x + 1]) {
              score += 3;
            }
          }
        }
      }
      var k = Math.ceil(Math.abs(dark * 20 - size * size * 10) / (size * size)) - 1;
      if (k > 0) score += k * 10;
      return score;
    }

    function utf8Bytes(str) {
      if (typeof TextEncoder !== 'undefined') return new TextEncoder().encode(str);
      var escaped = unescape(encodeURIComponent(str));
      var arr = new Uint8Array(escaped.length);
      for (var i = 0; i < escaped.length; i++) arr[i] = escaped.charCodeAt(i);
      return arr;
    }

    function encode(text) {
      if (!text) return null;
      var bytes = utf8Bytes(String(text));
      var ver = 0;
      for (var v = 1; v <= 10; v++) {
        var s = VERSIONS[v];
        var cap = s[1] * s[2] + s[3] * s[4];
        var cBits = v <= 9 ? 8 : 16;
        if (Math.ceil((4 + cBits + bytes.length * 8) / 8) <= cap) {
          ver = v;
          break;
        }
      }
      if (!ver) return null;

      var info = VERSIONS[ver];
      var ecLen = info[0];
      var g1B = info[1];
      var g1D = info[2];
      var g2B = info[3];
      var g2D = info[4];
      var totalDataCw = g1B * g1D + g2B * g2D;
      var bits = [];
      function push(val, len) {
        for (var i = len - 1; i >= 0; i--) bits.push((val >>> i) & 1);
      }
      push(4, 4);
      push(bytes.length, ver <= 9 ? 8 : 16);
      for (var bi = 0; bi < bytes.length; bi++) push(bytes[bi], 8);
      push(0, Math.min(4, totalDataCw * 8 - bits.length));
      while (bits.length & 7) bits.push(0);

      var dataCw = new Uint8Array(totalDataCw);
      for (var ci = 0; ci < bits.length >>> 3; ci++) {
        var w = 0;
        for (var q = 0; q < 8; q++) w = (w << 1) | bits[ci * 8 + q];
        dataCw[ci] = w;
      }
      for (var p = 0, pi = bits.length >>> 3; pi < totalDataCw; p++, pi++) {
        dataCw[pi] = p & 1 ? 0x11 : 0xec;
      }

      var dBlocks = [];
      var eBlocks = [];
      var off = 0;
      for (var b1 = 0; b1 < g1B; b1++) {
        var s1 = dataCw.subarray(off, off + g1D);
        off += g1D;
        dBlocks.push(s1);
        eBlocks.push(rsRemainder(s1, ecLen));
      }
      for (var b2 = 0; b2 < g2B; b2++) {
        var s2 = dataCw.subarray(off, off + g2D);
        off += g2D;
        dBlocks.push(s2);
        eBlocks.push(rsRemainder(s2, ecLen));
      }

      var finalCw = [];
      var maxD = Math.max(g1D, g2D);
      for (var col = 0; col < maxD; col++) {
        for (var row = 0; row < dBlocks.length; row++) {
          if (col < dBlocks[row].length) finalCw.push(dBlocks[row][col]);
        }
      }
      for (var ec = 0; ec < ecLen; ec++) {
        for (var er = 0; er < eBlocks.length; er++) finalCw.push(eBlocks[er][ec]);
      }

      var size = 17 + ver * 4;
      var base = [];
      var isFunc = [];
      for (var r = 0; r < size; r++) {
        base.push(new Uint8Array(size));
        isFunc.push(new Uint8Array(size));
      }
      function setF(r, c, dark) {
        if (r < 0 || r >= size || c < 0 || c >= size) return;
        base[r][c] = dark ? 1 : 0;
        isFunc[r][c] = 1;
      }
      function finder(tr, lc) {
        for (var dy = -1; dy <= 7; dy++) {
          for (var dx = -1; dx <= 7; dx++) {
            var d = Math.max(Math.abs(dx - 3), Math.abs(dy - 3));
            setF(tr + dy, lc + dx, d !== 2 && d !== 4);
          }
        }
      }
      finder(0, 0);
      finder(0, size - 7);
      finder(size - 7, 0);

      for (var t = 8; t < size - 8; t++) {
        setF(6, t, (t & 1) === 0);
        setF(t, 6, (t & 1) === 0);
      }

      var align = info[5];
      for (var ai = 0; ai < align.length; ai++) {
        for (var aj = 0; aj < align.length; aj++) {
          if (
            (ai === 0 && aj === 0) ||
            (ai === 0 && aj === align.length - 1) ||
            (ai === align.length - 1 && aj === 0)
          ) continue;
          for (var ay = -2; ay <= 2; ay++) {
            for (var ax = -2; ax <= 2; ax++) {
              setF(align[ai] + ay, align[aj] + ax, Math.max(Math.abs(ax), Math.abs(ay)) !== 1);
            }
          }
        }
      }

      setF(size - 8, 8, true);
      for (var fi = 0; fi <= 8; fi++) {
        if (fi !== 6) {
          if (!isFunc[8][fi]) setF(8, fi, false);
          if (!isFunc[fi][8]) setF(fi, 8, false);
        }
      }
      for (var fj = 0; fj < 8; fj++) {
        if (!isFunc[8][size - 1 - fj]) setF(8, size - 1 - fj, false);
        if (fj < 7 && !isFunc[size - 1 - fj][8]) setF(size - 1 - fj, 8, false);
      }

      if (ver >= 7) {
        var vRem = ver;
        for (var vi = 0; vi < 12; vi++) vRem = (vRem << 1) ^ ((vRem >>> 11) * 0x1f25);
        var vBits = (ver << 12) | vRem;
        for (var vb = 0; vb < 18; vb++) {
          var vd = ((vBits >>> vb) & 1) === 1;
          var va = size - 11 + (vb % 3);
          var vc = Math.floor(vb / 3);
          setF(va, vc, vd);
          setF(vc, va, vd);
        }
      }

      var bitIdx = 0;
      var totalBits = finalCw.length * 8;
      var upward = true;
      for (var right = size - 1; right >= 1; right -= 2) {
        if (right === 6) right = 5;
        for (var vert = 0; vert < size; vert++) {
          var y = upward ? size - 1 - vert : vert;
          for (var lr = 0; lr < 2; lr++) {
            var x = right - lr;
            if (!isFunc[y][x]) {
              var db = 0;
              if (bitIdx < totalBits) {
                db = (finalCw[bitIdx >>> 3] >>> (7 - (bitIdx & 7))) & 1;
                bitIdx++;
              }
              base[y][x] = db;
            }
          }
        }
        upward = !upward;
      }

      var bestGrid = null;
      var bestScore = Infinity;
      for (var m = 0; m < 8; m++) {
        var grid = base.map(function (row) { return new Uint8Array(row); });
        for (var mr = 0; mr < size; mr++) {
          for (var mc = 0; mc < size; mc++) {
            if (!isFunc[mr][mc] && maskBit(m, mr, mc)) grid[mr][mc] ^= 1;
          }
        }
        drawFormat(grid, size, m);
        var sc = penalty(grid, size);
        if (sc < bestScore) {
          bestScore = sc;
          bestGrid = grid;
        }
      }
      return { size: size, grid: bestGrid };
    }

    function svg(text) {
      var res = encode(text);
      if (!res) return null;
      var margin = 4;
      var dim = res.size + margin * 2;
      var path = [];
      for (var y = 0; y < res.size; y++) {
        for (var x = 0; x < res.size; x++) {
          if (res.grid[y][x]) path.push('M' + (x + margin) + ',' + (y + margin) + 'h1v1h-1z');
        }
      }
      return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ' +
        dim + ' ' + dim +
        '" shape-rendering="crispEdges"><rect width="' +
        dim + '" height="' + dim +
        '" fill="#ffffff"/><path d="' +
        path.join('') +
        '" fill="#0b0c0e"/></svg>'
      );
    }

    return { svg: svg };
  })();

  /* ── 1. Zero-Pop-In Physical Reveals ──────────────────────────────────── */
  /* Content never drops to opacity:0 or pops in during scroll. Items gently
     settle in physical space at full opacity:1, staying instant on fast
     scroll, upward scroll, view switches, or prefers-reduced-motion. */
  (function reveals() {
    var observer = null;
    var FAST_SCROLL = 0.85; // px/ms

    var lastY = window.scrollY || 0;
    var lastAt = performance.now();
    var velocity = 0;
    var goingUp = false;

    function measure() {
      var y = window.scrollY || 0;
      var now = performance.now();
      var dt = Math.max(1, now - lastAt);
      velocity = Math.abs(y - lastY) / dt;
      if (y !== lastY) goingUp = y < lastY;
      lastY = y;
      lastAt = now;
    }

    window.addEventListener('scroll', measure, { passive: true, capture: true });

    function showStatic(el) {
      el.style.setProperty('--reveal-delay', '0ms');
      el.classList.remove('is-above');
      el.classList.add('is-static', 'is-visible');
    }

    function showSettle(el) {
      if (reduced()) {
        showStatic(el);
        return;
      }
      el.style.setProperty('--reveal-delay', '0ms');
      el.classList.add('is-above');
      var done = false;
      function finish() {
        if (done) return;
        done = true;
        el.removeEventListener('animationend', finish);
        showStatic(el);
      }
      el.addEventListener('animationend', finish);
      window.setTimeout(finish, 400);
    }

    function ensureObserver() {
      if (observer) return observer;
      observer = new IntersectionObserver(
        function (entries) {
          entries.forEach(function (entry) {
            if (!entry.isIntersecting) return;
            var el = entry.target;
            observer.unobserve(el);
            if (reduced() || velocity > FAST_SCROLL || goingUp) {
              showStatic(el);
            } else {
              showSettle(el);
            }
          });
        },
        { rootMargin: '140px 0px 140px 0px', threshold: 0.01 }
      );
      return observer;
    }

    var bootLockedOnce = false;

    function lockBootStatic() {
      ['status-rack-3d', 'spatial-inspect-readout'].forEach(function (id) {
        var node = document.getElementById(id);
        if (node) node.classList.add('is-static');
      });
    }

    function scan() {
      var items = Array.prototype.slice.call(
        document.querySelectorAll('.rise:not([data-reveal-seen])')
      );
      if (!items.length) return;

      var viewH = window.innerHeight || document.documentElement.clientHeight || 800;

      if (!bootLockedOnce) {
        bootLockedOnce = true;
        if (!('IntersectionObserver' in window) || reduced()) {
          lockBootStatic();
        } else {
          window.setTimeout(lockBootStatic, 640);
        }
      }

      items.forEach(function (el) {
        if (el.closest('[hidden], .is-leaving')) return;
        el.setAttribute('data-reveal-seen', '1');
        if (!('IntersectionObserver' in window) || reduced()) {
          showStatic(el);
          return;
        }
        var rect = el.getBoundingClientRect();
        if (rect.bottom <= 0) {
          showStatic(el);
          return;
        }
        if (rect.top < viewH * 1.04) {
          window.setTimeout(function () {
            showStatic(el);
          }, 640);
          return;
        }
        ensureObserver().observe(el);
      });
    }

    var routeLockTimer = 0;

    function onViewChange(event) {
      var view = event && event.detail && event.detail.view;
      var dir = (event && event.detail && event.detail.direction) || 'forward';
      var activePane = view
        ? document.querySelector('.view-panel[data-view="' + view + '"]')
        : null;
      if (!activePane) activePane = document.querySelector('.view-panel:not([hidden])');
      if (!activePane) return;

      if (reduced()) {
        lockBootStatic();
        Array.prototype.forEach.call(activePane.querySelectorAll('.rise'), function (el) {
          el.setAttribute('data-reveal-seen', '1');
          showStatic(el);
        });
        scan();
        return;
      }

      if (routeLockTimer) window.clearTimeout(routeLockTimer);
      var viewH = window.innerHeight || document.documentElement.clientHeight || 800;
      var animatedItems = [];

      activePane.setAttribute('data-route-dir', dir);
      activePane.classList.remove('is-route-cascade');

      if (view === 'status') {
        ['status-rack-3d', 'spatial-inspect-readout'].forEach(function (id) {
          var node = document.getElementById(id);
          if (node) node.classList.remove('is-static');
        });
      } else {
        lockBootStatic();
      }

      Array.prototype.forEach.call(activePane.querySelectorAll('.rise'), function (el) {
        el.setAttribute('data-reveal-seen', '1');
        var rect = el.getBoundingClientRect();
        if (rect.bottom > 0 && rect.top < viewH * 1.1) {
          el.classList.remove('is-static', 'is-above');
          animatedItems.push(el);
        } else if (rect.bottom <= 0) {
          showStatic(el);
        } else {
          el.classList.remove('is-static', 'is-above');
          ensureObserver().observe(el);
        }
      });

      void activePane.offsetWidth;
      activePane.classList.add('is-route-cascade');

      routeLockTimer = window.setTimeout(function () {
        activePane.classList.remove('is-route-cascade');
        activePane.removeAttribute('data-route-dir');
        lockBootStatic();
        animatedItems.forEach(showStatic);
      }, 620);

      scan();
    }

    function onScrollTop(event) {
      if (reduced()) return;
      var view = event && event.detail && event.detail.view;
      var activePane = view
        ? document.querySelector('.view-panel[data-view="' + view + '"]')
        : document.querySelector('.view-panel:not([hidden])');
      if (!activePane) return;
      var hero = activePane.querySelector('.hero, .page-hero');
      if (!hero) return;
      hero.classList.remove('is-top-pulse');
      void hero.offsetWidth;
      hero.classList.add('is-top-pulse');
      window.setTimeout(function () {
        hero.classList.remove('is-top-pulse');
      }, 520);
    }

    scan();
    document.addEventListener('confih:content', scan);
    document.addEventListener('confih:viewchange', onViewChange);
    document.addEventListener('confih:viewsettled', scan);
    document.addEventListener('confih:scrolltop', onScrollTop);

    if ('MutationObserver' in window) {
      var pending = false;
      new MutationObserver(function () {
        if (pending) return;
        pending = true;
        requestAnimationFrame(function () {
          pending = false;
          scan();
        });
      }).observe(document.body, { childList: true, subtree: true });
    }

    if (reduceMotion.addEventListener) {
      reduceMotion.addEventListener('change', function (event) {
        if (event.matches) {
          lockBootStatic();
          Array.prototype.forEach.call(document.querySelectorAll('.rise'), showStatic);
        }
      });
    }
  })();

  /* ── 2. Smooth <details> disclosures (delegated) ──────────────────────── */
  (function accordions() {
    if (!('Element' in window) || !Element.prototype.animate) return;
    var anims = new WeakMap();
    var SEL = 'details.faq-item > summary, details.tech > summary, details.data-table > summary, details.metric-defs > summary';

    document.addEventListener('click', function (event) {
      var summary = event.target && event.target.closest ? event.target.closest(SEL) : null;
      if (!summary) return;
      var details = summary.parentElement;
      if (!details || details.tagName !== 'DETAILS') return;
      if (reduced()) return;

      event.preventDefault();
      var prev = anims.get(details);
      if (prev) {
        prev.cancel();
        anims.delete(details);
      }

      var startHeight = details.offsetHeight;
      var isClosing = details.open;
      var anim;

      if (isClosing) {
        var endHeight = summary.offsetHeight;
        details.style.overflow = 'hidden';
        anim = details.animate(
          [{ height: startHeight + 'px' }, { height: endHeight + 'px' }],
          { duration: 220, easing: 'cubic-bezier(0.16, 1, 0.3, 1)' }
        );
        anim.onfinish = function () {
          details.open = false;
          details.style.height = '';
          details.style.overflow = '';
          anims.delete(details);
        };
        anim.oncancel = function () {
          details.style.height = '';
          details.style.overflow = '';
        };
      } else {
        details.style.height = startHeight + 'px';
        details.style.overflow = 'hidden';
        details.open = true;
        var targetHeight = details.scrollHeight;
        anim = details.animate(
          [{ height: startHeight + 'px' }, { height: targetHeight + 'px' }],
          { duration: 260, easing: 'cubic-bezier(0.16, 1, 0.3, 1)' }
        );
        anim.onfinish = function () {
          details.style.height = '';
          details.style.overflow = '';
          anims.delete(details);
        };
        anim.oncancel = function () {
          details.style.height = '';
          details.style.overflow = '';
        };
      }
      anims.set(details, anim);
    });
  })();

  /* ── 3. Specular sweep & standalone 3D pointer tilt fallback ──────────── */
  (function sweepAndTilt() {
    var sweepSel = '.card-link, .hero-plate';
    var tiltSel = '.login-card, .panel, .hero-plate, .card-link, [data-tilt]';
    var desktopTiltMedia = window.matchMedia('(pointer: fine) and (min-width: 769px)');
    var tiltBound = new WeakSet();

    function clearTiltVars(el) {
      el.style.removeProperty('--rx');
      el.style.removeProperty('--ry');
      el.style.removeProperty('--spot-x');
      el.style.removeProperty('--spot-y');
      el.style.removeProperty('--tilt-active');
      el.style.removeProperty('--tilt-x');
      el.style.removeProperty('--tilt-y');
      el.style.removeProperty('--tilt-z');
    }

    function scan() {
      // Contact addresses are written as user + domain so no host (Cloudflare
      // included) rewrites them into a script-dependent link. Upgrade each one
      // to a real mailto: here; without JS the text is still readable.
      Array.prototype.forEach.call(document.querySelectorAll('[data-mail]'), function (link) {
        var parts = String(link.getAttribute('data-mail') || '').split('|');
        if (parts.length !== 2 || !parts[0] || !parts[1]) return;
        link.setAttribute('href', 'mailto:' + parts[0] + '@' + parts[1]);
      });

      document.querySelectorAll(sweepSel).forEach(function (el) {
        el.classList.add('sweep');
      });
      document.querySelectorAll('.hero-plate').forEach(function (el) {
        el.classList.add('tilt-plate');
      });

      // Public shell pages delegate surface sheen to window.ConfihSpatial; standalone
      // pages (such as /admin) get subtle crisp radial sheen without 3D text blur.
      if (window.ConfihSpatial) return;

      document.querySelectorAll(tiltSel).forEach(function (el) {
        if (tiltBound.has(el)) return;
        tiltBound.add(el);
        el.addEventListener('pointermove', function (e) {
          if (!desktopTiltMedia.matches || reduced()) return;
          var r = el.getBoundingClientRect();
          if (r.width < 16 || r.height < 16) return;
          var px = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
          var py = Math.max(0, Math.min(1, (e.clientY - r.top) / r.height));
          el.style.setProperty('--spot-x', (px * 100).toFixed(1) + '%');
          el.style.setProperty('--spot-y', (py * 100).toFixed(1) + '%');
          el.style.setProperty('--tilt-active', '1');
        });
        el.addEventListener('pointerleave', function () {
          clearTiltVars(el);
        });
      });
    }

    scan();
    document.addEventListener('confih:content', scan);
    document.addEventListener('confih:viewchange', scan);

    if (reduceMotion.addEventListener) {
      reduceMotion.addEventListener('change', function (ev) {
        if (ev.matches && !window.ConfihSpatial) {
          document.querySelectorAll(tiltSel).forEach(clearTiltVars);
        }
      });
    }
  })();

  /* ── 3b. View bundles ─────────────────────────────────────────────────
     plays.js is 40 KB that only the plays view runs. It is prefetched at idle
     everywhere else and injected the first time the view is opened, so the
     first paint of every other page never pays for it. */
  (function viewBundles() {
    var loaded = {};

    function bootField(name) {
      var el = document.getElementById('confih-boot');
      if (!el || !el.textContent) return '';
      try {
        var boot = JSON.parse(el.textContent);
        return boot && boot[name] ? String(boot[name]) : '';
      } catch (e) {
        return '';
      }
    }

    function ensure(bundle) {
      if (loaded[bundle] || document.querySelector('script[data-confih-bundle="' + bundle + '"]')) {
        return;
      }
      loaded[bundle] = true;
      var script = document.createElement('script');
      var version = bootField('asset_version');
      script.src = '/assets/' + bundle + '.js' + (version ? '?v=' + version : '');
      script.setAttribute('data-confih-bundle', bundle);
      script.defer = true;
      document.head.appendChild(script);
    }

    document.addEventListener('confih:viewchange', function (ev) {
      var view = ev && ev.detail && ev.detail.view;
      if (view === 'plays') ensure('plays');
    });

    /* Ask for the bundle the moment a visitor shows intent, not before: a
       hover or a focus on Plays is a reliable bet, and the click then lands
       on a panel whose data is already in flight. */
    var intent = document.querySelector('.site-nav a[data-nav-view="plays"]');
    if (intent) {
      ['pointerenter', 'focus', 'touchstart'].forEach(function (type) {
        intent.addEventListener(type, function () { ensure('plays'); }, { passive: true });
      });
    }
  })();

  /* ── 4. Magnetic buttons (yields on press for crisp 3D keycap detent) ─── */
  (function magnetic() {
    if (!finePointer.matches) return;
    var bound = new WeakSet();

    var raf = 0;
    var active = null;
    var pressed = false;
    var target = { x: 0, y: 0 };
    var current = { x: 0, y: 0 };

    function loop() {
      current.x += (target.x - current.x) * 0.26;
      current.y += (target.y - current.y) * 0.26;
      if (active && !pressed) {
        active.style.setProperty('--mx', current.x.toFixed(2) + 'px');
        active.style.setProperty('--my', current.y.toFixed(2) + 'px');
      }
      if (Math.abs(target.x - current.x) > 0.04 || Math.abs(target.y - current.y) > 0.04) {
        raf = requestAnimationFrame(loop);
      } else {
        if (active && target.x === 0 && target.y === 0) {
          active.style.removeProperty('--mx');
          active.style.removeProperty('--my');
        }
        raf = 0;
      }
    }

    function start(btn) {
      if (reduced() || btn.disabled) return;
      if (active && active !== btn) {
        active.classList.remove('magnetic');
        active.style.removeProperty('--mx');
        active.style.removeProperty('--my');
      }
      active = btn;
      pressed = false;
      current.x = 0;
      current.y = 0;
      btn.classList.add('magnetic');
    }

    function move(event) {
      var btn = event.currentTarget;
      if (reduced() || btn.disabled || pressed) return;
      if (active !== btn) start(btn);
      var rect = btn.getBoundingClientRect();
      var dx = event.clientX - (rect.left + rect.width / 2);
      var dy = event.clientY - (rect.top + rect.height / 2);
      target.x = Math.max(-2.2, Math.min(2.2, dx * 0.09));
      target.y = Math.max(-1.6, Math.min(1.6, dy * 0.12));
      if (!raf) raf = requestAnimationFrame(loop);
    }

    function pressDown(event) {
      var btn = event.currentTarget;
      pressed = true;
      target.x = 0;
      target.y = 0;
      current.x = 0;
      current.y = 0;
      btn.classList.remove('magnetic');
      btn.style.removeProperty('--mx');
      btn.style.removeProperty('--my');
    }

    function pressUp(event) {
      var btn = event.currentTarget;
      pressed = false;
      if (!reduced() && !btn.disabled && active === btn) {
        btn.classList.add('magnetic');
      }
    }

    function leave(event) {
      var btn = event.currentTarget;
      pressed = false;
      btn.classList.remove('magnetic');
      btn.style.removeProperty('--mx');
      btn.style.removeProperty('--my');
      if (active === btn) {
        active = null;
        target.x = 0;
        target.y = 0;
      }
    }

    function scanMagnetic() {
      document.querySelectorAll('.button.primary, .button.secondary').forEach(function (btn) {
        if (bound.has(btn) || btn.closest('[hidden]') || btn.closest('.connect-actions')) return;
        bound.add(btn);
        btn.addEventListener('pointerenter', function () { start(btn); });
        btn.addEventListener('pointermove', move);
        btn.addEventListener('pointerdown', pressDown);
        btn.addEventListener('pointerup', pressUp);
        btn.addEventListener('pointercancel', leave);
        btn.addEventListener('pointerleave', leave);
      });
    }

    scanMagnetic();
    document.addEventListener('confih:content', scanMagnetic);
    document.addEventListener('confih:viewchange', scanMagnetic);
  })();

  /* ── 5. Sliding nav indicator ─────────────────────────────────────────── */
  (function navIndicator() {
    var nav = document.querySelector('.site-nav');
    if (!nav) return;
    var links = Array.prototype.slice.call(nav.querySelectorAll('a'));
    if (!links.length) return;

    var indicator = document.createElement('span');
    indicator.className = 'nav-indicator';
    indicator.setAttribute('aria-hidden', 'true');
    nav.appendChild(indicator);
    nav.classList.add('has-indicator');

    function getCurrent() {
      return nav.querySelector('a[aria-current="page"]') || links[0];
    }

    function moveTo(link, animate) {
      if (!link) return;
      var navRect = nav.getBoundingClientRect();
      var rect = link.getBoundingClientRect();
      var width = Math.max(24, rect.width);
      var height = Math.max(28, rect.height);
      var x = rect.left - navRect.left + nav.scrollLeft;
      var y = rect.top - navRect.top;

      if (!animate) indicator.style.transition = 'none';
      indicator.style.width = width.toFixed(1) + 'px';
      indicator.style.height = height.toFixed(1) + 'px';
      indicator.style.setProperty('--nav-x', x.toFixed(1) + 'px');
      indicator.style.setProperty('--nav-y', y.toFixed(1) + 'px');
      indicator.style.opacity = '1';
      if (!animate) {
        void indicator.offsetWidth;
        indicator.style.transition = '';
      }
    }

    function settle() {
      moveTo(getCurrent(), true);
    }

    requestAnimationFrame(function () {
      moveTo(getCurrent(), false);
    });

    links.forEach(function (link) {
      link.addEventListener('pointerenter', function () { moveTo(link, true); });
      link.addEventListener('focus', function () { moveTo(link, true); });
    });

    nav.addEventListener('pointerleave', settle);
    nav.addEventListener('focusout', function (event) {
      if (!nav.contains(event.relatedTarget)) settle();
    });

    window.addEventListener('resize', function () {
      moveTo(getCurrent(), false);
    });

    document.addEventListener('confih:viewchange', function () {
      moveTo(getCurrent(), !reduced());
    });

    if (document.fonts && document.fonts.ready) {
      document.fonts.ready.then(function () {
        moveTo(getCurrent(), false);
      });
    }
  })();

  /* ── 6. Number tickers (hidden-pane safe) ─────────────────────────────── */
  (function tickers() {
    var SEL =
      '.metric-value, .plays-stat-value, #success-rate, .provider-stat dd, .readout-value, .count, .quality-legend b';
    var NUM = /^(-?[\d,]+(?:\.\d+)?)(.*)$/s;

    function parts(text) {
      var trimmed = String(text).trim();
      var match = NUM.exec(trimmed);
      if (!match) return null;
      var raw = match[1].replace(/,/g, '');
      if (raw === '' || raw === '-') return null;
      var value = Number(raw);
      if (!isFinite(value)) return null;
      return { value: value, sample: match[1], suffix: match[2] };
    }

    function format(value, sample) {
      var dotIdx = sample.indexOf('.');
      var decimals = dotIdx >= 0 ? sample.slice(dotIdx + 1).replace(/[^\d]/g, '').length : 0;
      var out = decimals ? value.toFixed(decimals) : String(Math.round(value));
      if (sample.indexOf(',') >= 0) {
        var chunks = out.split('.');
        chunks[0] = chunks[0].replace(/\B(?=(\d{3})+(?!\d))/g, ',');
        out = chunks.join('.');
      }
      return out;
    }

    function animate(el, from, to, suffix) {
      var duration = 480;
      var startTime = null;
      el.classList.add('ticking');
      function frame(now) {
        if (startTime === null) startTime = now;
        var t = Math.min(1, (now - startTime) / duration);
        var eased = 1 - Math.pow(1 - t, 3);
        var value = from + (to - from) * eased;
        el.textContent = format(value, el.dataset.tickSample || String(to)) + suffix;
        if (t < 1) {
          requestAnimationFrame(frame);
        } else {
          el.textContent = el.dataset.tickFinal;
          el.classList.remove('ticking');
          delete el.dataset.tickSample;
          delete el.dataset.tickFinal;
          el.dataset.tickValue = String(to);
        }
      }
      requestAnimationFrame(frame);
    }

    function update(el) {
      var next = parts(el.textContent);
      if (!next) return;
      var prevRaw = el.dataset.tickValue;
      var prev = prevRaw === undefined || prevRaw === '' ? NaN : Number(prevRaw);
      el.dataset.tickValue = String(next.value);
      if (reduced() || el.closest('[hidden]')) return;
      if (!isFinite(prev) || prev === next.value) return;
      el.dataset.tickSample = next.sample;
      el.dataset.tickFinal = el.textContent;
      animate(el, prev, next.value, next.suffix);
    }

    function seedAll() {
      Array.prototype.forEach.call(document.querySelectorAll(SEL), function (el) {
        if (el.dataset.tickValue === undefined) {
          var p = parts(el.textContent);
          el.dataset.tickValue = p ? String(p.value) : '';
        }
      });
    }

    seedAll();
    document.addEventListener('confih:content', seedAll);

    if (!('MutationObserver' in window)) return;
    var observer = new MutationObserver(function (records) {
      records.forEach(function (record) {
        var node = record.target;
        var el = node.nodeType === 3 ? node.parentElement : node;
        if (!el || el.nodeType !== 1) return;
        if (el.dataset && el.dataset.tickFinal !== undefined) return;
        var hit = el.matches && el.matches(SEL) ? el : el.closest ? el.closest(SEL) : null;
        if (hit) update(hit);
      });
    });

    observer.observe(document.body, {
      subtree: true,
      childList: true,
      characterData: true,
    });
  })();

  /* ── 7. Scroll rail + dynamic view-aware section ticks ───────────────── */
  (function scrollRail() {
    var shell = document.querySelector('main .shell');
    if (!shell) return;

    var activeViewName = (document.body && document.body.dataset.initialView) || null;
    var lastSig = '';

    function activeSections() {
      var activePane = activeViewName
        ? document.querySelector('.view-panel[data-view="' + activeViewName + '"]')
        : null;
      if (!activePane) activePane = document.querySelector('.view-panel:not([hidden])');
      var container = activePane || shell;
      return Array.prototype.slice.call(container.querySelectorAll(':scope > section'));
    }

    function labelFor(section) {
      var labelled = section.getAttribute('aria-labelledby');
      if (labelled) {
        var el = document.getElementById(labelled.split(/\s+/)[0]);
        if (el && el.textContent.trim()) return el.textContent.trim();
      }
      var heading = section.querySelector('h1, h2');
      return heading ? heading.textContent.trim() : 'Section';
    }

    var rail = document.createElement('div');
    rail.className = 'scroll-rail';
    rail.setAttribute('aria-hidden', 'true');

    var track = document.createElement('div');
    track.className = 'scroll-rail-track';
    var fill = document.createElement('div');
    fill.className = 'scroll-rail-fill';
    track.appendChild(fill);
    rail.appendChild(track);

    var siteHead = document.querySelector('.site-head');
    if (siteHead) {
      siteHead.appendChild(rail);
    } else {
      document.body.appendChild(rail);
    }

    var ticks = document.createElement('div');
    ticks.className = 'scroll-rail-dots';
    ticks.setAttribute('aria-hidden', 'true');
    document.body.appendChild(ticks);

    var currentSections = [];
    var buttons = [];

    function rebuildTicks(event) {
      if (event && event.detail && event.detail.view) {
        activeViewName = event.detail.view;
      }
      var nextSections = activeSections();
      var nextLabels = nextSections.map(labelFor);
      var sig = (activeViewName || '') + '|' + nextSections.length + '|' + nextLabels.join('\u0000');
      if (sig === lastSig) {
        onScroll();
        return;
      }
      lastSig = sig;
      currentSections = nextSections;
      ticks.textContent = '';
      var hideNav = currentSections.length < 2;
      rail.hidden = hideNav;
      ticks.hidden = hideNav;
      buttons = currentSections.map(function (section, index) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'scroll-rail-tick';
        button.setAttribute('data-label', nextLabels[index]);
        button.setAttribute('aria-hidden', 'true');
        button.tabIndex = -1;
        button.addEventListener('click', function () {
          section.scrollIntoView({
            behavior: reduced() ? 'auto' : 'smooth',
            block: 'start',
          });
        });
        ticks.appendChild(button);
        return button;
      });
      update();
    }

    var scrollContainer = document.getElementById('scrollable-page') || document.scrollingElement;

    function maxScroll() {
      if (scrollContainer === document.scrollingElement) {
        return Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
      }
      return Math.max(0, scrollContainer.scrollHeight - scrollContainer.clientHeight);
    }

    function currentTop() {
      return scrollContainer === document.scrollingElement ? window.scrollY : scrollContainer.scrollTop;
    }

    var ticking = false;
    function update() {
      ticking = false;
      var max = maxScroll();
      var progress = max <= 0 ? 0 : Math.max(0, Math.min(1, currentTop() / max));
      fill.style.width = (progress * 100).toFixed(2) + '%';
      var live = progress > 0.015;
      rail.classList.toggle('is-live', live);
      ticks.classList.toggle('is-live', live);

      var cutoff = window.innerHeight * 0.35;
      var activeIndex = 0;
      currentSections.forEach(function (section, index) {
        if (section.getBoundingClientRect().top <= cutoff) activeIndex = index;
      });
      buttons.forEach(function (button, index) {
        button.classList.toggle('is-current', index === activeIndex);
      });
    }

    function onScroll() {
      if (!ticking) {
        ticking = true;
        requestAnimationFrame(update);
      }
    }

    window.addEventListener('scroll', onScroll, { passive: true });
    window.addEventListener('resize', onScroll);
    document.addEventListener('confih:content', rebuildTicks);
    document.addEventListener('confih:viewchange', rebuildTicks);
    document.addEventListener('confih:viewsettled', rebuildTicks);
    if (scrollContainer !== document.scrollingElement) {
      scrollContainer.addEventListener('scroll', onScroll, { passive: true });
    }
    rebuildTicks();
  })();

  /* ── 8. Hydration-aware scroll meters & telemetry cascades ────────────── */
  (function meterBuild() {
    if (!('IntersectionObserver' in window)) return;
    var built = new WeakSet();
    var observed = new WeakSet();

    function isReady(meter) {
      if (
        meter.classList.contains('bars') ||
        meter.classList.contains('bars-30d') ||
        meter.classList.contains('plays-chart') ||
        meter.classList.contains('incidents-list')
      ) {
        return meter.children.length > 0;
      }
      if (
        meter.classList.contains('metrics') ||
        meter.classList.contains('quality') ||
        meter.classList.contains('plays-strip')
      ) {
        return meter.getAttribute('data-hydrated') === '1';
      }
      return true;
    }

    function triggerBuild(meter) {
      if (built.has(meter)) return;
      built.add(meter);
      observer.unobserve(meter);
      observed.delete(meter);
      if (reduced()) return;
      if (
        meter.classList.contains('bars') ||
        meter.classList.contains('bars-30d') ||
        meter.classList.contains('plays-chart') ||
        meter.classList.contains('incidents-list') ||
        meter.classList.contains('metrics') ||
        meter.classList.contains('quality') ||
        meter.classList.contains('plays-strip')
      ) {
        meter.classList.add('is-building');
        window.setTimeout(function () {
          meter.classList.remove('is-building');
        }, 720);
      } else {
        meter.classList.add('is-built');
      }
      try {
        meter.dispatchEvent(new CustomEvent('confih:meterreveal', { bubbles: true }));
      } catch (e) {
        /* ignore */
      }
    }

    var observer = new IntersectionObserver(
      function (entries) {
        entries.forEach(function (entry) {
          if (!entry.isIntersecting) return;
          var meter = entry.target;
          if (!isReady(meter)) return;
          triggerBuild(meter);
        });
      },
      { threshold: 0.14 }
    );

    function observeMeters() {
      var viewH = window.innerHeight || document.documentElement.clientHeight || 800;
      Array.prototype.forEach.call(
        document.querySelectorAll('.bars, .plays-chart, .quality-bar, .bars-30d, .incidents-list, .metrics, .quality, .plays-strip'),
        function (meter) {
          if (built.has(meter) || meter.closest('[hidden]')) return;
          if (!isReady(meter)) return;
          var rect = meter.getBoundingClientRect();
          if (rect.height > 0 && rect.bottom > 0 && rect.top < viewH * 0.92) {
            triggerBuild(meter);
            return;
          }
          if (!observed.has(meter)) {
            observed.add(meter);
            observer.observe(meter);
          }
        }
      );
    }

    function onViewReenterMeters(ev) {
      if (reduced()) return;
      var view = ev && ev.detail && ev.detail.view;
      var pane = view ? document.querySelector('.view-panel[data-view="' + view + '"]') : null;
      if (pane) {
        Array.prototype.forEach.call(
          pane.querySelectorAll('.bars, .plays-chart, .quality-bar, .bars-30d, .incidents-list, .metrics, .quality, .plays-strip'),
          function (m) {
            built.delete(m);
            m.classList.remove('is-building', 'is-built');
          }
        );
      }
      observeMeters();
    }

    observeMeters();
    document.addEventListener('confih:content', observeMeters);
    document.addEventListener('confih:viewchange', onViewReenterMeters);
    document.addEventListener('confih:viewsettled', observeMeters);
  })();

  /* ── 9. Confih Rotating CD-Disc Loading Controller ───────────────────── */
  (function confihLoaderController() {
    var root = document.documentElement;
    var activeCount = 0;
    var settleTimer = null;
    var badgeTimers = {};
    var badgeShownAt = {};

    function syncRootClass() {
      if (activeCount > 0) {
        if (settleTimer) {
          window.clearTimeout(settleTimer);
          settleTimer = null;
        }
        root.classList.add('is-confih-loading');
      } else {
        root.classList.remove('is-confih-loading');
      }
    }

    function begin() {
      activeCount += 1;
      var startedAt = performance.now();
      syncRootClass();
      var ended = false;
      return function end(minHoldMs) {
        if (ended) return;
        ended = true;
        var hold = reduced() ? 0 : Math.max(0, Number(minHoldMs) || 0);
        var elapsed = performance.now() - startedAt;
        var wait = Math.max(0, Math.round(hold - elapsed));
        if (wait <= 0) {
          activeCount = Math.max(0, activeCount - 1);
          syncRootClass();
          return;
        }
        window.setTimeout(function () {
          activeCount = Math.max(0, activeCount - 1);
          syncRootClass();
        }, wait);
      };
    }

    function pulse(ms) {
      if (reduced()) return;
      var done = begin();
      done(ms || 420);
    }

    function setBadge(id, visible, captionText, minHoldMs) {
      var el = document.getElementById(id);
      if (!el) return;
      if (badgeTimers[id]) {
        window.clearTimeout(badgeTimers[id]);
        badgeTimers[id] = null;
      }
      if (captionText) {
        var cap = el.querySelector('.confih-loader-caption');
        if (cap) cap.textContent = captionText;
      }
      if (visible) {
        badgeShownAt[id] = performance.now();
        el.hidden = false;
        return;
      }
      var hold = reduced() ? 0 : Math.max(0, Number(minHoldMs) || 0);
      var shownAt = badgeShownAt[id] || 0;
      var wait = shownAt ? Math.max(0, Math.round(hold - (performance.now() - shownAt))) : 0;
      if (wait <= 0) {
        el.hidden = true;
      } else {
        badgeTimers[id] = window.setTimeout(function () {
          badgeTimers[id] = null;
          el.hidden = true;
        }, wait);
      }
    }

    window.ConfihLoader = {
      begin: begin,
      pulse: pulse,
      setBadge: setBadge
    };

    if (!reduced()) {
      pulse(540);
      document.addEventListener('confih:viewchange', function () {
        pulse(380);
      });
    }
  })();
})();
