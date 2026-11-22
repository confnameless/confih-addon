/* ============================================================================
   Confih — /plays behaviour
   Fetches /api/plays and renders the summary, the stacked 24-hour chart,
   the ranked tracks and the anonymous recent feed.

   Chart ergonomics: hover or arrow-key across columns for exact readings,
   click a column to pin/unpin (or press Escape), and pick a quality tier
   chip in the legend to isolate it across the meter.
   ========================================================================= */
(function () {
  'use strict';

  var API = '/api/plays';
  var TIERS = ['dolby_atmos', 'hires_24bit', 'lossless_16bit', 'high_320'];

  var els = {
    status: document.getElementById('plays-status'),
    stats: document.getElementById('plays-stats'),
    chart: document.getElementById('plays-chart'),
    axis: document.getElementById('plays-axis'),
    readout: document.getElementById('plays-readout'),
    top: document.getElementById('plays-top'),
    recent: document.getElementById('plays-recent'),
    mix: document.getElementById('plays-mix'),
    updated: document.getElementById('plays-updated'),
    search: document.getElementById('plays-search'),
    searchClear: document.getElementById('plays-search-clear'),
    filterNote: document.getElementById('plays-filter-note'),
    heroTitle: document.getElementById('telemetry-track-title'),
    heroSub: document.getElementById('telemetry-track-sub'),
    heroCover: document.getElementById('telemetry-track-cover'),
    heroPeak: document.getElementById('telemetry-peak'),
    heroMode: document.getElementById('telemetry-mode'),
    heroTierPill: document.getElementById('telemetry-tier-pill'),
    heroPlate: document.getElementById('plays-telemetry-plate'),
    heroMeta: document.getElementById('telemetry-meta'),
    heroSpark: document.getElementById('telemetry-spark'),
    heroDisc: document.getElementById('telemetry-disc'),
    heroAmbient: document.getElementById('telemetry-ambient'),
  };

  /* ── small helpers ────────────────────────────────────────────────────── */

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function clear(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
  }

  function num(value) {
    var n = Number(value);
    return isFinite(n) ? n : 0;
  }

  function group(value) {
    return num(value).toLocaleString('en-US');
  }

  function hourLabel(epoch) {
    try {
      return new Date(num(epoch) * 1000).toLocaleTimeString('en-US', {
        hour: 'numeric',
      });
    } catch (e) {
      return '';
    }
  }

  function relative(epoch) {
    var seconds = Math.max(0, Date.now() / 1000 - num(epoch));
    if (seconds < 60) return 'just now';
    if (seconds < 3600) return Math.round(seconds / 60) + ' min ago';
    if (seconds < 86400) {
      var hours = Math.round(seconds / 3600);
      return hours + (hours === 1 ? ' hour ago' : ' hours ago');
    }
    var days = Math.round(seconds / 86400);
    return days + (days === 1 ? ' day ago' : ' days ago');
  }

  function cover(row) {
    var wrap = el('div', 'track-cover');
    if (row && row.artwork) {
      var img = el('img');
      img.src = row.artwork;
      img.alt = '';
      img.loading = 'lazy';
      img.decoding = 'async';
      img.referrerPolicy = 'no-referrer';
      img.addEventListener('error', function () {
        wrap.replaceChild(fallback(row), img);
      });
      wrap.appendChild(img);
    } else {
      wrap.appendChild(fallback(row));
    }
    return wrap;
  }

  function fallback(row) {
    var letter = ((row && (row.title || row.artist)) || '?').trim().charAt(0).toUpperCase();
    return el('div', 'cover-fallback', letter);
  }

  /* ── list motion ──────────────────────────────────────────────────────── */

  /* A poll rebuilds both lists, which would otherwise read as a redraw: rows
     teleport, the eye loses its place. So each render measures where the rows
     were, then moves the survivors from there to where they landed (the FLIP
     technique) and pops the genuinely new ones in. Only `translate`, `scale`
     and `opacity` are touched, so nothing here triggers layout. */
  var MOTION = {
    flip: 320,
    pop: 420,
    stagger: 42,
    count: 560,
    ease: 'cubic-bezier(0.22, 1, 0.36, 1)',
    popEase: 'cubic-bezier(0.34, 1.42, 0.52, 1)',
  };

  function reducedMotion() {
    return !!(
      window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches
    );
  }

  function onScreen(node) {
    if (document.hidden) return false;
    var box = node.getBoundingClientRect();
    var height = window.innerHeight || document.documentElement.clientHeight;
    return box.bottom > 0 && box.top < height;
  }

  /* Key → viewport top, taken before the rebuild replaces the nodes. Mid-flight
     rows measure mid-flight, so an interrupting poll continues from what is on
     screen rather than snapping back to the previous layout. */
  function rowTops(list, attr) {
    var tops = Object.create(null);
    var kids = list.children;
    for (var i = 0; i < kids.length; i++) {
      var key = kids[i].getAttribute(attr);
      if (key) tops[key] = kids[i].getBoundingClientRect().top;
    }
    return tops;
  }

  /* Where the list was scrolled to, and how tall it was, from before the
     rebuild — the two numbers that decide whether the reader keeps their place. */
  function listScroll(list, attr) {
    return {
      top: list.scrollTop,
      height: list.scrollHeight,
      first: list.firstElementChild
        ? list.firstElementChild.getAttribute(attr)
        : null,
    };
  }

  /* Both lists scroll, so they need to say so: a soft edge is shown only while
     there is more in that direction, which keeps the last card crisp at the end
     of the scroll. */
  var scrollEdges = typeof WeakMap === 'function' ? new WeakMap() : null;

  function markScrollEdges(list) {
    var update = function () {
      var below = list.scrollHeight - list.clientHeight - list.scrollTop;
      list.classList.toggle('is-more-below', below > 4);
      list.classList.toggle('is-more-above', list.scrollTop > 4);
    };
    if (scrollEdges && !scrollEdges.has(list)) {
      list.addEventListener('scroll', update, { passive: true });
      window.addEventListener('resize', update);
      scrollEdges.set(list, update);
    }
    update();
  }

  function settleList(list, before, attr, opts) {
    opts = opts || {};
    var rows = list.children;
    var motion = !reducedMotion() && onScreen(list);
    var grew = opts.scrollHeight === undefined ? 0 : list.scrollHeight - opts.scrollHeight;
    /* Scrolled down, the reader is looking at rows that a new card would push
       out of view; hold the scroll instead of sliding everything down under
       them. Scroll anchoring is switched off on these lists so this is the only
       thing that moves the box. */
    var held =
      !!opts.scrollTop &&
      opts.scrollTop > 0 &&
      grew > 0 &&
      (rows.length ? rows[0].getAttribute(attr) : null) !== opts.firstKey;

    if (held) list.scrollTop = opts.scrollTop + grew;

    if (!Object.keys(before).length) return; /* first paint: the page reveal covers it */

    var fresh = 0;
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var key = row.getAttribute(attr);
      if (!key) continue;
      var was = before[key];

      if (was === undefined) {
        fresh += 1;
        if (!motion) continue;
        row.animate(
          [
            { opacity: 0, translate: '0 -10px', scale: 0.97 },
            { opacity: 1, translate: '0 2px', scale: 1.006, offset: 0.62 },
            { opacity: 1, translate: '0 0', scale: 1 },
          ],
          {
            duration: MOTION.pop,
            delay: (fresh - 1) * MOTION.stagger,
            easing: MOTION.popEase,
          }
        );
        continue;
      }

      if (!motion || held) continue;
      var moved = was - row.getBoundingClientRect().top;
      if (Math.abs(moved) > 0.5) {
        row.animate(
          [{ translate: '0 ' + moved.toFixed(2) + 'px' }, { translate: '0 0' }],
          { duration: MOTION.flip, easing: MOTION.ease }
        );
      }

      /* The count that changed gets its own beat, so a track climbing the
         ranking is legible even when it only moves one place. */
      if (opts.counts) {
        var now = parseInt(row.getAttribute('data-plays'), 10);
        var wasCount = opts.counts[key];
        if (
          wasCount !== undefined &&
          now > wasCount &&
          !row.querySelector('.is-count-bump')
        ) {
          var badge = row.querySelector('.track-metric b');
          if (badge) {
            badge.classList.add('is-count-bump');
            (function (node) {
              window.setTimeout(function () {
                node.classList.remove('is-count-bump');
              }, MOTION.count + 60);
            })(badge);
          }
        }
      }
    }
  }

  function status(message, isError) {
    if (!els.status) return;
    els.status.hidden = !message;
    els.status.textContent = message || '';
    els.status.classList.toggle('is-error', !!isError);
  }

  /* ── hero telemetry plate ─────────────────────────────────────────────── */

  /* Mirrors the server's "active now" window in stats.py: a play that started
     inside this window is treated as still streaming. */
  var LIVE_WINDOW_SEC = 90;

  function heroKey(row) {
    if (!row) return '';
    return [row.id || '', row.title || '', row.artist || '', num(row.at)].join('|');
  }

  function coverKey(row) {
    if (!row) return '';
    return [row.artwork || '', row.title || '', row.artist || ''].join('|');
  }

  function isStreaming(row) {
    return !!row && Date.now() / 1000 - num(row.at) <= LIVE_WINDOW_SEC;
  }

  /* The foot sparkline existed in markup but was never fed, so it always
     collapsed. Bars are data, so they render without entrance motion. */
  function renderHeroSpark(timeline, peak) {
    var host = els.heroSpark;
    if (!host) return;
    clear(host);
    if (!timeline.length || !peak) return;
    timeline.forEach(function (row) {
      var value = num(row.total);
      var bar = el('i');
      bar.style.height = Math.max(2, Math.round((value / peak) * 12)) + 'px';
      if (value === peak) bar.className = 'is-peak';
      host.appendChild(bar);
    });
  }

  /* Crossfade the sleeve art instead of hard-replacing the node: the previous
     cover fades out under the new one, which waits for its bitmap first. */
  function swapHeroCover(row, instant) {
    var current = els.heroCover;
    var stage = current && current.parentNode;
    if (!stage) return;
    var key = coverKey(row);
    if (key === state.heroCoverKey) return;
    state.heroCoverKey = key;

    var fresh = cover(row);
    fresh.className = 'track-cover';
    fresh.id = 'telemetry-track-cover';
    fresh.setAttribute('data-cover-key', key);

    if (instant) {
      stage.replaceChild(fresh, current);
      els.heroCover = fresh;
      return;
    }

    function start() {
      fresh.classList.add('is-cover-in');
      stage.appendChild(fresh);
      void fresh.offsetWidth;
      fresh.classList.remove('is-cover-in');
      current.classList.add('is-cover-out');
      window.setTimeout(function () {
        if (current.parentNode) current.parentNode.removeChild(current);
      }, 280);
      els.heroCover = fresh;
    }

    var img = fresh.querySelector('img');
    if (img && !img.complete) {
      img.addEventListener('load', start, { once: true });
      img.addEventListener('error', start, { once: true });
    } else {
      start();
    }
  }

  /* The sleeve art also drives an ambient wash behind the plate: two stacked
     layers so a track change crossfades instead of snapping the colour. */
  function setHeroAmbient(row) {
    var host = els.heroAmbient;
    if (!host) return;
    host.setAttribute('data-tier', (row && row.tier) || '');
    var url = row && row.artwork ? 'url("' + String(row.artwork).replace(/"/g, '%22') + '")' : '';
    host.setAttribute('data-art', url ? '1' : '0');
    if (state.ambientUrl === url) return;
    state.ambientUrl = url;

    var layers = host.querySelectorAll('i');
    if (layers.length < 2) return;
    var active = host.querySelector('i.is-on');

    if (!url) {
      Array.prototype.forEach.call(layers, function (layer) {
        layer.classList.remove('is-on');
        layer.style.backgroundImage = '';
      });
      return;
    }

    var target = active && active === layers[0] ? layers[1] : layers[0];
    target.style.backgroundImage = url;
    void target.offsetWidth;
    target.classList.add('is-on');
    if (active) active.classList.remove('is-on');
  }

  function pulseHeroMeta() {
    var meta = els.heroMeta;
    if (!meta) return;
    meta.classList.remove('is-refresh');
    void meta.offsetWidth;
    meta.classList.add('is-refresh');
  }

  function renderHeroTelemetry(payload) {
    var recent = payload && Array.isArray(payload.recent) ? payload.recent : [];
    var timeline = payload && Array.isArray(payload.timeline) ? payload.timeline : [];
    var peak = 0;
    timeline.forEach(function (r) { peak = Math.max(peak, num(r.total)); });

    if (els.heroPeak) {
      els.heroPeak.textContent = peak ? group(peak) + ' / hr peak' : 'Idle';
    }
    renderHeroSpark(timeline, peak);

    var plate = els.heroPlate;
    var latest = recent[0];

    if (!latest) {
      state.heroKey = '';
      state.ambientUrl = null;
      setHeroAmbient(null);
      if (plate) {
        plate.classList.remove('is-spinning');
        plate.classList.remove('is-live-now');
      }
      if (els.heroMode) els.heroMode.textContent = 'Standby';
      if (els.heroTitle) els.heroTitle.textContent = 'Waiting for first stream';
      if (els.heroSub) els.heroSub.textContent = 'Lossless ledger ready';
      if (els.heroTierPill) els.heroTierPill.hidden = true;
      return;
    }

    var streaming = isStreaming(latest);
    if (plate) {
      plate.classList.add('is-spinning');
      plate.classList.toggle('is-live-now', streaming);
    }
    if (els.heroMode) {
      els.heroMode.textContent = streaming ? 'Live now' : relative(latest.at);
    }

    var key = heroKey(latest);
    if (key === state.heroKey) return;
    var firstPaint = !state.heroKey;
    state.heroKey = key;

    if (els.heroTitle) els.heroTitle.textContent = latest.title || 'Unknown track';
    if (els.heroSub) els.heroSub.textContent = latest.artist || 'Unknown artist';
    if (els.heroTierPill) {
      var tLbl = tierLabel(latest.tier);
      if (tLbl) {
        els.heroTierPill.textContent = tLbl;
        els.heroTierPill.setAttribute('data-tier', latest.tier || '');
        els.heroTierPill.hidden = false;
      } else {
        els.heroTierPill.hidden = true;
      }
    }
    /* The platter's label colour tracks the delivered tier, like the pill. When
       the track has a cover, that cover becomes the label itself. */
    if (els.heroDisc) {
      els.heroDisc.setAttribute('data-tier', latest.tier || '');
      if (latest.artwork) {
        els.heroDisc.style.setProperty(
          '--disc-art',
          'url("' + String(latest.artwork).replace(/"/g, '%22') + '")'
        );
      } else {
        els.heroDisc.style.removeProperty('--disc-art');
      }
    }

    swapHeroCover(latest, firstPaint);
    setHeroAmbient(latest);
    if (!firstPaint) pulseHeroMeta();
  }

  /* ── summary strip ────────────────────────────────────────────────────── */

  function makeStatIcon(kind) {
    var ns = 'http://www.w3.org/2000/svg';
    var svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('width', '16');
    svg.setAttribute('height', '16');
    svg.setAttribute('viewBox', '0 0 16 16');
    svg.setAttribute('fill', 'none');
    svg.setAttribute('aria-hidden', 'true');
    var p = document.createElementNS(ns, 'path');
    p.setAttribute('stroke', 'currentColor');
    p.setAttribute('stroke-width', '1.4');
    p.setAttribute('stroke-linecap', 'round');
    p.setAttribute('stroke-linejoin', 'round');
    if (kind === 'wave') {
      p.setAttribute('d', 'M2.5 9.5v-3M5.5 11.8V4.2M8.5 13.2V2.8M11.5 10.8V5.2M14 8.8V7.2');
    } else if (kind === 'disc') {
      var c1 = document.createElementNS(ns, 'circle');
      c1.setAttribute('cx', '8');
      c1.setAttribute('cy', '8');
      c1.setAttribute('r', '5.5');
      c1.setAttribute('stroke', 'currentColor');
      c1.setAttribute('stroke-width', '1.4');
      var c2 = document.createElementNS(ns, 'circle');
      c2.setAttribute('cx', '8');
      c2.setAttribute('cy', '8');
      c2.setAttribute('r', '1.6');
      c2.setAttribute('stroke', 'currentColor');
      c2.setAttribute('stroke-width', '1.3');
      svg.appendChild(c1);
      svg.appendChild(c2);
      return svg;
    } else if (kind === 'sessions') {
      var head = document.createElementNS(ns, 'circle');
      head.setAttribute('cx', '8');
      head.setAttribute('cy', '5.5');
      head.setAttribute('r', '2.3');
      head.setAttribute('stroke', 'currentColor');
      head.setAttribute('stroke-width', '1.4');
      svg.appendChild(head);
      p.setAttribute('d', 'M3.6 13.2c.6-2.1 2.2-3.3 4.4-3.3s3.8 1.2 4.4 3.3');
    } else {
      p.setAttribute('d', 'M8 2.2l1.65 3.75L13.4 7.6 9.65 9.25 8 13l-1.65-3.75L2.6 7.6l3.75-1.65L8 2.2z');
    }
    svg.appendChild(p);
    return svg;
  }

  function renderStats(totals) {
    var container = els.stats;
    if (!container) return;

    var topTier = (totals.tiers || []).slice().sort(function (a, b) {
      return num(b.count) - num(a.count);
    })[0];

    var rows = [
      {
        value: group(totals.plays),
        label: 'Streams played',
        note: 'Total resolved streams',
        tone: 'neutral',
        icon: 'wave',
      },
      {
        value: group(totals.tracks),
        label: 'Distinct tracks',
        note: 'Unique catalog recordings',
        tone: 'cyan',
        icon: 'disc',
      },
      {
        value: group(totals.listeners),
        label: 'Listener sessions',
        note: 'Ephemeral · reset on restart',
        tone: 'emerald',
        icon: 'sessions',
      },
      {
        value: topTier && num(topTier.count) ? topTier.label : '—',
        label: 'Top delivered tier',
        note: 'Verified upstream codec',
        tone: 'gold',
        icon: 'spark',
        isText: !!(topTier && num(topTier.count)),
      },
    ];

    container.setAttribute('data-hydrated', '1');

    // Update in place on refresh so number tickers animate smoothly!
    var existing = container.querySelectorAll('.plays-stat-value');
    if (existing.length === rows.length) {
      rows.forEach(function (row, idx) {
        existing[idx].classList.toggle('is-text', !!row.isText);
        if (existing[idx].textContent !== row.value) {
          existing[idx].textContent = row.value;
        }
      });
      return;
    }

    clear(container);
    rows.forEach(function (row, idx) {
      var cell = el('div', 'plays-stat');
      cell.setAttribute('data-stat-tone', row.tone);
      cell.style.setProperty('--m-i', String(idx));
      var head = el('div', 'plays-stat-head');
      var valEl = el('p', row.isText ? 'plays-stat-value is-text' : 'plays-stat-value', row.value);
      var iconWrap = el('span', 'plays-stat-icon');
      iconWrap.setAttribute('aria-hidden', 'true');
      iconWrap.appendChild(makeStatIcon(row.icon));
      head.appendChild(valEl);
      head.appendChild(iconWrap);
      cell.appendChild(head);
      cell.appendChild(el('p', 'plays-stat-label', row.label));
      cell.appendChild(el('p', 'plays-stat-note', row.note));
      container.appendChild(cell);
    });
  }

  /* ── the stacked chart (with roving tabindex) ─────────────────────────── */

  var defaultReadout = '';
  var pinnedBar = null;
  var pinnedRow = null;
  var activeBarIdx = 0;

  function setPlaysRoving(chart, idx) {
    if (!chart) return 0;
    var bars = chart.querySelectorAll('.plays-bar');
    if (!bars.length) return 0;
    var clamped = Math.max(0, Math.min(bars.length - 1, idx));
    for (var i = 0; i < bars.length; i++) {
      bars[i].setAttribute('tabindex', i === clamped ? '0' : '-1');
    }
    return clamped;
  }

  function renderChart(timeline) {
    var chart = els.chart;
    if (!chart) return;
    var prevBars = chart.querySelectorAll('.plays-bar');
    var hadFocusIdx = prevBars.length
      ? Array.prototype.indexOf.call(prevBars, document.activeElement)
      : -1;
    clear(chart);
    pinnedBar = null;
    pinnedRow = null;

    var hours = Array.isArray(timeline) ? timeline : [];
    var max = 1;
    hours.forEach(function (row) {
      max = Math.max(max, num(row.total));
    });

    if (!hours.length) {
      chart.removeAttribute('data-peak');
      chart.appendChild(el('div', 'plays-empty'));
      defaultReadout = 'No plays have been recorded yet.';
      setReadout(null);
      return;
    }

    activeBarIdx = Math.max(
      0,
      Math.min(hours.length - 1, hadFocusIdx >= 0 ? hadFocusIdx : activeBarIdx)
    );

    hours.forEach(function (row, index) {
      var bar = el('button', 'plays-bar');
      bar.type = 'button';
      bar.dataset.index = String(index);
      bar.style.setProperty('--bar-i', String(index));
      bar.setAttribute('tabindex', index === activeBarIdx ? '0' : '-1');
      if (!num(row.total)) bar.classList.add('is-empty');

      TIERS.forEach(function (tier) {
        var count = num(row.tiers && row.tiers[tier]);
        if (!count) return;
        var seg = el('span', 'plays-bar-seg');
        seg.setAttribute('data-tier', tier);
        seg.style.height = ((count / max) * 100).toFixed(2) + '%';
        bar.appendChild(seg);
      });

      var reading =
        hourLabel(row.hour) + ': ' + group(row.total) + (num(row.total) === 1 ? ' play' : ' plays');
      var detail = TIERS.filter(function (tier) {
        return num(row.tiers && row.tiers[tier]);
      })
        .map(function (tier) {
          var count = num(row.tiers[tier]);
          return count + '× ' + tierLabel(tier);
        })
        .join(' · ');

      bar.setAttribute('aria-label', reading + (detail ? ' — ' + detail : ''));
      bar.title = reading + (detail ? ' — ' + detail : '');

      var rowData = { hour: row.hour, total: row.total, tiers: row.tiers };

      bar.addEventListener('pointerenter', function () {
        if (!pinnedBar) setReadout(rowData, false);
      });
      bar.addEventListener('focus', function () {
        activeBarIdx = setPlaysRoving(chart, index);
        if (!pinnedBar) setReadout(rowData, false);
      });
      bar.addEventListener('pointerleave', restoreReadout);
      bar.addEventListener('blur', restoreReadout);
      bar.addEventListener('click', function () {
        activeBarIdx = setPlaysRoving(chart, index);
        if (pinnedBar === bar) {
          bar.classList.remove('is-pinned');
          pinnedBar = null;
          pinnedRow = null;
          setReadout(null, false);
        } else {
          if (pinnedBar) pinnedBar.classList.remove('is-pinned');
          pinnedBar = bar;
          pinnedRow = rowData;
          bar.classList.add('is-pinned');
          setReadout(rowData, true);
        }
      });

      chart.appendChild(bar);
    });

    var total = hours.reduce(function (sum, row) {
      return sum + num(row.total);
    }, 0);

    chart.setAttribute('data-peak', 'peak ' + group(max) + ' / hour');

    defaultReadout =
      group(total) +
      (total === 1 ? ' play' : ' plays') +
      ' in the last 24 hours. Hover, arrow-key, or click a column to pin its reading.';
    setReadout(null, false);

    if (els.axis) {
      clear(els.axis);
      ['24 hours ago', '12 hours ago', 'Now'].forEach(function (label) {
        els.axis.appendChild(el('span', null, label));
      });
    }

    if (hadFocusIdx >= 0) {
      var currentBars = chart.querySelectorAll('.plays-bar');
      if (currentBars[activeBarIdx]) currentBars[activeBarIdx].focus();
    }
  }

  if (els.chart) {
    els.chart.addEventListener('keydown', function (event) {
      var list = els.chart.querySelectorAll('.plays-bar');
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
        activeBarIdx = setPlaysRoving(els.chart, next);
        list[next].focus();
      }
    });
  }

  document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape' && pinnedBar) {
      pinnedBar.classList.remove('is-pinned');
      pinnedBar = null;
      pinnedRow = null;
      setReadout(null, false);
    }
  });

  function tierLabel(key) {
    var found = (state.totals.tiers || []).filter(function (t) {
      return t.key === key;
    })[0];
    return found ? found.label : key;
  }

  function setReadout(row, pinned) {
    if (!els.readout) return;
    if (!row) {
      els.readout.textContent = defaultReadout;
      els.readout.removeAttribute('data-pinned');
      return;
    }
    clear(els.readout);
    var head = el('b', null, hourLabel(row.hour) + ' — ' + group(row.total));
    els.readout.appendChild(head);
    els.readout.appendChild(
      document.createTextNode(num(row.total) === 1 ? ' play' : ' plays')
    );
    if (pinned) {
      els.readout.appendChild(el('span', 'pin-hint', 'Pinned · click or Esc to clear'));
      els.readout.setAttribute('data-pinned', 'true');
    } else {
      els.readout.removeAttribute('data-pinned');
    }
    var parts = TIERS.filter(function (tier) {
      return num(row.tiers && row.tiers[tier]);
    }).map(function (tier) {
      return group(row.tiers[tier]) + '× ' + tierLabel(tier);
    });
    if (parts.length) {
      els.readout.appendChild(document.createElement('br'));
      els.readout.appendChild(document.createTextNode(parts.join('  ·  ')));
    }
  }

  function restoreReadout() {
    if (pinnedBar && pinnedRow) {
      setReadout(pinnedRow, true);
      return;
    }
    setReadout(null, false);
  }

  /* ── ranked tracks ────────────────────────────────────────────────────── */

  function renderTop(rows) {
    var list = els.top;
    if (!list) return;
    var before = rowTops(list, 'data-key');
    var scrollState = listScroll(list, 'data-key');
    clear(list);

    if (!rows || !rows.length) {
      list.appendChild(emptyState());
      return;
    }

    rows.forEach(function (row, index) {
      var item = el('div', 'track-row');
      item.setAttribute('data-key', row.id || 'row-' + index);
      item.setAttribute('data-plays', String(num(row.plays)));
      item.appendChild(el('div', 'track-pos', String(index + 1)));
      item.appendChild(cover(row));

      var names = el('div', 'track-names');
      names.appendChild(el('div', 'track-title', row.title || 'Unknown track'));
      names.appendChild(
        el('div', 'track-artist', [row.artist, row.album].filter(Boolean).join(' · '))
      );

      /* The bar always travels with the name of what it is showing: a bare
         five-pixel line under an artist reads as a rule on a phone, and a rule
         that changes colour per row reads as a rendering bug. */
      var quality = row.quality || [];
      var bar = el('div', 'track-quality');
      bar.setAttribute('role', 'img');
      var breakdown = quality
        .map(function (segment) {
          return Math.round(num(segment.share) * 100) + '% ' + segment.label;
        })
        .join(', ');
      bar.setAttribute('aria-label', breakdown || 'No quality data');
      if (breakdown) bar.title = 'Delivered quality: ' + breakdown;
      quality.forEach(function (segment) {
        var span = el('span');
        span.setAttribute('data-tier', segment.key);
        span.style.width = (num(segment.share) * 100).toFixed(2) + '%';
        bar.appendChild(span);
      });

      var qualityRow = el('div', 'track-quality-row');
      qualityRow.appendChild(bar);
      if (quality.length) {
        var dominant = quality.reduce(function (best, segment) {
          return num(segment.share) > num(best.share) ? segment : best;
        }, quality[0]);
        var tierLabel = el('span', 'track-tier', dominant.label || '');
        tierLabel.setAttribute('data-tier', dominant.key);
        qualityRow.appendChild(tierLabel);
      }
      names.appendChild(qualityRow);
      item.appendChild(names);

      /* One number per row. The listener count is deduplicated, so on a feed
         with a handful of listeners it just repeats the play count and reads as
         two columns saying the same thing; it stays in the totals strip and in
         this tooltip, where it is actually news. */
      var plays = el('div', 'track-metric');
      plays.appendChild(el('b', null, group(row.plays)));
      plays.appendChild(el('span', null, row.plays === 1 ? 'play' : 'plays'));
      plays.title =
        num(row.listeners) > 1 && num(row.listeners) !== num(row.plays)
          ? group(row.plays) + ' plays from ' + group(row.listeners) + ' listeners'
          : 'Streams served for this track, repeats included';
      item.appendChild(plays);

      list.appendChild(item);
    });

    settleList(list, before, 'data-key', {
      scrollTop: scrollState.top,
      scrollHeight: scrollState.height,
      firstKey: scrollState.first,
      counts: state.counts,
    });
    markScrollEdges(list);
    state.counts = {};
    rows.forEach(function (row, index) {
      state.counts[row.id || 'row-' + index] = num(row.plays);
    });
  }

  /* ── recent feed ──────────────────────────────────────────────────────── */

  function renderRecent(rows) {
    var list = els.recent;
    if (!list) return;
    var before = rowTops(list, 'data-key');
    var scrollState = listScroll(list, 'data-key');
    clear(list);

    if (!rows || !rows.length) {
      list.appendChild(emptyState());
      return;
    }

    rows.forEach(function (row, index) {
      var item = el('div', 'recent-row');
      /* Two plays of one track are two cards, so the minute they landed is part
         of the identity — the key has to survive a rebuild and nothing else. */
      item.setAttribute('data-key', (row.id || 'row-' + index) + '@' + num(row.at));
      item.appendChild(cover(row));

      var names = el('div', 'track-names');
      names.appendChild(el('div', 'track-title', row.title || 'Unknown track'));
      names.appendChild(el('div', 'track-artist', row.artist || ''));
      item.appendChild(names);

      var badge = el('span', 'tier-badge', tierLabel(row.tier));
      badge.setAttribute('data-tier', row.tier);
      item.appendChild(badge);

      item.appendChild(el('span', 'recent-when', relative(row.at)));
      list.appendChild(item);
    });

    settleList(list, before, 'data-key', {
      scrollTop: scrollState.top,
      scrollHeight: scrollState.height,
      firstKey: scrollState.first,
    });
    markScrollEdges(list);
  }

  /* ── quality mix panel, doubling as the chart filter ──────────────────── */

  function setTierFilter(key) {
    var next = state.tier === key ? '' : key;
    state.tier = next;
    if (els.chart) {
      if (next) els.chart.setAttribute('data-focus', next);
      else els.chart.removeAttribute('data-focus');
    }
    load(false);
  }

  function renderMix(totals) {
    var panel = els.mix;
    if (!panel) return;
    clear(panel);

    var total = (totals.tiers || []).reduce(function (sum, tier) {
      return sum + num(tier.count);
    }, 0);

    if (!total) {
      panel.appendChild(
        el('p', null, 'No stream quality data has been recorded yet.')
      );
      return;
    }

    var rows = el('div', 'mix-rows');
    rows.setAttribute('role', 'group');
    rows.setAttribute('aria-label', 'Filter the chart and lists by delivered quality');

    (totals.tiers || []).forEach(function (tier) {
      var count = num(tier.count);
      var pressed = state.tier === tier.key;
      var row = el('button', 'mix-row');
      row.type = 'button';
      row.setAttribute('data-tier', tier.key);
      row.setAttribute('aria-pressed', pressed ? 'true' : 'false');
      // A tier with nothing recorded has nothing to isolate.
      row.disabled = !count;
      row.title = count
        ? (pressed ? 'Show every tier again' : 'Isolate ' + tier.label + ' in the chart')
        : 'No ' + tier.label + ' streams recorded yet';

      var swatch = el('span', 'swatch');
      swatch.setAttribute('data-tier', tier.key);
      swatch.setAttribute('aria-hidden', 'true');
      row.appendChild(swatch);
      row.appendChild(el('span', 'mix-label', tier.label));
      row.appendChild(
        el(
          'span',
          'pct',
          group(count) + ' · ' + Math.round((count / total) * 100) + '%'
        )
      );

      if (count) {
        row.addEventListener('click', function () { setTierFilter(tier.key); });
      }

      rows.appendChild(row);
    });
    panel.appendChild(rows);
  }

  function emptyState() {
    var box = el('div', 'plays-empty');
    box.appendChild(el('h3', null, 'Nothing recorded yet'));
    box.appendChild(
      el(
        'p',
        null,
        'Plays appear here the first time somebody streams through this gateway. ' +
          'Counts are aggregate and anonymous.'
      )
    );
    return box;
  }

  /* ── boot & refresh ───────────────────────────────────────────────────── */

  var state = {
    totals: { tiers: [] },
    loadedOnce: false,
    needsReload: false,
    loadEpoch: 0,
    query: '',
    tier: '',
    counts: {},
    pollTimer: null,
    controller: null,
    debounceTimer: null,
    heroKey: '',
    heroCoverKey: '',
    ambientUrl: null,
  };

  function updateFilterNote(payload) {
    if (!els.filterNote) return;
    var parts = [];
    if (state.query) parts.push('query "' + state.query + '"');
    if (state.tier) parts.push('tier ' + tierLabel(state.tier));
    if (!parts.length) {
      els.filterNote.hidden = true;
      els.filterNote.textContent = '';
      return;
    }
    var count = payload && Array.isArray(payload.search_results)
      ? payload.search_results.length
      : (payload && Array.isArray(payload.top) ? payload.top.length : 0);
    els.filterNote.hidden = false;
    els.filterNote.textContent = 'Filtered by ' + parts.join(' · ') + ' (' + count + ' matches)';
  }

  function render(payload) {
    state.totals = (payload && payload.totals) || { tiers: [] };
    state.loadedOnce = true;
    state.needsReload = false;
    renderStats(state.totals);
    renderChart(payload && payload.timeline);
    renderTop(payload && (payload.search_results || payload.top));
    renderRecent(payload && payload.recent);
    renderMix(state.totals);
    renderHeroTelemetry(payload);
    updateFilterNote(payload);
    status(null);
    if (els.updated) {
      els.updated.textContent = 'Updated ' + new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    }
    if (window.ConfihSpatial && typeof window.ConfihSpatial.updatePlays === 'function') {
      window.ConfihSpatial.updatePlays(payload, state.tier);
    }
    document.dispatchEvent(new CustomEvent('confih:content'));
  }

  function fail(message) {
    status(message, true);
    if (!state.loadedOnce) {
      renderTop(null);
      renderRecent(null);
      if (els.chart) clear(els.chart);
      if (els.mix) clear(els.mix);
      if (els.stats) clear(els.stats);
      defaultReadout = '';
    }
  }

  function scheduleLivePoll() {
    if (state.pollTimer) {
      window.clearTimeout(state.pollTimer);
      state.pollTimer = null;
    }
    var onPlays = !window.ConfihRouter || window.ConfihRouter.currentView() === 'plays';
    if (document.hidden || !onPlays) return;
    state.pollTimer = window.setTimeout(load, 10000);
  }

  function finishLoad() {
    if (state.doneGlobal) {
      var doneFn = state.doneGlobal;
      state.doneGlobal = null;
      doneFn(260);
    }
    scheduleLivePoll();
  }

  function load() {
    var myEpoch = ++state.loadEpoch;
    if (state.doneGlobal) {
      state.doneGlobal(0);
      state.doneGlobal = null;
    }
    if (window.ConfihLoader && typeof window.ConfihLoader.begin === 'function') {
      state.doneGlobal = window.ConfihLoader.begin();
    }
    if (!state.loadedOnce) status('Loading play history…');

    if (!state.loadedOnce && !state.query && !state.tier &&
        window.ConfihRouter && typeof window.ConfihRouter.bootstrapOverview === 'function') {
      window.ConfihRouter.bootstrapOverview()
        .then(function (ov) {
          if (myEpoch !== state.loadEpoch) return true;
          if (window.ConfihRouter && window.ConfihRouter.currentView() !== 'plays') {
            state.needsReload = true;
            if (state.doneGlobal) { state.doneGlobal(0); state.doneGlobal = null; }
            return true;
          }
          if (ov && ov.plays) {
            render(ov.plays);
            finishLoad();
            return true;
          }
          return false;
        })
        .then(function (handled) {
          if (handled || myEpoch !== state.loadEpoch) return;
          if (window.ConfihRouter && window.ConfihRouter.currentView() !== 'plays') {
            state.needsReload = true;
            if (state.doneGlobal) { state.doneGlobal(0); state.doneGlobal = null; }
            return;
          }
          fetchPlays();
        })
        .catch(function () {
          if (myEpoch !== state.loadEpoch) return;
          if (window.ConfihRouter && window.ConfihRouter.currentView() !== 'plays') {
            state.needsReload = true;
            if (state.doneGlobal) { state.doneGlobal(0); state.doneGlobal = null; }
            return;
          }
          fetchPlays();
        });
      return;
    }

    fetchPlays();
  }

  function fetchPlays() {
    if (state.controller) {
      try { state.controller.abort(); } catch (e) { /* ignore */ }
    }
    var controller = 'AbortController' in window ? new AbortController() : null;
    state.controller = controller;
    var myEpoch = state.loadEpoch;
    var timer = window.setTimeout(function () {
      if (controller) controller.abort();
    }, 10000);

    var params = new URLSearchParams();
    if (state.query) params.set('q', state.query);
    if (state.tier) params.set('tier', state.tier);
    var qs = params.toString();
    var url = API + (qs ? '?' + qs : '');

    fetch(url, {
      headers: { Accept: 'application/json' },
      cache: 'no-store',
      signal: controller ? controller.signal : undefined,
    })
      .then(function (response) {
        if (!response.ok) throw new Error('HTTP ' + response.status);
        return response.json();
      })
      .then(function (payload) {
        if (controller && controller.signal && controller.signal.aborted) return;
        if (myEpoch !== state.loadEpoch) return;
        if (window.ConfihRouter && window.ConfihRouter.currentView() !== 'plays') {
          state.needsReload = true;
          return;
        }
        render(payload);
      })
      .catch(function (error) {
        if (error && error.name === 'AbortError') {
          if (state.controller === controller ||
              (window.ConfihRouter && window.ConfihRouter.currentView() !== 'plays')) {
            state.needsReload = true;
          }
          return;
        }
        state.needsReload = true;
        fail('The play feed is unavailable right now. Try again in a moment.');
      })
      .then(function () {
        window.clearTimeout(timer);
        if (state.controller === controller) state.controller = null;
        finishLoad();
      });
  }

  if (els.search) {
    els.search.addEventListener('input', function () {
      var val = els.search.value.trim();
      if (els.searchClear) els.searchClear.hidden = !val;
      if (state.debounceTimer) window.clearTimeout(state.debounceTimer);
      state.debounceTimer = window.setTimeout(function () {
        state.debounceTimer = null;
        state.query = els.search ? els.search.value.trim() : '';
        load();
      }, 250);
    });
  }

  if (els.searchClear) {
    els.searchClear.addEventListener('click', function () {
      if (state.debounceTimer) {
        window.clearTimeout(state.debounceTimer);
        state.debounceTimer = null;
      }
      if (els.search) els.search.value = '';
      els.searchClear.hidden = true;
      state.query = '';
      load();
    });
  }

  document.addEventListener('visibilitychange', scheduleLivePoll);
  document.addEventListener('confih:viewchange', function (ev) {
    var view = ev && ev.detail && ev.detail.view;
    if (view && view !== 'plays') {
      state.loadEpoch += 1;
      if (state.debounceTimer) {
        window.clearTimeout(state.debounceTimer);
        state.debounceTimer = null;
        state.needsReload = true;
      }
      if (state.controller) {
        try { state.controller.abort(); } catch (e) { /* ignore */ }
        state.controller = null;
        state.needsReload = true;
      }
      if (!state.loadedOnce) {
        state.needsReload = true;
      }
    } else if (view === 'plays') {
      state.query = els.search ? els.search.value.trim() : '';
      if (els.searchClear) els.searchClear.hidden = !state.query;
      if ((els.stats && els.chart) && (!state.loadedOnce || state.needsReload)) {
        state.needsReload = false;
        load(false);
      }
    }
    scheduleLivePoll();
  });

  if (els.stats && els.chart) load(false);
})();
