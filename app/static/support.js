    (function () {
      'use strict';
      var list = document.getElementById('support-assets');
      var status = document.getElementById('support-status');
      var demo = document.getElementById('support-demo');
      var coversBlock = document.getElementById('support-covers-block');
      var doneGlobal = null;
      if (!list || !status) return;

      function finishSupportLoad() {
        if (doneGlobal) {
          var fn = doneGlobal;
          doneGlobal = null;
          fn(280);
        }
      }

      function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }
      function el(tag, cls, text) {
        var node = document.createElement(tag);
        if (cls) node.className = cls;
        if (text !== undefined) node.textContent = text;
        return node;
      }
      /* Coin marks live in the shell as <symbol> elements, so a logo costs no
         request and cannot be swapped for a third-party image. */
      function coinMark(logoId, cls) {
        var svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
        svg.setAttribute('class', cls || 'coin-mark');
        svg.setAttribute('viewBox', '0 0 32 32');
        svg.setAttribute('aria-hidden', 'true');
        svg.setAttribute('focusable', 'false');
        var use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
        use.setAttribute('href', '#' + (logoId || 'coin-btc'));
        svg.appendChild(use);
        return svg;
      }

      function mountQrSvg(slot, text) {
        if (!slot || !window.ConfihQR || typeof window.ConfihQR.svg !== 'function') return false;
        var markup = window.ConfihQR.svg(text);
        if (!markup) return false;
        try {
          var doc = new DOMParser().parseFromString(markup, 'image/svg+xml');
          var svgNode = doc && doc.documentElement;
          if (!svgNode || svgNode.nodeName.toLowerCase() !== 'svg') return false;
          slot.textContent = '';
          slot.appendChild(document.importNode(svgNode, true));
          return true;
        } catch (e) {
          return false;
        }
      }
      function copyText(text, done, failed) {
        if (navigator.clipboard && window.isSecureContext) {
          navigator.clipboard.writeText(text).then(done).catch(failed);
          return;
        }
        var area = document.createElement('textarea');
        area.value = text;
        area.setAttribute('readonly', '');
        area.style.position = 'fixed';
        area.style.opacity = '0';
        document.body.appendChild(area);
        area.select();
        var ok = false;
        try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
        document.body.removeChild(area);
        if (ok) done(); else failed();
      }

      function render(payload) {
        finishSupportLoad();
        var assets = payload && Array.isArray(payload.assets) ? payload.assets : [];
        clear(list);
        if (demo) demo.hidden = !(payload && payload.demo);
        if (!assets.length) {
          if (coversBlock) coversBlock.hidden = true;
          status.hidden = false;
          status.classList.remove('is-error');
          status.textContent = 'Donations are not configured on this gateway. Reporting issues or starring BitChord on GitHub helps just as much.';
          list.hidden = true;
          var impact = document.getElementById('support-impact');
          if (impact) impact.hidden = true;
          return;
        }
        if (coversBlock) coversBlock.hidden = false;
        status.hidden = true;
        status.classList.remove('is-error');
        list.hidden = false;

        assets.forEach(function (asset) {
          var accepts = Array.isArray(asset.accepts) && asset.accepts.length
            ? asset.accepts
            : [{ symbol: asset.symbol || '?', name: asset.name || 'Asset', logo: asset.logo }];
          var item = el('article', 'asset-row');
          item.setAttribute('data-chain', asset.id || '');

          var ident = el('div', 'asset-ident');
          var tile = el('span', 'asset-icon');
          tile.appendChild(coinMark(asset.logo || (accepts[0] && accepts[0].logo), 'asset-icon-mark'));
          ident.appendChild(tile);

          var names = el('div', 'asset-names');
          names.appendChild(el('h3', '', asset.name || 'Asset'));
          names.appendChild(el('p', '', asset.network || ''));

          var coins = el('ul', 'asset-accepts');
          coins.setAttribute('aria-label', 'Accepts on ' + (asset.network || 'this network'));
          accepts.forEach(function (coin) {
            var chip = el('li', 'asset-coin');
            chip.appendChild(coinMark(coin.logo, 'coin-mark'));
            chip.appendChild(el('span', '', coin.symbol || ''));
            chip.title = (coin.name || coin.symbol || '') + ' on ' + (asset.network || '');
            coins.appendChild(chip);
          });
          names.appendChild(coins);
          ident.appendChild(names);

          var addressWrap = el('div', 'asset-address');
          var code = el('code', '', asset.address || '');
          addressWrap.appendChild(code);
          addressWrap.appendChild(
            el('span', 'asset-hint', 'Click to select the whole address')
          );

          var actions = el('div', 'asset-actions');
          var copy = el('button', 'button secondary', 'Copy');
          copy.type = 'button';
          copy.setAttribute('aria-label', 'Copy ' + (asset.name || 'donation') + ' address');
          var qrBtn = el('button', 'button secondary', 'Show QR');
          qrBtn.type = 'button';
          qrBtn.setAttribute('aria-expanded', 'false');
          qrBtn.setAttribute('aria-controls', 'qr-' + asset.id);
          actions.appendChild(copy);
          actions.appendChild(qrBtn);

          var qr = el('div', 'asset-qr');
          qr.id = 'qr-' + asset.id;
          qr.hidden = true;
          var svgSlot = el('div', 'asset-qr-svg');
          svgSlot.setAttribute('role', 'img');
          svgSlot.setAttribute('aria-label', 'QR code for the ' + (asset.name || 'donation') + ' address');
          qr.appendChild(svgSlot);
          var caption = el(
            'p',
            'asset-qr-caption',
            'This address takes ' + accepts.map(function (c) { return c.symbol; }).join(', ') +
              ' on ' + (asset.network || 'the listed network') + ' only.'
          );
          qr.appendChild(caption);

          copy.addEventListener('click', function () {
            copyText(asset.address || '', function () {
              copy.textContent = 'Copied';
              copy.classList.add('is-copied');
              status.hidden = false;
              status.classList.remove('is-error');
              status.textContent = (asset.name || 'Address') + ' address copied. Thank you.';
              if (copy._timer) clearTimeout(copy._timer);
              copy._timer = setTimeout(function () {
                copy.textContent = 'Copy';
                copy.classList.remove('is-copied');
              }, 1800);
            }, function () {
              status.hidden = false;
              status.classList.add('is-error');
              status.textContent = 'Could not copy automatically. Click the address box and copy it manually.';
            });
          });

          qrBtn.addEventListener('click', function () {
            var open = qr.hidden;
            if (open && !svgSlot.firstChild) {
              mountQrSvg(svgSlot, asset.address || '');
            }
            qr.hidden = !open;
            qrBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
            qrBtn.textContent = open ? 'Hide QR' : 'Show QR';
          });

          item.appendChild(ident);
          item.appendChild(addressWrap);
          item.appendChild(actions);
          item.appendChild(qr);
          list.appendChild(item);
        });
      }

      function fmtCount(value) {
        var n = Number(value) || 0;
        try {
          return n.toLocaleString('en');
        } catch (e) {
          return String(n);
        }
      }

      /* An honest counter beats a hypothetical one: this is what the donation
         actually keeps running, taken from the same ledger /plays reads. */
      function renderImpact(totals) {
        var node = document.getElementById('support-impact');
        if (!node || !totals) return;
        var plays = Number(totals.plays) || 0;
        if (plays < 1) {
          node.hidden = true;
          return;
        }
        var tracks = Number(totals.tracks) || 0;
        var listeners = Number(totals.listeners) || 0;
        node.textContent =
          'So far this gateway has served ' + fmtCount(plays) + ' plays of ' +
          fmtCount(tracks) + ' tracks for ' + fmtCount(listeners) +
          ' listener sessions. None of them identified, none of them sold.';
        node.hidden = false;
      }

      function fail() {
        finishSupportLoad();
        status.hidden = false;
        status.classList.add('is-error');
        status.textContent = 'Donation details are unavailable right now. Please try again in a moment.';
      }

      var controller = null;
      function fetchSupport() {
        if (controller) controller.abort();
        controller = ('AbortController' in window) ? new AbortController() : null;
        var timer = setTimeout(function () { if (controller) controller.abort(); }, 10000);
        fetch('/api/support', {
          headers: { 'Accept': 'application/json' },
          signal: controller ? controller.signal : undefined,
          cache: 'no-store'
        }).then(function (res) {
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.json();
        }).then(render).catch(function (err) {
          if (err && err.name === 'AbortError') return;
          fail();
        }).then(function () { clearTimeout(timer); });
      }

      function load() {
        if (!doneGlobal && window.ConfihLoader && typeof window.ConfihLoader.begin === 'function') {
          doneGlobal = window.ConfihLoader.begin();
        }
        if (window.ConfihRouter && typeof window.ConfihRouter.bootstrapOverview === 'function') {
          window.ConfihRouter.bootstrapOverview().then(function (ov) {
            if (ov && ov.support) render(ov.support);
            else fetchSupport();
            if (ov && ov.plays) renderImpact(ov.plays.totals);
          }).catch(fetchSupport);
          return;
        }
        fetchSupport();
      }

      /* Support lives on its own route now, so the shell may open anywhere.
         Fetch when the bay is actually on screen, and only once. */
      var started = false;
      function start() {
        if (started) return;
        started = true;
        load();
      }

      var supportPanel = document.getElementById('view-support');
      if (supportPanel && !supportPanel.hidden) {
        start();
      } else {
        document.addEventListener('confih:viewchange', function (ev) {
          if (ev && ev.detail && ev.detail.view === 'support') start();
        });
      }
    })();
