    (function () {
      'use strict';

      var $id = function (id) { return document.getElementById(id); };
      var BASE = window.location.pathname.replace(new RegExp('/+$'), '');
      var VAULT_KEY = sessionStorage.getItem('bc_vault_key') || '';

      // ── Toast ──────────────────────────────────────────────────────
      var toastTimer = null;
      function showToast(msg, isError) {
        var t = $id('toast');
        if (!t) return;
        t.textContent = msg;
        t.setAttribute('role', isError ? 'alert' : 'status');
        if (isError) t.classList.add('error'); else t.classList.remove('error');
        t.classList.remove('hidden');
        if (toastTimer) clearTimeout(toastTimer);
        toastTimer = setTimeout(function () { t.classList.add('hidden'); }, 3500);
      }

      // ── Show/hide token fields ─────────────────────────────────────
      function bindToggle(btnId, inputId) {
        var btn = $id(btnId), inp = $id(inputId);
        if (!btn || !inp) return;
        btn.setAttribute('data-what', inp.getAttribute('data-label') || inputId);
        btn.addEventListener('click', function () {
          var showing = inp.type === 'text';
          inp.type = showing ? 'password' : 'text';
          btn.setAttribute('aria-pressed', showing ? 'false' : 'true');
          btn.setAttribute('aria-label', (showing ? 'Show ' : 'Hide ') + btn.getAttribute('data-what') + ' value');
          btn.textContent = showing ? 'Show' : 'Hide';
        });
      }
      function describeToggle(btnId, what, label) {
        var btn = $id(btnId);
        if (btn) btn.setAttribute('data-what', what);
        var inp = $id(btnId.replace('toggle-', '').replace('pwd-btn', 'vault-key'));
        if (inp) inp.setAttribute('data-label', label || what);
      }
      describeToggle('toggle-pwd-btn', 'vault token', 'vault token');
      describeToggle('toggle-q-token', 'Qobuz token', 'Qobuz token');
      describeToggle('toggle-q-appsecret', 'Qobuz app secret', 'Qobuz app secret');
      describeToggle('toggle-t-access', 'Tidal access token', 'Tidal access token');
      describeToggle('toggle-t-refresh', 'Tidal refresh token', 'Tidal refresh token');
      bindToggle('toggle-pwd-btn', 'vault-key');
      bindToggle('toggle-q-token', 'q-token');
      bindToggle('toggle-q-appsecret', 'q-appsecret');
      bindToggle('toggle-t-access', 't-access');
      bindToggle('toggle-t-refresh', 't-refresh');

      // ── API helper ─────────────────────────────────────────────────
      function apiErrorName(err) {
        return err && err.name === 'AbortError' ? 'timeout' : (err && err.message ? err.message : String(err));
      }
      var adminApiActive = 0;
      function adminLoaderCaption(path) {
        if (path.indexOf('/cache/clear') !== -1) return 'Clearing gateway caches…';
        if (path.indexOf('/verify') !== -1 || path.indexOf('/credentials') !== -1) return 'Verifying provider credentials…';
        if (path.indexOf('/routing') !== -1) return 'Updating provider routing…';
        if (path.indexOf('/device') !== -1) return 'Polling device authorization…';
        return 'Syncing vault…';
      }
      async function api(path, opts) {
        opts = opts || {};
        var headers = Object.assign({
          'Content-Type': 'application/json',
          'X-Vault-Key': VAULT_KEY
        }, opts.headers || {});

        adminApiActive += 1;
        var doneGlobal = (window.ConfihLoader && typeof window.ConfihLoader.begin === 'function')
          ? window.ConfihLoader.begin()
          : null;
        var loginBox = $id('login-box');
        var onLogin = loginBox && !loginBox.classList.contains('hidden');
        var activeBadgeId = onLogin ? 'admin-login-loader' : 'admin-console-loader';
        var badgeCaption = onLogin ? 'Verifying vault token…' : adminLoaderCaption(path);
        if (window.ConfihLoader && typeof window.ConfihLoader.setBadge === 'function') {
          window.ConfihLoader.setBadge(activeBadgeId, true, badgeCaption, 0);
        } else {
          var badgeEl = $id(activeBadgeId);
          if (badgeEl) badgeEl.hidden = false;
        }

        var controller = new AbortController();
        var timer = setTimeout(function () { controller.abort(); }, 12000);
        var init = Object.assign({}, opts, { headers: headers, signal: controller.signal });
        var prefix = BASE || '/admin';
        var fullPath = prefix + path;
        try {
          var res = await fetch(fullPath, init);
          if (res.status !== 404) return res;

          if (prefix !== '/admin') {
            var adminRes = await fetch('/admin' + path, init);
            if (adminRes.status !== 404) return adminRes;
          }
          return res;
        } finally {
          clearTimeout(timer);
          adminApiActive = Math.max(0, adminApiActive - 1);
          if (adminApiActive === 0) {
            if (window.ConfihLoader && typeof window.ConfihLoader.setBadge === 'function') {
              window.ConfihLoader.setBadge('admin-login-loader', false, null, 0);
              window.ConfihLoader.setBadge('admin-console-loader', false, null, 320);
            } else {
              if ($id('admin-login-loader')) $id('admin-login-loader').hidden = true;
              if ($id('admin-console-loader')) $id('admin-console-loader').hidden = true;
            }
          }
          if (doneGlobal) doneGlobal(320);
        }
      }

      // ── Field errors ───────────────────────────────────────────────
      function setFieldError(input, errEl, msg) {
        if (input) input.setAttribute('aria-invalid', 'true');
        if (errEl) errEl.textContent = msg;
      }
      function clearFieldError(errEl) {
        try {
          var scope = errEl.closest('.panel') || errEl.closest('.login-card');
          if (scope) scope.querySelectorAll('input[aria-invalid]').forEach(function (i) { i.removeAttribute('aria-invalid'); });
        } catch (e) { /* noop */ }
        if (errEl) errEl.textContent = '';
      }

      // ── Unlock flow ────────────────────────────────────────────────
      $id('vault-form').addEventListener('submit', function (ev) {
        ev.preventDefault();
        unlockVault();
      });

      async function unlockVault() {
        var inp = $id('vault-key');
        var errEl = $id('login-err');
        var btn = $id('unlock-btn');
        var k = ((inp && inp.value) || '').trim();
        k = k.replace(/^['"]+|['"]+$/g, '');
        k = k.replace(/^Bearer\s+/i, '');
        k = k.replace(/^(?:STATS_TOKEN|ADMIN_TOKEN|TOKEN)\s*=\s*/i, '');
        k = k.trim();

        if (!k) {
          setFieldError(inp, errEl, 'Enter the vault token (STATS_TOKEN) to continue.');
          if (inp) inp.focus();
          return;
        }

        VAULT_KEY = k;
        if (inp) inp.removeAttribute('aria-invalid');
        errEl.textContent = 'Checking the token…';
        btn.disabled = true;
        btn.setAttribute('aria-busy', 'true');
        var prevLabel = btn.textContent;
        btn.textContent = 'Checking…';

        try {
          var res = await api('/api/summary');
          if (res.status === 200) {
            sessionStorage.setItem('bc_vault_key', k);
            errEl.textContent = '';
            $id('login-box').classList.add('hidden');
            $id('admin-console').classList.remove('hidden');
            renderSummary(await res.json());
            showToast('Vault unlocked');
            document.dispatchEvent(new CustomEvent('confih:content'));
            var heading = $id('console-heading');
            if (heading) heading.focus();
          } else if (res.status === 401) {
            setFieldError(inp, errEl, 'That token was not accepted. Check STATS_TOKEN in the server environment.');
            if (inp) { inp.focus(); inp.select(); }
          } else if (res.status === 429) {
            setFieldError(inp, errEl, 'Too many failed attempts. Wait a minute, then try again.');
            if (inp) inp.focus();
          } else {
            errEl.textContent = 'The server returned HTTP ' + res.status + '. Try again.';
            if (inp) inp.focus();
          }
        } catch (err) {
          var reason = apiErrorName(err);
          errEl.textContent = reason === 'timeout'
            ? 'The server did not respond in time. Try again.'
            : 'Could not reach the engine: ' + reason;
          if (inp) inp.focus();
        } finally {
          btn.disabled = false;
          btn.removeAttribute('aria-busy');
          btn.textContent = prevLabel;
        }
      }

      function lockVault() {
        sessionStorage.removeItem('bc_vault_key');
        VAULT_KEY = '';
        if (devicePollTimer) {
          clearInterval(devicePollTimer);
          devicePollTimer = null;
        }
        var inp = $id('vault-key');
        if (inp) inp.value = '';
        $id('admin-console').classList.add('hidden');
        $id('login-box').classList.remove('hidden');
        var errEl = $id('login-err');
        if (errEl) errEl.textContent = '';
        showToast('Vault locked. Key cleared from this tab.');
        if (inp) inp.focus();
      }
      $id('btn-lock').addEventListener('click', lockVault);
      $id('btn-refresh').addEventListener('click', async function () {
        var btn = $id('btn-refresh');
        btn.disabled = true;
        btn.setAttribute('aria-busy', 'true');
        try { await loadSummary(); }
        finally {
          btn.disabled = false;
          btn.removeAttribute('aria-busy');
        }
      });
      $id('btn-clear-cache').addEventListener('click', async function () {
        var btn = $id('btn-clear-cache');
        btn.disabled = true;
        btn.setAttribute('aria-busy', 'true');
        try {
          var res = await api('/api/cache/clear', { method: 'POST' });
          if (res.ok) showToast('Search and stream caches cleared');
          else showToast('Could not clear caches (HTTP ' + res.status + ')', true);
        } catch (err) {
          showToast(apiErrorName(err) === 'timeout' ? 'Clearing caches timed out' : 'Could not clear caches', true);
        } finally {
          btn.disabled = false;
          btn.removeAttribute('aria-busy');
        }
      });

      async function loadSummary() {
        try {
          var res = await api('/api/summary');
          if (res.ok) renderSummary(await res.json());
          else showToast('Could not load the account summary (HTTP ' + res.status + ')', true);
        } catch (err) {
          showToast(apiErrorName(err) === 'timeout' ? 'Loading the summary timed out' : 'Could not load the account summary', true);
        }
      }

      // ── Safe URL check for device links ────────────────────────────
      function safeHttpsUrl(u) {
        try {
          var p = new URL(u, window.location.origin);
          if (p.protocol !== 'https:') return null;
          return p.href;
        } catch (e) { return null; }
      }

      // ── Summary rendering (DOM-built — no innerHTML from API data) ─
      function statusTag(okFrag) {
        var span = document.createElement('span');
        span.className = 'tag' + (okFrag.ok ? ' ok' : (okFrag.dim ? ' dim' : ' off'));
        span.textContent = okFrag.text;
        return span;
      }
      function healthState(a, provider) {
        if (!a.enabled) return { ok: false, text: 'Disabled', dim: true };
        if (!a.configured) return { ok: false, text: 'Not configured', dim: true };
        if (a.token_ok === false) return { ok: false, text: 'Token issue' };
        if (a.geo_ok === false && provider === 'qobuz') return { ok: false, text: 'Geo issue' };
        return { ok: true, text: 'Healthy' };
      }
      function accButton(text, aria, cls, handler, acc) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'button ' + cls;
        b.textContent = text;
        if (aria) b.setAttribute('aria-label', aria);
        b.addEventListener('click', function () {
          b.disabled = true;
          b.setAttribute('aria-busy', 'true');
          Promise.resolve(handler(acc, b)).catch(function (e) {
            showToast('Action failed: ' + (e && e.message ? e.message : e), true);
          }).finally(function () {
            b.disabled = false;
            b.removeAttribute('aria-busy');
          });
        });
        return b;
      }
      function deleteButton(brandName, displayName, acc, handler) {
        var b = document.createElement('button');
        b.type = 'button';
        b.className = 'button danger';
        b.textContent = 'Delete';
        var baseAria = 'Delete ' + brandName + ' account "' + displayName + '"';
        b.setAttribute('aria-label', baseAria);
        var armTimer = null;
        function disarm() {
          b.classList.remove('is-confirming');
          b.textContent = 'Delete';
          b.setAttribute('aria-label', baseAria);
          if (armTimer) { clearTimeout(armTimer); armTimer = null; }
        }
        b.addEventListener('click', function () {
          if (!b.classList.contains('is-confirming')) {
            b.classList.add('is-confirming');
            b.textContent = 'Confirm?';
            b.setAttribute('aria-label', 'Confirm deleting ' + brandName + ' account "' + displayName + '"');
            armTimer = setTimeout(disarm, 3200);
            return;
          }
          disarm();
          b.disabled = true;
          b.setAttribute('aria-busy', 'true');
          Promise.resolve(handler(acc)).catch(function (e) {
            showToast('Delete failed: ' + (e && e.message ? e.message : e), true);
          }).finally(function () {
            b.disabled = false;
            b.removeAttribute('aria-busy');
          });
        });
        b.addEventListener('keydown', function (ev) {
          if (ev.key === 'Escape' && b.classList.contains('is-confirming')) {
            ev.stopPropagation();
            disarm();
          }
        });
        b.addEventListener('blur', disarm);
        return b;
      }
      function buildAccountCard(a, provider, actions) {
        var state = healthState(a, provider);
        var brandName = provider === 'qobuz' ? 'Qobuz' : 'Tidal';
        var div = document.createElement('div');
        div.className = 'acc-item' + (a.error ? ' has-error' : '');

        var info = document.createElement('div');
        var labelRow = document.createElement('div');
        labelRow.className = 'acc-label';
        var nameSpan = document.createElement('span');
        nameSpan.textContent = a.label || (brandName + ' account');
        labelRow.appendChild(nameSpan);
        labelRow.appendChild(statusTag(state));
        info.appendChild(labelRow);

        var subRow = document.createElement('div');
        subRow.className = 'acc-sub';
        var bits = [];
        if (provider === 'qobuz') bits.push(a.subscription || 'Qobuz');
        bits.push('ID ' + (a.id || '—'));
        if (provider === 'tidal') bits.push('Plays ' + (a.requests_served || 0));
        if (a.error) bits.push('Issue: ' + a.error);
        subRow.textContent = bits.join(' · ');
        info.appendChild(subRow);
        div.appendChild(info);

        var actionsRow = document.createElement('div');
        actionsRow.className = 'acc-actions';
        var displayName = a.label || a.id || brandName;
        actionsRow.appendChild(accButton('Test', 'Test ' + brandName + ' account "' + displayName + '"', 'secondary', actions.test, a));
        actionsRow.appendChild(accButton(a.enabled ? 'Disable' : 'Enable', (a.enabled ? 'Disable' : 'Enable') + ' ' + brandName + ' account "' + displayName + '"', 'secondary', actions.toggle, a));
        var isPrimary = provider === 'qobuz' && a.id === 'qobuz-primary';
        if (!isPrimary) {
          actionsRow.appendChild(deleteButton(brandName, displayName, a, actions.del));
        }
        div.appendChild(actionsRow);
        return div;
      }

      var ACCOUNT_ACTIONS = {
        test: async function (a) {
          showToast('Testing ' + (a.label || a.id) + '…');
          var res = await api('/api/accounts/' + a.id + '/test', { method: 'POST' });
          if (res.ok) { showToast('Health check finished'); await loadSummary(); }
          else showToast('Test failed (HTTP ' + res.status + ')', true);
        },
        toggle: async function (a) {
          var res = await api('/api/accounts/' + a.id + '/toggle', { method: 'POST' });
          if (res.ok) { showToast((a.enabled ? 'Disabled ' : 'Enabled ') + (a.label || a.id)); await loadSummary(); }
          else showToast('Toggle failed (HTTP ' + res.status + ')', true);
        },
        del: async function (a) {
          var res = await api('/api/accounts/' + a.id, { method: 'DELETE' });
          if (res.ok) { showToast('Deleted ' + (a.label || a.id)); await loadSummary(); }
          else showToast('Delete failed (HTTP ' + res.status + ')', true);
        }
      };

      function renderSummary(d) {
        var pri = String(d.primary_provider || 'tidal').toLowerCase();
        var users = d.users || {};
        var priLabel = pri === 'both' ? 'Parallel' : pri.charAt(0).toUpperCase() + pri.slice(1);
        $id('routing-status-line').textContent =
          'Primary: ' + priLabel +
          ' · Auto-failover ' + (d.auto_failover ? 'on' : 'off') +
          ' · ' + (users.active_now || 0) + ' active clients' +
          ' · ' + (users.total_all_time || 0) + ' all time';

        ['qobuz', 'tidal', 'both'].forEach(function (mode) {
          var btn = $id('btn-pri-' + mode);
          if (!btn) return;
          var active = pri === mode;
          btn.className = active ? 'active' : 'secondary';
          btn.setAttribute('aria-pressed', active ? 'true' : 'false');
        });

        var qEl = $id('qobuz-list');
        var qScroll = qEl.scrollTop;
        qEl.textContent = '';
        var qAccs = d.qobuz_accounts || [];
        var qCount = $id('qobuz-count');
        if (qCount) qCount.textContent = qAccs.length + (qAccs.length === 1 ? ' account' : ' accounts');
        if (!qAccs.length) {
          var qEmpty = document.createElement('div');
          qEmpty.className = 'empty-state';
          qEmpty.textContent = 'No Qobuz accounts yet. Paste a user_auth_token below to add one.';
          qEl.appendChild(qEmpty);
        }
        qAccs.forEach(function (a) {
          qEl.appendChild(buildAccountCard(a, 'qobuz', ACCOUNT_ACTIONS));
        });
        qEl.scrollTop = qScroll;

        var tEl = $id('tidal-list');
        var tScroll = tEl.scrollTop;
        tEl.textContent = '';
        var tAccs = d.tidal_accounts || [];
        var tCount = $id('tidal-count');
        if (tCount) tCount.textContent = tAccs.length + (tAccs.length === 1 ? ' account' : ' accounts');
        if (!tAccs.length) {
          var tEmpty = document.createElement('div');
          tEmpty.className = 'empty-state';
          tEmpty.textContent = 'No Tidal accounts yet. Use device login below, or paste tokens manually.';
          tEl.appendChild(tEmpty);
        }
        tAccs.forEach(function (a) {
          tEl.appendChild(buildAccountCard(a, 'tidal', ACCOUNT_ACTIONS));
        });
        tEl.scrollTop = tScroll;
        requestAnimationFrame(function () {
          [qEl, tEl].forEach(function (el) {
            if (el.scrollHeight > el.clientHeight + 2) el.setAttribute('tabindex', '0');
            else el.removeAttribute('tabindex');
          });
        });

        var box = $id('tidal-device-box');
        box.textContent = '';
        var da = d.device_auth || {};
        if (da.pending_url || da.pending_code) {
          var wrap = document.createElement('div');
          var lead = document.createElement('div');
          lead.textContent = 'Waiting for approval' + (da.pending_code ? ' — code ' + da.pending_code : '');
          wrap.appendChild(lead);
          var href = da.pending_url ? safeHttpsUrl(da.pending_url) : null;
          if (href) {
            var aEl = document.createElement('a');
            aEl.href = href;
            aEl.target = '_blank';
            aEl.rel = 'noopener noreferrer';
            aEl.textContent = 'Open the Tidal activation page ↗';
            wrap.appendChild(aEl);
          } else if (da.pending_url) {
            var warn = document.createElement('div');
            warn.className = 'device-warn';
            warn.textContent = 'The device URL was not a secure https link, so opening it is blocked.';
            wrap.appendChild(warn);
          }
          var poll = document.createElement('div');
          poll.className = 'device-note';
          poll.textContent = 'Checking automatically. This page updates once you sign in.';
          wrap.appendChild(poll);
          box.appendChild(wrap);
        } else if (da.status === 'completed' && da.last_account) {
          var done = document.createElement('div');
          done.className = 'device-done';
          done.textContent = 'Tidal account linked (' + String(da.last_account) + ').';
          box.appendChild(done);
        }
      }

      // ── Routing mode ───────────────────────────────────────────────
      [['btn-pri-qobuz', 'qobuz'], ['btn-pri-tidal', 'tidal'], ['btn-pri-both', 'both']].forEach(function (pair) {
        $id(pair[0]).addEventListener('click', async function () {
          var btn = $id(pair[0]);
          btn.disabled = true;
          btn.setAttribute('aria-busy', 'true');
          try {
            var res = await api('/api/routing', {
              method: 'POST',
              body: JSON.stringify({ primary_provider: pair[1], auto_failover: true })
            });
            if (res.ok) {
              showToast('Routing updated: ' + (pair[1] === 'both' ? 'parallel' : pair[1] + ' first'));
              await loadSummary();
            } else {
              showToast('Routing update failed (HTTP ' + res.status + ')', true);
            }
          } catch (err) {
            showToast(apiErrorName(err) === 'timeout' ? 'Routing update timed out' : 'Could not update routing', true);
          } finally {
            btn.disabled = false;
            btn.removeAttribute('aria-busy');
          }
        });
      });

      // ── Add Qobuz account ──────────────────────────────────────────
      $id('btn-qobuz-add').addEventListener('click', addQobuz);
      async function addQobuz() {
        var tokenInput = $id('q-token');
        var errEl = $id('q-err');
        clearFieldError(errEl);
        var token = (tokenInput.value || '').trim();
        if (!token) {
          setFieldError(tokenInput, errEl, 'Paste a Qobuz user_auth_token first.');
          tokenInput.focus();
          return;
        }
        token = token.replace(/^['"]+|['"]+$/g, '').replace(/^Bearer\s+/i, '');
        var uid = ($id('q-userid').value || '').trim();
        if (token.charAt(0) === '{' && token.charAt(token.length - 1) === '}') {
          try {
            var parsed = JSON.parse(token);
            token = String(parsed.user_auth_token || parsed.token || token).replace(/^['"]+|['"]+$/g, '');
            if (!uid && (parsed.user_id || parsed.id)) uid = String(parsed.user_id || parsed.id);
          } catch (e) { /* keep raw token */ }
        }
        var body = {
          label: ($id('q-label').value || '').trim(),
          user_auth_token: token,
          user_id: uid,
          app_id: ($id('q-appid').value || '').trim(),
          app_secret: ($id('q-appsecret').value || '').trim()
        };
        var btn = $id('btn-qobuz-add');
        btn.disabled = true;
        btn.setAttribute('aria-busy', 'true');
        var prevLabel = btn.textContent;
        btn.textContent = 'Verifying…';
        try {
          var res = await api('/api/qobuz/add', { method: 'POST', body: JSON.stringify(body) });
          if (res.ok) {
            var d = await res.json();
            var h = (d.account && d.account.health) || {};
            if (h.token_ok) showToast('Qobuz account verified and active (' + (h.subscription || 'Studio') + ')');
            else if (h.token_error) showToast('Added, but Qobuz reported: ' + h.token_error, true);
            else showToast('Qobuz account saved to the pool');
            tokenInput.value = '';
            await loadSummary();
          } else {
            var err = {};
            try { err = await res.json(); } catch (e) { /* no body */ }
            showToast('Could not add the account: ' + (err.message || 'Qobuz rejected the token'), true);
          }
        } catch (e) {
          showToast(apiErrorName(e) === 'timeout' ? 'Verifying the token timed out' : 'Could not reach the server', true);
        } finally {
          btn.disabled = false;
          btn.removeAttribute('aria-busy');
          btn.textContent = prevLabel;
        }
      }

      // ── Tidal device flow ──────────────────────────────────────────
      var devicePollTimer = null;
      var devicePollBusy = false;
      $id('btn-tidal-device').addEventListener('click', startTidalDeviceFlow);
      async function startTidalDeviceFlow() {
        var btn = $id('btn-tidal-device');
        btn.disabled = true;
        btn.setAttribute('aria-busy', 'true');
        var box = $id('tidal-device-box');
        box.textContent = 'Requesting a device link…';
        try {
          var res = await api('/api/tidal/device-start', { method: 'POST', body: JSON.stringify({}) });
          if (!res.ok) {
            box.textContent = 'Could not start device login (HTTP ' + res.status + ').';
            return;
          }
          var d = await res.json();
          box.textContent = '';
          var href = safeHttpsUrl(d.verification_uri_complete || d.verification_uri);
          var lead = document.createElement('div');
          if (href) {
            var aEl = document.createElement('a');
            aEl.href = href;
            aEl.target = '_blank';
            aEl.rel = 'noopener noreferrer';
            aEl.textContent = 'Open the Tidal activation page' + (d.user_code ? ' (code ' + d.user_code + ')' : '');
            lead.appendChild(aEl);
          } else {
            lead.textContent = 'Tidal did not return a secure https activation link, so opening it is blocked.';
          }
          box.appendChild(lead);
          var hint = document.createElement('div');
          hint.className = 'device-note';
          hint.textContent = 'Checking automatically. This page updates once you sign in.';
          box.appendChild(hint);
          if (devicePollTimer) clearInterval(devicePollTimer);
          devicePollTimer = setInterval(async function () {
            if (devicePollBusy || document.hidden) return;
            devicePollBusy = true;
            try {
              var s = await api('/api/summary');
              if (!s.ok) {
                clearInterval(devicePollTimer);
                devicePollTimer = null;
                return;
              }
              var sd = await s.json();
              renderSummary(sd);
              var da = sd.device_auth || {};
              if (!da.pending_url) {
                clearInterval(devicePollTimer);
                devicePollTimer = null;
              }
            } catch (e) { /* keep polling */ }
            finally { devicePollBusy = false; }
          }, 3000);
        } catch (e) {
          box.textContent = apiErrorName(e) === 'timeout' ? 'The device login request timed out.' : 'Could not reach the server.';
        } finally {
          btn.disabled = false;
          btn.removeAttribute('aria-busy');
        }
      }

      // ── Add Tidal manually ─────────────────────────────────────────
      $id('btn-tidal-add').addEventListener('click', addTidalManual);
      async function addTidalManual() {
        var accessInput = $id('t-access');
        var refreshInput = $id('t-refresh');
        var errEl = $id('t-err');
        clearFieldError(errEl);
        var body = {
          label: ($id('t-label').value || '').trim(),
          access_token: (accessInput.value || '').trim(),
          refresh_token: (refreshInput.value || '').trim()
        };
        var firstInvalid = null;
        if (!body.access_token) {
          setFieldError(accessInput, errEl, 'Tidal access_token is required.');
          firstInvalid = accessInput;
        } else if (!body.refresh_token) {
          setFieldError(refreshInput, errEl, 'Tidal refresh_token is required for auto-renewal.');
          firstInvalid = refreshInput;
        }
        if (firstInvalid) {
          firstInvalid.focus();
          return;
        }
        var btn = $id('btn-tidal-add');
        btn.disabled = true;
        btn.setAttribute('aria-busy', 'true');
        var prevLabel = btn.textContent;
        btn.textContent = 'Saving…';
        try {
          var res = await api('/api/tidal/add', { method: 'POST', body: JSON.stringify(body) });
          if (res.ok) {
            showToast('Tidal account added');
            accessInput.value = '';
            refreshInput.value = '';
            await loadSummary();
          } else {
            showToast('Could not save the Tidal token (HTTP ' + res.status + ')', true);
          }
        } catch (e) {
          showToast(apiErrorName(e) === 'timeout' ? 'Saving the token timed out' : 'Could not reach the server', true);
        } finally {
          btn.disabled = false;
          btn.removeAttribute('aria-busy');
          btn.textContent = prevLabel;
        }
      }

      // ── Legacy ?key=/?token= links: never authenticate from them ───
      (function stripQueryKeys() {
        var params = new URLSearchParams(window.location.search);
        var leaked = params.has('key') || params.has('token');
        if (!leaked) return;
        params.delete('key');
        params.delete('token');
        var qs = params.toString();
        window.history.replaceState(null, '', window.location.pathname + (qs ? '?' + qs : ''));
        var errEl = $id('login-err');
        if (errEl && !sessionStorage.getItem('bc_vault_key')) {
          errEl.textContent = 'This link embedded a vault key in the URL. That leaks the secret into history, logs, and referrers, so it was ignored and removed. Paste the vault key manually instead.';
        }
      })();

      (function autoUnlock() {
        if (!VAULT_KEY) return;
        var inp = $id('vault-key');
        if (inp) inp.value = VAULT_KEY;
        unlockVault();
      })();
    })();
