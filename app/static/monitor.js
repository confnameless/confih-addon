    (function () {
      'use strict';

      var $ = function (id) { return document.getElementById(id); };
      var nf = new Intl.NumberFormat();
      var compactFmt = new Intl.NumberFormat(undefined, { notation: 'compact', maximumFractionDigits: 1 });
      var clockFmt = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' });
      var hourFmt = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' });
      var dayFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', timeZone: 'UTC' });

      var state = {
        addonUrl: '',
        pathSecret: false,
        lastSuccessAt: 0,
        failCount: 0,
        inFlight: false,
        timer: null,
        data: null,
        stale: false,
        lastAnnouncement: '',
        lastTimelineSig: '',
        last30dSig: '',
        activeTimelineIdx: 0,
        active30dIdx: 0,
        readoutDefault: 'Hourly API health',
        readout30dDefault: 'Daily uptime'
      };

      function setText(el, text) {
        if (el && el.textContent !== text) el.textContent = text;
      }
      function num(value) {
        var n = Number(value);
        return Number.isFinite(n) ? n : 0;
      }
      function compact(value) {
        var n = num(value);
        if (Math.abs(n) < 1000) return nf.format(Math.round(n));
        return compactFmt.format(n);
      }
      function full(value) {
        return nf.format(Math.round(num(value)));
      }
      function formatUptime(seconds) {
        var s = Math.max(0, Math.floor(num(seconds)));
        var d = Math.floor(s / 86400);
        var h = Math.floor((s % 86400) / 3600);
        var m = Math.floor((s % 3600) / 60);
        if (d > 0) return d + 'd ' + h + 'h';
        if (h > 0) return h + 'h ' + m + 'm';
        if (m > 0) return m + 'm';
        return s + 's';
      }
      function formatLatency(ms) {
        if (ms === null || ms === undefined || ms === '' || Number(ms) <= 0) return '—';
        var v = Number(ms);
        return v >= 1000 ? (v / 1000).toFixed(2) + ' s' : Math.round(v) + ' ms';
      }
      function absoluteTime(ms) {
        return ms ? clockFmt.format(new Date(ms)) : '—';
      }
      function relativeTime(ms) {
        if (!ms) return 'not yet';
        var sec = Math.max(0, Math.floor((Date.now() - ms) / 1000));
        if (sec < 10) return 'just now';
        if (sec < 60) return sec + 's ago';
        var min = Math.floor(sec / 60);
        if (min < 60) return min + 'm ago';
        var hr = Math.floor(min / 60);
        return hr + 'h ago';
      }
      function plural(value, one, many) {
        var n = num(value);
        return nf.format(n) + ' ' + (n === 1 ? one : many);
      }
      function setMetric(el, value, display, title) {
        if (!el) return;
        setText(el, display);
        if (title) el.setAttribute('title', title);
      }

      // ── Copy buttons ──────────────────────────────────────────────
      function fallbackCopy(text) {
        var area = document.createElement('textarea');
        area.value = text;
        area.setAttribute('readonly', 'readonly');
        area.style.position = 'fixed';
        area.style.opacity = '0';
        document.body.appendChild(area);
        area.select();
        var ok = false;
        try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
        document.body.removeChild(area);
        return ok;
      }
      function pulseConnectBar(cls, ms) {
        var bar = $('connect-bar');
        if (!bar) return;
        bar.classList.remove(cls);
        void bar.offsetWidth;
        bar.classList.add(cls);
        var timerKey = '_timer_' + cls;
        if (bar[timerKey]) clearTimeout(bar[timerKey]);
        bar[timerKey] = setTimeout(function () {
          bar.classList.remove(cls);
        }, ms);
      }
      function copyFeedback(btn, ok) {
        var label = btn.querySelector('.btn-label') || btn;
        if (!btn.dataset.baseLabel) btn.dataset.baseLabel = label.textContent;
        var okText = btn.id === 'copy-btn' ? 'Copied!' : 'Copied to clipboard';
        label.textContent = ok ? okText : 'Copy failed';
        btn.classList.remove('is-copied');
        void btn.offsetWidth;
        btn.classList.toggle('is-copied', !!ok);
        if (btn.id === 'copy-btn' && ok) {
          pulseConnectBar('is-copy-beam', 680);
          pulseConnectBar('is-copied-sync', 2000);
        }
        if (btn._copyTimer) clearTimeout(btn._copyTimer);
        btn._copyTimer = setTimeout(function () {
          label.textContent = btn.dataset.baseLabel || 'Copy';
          btn.classList.remove('is-copied');
        }, 2000);
      }
      function copyTextValue(btn, text) {
        if (!text || state.pathSecret) return;
        function finish(ok) { copyFeedback(btn, ok); }
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(text).then(function () { finish(true); }, function () { finish(fallbackCopy(text)); });
        } else {
          finish(fallbackCopy(text));
        }
      }
      ['copy-btn', 'copy-modal-btn'].forEach(function (id) {
        var btn = $(id);
        if (btn) btn.addEventListener('click', function (ev) { copyTextValue(ev.currentTarget, state.addonUrl); });
      });
      var copyManifestBtn = $('copy-manifest-btn');
      if (copyManifestBtn) {
        copyManifestBtn.addEventListener('click', function (ev) {
          var mUrl = state.addonUrl ? state.addonUrl.replace(/\/+$/, '') + '/manifest.json' : '';
          copyTextValue(ev.currentTarget, mUrl);
        });
      }

      // ── QR dialog (100% client-side inline SVG, zero empty <img>) ───
      var modal = $('qr-modal');
      var qrLoadedFor = '';
      var lastModalFocus = null;

      function safeQrUrl(value) {
        try {
          var url = new URL(value, window.location.origin);
          return (url.protocol === 'https:' || url.protocol === 'http:') ? url.href : null;
        } catch (e) { return null; }
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
      function setBackgroundInert(value) {
        var targets = document.querySelectorAll('body > *:not(#qr-modal)');
        for (var i = 0; i < targets.length; i++) {
          var el = targets[i];
          if (!el || !el.setAttribute) continue;
          if (value) {
            if (!el.hasAttribute('data-prev-aria-hidden')) {
              var prev = el.getAttribute('aria-hidden');
              el.setAttribute('data-prev-aria-hidden', prev === null ? '__none__' : prev);
            }
            el.setAttribute('inert', 'inert');
            el.setAttribute('aria-hidden', 'true');
          } else {
            el.removeAttribute('inert');
            if (el.hasAttribute('data-prev-aria-hidden')) {
              var saved = el.getAttribute('data-prev-aria-hidden');
              el.removeAttribute('data-prev-aria-hidden');
              if (saved === '__none__') el.removeAttribute('aria-hidden');
              else el.setAttribute('aria-hidden', saved);
            } else {
              el.removeAttribute('aria-hidden');
            }
          }
        }
      }
      function openQrModal() {
        if (!modal) return;
        lastModalFocus = document.activeElement;
        pulseConnectBar('is-qr-pulse', 520);
        var url = safeQrUrl(state.addonUrl);
        var container = document.querySelector('#qr-modal .qr-container');
        var slot = $('qr-svg-slot');
        var error = $('qr-error');
        var note = $('qr-note');
        var help = $('qr-help');
        setText($('qr-url-line'), state.pathSecret ? 'Private path hidden' : (state.addonUrl || '—'));

        if (state.pathSecret || !url) {
          if (note) note.hidden = false;
          if (help) help.hidden = true;
          if (container) container.style.display = 'none';
          if (slot) slot.textContent = '';
          if (error) error.hidden = true;
        } else {
          if (note) note.hidden = true;
          if (help) help.hidden = false;
          if (container) container.style.display = '';
          var mounted = qrLoadedFor === url && slot && slot.firstChild ? true : mountQrSvg(slot, url);
          if (mounted) {
            qrLoadedFor = url;
            if (slot) slot.style.display = 'grid';
            if (error) error.hidden = true;
          } else {
            if (slot) { slot.textContent = ''; slot.style.display = 'none'; }
            if (error) error.hidden = false;
          }
        }

        document.body.classList.add('modal-open');
        modal.classList.add('active');
        setBackgroundInert(true);
        var close = $('qr-close-btn');
        if (close) {
          close.focus();
          setTimeout(function () { close.focus(); }, 0);
        }
      }
      function closeQrModal() {
        if (!modal || !modal.classList.contains('active')) return;
        modal.classList.remove('active');
        document.body.classList.remove('modal-open');
        setBackgroundInert(false);
        if (lastModalFocus && typeof lastModalFocus.focus === 'function') lastModalFocus.focus();
        else if ($('open-qr-btn')) $('open-qr-btn').focus();
      }
      var openQrBtn = $('open-qr-btn');
      var closeQrBtn = $('qr-close-btn');
      if (openQrBtn) openQrBtn.addEventListener('click', openQrModal);
      if (closeQrBtn) closeQrBtn.addEventListener('click', closeQrModal);
      if (modal) {
        modal.addEventListener('click', function (event) { if (event.target === modal) closeQrModal(); });
        modal.addEventListener('keydown', function (event) {
          if (event.key !== 'Tab') return;
          var focusables = modal.querySelectorAll('button, a[href]');
          var list = Array.prototype.filter.call(focusables, function (el) { return el.offsetParent !== null && !el.disabled; });
          if (!list.length) return;
          var first = list[0];
          var last = list[list.length - 1];
          if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
          } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
          }
        });
      }
      document.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') closeQrModal();
      });

      // ── Status copy ───────────────────────────────────────────────
      var SERVICE_NAMES = { qobuz: 'Qobuz', tidal: 'Tidal' };
      var STATUS_WORDS = {
        online: 'Online',
        standby: 'Standby',
        degraded: 'Degraded',
        offline: 'Offline',
        unconfigured: 'Not configured'
      };
      var ROLE_WORDS = {
        online: 'Serving requests',
        standby: 'Standby',
        degraded: 'Responding slowly',
        offline: 'Not responding',
        unconfigured: 'Not configured'
      };
      function providerStatus(svc) {
        var raw = String((svc && svc.status) || '').toLowerCase().replace(/[^a-z]/g, '');
        if (raw === 'notconfigured' || raw === 'unconfigured') return 'unconfigured';
        if (raw === 'online' || raw === 'active') return 'online';
        if (raw === 'standby') return 'standby';
        if (raw === 'degraded') return 'degraded';
        if (raw === 'offline' || raw === 'down') return 'offline';
        if (svc && svc.online) return 'online';
        return svc && svc.configured ? 'degraded' : 'unconfigured';
      }
      function providerName(key) { return SERVICE_NAMES[key] || 'Provider'; }
      function providerDetail(svc) {
        var observed = svc && svc.observed_max_quality;
        if (observed && typeof observed === 'object') return observed.quality_label || 'Not observed';
        if (typeof observed === 'string' && observed) return observed;
        return 'Not observed';
      }
      function providerLatency(svc, key) {
        var value = svc && svc.health_latency_ms;
        if ((value === undefined || value === null || value === '') && key === 'tidal') value = svc && svc.avg_latency_ms;
        return formatLatency(value);
      }

      function statusCopy(d) {
        var raw = String((d && d.status) || '').toLowerCase();
        var overall = raw === 'operational' || raw === 'degraded' || raw === 'outage' ? raw : 'unknown';
        var services = (d && d.services) || {};
        var q = services.qobuz || {};
        var t = services.tidal || {};
        var qState = providerStatus(q);
        var tState = providerStatus(t);
        var qOnline = qState === 'online';
        var tOnline = tState === 'online';
        var anyOnline = qOnline || tOnline;
        var primary = String((d && d.primary_provider) || services.primary_provider || '').toLowerCase();
        var failover = !!(d && d.failover_active) || (d && d.primary_online === false && anyOnline && overall === 'operational');

        if (overall === 'unknown') {
          return {
            kind: 'unknown',
            label: 'Unavailable',
            headline: 'Status unavailable.',
            detail: 'The status feed did not report a health state. Retrying automatically.'
          };
        }
        if (overall === 'outage' || !anyOnline) {
          return {
            kind: 'outage',
            label: 'Gateway Offline',
            headline: 'Gateway streams are paused.',
            detail: 'Neither Qobuz nor Tidal has an active session right now. BitChord playback resumes as soon as an upstream engine reconnects.'
          };
        }
        if (failover) {
          var serving = qOnline && tOnline ? 'Both engines are' : (qOnline ? 'Qobuz is' : 'Tidal is');
          var primaryLabel = primary === 'both' || !primary ? 'the primary engine' : providerName(primary);
          return {
            kind: 'degraded',
            label: 'Auto-Failover',
            headline: 'Streaming via backup engine.',
            detail: serving + ' carrying playback seamlessly while ' + primaryLabel + ' is unreachable. Failover is automatic.'
          };
        }
        if (overall === 'degraded') {
          var issue = [];
          if (!qOnline && q.configured !== false) issue.push('Qobuz is not responding.');
          if (!tOnline && t.configured !== false) issue.push('Tidal is not responding.');
          if (q.configured === false) issue.push('Qobuz is not configured.');
          if (t.configured === false) issue.push('Tidal is not configured.');
          var stillServing = qOnline && tOnline
            ? 'Both engines remain online.'
            : (qOnline ? 'Qobuz is serving lossless playback.' : (tOnline ? 'Tidal is serving lossless playback.' : ''));
          if (stillServing) issue.push(stillServing);
          return {
            kind: 'degraded',
            label: 'Partial Capacity',
            headline: 'Serving on single engine.',
            detail: issue.length ? issue.join(' ') : 'Routing at reduced capacity while upstream engines recover.'
          };
        }
        var onlineNames = [];
        if (qOnline) onlineNames.push('Qobuz');
        if (tOnline) onlineNames.push('Tidal');
        var onlineSentence = onlineNames.length > 1
          ? 'Qobuz and Tidal are delivering bit-exact lossless streams with automatic failover.'
          : (onlineNames[0] ? onlineNames[0] + ' is delivering bit-exact lossless streams directly to BitChord.' : 'Bit-exact lossless pass-through is active for BitChord.');
        return {
          kind: 'operational',
          label: 'Live · Pass-Through',
          headline: 'Studio masters, straight to BitChord.',
          detail: onlineSentence
        };
      }

      function announceStatus(copy) {
        var text = copy.label + '. ' + copy.headline + ' ' + copy.detail;
        if (text === state.lastAnnouncement) return;
        state.lastAnnouncement = text;
        setText($('status-announcer'), text);
      }
      function applyStatusCopy(copy) {
        var line = $('status-line');
        var led = line ? line.querySelector('.led') : null;
        var heroCopy = document.querySelector('#view-status .hero-copy');
        if (heroCopy) heroCopy.setAttribute('data-gateway-state', copy.kind);
        if (line) line.className = 'status-line ' + copy.kind;
        if (led) {
          var ledKind = copy.kind === 'operational' ? 'ok' : (copy.kind === 'degraded' ? 'mid' : 'warn');
          led.className = 'led ' + ledKind + ' pulse';
        }
        setText($('status-label'), copy.label);
        setText($('status-headline'), copy.headline);
        setText($('status-detail'), copy.detail);
        announceStatus(copy);
        if (!window.ConfihRouter || window.ConfihRouter.currentView() === 'status') {
          document.title = copy.label + ' — Confih Gateway';
        }
      }
      function applyUnavailable() {
        state.stale = true;
        var detail = state.lastSuccessAt
          ? 'The status service is not responding. The numbers below are from ' + absoluteTime(state.lastSuccessAt) + '.'
          : 'The status service is not responding. Retrying automatically.';
        applyStatusCopy({
          kind: 'unknown',
          label: 'Unavailable',
          headline: 'Status unavailable.',
          detail: detail
        });
      }

      // ── Render: connection card ───────────────────────────────────
      function renderAddon(d) {
        var addon = d.addon || {};
        var url = addon.display_url || addon.url || window.location.origin;
        var pathSecret = !!addon.path_secret;
        state.pathSecret = pathSecret;
        state.addonUrl = pathSecret ? '' : url;
        var addonUrl = $('addon-url');
        if (addonUrl) {
          setText(addonUrl, pathSecret ? 'Private path hidden' : url);
          addonUrl.setAttribute('title', pathSecret ? 'This gateway hides its access path on the public page.' : url);
        }
        setText($('addon-name'), addon.name || 'Confih Hi-Res');
        setText($('addon-version'), addon.version || '—');
        setText($('uptime'), formatUptime(addon.uptime_seconds));
        var copyBtn = $('copy-btn');
        var modalCopyBtn = $('copy-modal-btn');
        if (copyBtn) {
          copyBtn.disabled = pathSecret;
          if (pathSecret) copyBtn.title = 'The addon URL is private on this page.';
          else copyBtn.removeAttribute('title');
        }
        if (modalCopyBtn) modalCopyBtn.disabled = pathSecret;
        var connectBar = $('connect-bar');
        if (connectBar) connectBar.classList.toggle('is-path-secret', pathSecret);
        var qrBtn = $('open-qr-btn');
        if (qrBtn) {
          setText(qrBtn.querySelector('.btn-label') || qrBtn, pathSecret ? 'Why no QR?' : 'QR code');
          qrBtn.removeAttribute('aria-label');
        }
        var manifestEl = $('manifest-url');
        var manifestBtn = $('copy-manifest-btn');
        if (manifestEl) {
          setText(manifestEl, pathSecret ? 'Private path hidden' : url.replace(/\/+$/, '') + '/manifest.json');
        }
        if (manifestBtn) {
          manifestBtn.disabled = pathSecret;
          if (pathSecret) manifestBtn.title = 'The manifest URL is private on this page.';
          else manifestBtn.removeAttribute('title');
        }
        renderCapabilities(d.capabilities || {});
      }

      function renderCapabilities(caps) {
        var authPill = $('cap-auth-pill');
        if (authPill) {
          var authText = caps.addon_auth_enabled ? 'Auth: BitChord Proof' : 'Auth: Open';
          if (caps.path_secret) authText += ' · Private Path';
          setText(authPill, authText);
        }
        var pillsWrap = $('cap-quality-pills');
        if (!pillsWrap) return;
        var opts = Array.isArray(caps.quality_options) ? caps.quality_options : [];
        if (!opts.length) return;
        pillsWrap.textContent = '';
        opts.forEach(function (opt) {
          var span = document.createElement('span');
          span.className = 'cap-pill';
          span.textContent = opt.label || opt.value || '';
          pillsWrap.appendChild(span);
        });
      }

      // ── Render: providers ─────────────────────────────────────────
      function renderProvider(key, svc) {
        var status = providerStatus(svc);
        var card = $(key + '-card');
        if (card) card.setAttribute('data-engine-state', status);
        var stateEl = $(key + '-state');
        if (stateEl) {
          stateEl.className = 'state ' + status;
          setText(stateEl, STATUS_WORDS[status] || 'Unknown');
        }
        setText($(key + '-role'), (svc && svc.role) || ROLE_WORDS[status]);
        setText($(key + '-quality'), providerDetail(svc));
        setText($(key + '-response'), providerLatency(svc, key));
        setText($(key + '-streams'), compact(svc && svc.streams_served));
        var streamsEl = $(key + '-streams');
        if (streamsEl && svc && svc.streams_served !== undefined) streamsEl.setAttribute('title', full(svc.streams_served));
        setText($(key + '-tech-role'), (svc && svc.role) || ROLE_WORDS[status]);
        var accountsEl = $(key + '-accounts');
        if (accountsEl) {
          var total = num(svc && svc.total_nodes);
          var active = num(svc && svc.active_nodes);
          if (!total) {
            accountsEl.hidden = true;
            setText(accountsEl, '');
          } else {
            // The pool is what actually serves a play, so the card says how
            // many accounts are behind it, not just whether one answered.
            accountsEl.hidden = false;
            setText(accountsEl, active === total
              ? total + (total === 1 ? ' account' : ' accounts')
              : active + ' of ' + total + ' accounts');
            accountsEl.setAttribute('title', active + ' of ' + total + ' accounts healthy');
            accountsEl.setAttribute('data-degraded', active < total ? 'true' : 'false');
          }
        }
        var nodes = '—';
        if (svc && svc.total_nodes !== undefined && svc.total_nodes !== null) {
          nodes = num(svc.active_nodes) + ' of ' + num(svc.total_nodes) + ' online';
        }
        setText($(key + '-nodes'), nodes);
        var egress = String((svc && (svc.egress || svc.network_path)) || '').toLowerCase();
        setText($(key + '-egress'), egress === 'proxy' ? 'Proxy' : (egress === 'direct' ? 'Direct' : '—'));
        setText($(key + '-claimed'), (svc && svc.claimed_tier) || 'Not reported');
      }
      function renderProviders(d) {
        var services = d.services || {};
        renderProvider('qobuz', services.qobuz || {});
        renderProvider('tidal', services.tidal || {});

        var primary = String(d.primary_provider || services.primary_provider || '').toLowerCase();
        var qState = providerStatus(services.qobuz || {});
        var tState = providerStatus(services.tidal || {});
        var qOnline = qState === 'online';
        var tOnline = tState === 'online';
        var failover = !!d.failover_active || (d.primary_online === false && (qOnline || tOnline));
        var auto = services.auto_failover !== false;

        var note;
        if (failover) {
          var serving = qOnline && tOnline ? 'Both engines are' : (qOnline ? 'Qobuz is' : 'Tidal is');
          note = 'Failover is active. ' + serving + ' carrying requests while the primary engine is unavailable.';
        } else if (primary === 'both') {
          note = (qOnline && tOnline)
            ? 'Both engines are connected. Requests are spread across Qobuz and Tidal.'
            : 'Requests go to whichever engine is available.';
        } else if (primary === 'qobuz') {
          note = tState === 'unconfigured'
            ? 'Qobuz is primary. Tidal is not configured as a backup.'
            : 'Qobuz is primary. Tidal takes over automatically if it fails.';
        } else if (primary === 'tidal') {
          note = qState === 'unconfigured'
            ? 'Tidal is primary. Qobuz is not configured as a backup.'
            : 'Tidal is primary. Qobuz takes over automatically if it fails.';
        } else {
          note = 'No primary provider is reported.';
        }
        setText($('routing-note'), note);
        var failoverEl = $('failover-note');
        setText(failoverEl, auto ? 'Auto-failover on' : 'Auto-failover off');
        if (failoverEl) failoverEl.setAttribute('data-failover', auto ? 'on' : 'off');

        var qCount = num(services.qobuz && services.qobuz.streams_served);
        var tCount = num(services.tidal && services.tidal.streams_served);
        var sTotal = qCount + tCount;
        [['qobuz', qCount], ['tidal', tCount]].forEach(function (pair) {
          var pKey = pair[0];
          var val = pair[1];
          var bar = $(pKey + '-share-bar');
          var pctEl = $(pKey + '-share-pct');
          if (sTotal <= 0) {
            if (bar) bar.style.width = '0%';
            setText(pctEl, '0% share');
          } else {
            var share = (val / sTotal) * 100;
            if (bar) bar.style.width = share.toFixed(1) + '%';
            setText(pctEl, Math.round(share) + '% stream share');
          }
        });
      }

      // ── Render: metrics and quality ───────────────────────────────
      var qualityDefaultReadout = '4 tiers tracked';
      var qualityCardEl = $('quality-card');
      if (qualityCardEl) {
        function activateQualityTier(tierEl) {
          if (!tierEl) return;
          var t = tierEl.getAttribute('data-tier');
          if (!t) return;
          qualityCardEl.setAttribute('data-active-tier', t);
          var r = tierEl.getAttribute('data-readout');
          if (r) setText($('quality-hover-readout'), r);
        }
        function clearQualityTier() {
          qualityCardEl.removeAttribute('data-active-tier');
          setText($('quality-hover-readout'), qualityDefaultReadout);
        }
        qualityCardEl.addEventListener('pointerover', function (ev) {
          var tEl = ev.target && ev.target.closest ? ev.target.closest('[data-tier]') : null;
          if (tEl) activateQualityTier(tEl);
        });
        qualityCardEl.addEventListener('focusin', function (ev) {
          var tEl = ev.target && ev.target.closest ? ev.target.closest('[data-tier]') : null;
          if (tEl) activateQualityTier(tEl);
        });
        qualityCardEl.addEventListener('pointerleave', clearQualityTier);
        qualityCardEl.addEventListener('focusout', function (ev) {
          if (!qualityCardEl.contains(ev.relatedTarget)) clearQualityTier();
        });
      }

      function renderMetrics(d) {
        var traffic = d.traffic || {};
        var users = d.users || {};
        var tiers = d.quality_tiers || {};
        var streams = num(traffic.streams_served ?? traffic.total_streams);
        var hires = num(tiers.hires_24bit) + num(tiers.dolby_atmos);
        var active = num(users.active_now);
        var windowSec = num(users.active_now_window_seconds) || 90;
        var allTime = num(users.total_all_time);
        var avg = num(traffic.avg_response_ms);

        setMetric($('m-streams'), streams, compact(streams), full(streams) + ' streams served');
        setText($('m-streams-note'), streams > 0 ? 'All-time resolved' : 'Awaiting first stream');
        setMetric($('m-hires'), hires, compact(hires), full(hires) + ' Hi-Res or Atmos streams');
        setText(
          $('m-hires-note'),
          streams > 0 ? Math.round((hires / streams) * 100) + '% studio-master mix' : 'Studio-master share'
        );
        setMetric($('m-clients'), active, compact(active), full(active) + ' API clients in the last ' + windowSec + ' seconds');
        setText($('m-clients-note'), windowSec + 's window · ' + compact(allTime) + ' total');
        var clientsCard = $('metric-card-clients');
        if (clientsCard) clientsCard.setAttribute('data-active', active > 0 ? '1' : '0');
        setMetric($('m-response'), avg, avg > 0 ? Math.round(avg) + ' ms' : '—', avg > 0 ? Math.round(avg) + ' milliseconds average' : 'No response data yet');
        setText($('m-response-note'), avg > 0 ? 'Direct upstream response' : 'Upstream pass-through');
        var metricsWrap = $('usage-metrics');
        if (metricsWrap) metricsWrap.setAttribute('data-hydrated', '1');
        try {
          document.dispatchEvent(new CustomEvent('confih:content'));
        } catch (e) { /* ignore */ }
      }

      function renderQuality(d) {
        var tiers = d.quality_tiers || {};
        var values = {
          atmos: num(tiers.dolby_atmos),
          hires: num(tiers.hires_24bit),
          cd: num(tiers.lossless_16bit),
          high: num(tiers.high_320)
        };
        var total = values.atmos + values.hires + values.cd + values.high;
        var ids = { atmos: 'atmos', hires: 'hires', cd: 'cd', high: 'high' };
        var names = {
          atmos: 'Dolby Atmos',
          hires: '24-bit Hi-Res',
          cd: 'CD Lossless',
          high: '320 kbps'
        };
        var labels = [];
        Object.keys(values).forEach(function (key) {
          var seg = $('q-' + ids[key]);
          var countEl = $('qt-' + ids[key]);
          var pctEl = $('qp-' + ids[key]);
          var legendItem = document.querySelector('#quality-legend li[data-tier="' + ids[key] + '"]');
          if (total === 0) {
            if (seg) {
              seg.style.width = '0%';
              seg.setAttribute('data-readout', names[key] + ' · 0 streams');
            }
            if (legendItem) legendItem.setAttribute('data-readout', names[key] + ' · 0 streams');
            setText(countEl, '—');
            setText(pctEl, '—');
            if (countEl) countEl.removeAttribute('title');
            return;
          }
          var pct = Math.round((values[key] / total) * 100);
          var readoutStr = names[key] + ' · ' + plural(values[key], 'stream', 'streams') + ' (' + pct + '%)';
          if (seg) {
            seg.style.width = ((values[key] / total) * 100).toFixed(2) + '%';
            seg.setAttribute('data-readout', readoutStr);
          }
          if (legendItem) legendItem.setAttribute('data-readout', readoutStr);
          setText(countEl, compact(values[key]));
          setText(pctEl, pct + '%');
          if (countEl) countEl.setAttribute('title', full(values[key]) + ' streams');
          labels.push(names[key] + ' ' + pct + '%');
        });
        var bar = $('quality-bar');
        var qNote = $('quality-note');
        if (total === 0) {
          qualityDefaultReadout = 'No codec data yet';
          setText(qNote, 'No stream quality data has been recorded yet.');
          if (qNote) qNote.setAttribute('data-has-data', '0');
          if (bar) bar.setAttribute('aria-label', 'No stream quality data yet');
        } else {
          var masterPct = Math.round(((values.atmos + values.hires) / total) * 100);
          qualityDefaultReadout = masterPct + '% studio-master share';
          setText(qNote, 'Based on ' + full(total) + ' streams served');
          if (qNote) qNote.setAttribute('data-has-data', '1');
          if (bar) bar.setAttribute('aria-label', 'Stream quality mix: ' + labels.join(', ') + '.');
        }
        if (!qualityCardEl || !qualityCardEl.hasAttribute('data-active-tier')) {
          setText($('quality-hover-readout'), qualityDefaultReadout);
        }
        if (qualityCardEl) qualityCardEl.setAttribute('data-hydrated', '1');
        try {
          document.dispatchEvent(new CustomEvent('confih:content'));
        } catch (e) { /* ignore */ }
      }

      // ── Render: 24-hour timeline + roving tabindex arrow navigation ──
      var barsEl = $('timeline-bars');
      function setRovingTabindex(container, idx) {
        if (!container) return 0;
        var items = container.children;
        var count = items.length;
        if (!count) return 0;
        var active = Math.max(0, Math.min(count - 1, idx));
        for (var i = 0; i < count; i++) {
          items[i].setAttribute('tabindex', i === active ? '0' : '-1');
        }
        return active;
      }
      function updateReadoutFromBar(bar) {
        if (!bar) return;
        setText($('chart-readout'), bar.getAttribute('data-readout') || '');
      }
      if (barsEl) {
        barsEl.addEventListener('pointerover', function (event) {
          var bar = event.target && event.target.closest ? event.target.closest('.t-bar') : null;
          if (bar) updateReadoutFromBar(bar);
        });
        barsEl.addEventListener('click', function (event) {
          var bar = event.target && event.target.closest ? event.target.closest('.t-bar') : null;
          if (!bar) return;
          var idx = Array.prototype.indexOf.call(barsEl.children, bar);
          if (idx >= 0) state.activeTimelineIdx = setRovingTabindex(barsEl, idx);
          bar.focus();
          updateReadoutFromBar(bar);
        });
        barsEl.addEventListener('focusin', function (event) {
          var bar = event.target && event.target.closest ? event.target.closest('.t-bar') : null;
          if (!bar) return;
          var idx = Array.prototype.indexOf.call(barsEl.children, bar);
          if (idx >= 0) state.activeTimelineIdx = setRovingTabindex(barsEl, idx);
          updateReadoutFromBar(bar);
        });
        barsEl.addEventListener('pointerleave', function () {
          setText($('chart-readout'), state.readoutDefault);
        });
        barsEl.addEventListener('focusout', function (event) {
          if (!barsEl.contains(event.relatedTarget)) setText($('chart-readout'), state.readoutDefault);
        });
        barsEl.addEventListener('keydown', function (event) {
          var list = barsEl.children;
          if (!list.length) return;
          var idx = Array.prototype.indexOf.call(list, document.activeElement);
          if (idx < 0) return;
          var next = -1;
          if (event.key === 'ArrowRight') next = Math.min(list.length - 1, idx + 1);
          else if (event.key === 'ArrowLeft') next = Math.max(0, idx - 1);
          else if (event.key === 'Home') next = 0;
          else if (event.key === 'End') next = list.length - 1;
          else return;
          event.preventDefault();
          if (next !== idx) {
            state.activeTimelineIdx = setRovingTabindex(barsEl, next);
            list[next].focus();
          }
        });
      }

      document.addEventListener('confih:meterreveal', function (ev) {
        var target = ev && ev.target;
        if (!target || !target.id || window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
        if (target.id === 'timeline-bars') {
          var sEl = $('success-rate');
          if (sEl && sEl.dataset.targetPct && sEl.dataset.finalText && !sEl.classList.contains('ticking')) {
            var endP = parseFloat(sEl.dataset.targetPct);
            if (!isNaN(endP)) {
              sEl.dataset.tickValue = String(Math.max(0, endP - 20));
              sEl.textContent = '';
              setText(sEl, sEl.dataset.finalText);
            }
          }
        } else if (target.id === 'timeline-30d-bars') {
          var p30 = $('uptime-30d-pct');
          if (p30 && p30.dataset.targetPct && p30.dataset.finalText && !p30.classList.contains('ticking')) {
            var end30 = parseFloat(p30.dataset.targetPct);
            if (!isNaN(end30)) {
              p30.dataset.tickValue = String(Math.max(0, end30 - 8));
              p30.textContent = '';
              setText(p30, p30.dataset.finalText);
            }
          }
        } else if (target.id === 'usage-metrics') {
          ['m-streams', 'm-hires', 'm-clients', 'm-response'].forEach(function (mid) {
            var mEl = $(mid);
            if (!mEl || mEl.classList.contains('ticking')) return;
            var curTxt = mEl.textContent.trim();
            if (!curTxt || curTxt === '—' || curTxt === '0') return;
            mEl.dataset.tickValue = '0';
            mEl.textContent = '';
            setText(mEl, curTxt);
          });
        } else if (target.id === 'quality-card') {
          ['qt-atmos', 'qt-hires', 'qt-cd', 'qt-high'].forEach(function (qid) {
            var qEl = $(qid);
            if (!qEl || qEl.classList.contains('ticking')) return;
            var curTxt = qEl.textContent.trim();
            if (!curTxt || curTxt === '—' || curTxt === '0') return;
            qEl.dataset.tickValue = '0';
            qEl.textContent = '';
            setText(qEl, curTxt);
          });
        }
      });

      function renderTimeline(timeline) {
        if (!barsEl) return;
        var tableBody = $('timeline-table-body');
        var slots = Array.isArray(timeline) ? timeline : [];
        var signature = slots.map(function (slot) {
          return [slot.requests, slot.ok, slot.client_err, slot.errors, slot.hour_epoch].join(':');
        }).join(';');
        if (signature === state.lastTimelineSig && barsEl.children.length) return;
        state.lastTimelineSig = signature;

        var hadFocus = -1;
        if (document.activeElement && document.activeElement.classList && document.activeElement.classList.contains('t-bar')) {
          hadFocus = Array.prototype.indexOf.call(barsEl.children, document.activeElement);
        }
        var maxReq = 1;
        var sumReq = 0;
        var sumOk = 0;
        var sumClient = 0;
        var sumServer = 0;
        var peak = null;
        slots.forEach(function (slot) {
          var req = num(slot.requests);
          if (req > maxReq) maxReq = req;
          sumReq += req;
          sumOk += num(slot.ok);
          sumClient += num(slot.client_err);
          sumServer += num(slot.errors);
          if (!peak || req > num(peak.requests)) peak = slot;
        });

        barsEl.textContent = '';
        if (tableBody) tableBody.textContent = '';

        if (!slots.length) {
          for (var e = 0; e < 24; e++) {
            var emptyBar = document.createElement('button');
            emptyBar.type = 'button';
            emptyBar.className = 't-bar idle';
            emptyBar.style.height = '4%';
            emptyBar.dataset.index = String(e);
            emptyBar.style.setProperty('--bar-i', String(e));
            emptyBar.setAttribute('aria-label', 'No traffic recorded');
            emptyBar.setAttribute('data-readout', 'No addon API traffic in the last 24 hours yet.');
            emptyBar.title = 'No traffic recorded';
            barsEl.appendChild(emptyBar);
          }
          state.readoutDefault = 'No addon API traffic in the last 24 hours yet.';
        } else {
          slots.forEach(function (slot, index) {
            var req = num(slot.requests);
            var ok = num(slot.ok);
            var clientErr = num(slot.client_err);
            var serverErr = num(slot.errors);
            var hour = hourFmt.format(new Date(num(slot.hour_epoch) * 1000));
            var status = String(slot.status || '').toLowerCase();
            if (['up', 'idle', 'client-err', 'down', 'degraded'].indexOf(status) === -1) {
              status = req === 0 ? 'idle' : (serverErr > 0 ? 'down' : (clientErr > 0 ? 'client-err' : 'up'));
            }
            var readout = req === 0
              ? hour + ': no requests recorded.'
              : hour + ': ' + plural(req, 'request', 'requests') + ', ' + plural(ok, 'successful request', 'successful requests') + ', ' + plural(clientErr, 'client error', 'client errors') + ', ' + plural(serverErr, 'server error', 'server errors') + '.';
            var bar = document.createElement('button');
            bar.type = 'button';
            bar.className = 't-bar ' + status;
            bar.style.height = (req === 0 ? 6 : Math.max(18, Math.round(Math.pow(req / maxReq, 0.68) * 100))) + '%';
            bar.setAttribute('aria-label', readout);
            bar.setAttribute('data-readout', readout);
            bar.title = readout;
            bar.dataset.index = String(index);
            bar.style.setProperty('--bar-i', String(index));
            barsEl.appendChild(bar);

            if (tableBody) {
              var row = document.createElement('tr');
              var cells = [hour, nf.format(req), nf.format(ok), nf.format(clientErr), nf.format(serverErr)];
              cells.forEach(function (value) {
                var cell = document.createElement('td');
                cell.textContent = value;
                row.appendChild(cell);
              });
              tableBody.appendChild(row);
            }
          });
          state.readoutDefault = 'Peak ' + hourFmt.format(new Date(num(peak.hour_epoch) * 1000)) + ' · ' + plural(peak.requests, 'req', 'reqs');
        }
        setText($('chart-readout'), state.readoutDefault);

        var successRate = sumReq > 0 ? (sumOk / sumReq) * 100 : null;
        var successEl = $('success-rate');
        var statPill = $('timeline-stat');
        var uptimeCard = $('uptime-card');
        var healthKind = 'unknown';
        if (successEl) {
          successEl.classList.remove('good', 'warn', 'bad');
          if (successRate === null) {
            delete successEl.dataset.targetPct;
            delete successEl.dataset.finalText;
            setText(successEl, '—');
          } else {
            var finalRateStr = successRate >= 99.95 ? '100%' : successRate.toFixed(1) + '%';
            successEl.dataset.targetPct = String(successRate);
            successEl.dataset.finalText = finalRateStr;
            setText(successEl, finalRateStr);
            if (successRate >= 99.5) {
              healthKind = 'good';
              successEl.classList.add('good');
            } else if (successRate >= 95) {
              healthKind = 'warn';
              successEl.classList.add('warn');
            } else {
              healthKind = 'bad';
              successEl.classList.add('bad');
            }
          }
        }
        if (statPill) statPill.setAttribute('data-health', healthKind);
        if (uptimeCard) uptimeCard.setAttribute('data-health', healthKind);
        setText($('success-label'), '24h ok');

        var summary;
        if (sumReq === 0) {
          summary = 'No addon API requests were recorded in the last 24 hours.';
        } else if (sumClient === 0 && sumServer === 0 && sumOk >= sumReq) {
          summary = plural(sumReq, 'request', 'requests') + ', all successful.';
        } else if (sumClient === 0 && sumServer === 0) {
          summary = plural(sumReq, 'request', 'requests') + ' · ' + plural(sumOk, 'successful response', 'successful responses') + '.';
        } else {
          summary = plural(sumReq, 'request', 'requests') + ': ' + plural(sumOk, 'successful', 'successful') + ', ' + plural(sumClient, 'client error', 'client errors') + ' and ' + plural(sumServer, 'server error', 'server errors') + '.';
        }
        setText($('timeline-summary'), summary);

        var targetIdx = hadFocus >= 0 ? hadFocus : state.activeTimelineIdx;
        state.activeTimelineIdx = setRovingTabindex(barsEl, targetIdx);
        if (hadFocus >= 0 && barsEl.children[state.activeTimelineIdx]) {
          barsEl.children[state.activeTimelineIdx].focus();
        }
        try {
          document.dispatchEvent(new CustomEvent('confih:content'));
        } catch (e) { /* ignore */ }
      }

      // ── Global render ─────────────────────────────────────────────
      function updateTimes() {
        var relative = relativeTime(state.lastSuccessAt);
        var iso = state.lastSuccessAt ? new Date(state.lastSuccessAt).toISOString() : '';
        var absolute = absoluteTime(state.lastSuccessAt);
        var last = $('last-updated');
        if (last) {
          setText(last, relative);
          if (iso) last.setAttribute('datetime', iso);
          last.setAttribute('title', absolute);
        }
        var foot = $('foot-updated');
        if (foot) {
          setText(foot, absolute);
          if (iso) foot.setAttribute('datetime', iso);
        }
      }
      function renderTimeline30d(days, uptimePct, totalReq30d) {
        var pctEl = $('uptime-30d-pct');
        if (pctEl) {
          if (uptimePct === null || uptimePct === undefined) {
            delete pctEl.dataset.targetPct;
            delete pctEl.dataset.finalText;
            setText(pctEl, '—');
          } else {
            var final30 = Number(uptimePct).toFixed(2) + '%';
            pctEl.dataset.targetPct = String(Number(uptimePct));
            pctEl.dataset.finalText = final30;
            setText(pctEl, final30);
          }
        }
        var reqEl = $('requests-30d');
        if (reqEl) {
          setText(reqEl, totalReq30d ? plural(totalReq30d, 'request', 'requests') + ' (30d)' : '0 requests (30d)');
        }
        var strip = $('timeline-30d-bars');
        if (!strip) return;
        var list = Array.isArray(days) ? days : [];
        var sig = list.map(function (b) { return [b.day_epoch, b.requests, b.errors, b.status].join(':'); }).join(';');
        if (sig === state.last30dSig && strip.children.length) return;
        state.last30dSig = sig;

        var hadFocus = -1;
        if (document.activeElement && document.activeElement.classList && document.activeElement.classList.contains('d-bar')) {
          hadFocus = Array.prototype.indexOf.call(strip.children, document.activeElement);
        }
        strip.textContent = '';

        var buckets = list.length ? list : new Array(30).fill(null);
        buckets.forEach(function (bucket, idx) {
          var b = bucket || { requests: 0, ok: 0, errors: 0, status: 'idle', uptime_pct: null, day_epoch: 0 };
          var req = num(b.requests);
          var st = String(b.status || (req === 0 ? 'idle' : (num(b.errors) > 0 ? 'degraded' : 'up'))).toLowerCase();
          if (['up', 'degraded', 'down', 'idle'].indexOf(st) === -1) st = req === 0 ? 'idle' : 'up';
          var labelDay = b.day_epoch ? dayFmt.format(new Date(num(b.day_epoch) * 1000)) : 'Day';
          var pctStr = b.uptime_pct !== null && b.uptime_pct !== undefined ? Number(b.uptime_pct).toFixed(1) + '% ok' : 'no traffic';
          var readout = labelDay + ': ' + plural(req, 'request', 'requests') + ' (' + pctStr + ').';
          var cell = document.createElement('button');
          cell.type = 'button';
          cell.className = 'd-bar ' + st;
          cell.dataset.index = String(idx);
          cell.style.setProperty('--bar-i', String(idx));
          cell.setAttribute('aria-label', readout);
          cell.setAttribute('data-readout', readout);
          cell.title = readout;
          strip.appendChild(cell);
        });

        var target30Idx = hadFocus >= 0 ? hadFocus : state.active30dIdx;
        state.active30dIdx = setRovingTabindex(strip, target30Idx);
        if (hadFocus >= 0 && strip.children[state.active30dIdx]) {
          strip.children[state.active30dIdx].focus();
        }
        try {
          document.dispatchEvent(new CustomEvent('confih:content'));
        } catch (e) { /* ignore */ }
      }

      var bars30dEl = $('timeline-30d-bars');
      if (bars30dEl) {
        bars30dEl.addEventListener('pointerover', function (e) {
          var cell = e.target && e.target.closest ? e.target.closest('.d-bar') : null;
          if (cell) setText($('timeline-30d-readout'), cell.getAttribute('data-readout') || '');
        });
        bars30dEl.addEventListener('click', function (e) {
          var cell = e.target && e.target.closest ? e.target.closest('.d-bar') : null;
          if (!cell) return;
          var idx = Array.prototype.indexOf.call(bars30dEl.children, cell);
          if (idx >= 0) state.active30dIdx = setRovingTabindex(bars30dEl, idx);
          cell.focus();
          setText($('timeline-30d-readout'), cell.getAttribute('data-readout') || '');
        });
        bars30dEl.addEventListener('focusin', function (e) {
          var cell = e.target && e.target.closest ? e.target.closest('.d-bar') : null;
          if (!cell) return;
          var idx = Array.prototype.indexOf.call(bars30dEl.children, cell);
          if (idx >= 0) state.active30dIdx = setRovingTabindex(bars30dEl, idx);
          setText($('timeline-30d-readout'), cell.getAttribute('data-readout') || '');
        });
        bars30dEl.addEventListener('pointerleave', function () {
          setText($('timeline-30d-readout'), state.readout30dDefault);
        });
        bars30dEl.addEventListener('focusout', function (event) {
          if (!bars30dEl.contains(event.relatedTarget)) {
            setText($('timeline-30d-readout'), state.readout30dDefault);
          }
        });
        bars30dEl.addEventListener('keydown', function (event) {
          var list = bars30dEl.children;
          if (!list.length) return;
          var idx = Array.prototype.indexOf.call(list, document.activeElement);
          if (idx < 0) return;
          var next = -1;
          if (event.key === 'ArrowRight') next = Math.min(list.length - 1, idx + 1);
          else if (event.key === 'ArrowLeft') next = Math.max(0, idx - 1);
          else if (event.key === 'Home') next = 0;
          else if (event.key === 'End') next = list.length - 1;
          else return;
          event.preventDefault();
          if (next !== idx) {
            state.active30dIdx = setRovingTabindex(bars30dEl, next);
            list[next].focus();
          }
        });
      }

      function renderIncidents(incidents) {
        var wrapEl = $('incidents-wrap');
        var listEl = $('incidents-list');
        var emptyEl = $('incidents-empty');
        if (!listEl) return;
        var rows = Array.isArray(incidents) ? incidents : [];
        listEl.textContent = '';
        if (!rows.length) {
          listEl.hidden = true;
          if (wrapEl) wrapEl.hidden = true;
          if (emptyEl) emptyEl.hidden = false;
          return;
        }
        listEl.hidden = false;
        if (wrapEl) wrapEl.hidden = false;
        if (emptyEl) emptyEl.hidden = true;
        rows.slice(0, 12).forEach(function (inc, idx) {
          var row = document.createElement('div');
          var rawKind = String(inc.kind || 'incident').toLowerCase();
          var msgText = inc.message || 'Provider status event';
          /* The server now says how alarmed to be, and it knows the difference
             between a fault and a track that simply is not in the catalogue.
             The keyword sniffing stays as the fallback for older payloads. */
          var severity = String(inc.severity || '').toLowerCase();
          var toneCls;
          if (severity === 'error') {
            toneCls = 'down';
          } else if (severity === 'info') {
            toneCls = 'info';
          } else if (severity === 'warn') {
            toneCls = 'warn';
          } else if (rawKind === 'recovery' || /\b(healthy|online|restored|recovered)\b/i.test(msgText)) {
            toneCls = 'healthy';
          } else if (rawKind === 'outage' || rawKind === 'error' || /\b(offline|down|failed|unreachable)\b/i.test(msgText)) {
            toneCls = 'down';
          } else {
            toneCls = 'warn';
          }
          row.className = 'incident-row is-' + rawKind + ' tone-' + toneCls;
          row.style.setProperty('--row-i', String(idx));
          var chip = document.createElement('span');
          chip.className = 'incident-chip';
          chip.textContent = (inc.provider || rawKind) + (num(inc.count) > 1 ? ' ×' + num(inc.count) : '');
          var msg = document.createElement('span');
          msg.className = 'incident-msg';
          msg.textContent = msgText;
          var when = document.createElement('span');
          when.className = 'incident-time';
          when.textContent = inc.at ? absoluteTime(num(inc.at) * 1000) : 'recent';
          row.appendChild(chip);
          row.appendChild(msg);
          row.appendChild(when);
          listEl.appendChild(row);
        });
        try {
          document.dispatchEvent(new CustomEvent('confih:content'));
        } catch (e) { /* ignore */ }
      }

      function renderTelemetryHud(telemetry, artCache) {
        var t = telemetry || {};
        var a = artCache || {};
        setText($('tel-rss'), t.rss_mb !== undefined ? Number(t.rss_mb).toFixed(1) + ' MB' : '—');
        var loadStr = t.cpu_cores !== undefined
          ? t.cpu_cores + ' cores · load ' + Number(t.load_1m || 0).toFixed(2)
          : '—';
        setText($('tel-load'), loadStr);
        var archStr = t.arch ? t.arch + ' · Python ' + (t.python_version || t.python || '') : '—';
        setText($('tel-arch'), archStr);
        var artStr = a.files !== undefined
          ? nf.format(a.files) + ' / ' + nf.format(a.max_files || 0) + ' (' + Number(a.megabytes || 0).toFixed(1) + ' MB)'
          : '—';
        setText($('tel-art-cache'), artStr);
      }

      function render(d) {
        state.data = d;
        state.stale = false;
        applyStatusCopy(statusCopy(d));
        renderAddon(d);
        renderProviders(d);
        renderMetrics(d);
        renderQuality(d);
        renderTimeline(d.timeline_24h || []);
        renderTimeline30d(d.timeline_30d || [], d.uptime_30d_percent, d.total_requests_30d);
        renderIncidents(d.incidents || []);
        renderTelemetryHud(d.telemetry, d.artwork_cache);
        updateTimes();
        if (window.ConfihSpatial && typeof window.ConfihSpatial.updateStats === 'function') {
          window.ConfihSpatial.updateStats(d);
        }
        document.dispatchEvent(new CustomEvent('confih:content'));
      }

      // ── Polling and connection state ──────────────────────────────
      function showBanner(text) {
        var banner = $('conn-banner');
        if (!banner) return;
        setText($('conn-banner-text'), text);
        banner.hidden = false;
      }
      function hideBanner() {
        var banner = $('conn-banner');
        if (banner) banner.hidden = true;
      }
      function markFailure() {
        state.failCount += 1;
        if (typeof navigator !== 'undefined' && navigator.onLine === false) {
          showBanner('You are offline. Waiting for a connection…');
        } else if (state.lastSuccessAt) {
          showBanner('Cannot refresh the status. Showing the last update from ' + absoluteTime(state.lastSuccessAt) + '.');
        } else {
          showBanner('Cannot reach the status service. Retrying…');
        }
        var stale = state.lastSuccessAt ? (state.failCount >= 3 || Date.now() - state.lastSuccessAt > 15000) : true;
        if (stale) applyUnavailable();
      }
      function isStatusView() {
        return !window.ConfihRouter || window.ConfihRouter.currentView() === 'status';
      }
      function schedulePoll() {
        if (state.timer) {
          clearTimeout(state.timer);
          state.timer = null;
        }
        if (document.hidden || !isStatusView()) return;
        var base = state.failCount === 0 ? 5000 : Math.min(30000, 5000 * Math.pow(2, state.failCount));
        var jitter = base * 0.1 * (Math.random() * 2 - 1);
        state.timer = setTimeout(refreshStats, Math.max(3000, Math.round(base + jitter)));
      }
      async function refreshStats() {
        if (state.inFlight) return;
        state.inFlight = true;
        if (state.timer) {
          clearTimeout(state.timer);
          state.timer = null;
        }
        var controller = new AbortController();
        var timeout = setTimeout(function () { controller.abort(); }, 8000);
        var main = $('main-content');
        if (main) main.setAttribute('aria-busy', 'true');
        var trackLoader = !state.lastSuccessAt || state.failCount > 0;
        var doneLoader = (trackLoader && window.ConfihLoader) ? window.ConfihLoader.begin() : null;
        try {
          var response = await fetch('/api/public-stats', {
            cache: 'no-store',
            headers: { Accept: 'application/json' },
            signal: controller.signal
          });
          if (!response.ok) throw new Error('HTTP ' + response.status);
          var data = await response.json();
          state.failCount = 0;
          state.lastSuccessAt = Date.now();
          hideBanner();
          render(data);
        } catch (error) {
          markFailure();
        } finally {
          clearTimeout(timeout);
          state.inFlight = false;
          if (main) main.removeAttribute('aria-busy');
          if (doneLoader) doneLoader(340);
          schedulePoll();
        }
      }

      var retryButton = $('conn-retry');
      if (retryButton) {
        retryButton.addEventListener('click', function () {
          retryButton.disabled = true;
          retryButton.textContent = 'Trying…';
          refreshStats();
          setTimeout(function () {
            retryButton.disabled = false;
            retryButton.textContent = 'Try again';
          }, 2500);
        });
      }
      document.addEventListener('visibilitychange', function () {
        if (document.hidden || !isStatusView()) {
          if (state.timer) {
            clearTimeout(state.timer);
            state.timer = null;
          }
        } else {
          refreshStats();
        }
      });
      document.addEventListener('confih:viewchange', function (ev) {
        var view = ev && ev.detail && ev.detail.view;
        if (view === 'status' || (!view && isStatusView())) {
          refreshStats();
        } else if (state.timer) {
          clearTimeout(state.timer);
          state.timer = null;
        }
      });
      window.addEventListener('online', function () {
        if (!isStatusView()) return;
        showBanner('Connection restored. Refreshing status…');
        refreshStats();
      });
      window.addEventListener('offline', function () {
        showBanner('You are offline. Waiting for a connection…');
      });

      var FLOW_COPY = {
        client: 'BitChord queries the gateway manifest, searches tracks, and opens streams over HTTP.',
        gateway: 'Confih normalizes quality rungs, caches metadata, and routes around provider outages.',
        providers: 'Qobuz or Tidal returns a direct lossless/Atmos stream URL with verified bit-depth.'
      };
      var flowLegend = $('signal-flow-legend');
      if (flowLegend) {
        flowLegend.addEventListener('click', function (event) {
          var btn = event.target && event.target.closest ? event.target.closest('[data-flow-step]') : null;
          if (!btn) return;
          var wasPressed = btn.getAttribute('aria-pressed') === 'true';
          var step = wasPressed ? null : btn.getAttribute('data-flow-step');
          var btns = flowLegend.querySelectorAll('[data-flow-step]');
          for (var i = 0; i < btns.length; i++) {
            btns[i].setAttribute('aria-pressed', !wasPressed && btns[i] === btn ? 'true' : 'false');
          }
          var detailEl = $('signal-flow-detail');
          if (detailEl) {
            var copyTextVal = step ? (FLOW_COPY[step] || '') : '';
            setText(detailEl, copyTextVal);
            detailEl.hidden = !copyTextVal;
          }
          if (window.ConfihSpatial && typeof window.ConfihSpatial.focusNode === 'function') {
            window.ConfihSpatial.focusNode(step);
          }
        });
      }

      function tick() {
        updateTimes();
        if (isStatusView() && !state.stale && state.lastSuccessAt > 0 && Date.now() - state.lastSuccessAt > 15000) {
          applyUnavailable();
        }
      }
      setInterval(tick, 1000);
      updateTimes();
      if (window.ConfihRouter && typeof window.ConfihRouter.bootstrapOverview === 'function') {
        window.ConfihRouter.bootstrapOverview().then(function (ov) {
          if (ov && ov.stats) {
            state.failCount = 0;
            state.lastSuccessAt = Date.now();
            hideBanner();
            render(ov.stats);
            schedulePoll();
          } else {
            refreshStats();
          }
        }).catch(function () {
          refreshStats();
        });
      } else {
        refreshStats();
      }

    })();
