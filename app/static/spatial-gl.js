/* ============================================================================
   Confih — Tactile Spatial 3D UI & Dynamic Topology Signal Animator
   ----------------------------------------------------------------------------
   Drives straight-aligned HTML + CSS `preserve-3d` hardware layers, optional
   hover/drag 3D orbit, pointer-tracked card perspective tilt + specular sheen,
   and state-reactive perimeter + optical bus animations across BitChord →
   Confih Gateway → Qobuz / Tidal → BitChord:
   - dual / single healthy: white-iridescent prism outbound + emerald stream return
   - failover active: amber failover probe tries offline primary (crimson flash),
     reroutes through backup provider, returns verified emerald lossless stream
   - degraded: amber-white slow probe & cautious recovery return
   - outage / both offline: crimson alert perimeter & aborted branch probe with
     crimson return ripple (never mimics a healthy stream when offline)
   - Strictly static (0deg tilt, 0 rAF, hidden animated beams) under
     `prefers-reduced-motion: reduce`
   ========================================================================= */
(function () {
  'use strict';

  var reduceMedia = window.matchMedia('(prefers-reduced-motion: reduce)');
  var fineMedia = window.matchMedia('(pointer: fine)');

  var TILT_SELECTOR =
    '[data-tilt], .provider-row, .metric, .plays-stat, .step, .def, .card-link, .login-card';

  var NODE_ORDER = ['listener', 'bitchord', 'confih', 'qobuz', 'tidal'];
  var NODE_ALIASES = {
    listener: 'listener',
    user: 'listener',
    client: 'bitchord',
    bitchord: 'bitchord',
    gateway: 'confih',
    confih: 'confih',
    providers: 'providers',
    qobuz: 'qobuz',
    tidal: 'tidal',
  };

  /* One full signal round, in real milliseconds. The choreography below is
     written against FLOW_MS, so this is purely how slowly it plays: at 4600 a
     beam crossed a card's outline in well under a second and read as a flash
     rather than as travel. */
  var CYCLE_MS = 12000;
  var FLOW_MS = 4800;
  var PULSE_DASH_LEN = 38;
  var PULSE_SWEEP_LEN = 100 + PULSE_DASH_LEN;

  var state = {
    initialized: false,
    backend: 'css-3d',
    activeView: 'status',
    theme: 'dark',
    reducedMotion: !!reduceMedia.matches,
    finePointer: !!fineMedia.matches,
    running: false,
    offscreen: false,
    framesRendered: 0,
    rafId: 0,
    frameBudgetMs: 0,
    lastFrameMs: 0,
    flowClockMs: 0,
    selectedNode: null,
    hoveredNode: null,
    hasStats: false,
    hasPlays: false,
    stats: null,
    plays: null,
    activeTier: '',
    hoveredTiltEl: null,
    tiltItems: new Map(),
    boundStages: new WeakSet(),
    flowRefs: null,
    lastRoundKey: '',
    flowMode: 'outage',
    flowTarget: 'qobuz',
    stages: {
      status: {
        key: 'status',
        baseRx: 0,
        baseRy: 0,
        curRx: 0,
        curRy: 0,
        orbitRx: 0,
        orbitRy: 0,
        hoverRx: 0,
        hoverRy: 0,
        dragging: false,
        dragMoved: false,
        wasDragged: false,
        startX: 0,
        startY: 0,
        windowEl: null,
        stackEl: null,
      },
      info: {
        key: 'info',
        flat: true,
        baseRx: 0,
        baseRy: 0,
        curRx: 0,
        curRy: 0,
        orbitRx: 0,
        orbitRy: 0,
        hoverRx: 0,
        hoverRy: 0,
        dragging: false,
        dragMoved: false,
        wasDragged: false,
        startX: 0,
        startY: 0,
        windowEl: null,
        stackEl: null,
      },
    },
  };

  function clamp(v, lo, hi) {
    return v < lo ? lo : v > hi ? hi : v;
  }

  function normalizeView(view) {
    var v = String(view || '').toLowerCase();
    if (v === 'plays') return 'plays';
    if (v === 'about' || v === 'support' || v === 'guide' || v === 'info') return 'info';
    return 'status';
  }

  /* The beam loop only has something to draw when a rack is on screen. On a
     phone the status rack starts below the fold, so without this the page
     animates nine SVG paths a second for a reader looking at the headline. */
  var stageVisibility = new Map();
  var stageObserver =
    typeof window.IntersectionObserver === 'function'
      ? new window.IntersectionObserver(
          function (entries) {
            entries.forEach(function (entry) {
              stageVisibility.set(entry.target, entry.isIntersecting);
            });
            var visible = false;
            stageVisibility.forEach(function (isVisible) {
              if (isVisible) visible = true;
            });
            state.offscreen = !visible;
            if (state.offscreen) {
              stopRaf();
            } else {
              ensureRaf();
            }
          },
          { rootMargin: '0px 0px' }
        )
      : null;

  function canRunLoop() {
    return !state.reducedMotion && !document.hidden && !state.offscreen;
  }

  function canPointerTilt() {
    return !state.reducedMotion && state.finePointer && !document.hidden;
  }

  function setText(el, text) {
    if (el && el.textContent !== text) el.textContent = text;
  }

  /* ── Stage & Signal Node Element Lookup ──────────────────────────────── */

  var TONE_COLORS = {
    prism: ['#7dd3fc', '#c4b5fd', '#f9a8d4', '#fef3c7', '#ffffff'],
    stream: ['#34d399', '#6ee7b7', '#a7f3d0', '#ecfdf5', '#ffffff'],
    failover: ['#f59e0b', '#fbbf24', '#fde68a', '#fef3c7', '#ffffff'],
    blocked: ['#e11d48', '#f43f5e', '#fb7185', '#fecdd3', '#fff1f2'],
  };

  function syncGradVectorAndTone(grad, tone, tx, ty, hx, hy) {
    if (!grad) return;
    if (grad._tone !== tone) {
      grad._tone = tone;
      var colors = TONE_COLORS[tone] || TONE_COLORS.prism;
      var stops = grad.children;
      for (var i = 0; i < stops.length && i < colors.length; i++) {
        stops[i].setAttribute('stop-color', colors[i]);
      }
    }
    var dx = hx - tx;
    var dy = hy - ty;
    if (dx * dx + dy * dy < 9) {
      grad.setAttribute('x1', (hx - 12).toFixed(2));
      grad.setAttribute('y1', (hy - 12).toFixed(2));
    } else {
      grad.setAttribute('x1', tx.toFixed(2));
      grad.setAttribute('y1', ty.toFixed(2));
    }
    grad.setAttribute('x2', hx.toFixed(2));
    grad.setAttribute('y2', hy.toFixed(2));
  }

  function buildNodeRef(el, id) {
    if (!el) return null;
    return {
      el: el,
      id: id,
      svg: el.querySelector('.rack-beam-svg'),
      glow: el.querySelector('[data-beam-glow]'),
      core: el.querySelector('[data-beam-core]'),
      spark: el.querySelector('[data-beam-spark]'),
      grad: document.getElementById('beam-grad-' + id),
      w: 0,
      h: 0,
      r: 0,
      totalLen: 0,
      measuredAt: 0,
    };
  }

  function buildDoubleLoopPath(w, h, r) {
    var p = 0.6;
    var x0 = p;
    var y0 = p;
    var x1 = Math.max(p + 10, w - p);
    var y1 = Math.max(p + 10, h - p);
    var mx = w * 0.5;
    if (Math.abs(w - h) < 2 && r >= Math.min(w, h) * 0.45) {
      var cr = Math.min(x1 - x0, y1 - y0) * 0.5;
      var my = (y0 + y1) * 0.5;
      var clap =
        ' A ' + cr.toFixed(2) + ' ' + cr.toFixed(2) + ' 0 1 1 ' + mx.toFixed(2) + ' ' + (my + cr).toFixed(2) +
        ' A ' + cr.toFixed(2) + ' ' + cr.toFixed(2) + ' 0 1 1 ' + mx.toFixed(2) + ' ' + y0.toFixed(2);
      return 'M ' + mx.toFixed(2) + ' ' + y0.toFixed(2) + clap + clap;
    }
    var rr = clamp((r || 15) - p, 4, Math.min((x1 - x0) * 0.45, (y1 - y0) * 0.45));
    var lap =
      ' L ' + (x1 - rr).toFixed(2) + ' ' + y0.toFixed(2) +
      ' A ' + rr.toFixed(2) + ' ' + rr.toFixed(2) + ' 0 0 1 ' + x1.toFixed(2) + ' ' + (y0 + rr).toFixed(2) +
      ' L ' + x1.toFixed(2) + ' ' + (y1 - rr).toFixed(2) +
      ' A ' + rr.toFixed(2) + ' ' + rr.toFixed(2) + ' 0 0 1 ' + (x1 - rr).toFixed(2) + ' ' + y1.toFixed(2) +
      ' L ' + (x0 + rr).toFixed(2) + ' ' + y1.toFixed(2) +
      ' A ' + rr.toFixed(2) + ' ' + rr.toFixed(2) + ' 0 0 1 ' + x0.toFixed(2) + ' ' + (y1 - rr).toFixed(2) +
      ' L ' + x0.toFixed(2) + ' ' + (y0 + rr).toFixed(2) +
      ' A ' + rr.toFixed(2) + ' ' + rr.toFixed(2) + ' 0 0 1 ' + (x0 + rr).toFixed(2) + ' ' + y0.toFixed(2) +
      ' L ' + mx.toFixed(2) + ' ' + y0.toFixed(2);
    return 'M ' + mx.toFixed(2) + ' ' + y0.toFixed(2) + lap + lap;
  }

  /* ── Path sampling, cached ────────────────────────────────────────────────
     getPointAtLength() re-walks the path every call. Asking it for three
     points per node and two per bus, sixty times a second, spent more main
     thread than every animation on the page combined — and a phone has far
     less to spend. Sample each path once into a flat table, then interpolate:
     the geometry only changes when the rack is re-measured. */
  var LUT_STEPS = 128;
  var lutCache = new WeakMap();
  var pointOut = { x: 0, y: 0 };

  function lutFor(pathEl, length) {
    var entry = lutCache.get(pathEl);
    if (entry && Math.abs(entry.len - length) < 0.5) return entry;
    var n = LUT_STEPS;
    var pts = new Float32Array((n + 1) * 2);
    for (var i = 0; i <= n; i += 1) {
      var pt = pathEl.getPointAtLength((i / n) * length);
      pts[i * 2] = pt.x;
      pts[i * 2 + 1] = pt.y;
    }
    entry = { len: length, n: n, pts: pts };
    lutCache.set(pathEl, entry);
    return entry;
  }

  function pointAtFraction(pathEl, length, frac, out) {
    var target = out || pointOut;
    if (!(length > 0)) {
      target.x = 0;
      target.y = 0;
      return target;
    }
    var lut = lutFor(pathEl, length);
    var pos = clamp(frac, 0, 1) * lut.n;
    var i0 = Math.min(lut.n - 1, Math.floor(pos));
    var k = pos - i0;
    var x0 = lut.pts[i0 * 2];
    var y0 = lut.pts[i0 * 2 + 1];
    var x1 = lut.pts[(i0 + 1) * 2];
    var y1 = lut.pts[(i0 + 1) * 2 + 1];
    target.x = x0 + (x1 - x0) * k;
    target.y = y0 + (y1 - y0) * k;
    return target;
  }

  function syncNodePerimeter(node, force) {
    if (!node || !node.el || !node.core || !node.svg) return;
    /* Reading offsetWidth is a forced layout; doing it for every node on every
       frame was the main source of stutter on a busy page. A quarter second is
       far below what a resize can be perceived at, and layout changes that
       matter (breakpoint swap, font load) are caught by the resize handler. */
    var now = Date.now();
    if (!force && node.measuredAt && now - node.measuredAt < 250) return;
    node.measuredAt = now;
    var w = node.el.offsetWidth;
    var h = node.el.offsetHeight;
    if (w < 20 || h < 20) return;
    var r = parseFloat(window.getComputedStyle(node.el).borderTopLeftRadius) || 15;
    if (w === node.w && h === node.h && Math.abs(r - node.r) < 0.2 && node.totalLen > 0) return;
    node.w = w;
    node.h = h;
    node.r = r;
    node.svg.setAttribute('viewBox', '0 0 ' + w + ' ' + h);
    var d = buildDoubleLoopPath(w, h, r);
    node.core.setAttribute('d', d);
    lutCache.delete(node.core);
    if (node.glow) lutCache.delete(node.glow);
    if (node.glow) node.glow.setAttribute('d', d);
    try {
      node.totalLen = typeof node.core.getTotalLength === 'function' ? node.core.getTotalLength() : 0;
    } catch (err) {
      node.totalLen = 0;
    }
  }

  function syncAllPerimeters() {
    var refs = state.flowRefs;
    if (!refs || !refs.nodes) return;
    NODE_ORDER.forEach(function (id) {
      syncNodePerimeter(refs.nodes[id], true);
    });
  }

  function collectFlowRefs(rack) {
    if (!rack) return null;
    var refs = {
      rack: rack,
      nodes: {
        listener: buildNodeRef(rack.querySelector('[data-rack-node="listener"]'), 'listener'),
        bitchord: buildNodeRef(rack.querySelector('[data-rack-node="bitchord"]'), 'bitchord'),
        confih: buildNodeRef(rack.querySelector('[data-rack-node="confih"]'), 'confih'),
        qobuz: buildNodeRef(rack.querySelector('[data-rack-node="qobuz"]'), 'qobuz'),
        tidal: buildNodeRef(rack.querySelector('[data-rack-node="tidal"]'), 'tidal'),
      },
      busListener: {
        pulse: document.getElementById('bus-pulse-listener'),
        bead: document.getElementById('bus-bead-listener'),
        grad: document.getElementById('bus-grad-listener'),
      },
      busTop: {
        pulse: document.getElementById('bus-pulse-top'),
        bead: document.getElementById('bus-bead-top'),
        grad: document.getElementById('bus-grad-top'),
      },
      busQobuz: {
        pulse: document.getElementById('bus-pulse-qobuz'),
        bead: document.getElementById('bus-bead-qobuz'),
        grad: document.getElementById('bus-grad-qobuz'),
      },
      busTidal: {
        pulse: document.getElementById('bus-pulse-tidal'),
        bead: document.getElementById('bus-bead-tidal'),
        grad: document.getElementById('bus-grad-tidal'),
      },
    };
    NODE_ORDER.forEach(function (id) {
      syncNodePerimeter(refs.nodes[id]);
    });
    return refs;
  }

  function refreshStageElements() {
    var statusWin = document.querySelector('.hero-stage-slot[data-spatial-slot="status"]');
    var statusRack = document.getElementById('status-rack-3d');
    state.stages.status.windowEl = statusWin;
    state.stages.status.stackEl = statusRack;
    state.flowRefs = collectFlowRefs(statusRack);
    if (statusWin) bindStageOrbit(statusWin, state.stages.status);

    var infoWin = document.getElementById('about-signal-plate');
    var infoStack = infoWin ? infoWin.querySelector('.arch-3d-stack') : null;
    state.stages.info.windowEl = infoWin;
    state.stages.info.stackEl = infoStack;
    if (infoWin) bindStageOrbit(infoWin, state.stages.info);

    if (stageObserver) {
      [statusWin, infoWin].forEach(function (el) {
        if (!el || stageVisibility.has(el)) return;
        stageVisibility.set(el, false);
        stageObserver.observe(el);
      });
    }
  }

  function targetStageAngles(stage) {
    if (state.reducedMotion || (!state.finePointer && !stage.dragging && stage.orbitRx === 0 && stage.orbitRy === 0)) {
      return { rx: stage.baseRx, ry: stage.baseRy };
    }
    return {
      rx: clamp(stage.baseRx + stage.orbitRx + stage.hoverRx, -22, 24),
      ry: clamp(stage.baseRy + stage.orbitRy + stage.hoverRy, -32, 32),
    };
  }

  function writeStageVars(stage, rx, ry) {
    var rxStr = rx.toFixed(2) + 'deg';
    var ryStr = ry.toFixed(2) + 'deg';
    /* The status rack is a schematic and can take a real 3D tilt. The About
       plates carry type: rotating them through a perspective projection is
       what made their headings look low quality, so that stage gets the same
       pointer input as a 2D parallax shift instead. */
    var flat = !!stage.flat;
    var sx = flat ? (ry * 1.1).toFixed(1) + 'px' : '';
    var sy = flat ? (rx * 1.1).toFixed(1) + 'px' : '';
    [stage.windowEl, stage.stackEl].forEach(function (el) {
      if (!el) return;
      if (flat) {
        el.style.setProperty('--rack-shift-x', sx);
        el.style.setProperty('--rack-shift-y', sy);
        return;
      }
      el.style.setProperty('--rack-rx', rxStr);
      el.style.setProperty('--rack-ry', ryStr);
    });
  }

  function snapStagesImmediate() {
    Object.keys(state.stages).forEach(function (k) {
      var st = state.stages[k];
      var tgt = targetStageAngles(st);
      st.curRx = tgt.rx;
      st.curRy = tgt.ry;
      writeStageVars(st, st.curRx, st.curRy);
    });
  }

  /* ── Dynamic Topology Resolver & Function-Aware Beam Rendering ───────── */

  function setNodeSegment(node, vPos, minS, maxS, beamLen, opacity, dirMode, tone, showSpark) {
    if (!node || !node.el) return;
    var el = node.el;
    if (opacity <= 0.02 || vPos === null) {
      el.style.setProperty('--beam-op', '0');
      el.removeAttribute('data-flow-tone');
      el.removeAttribute('data-flow-dir');
      el.classList.remove('is-flow-active', 'is-flow-return', 'is-flow-failover', 'is-flow-blocked');
      if (node.core) {
        node.core.style.strokeDasharray = '0 500';
        node.core.style.strokeDashoffset = '0';
      }
      if (node.glow) {
        node.glow.style.strokeDasharray = '0 500';
        node.glow.style.strokeDashoffset = '0';
      }
      if (node.spark) node.spark.style.opacity = '0';
      return;
    }

    syncNodePerimeter(node);
    var segStart = Math.max(minS, vPos - beamLen);
    var segEnd = Math.min(maxS, vPos);
    var segLen = Math.max(0, segEnd - segStart);
    if (segLen <= 0.25) {
      el.style.setProperty('--beam-op', '0');
      if (node.spark) node.spark.style.opacity = '0';
      return;
    }

    var t = tone || (dirMode === 'return' ? 'stream' : 'prism');
    var opStr = clamp(opacity, 0, 1).toFixed(3);
    el.style.setProperty('--beam-op', opStr);
    el.setAttribute('data-flow-tone', t);
    el.classList.add('is-flow-active');
    el.classList.toggle('is-flow-return', t === 'stream');
    el.classList.toggle('is-flow-failover', t === 'failover');
    el.classList.toggle('is-flow-blocked', t === 'blocked');
    if (dirMode === 'return') {
      el.setAttribute('data-flow-dir', 'return');
    } else {
      el.removeAttribute('data-flow-dir');
    }

    var da = segLen.toFixed(2) + ' 500';
    var doff = (-segStart).toFixed(2);
    if (node.core) {
      node.core.style.strokeDasharray = da;
      node.core.style.strokeDashoffset = doff;
    }
    if (node.glow) {
      node.glow.style.strokeDasharray = da;
      node.glow.style.strokeDashoffset = doff;
    }

    if (node.totalLen > 0) {
      var ptTail = pointAtFraction(node.core, node.totalLen, segStart / 200);
      var tailX = ptTail.x;
      var tailY = ptTail.y;
      var ptTip = pointAtFraction(node.core, node.totalLen, segEnd / 200);
      if (node.grad) {
        syncGradVectorAndTone(node.grad, t, tailX, tailY, ptTip.x, ptTip.y);
      }
      if (node.spark) {
        if (showSpark !== false && vPos >= minS && vPos <= maxS) {
          var ptHead = pointAtFraction(node.core, node.totalLen, vPos / 200);
          var edgeVis = Math.min(1, segLen / 4.5);
          node.spark.setAttribute('cx', ptHead.x.toFixed(2));
          node.spark.setAttribute('cy', ptHead.y.toFixed(2));
          node.spark.style.opacity = (edgeVis * opacity).toFixed(3);
        } else {
          node.spark.style.opacity = '0';
        }
      }
    }
  }

  function setBusSegment(bus, active, travel, maxSpan, beamLen, direction, opacity, tone) {
    if (!bus || !bus.pulse) return;
    var pulse = bus.pulse;
    var bead = bus.bead;
    if (!active || opacity <= 0.02) {
      pulse.style.opacity = '0';
      pulse.style.strokeDasharray = '0 300';
      pulse.style.strokeDashoffset = '0';
      pulse.classList.remove('is-return');
      pulse.removeAttribute('data-bus-tone');
      if (bead) {
        bead.style.opacity = '0';
        bead.classList.remove('is-return');
        bead.removeAttribute('data-bus-tone');
      }
      return;
    }

    var isReturn = direction === 'return';
    var span = clamp(maxSpan || 100, 20, 100);
    pulse.classList.toggle('is-return', isReturn);
    var t = tone || (isReturn ? 'stream' : 'prism');
    if (tone === 'failover' || tone === 'blocked') {
      pulse.setAttribute('data-bus-tone', tone);
    } else {
      pulse.removeAttribute('data-bus-tone');
    }

    var segStart;
    var segEnd;
    var headS;
    if (!isReturn) {
      segStart = Math.max(0, travel - beamLen);
      segEnd = Math.min(span, travel);
      headS = travel;
    } else {
      headS = 100 - travel;
      segStart = Math.max(0, headS);
      segEnd = Math.min(100, headS + beamLen);
    }

    var segLen = Math.max(0, segEnd - segStart);
    if (segLen <= 0.25) {
      pulse.style.opacity = '0';
      if (bead) bead.style.opacity = '0';
      return;
    }

    pulse.style.strokeDasharray = segLen.toFixed(2) + ' 300';
    pulse.style.strokeDashoffset = (-segStart).toFixed(2);
    pulse.style.opacity = clamp(opacity, 0, 1).toFixed(3);

    if (bus.totalLen === undefined) {
      bus.totalLen = typeof pulse.getTotalLength === 'function' ? pulse.getTotalLength() : 0;
    }
    var totalLen = bus.totalLen;
    if (totalLen > 0) {
      var tailS = isReturn ? segEnd : segStart;
      var tipS = isReturn ? segStart : segEnd;
      var ptTail = pointAtFraction(pulse, totalLen, tailS / 100);
      var tailX = ptTail.x;
      var tailY = ptTail.y;
      var ptTip = pointAtFraction(pulse, totalLen, tipS / 100);
      if (bus.grad) {
        syncGradVectorAndTone(bus.grad, t, tailX, tailY, ptTip.x, ptTip.y);
      }
      if (bead) {
        bead.classList.toggle('is-return', isReturn);
        if (tone === 'failover' || tone === 'blocked') {
          bead.setAttribute('data-bus-tone', tone);
        } else {
          bead.removeAttribute('data-bus-tone');
        }
        var inPortRange = !isReturn ? (headS >= 0 && headS <= span) : (headS >= 0 && headS <= 100);
        if (inPortRange) {
          var ptHead = pointAtFraction(pulse, totalLen, headS / 100);
          bead.setAttribute('cx', ptHead.x.toFixed(2));
          bead.setAttribute('cy', ptHead.y.toFixed(2));
          bead.style.opacity = clamp(opacity, 0, 1).toFixed(3);
        } else {
          bead.style.opacity = '0';
        }
      }
    }
  }

  function clearFlowEffects() {
    var refs = state.flowRefs;
    if (!refs) return;
    NODE_ORDER.forEach(function (id) {
      setNodeSegment(refs.nodes[id], null, 0, 100, 32, 0, 'req', 'prism', false);
    });
    setBusSegment(refs.busListener, false, 0, 100, 40, 'req', 0, 'prism');
    setBusSegment(refs.busTop, false, 0, 100, 40, 'req', 0, 'prism');
    setBusSegment(refs.busQobuz, false, 0, 100, 40, 'req', 0, 'prism');
    setBusSegment(refs.busTidal, false, 0, 100, 40, 'req', 0, 'prism');
  }

  function resolveFlowMode(roundIndex) {
    var stats = state.stats;
    if (!stats) {
      return {
        mode: 'outage',
        target: roundIndex % 2 === 0 ? 'qobuz' : 'tidal',
        failedPrimary: null,
        qKind: 'unconfigured',
        tKind: 'unconfigured',
        qUsable: false,
        tUsable: false,
        pace: 0.92,
        readout: 'Checking upstream connectivity…',
      };
    }

    var services = stats.services || {};
    var qSvc = services.qobuz || {};
    var tSvc = services.tidal || {};
    var qKind = providerStateKind(qSvc);
    var tKind = providerStateKind(tSvc);
    var overall = String(stats.status || '').toLowerCase();
    var primary = String(stats.primary_provider || services.primary_provider || '').toLowerCase();

    var qUsable = qKind === 'online' || qKind === 'degraded';
    var tUsable = tKind === 'online' || tKind === 'degraded';

    // 1. Outage: neither provider is connected/online
    if (overall === 'outage' || (!qUsable && !tUsable)) {
      var probeTarget = (qKind === 'offline' && tKind !== 'offline')
        ? 'qobuz'
        : ((tKind === 'offline' && qKind !== 'offline')
          ? 'tidal'
          : (primary === 'qobuz' ? 'qobuz' : (primary === 'tidal' ? 'tidal' : (roundIndex % 2 === 0 ? 'qobuz' : 'tidal'))));
      return {
        mode: 'outage',
        target: probeTarget,
        failedPrimary: null,
        qKind: qKind,
        tKind: tKind,
        qUsable: false,
        tUsable: false,
        pace: 0.9,
        readout: 'Gateway offline · Upstream providers unavailable',
      };
    }

    // 2. Active Failover: primary is explicitly down/failed while backup serves
    var primaryHealthy = primary === 'qobuz'
      ? qUsable
      : (primary === 'tidal' ? tUsable : (qUsable || tUsable));
    if (stats.failover_active || (stats.primary_online === false && !primaryHealthy && (qUsable || tUsable))) {
      var serving = qUsable ? 'qobuz' : 'tidal';
      var failed = serving === 'qobuz' ? 'tidal' : 'qobuz';
      var fName = failed === 'qobuz' ? 'Qobuz' : 'Tidal';
      var sName = serving === 'qobuz' ? 'Qobuz' : 'Tidal';
      return {
        mode: 'failover',
        target: serving,
        failedPrimary: failed,
        qKind: qKind,
        tKind: tKind,
        qUsable: qUsable,
        tUsable: tUsable,
        pace: 0.9,
        readout: 'Failover · ' + fName + ' offline → ' + sName + ' serving',
      };
    }

    // 3. Single Active Engine
    if (qUsable && !tUsable) {
      var isDegQ = qKind === 'degraded' || overall === 'degraded';
      return {
        mode: isDegQ ? 'degraded' : 'single',
        target: 'qobuz',
        failedPrimary: null,
        qKind: qKind,
        tKind: tKind,
        qUsable: true,
        tUsable: false,
        pace: isDegQ ? 0.78 : 1.0,
        readout: (isDegQ ? 'Degraded' : 'Active route') + ' · BitChord → Confih → Qobuz → BitChord',
      };
    }
    if (tUsable && !qUsable) {
      var isDegT = tKind === 'degraded' || overall === 'degraded';
      return {
        mode: isDegT ? 'degraded' : 'single',
        target: 'tidal',
        failedPrimary: null,
        qKind: qKind,
        tKind: tKind,
        qUsable: false,
        tUsable: true,
        pace: isDegT ? 0.78 : 1.0,
        readout: (isDegT ? 'Degraded' : 'Active route') + ' · BitChord → Confih → Tidal → BitChord',
      };
    }

    // 4. Both Usable: alternates Qobuz <-> Tidal stream rounds cleanly
    var chosen = roundIndex % 2 === 0 ? 'qobuz' : 'tidal';
    var chosenKind = chosen === 'qobuz' ? qKind : tKind;
    var chosenLabel = chosen === 'qobuz' ? 'Qobuz' : 'Tidal';
    var isDeg = chosenKind === 'degraded' || overall === 'degraded';
    return {
      mode: isDeg ? 'degraded' : 'dual',
      target: chosen,
      failedPrimary: null,
      qKind: qKind,
      tKind: tKind,
      qUsable: true,
      tUsable: true,
      pace: isDeg ? 0.78 : 1.0,
      readout: (isDeg ? 'Degraded' : 'Stream round') + ' · BitChord → Confih → ' + chosenLabel + ' → BitChord',
    };
  }

  function selectedNodeTone(nodeId) {
    if (!state.stats) return nodeId === 'confih' ? 'blocked' : 'prism';
    var services = state.stats.services || {};
    var overall = String(state.stats.status || '').toLowerCase();
    if (nodeId === 'listener' || nodeId === 'bitchord') return overall === 'outage' ? 'failover' : 'prism';
    if (nodeId === 'confih') {
      if (overall === 'outage') return 'blocked';
      if (state.stats.failover_active || overall === 'degraded') return 'failover';
      return 'prism';
    }
    if (nodeId === 'qobuz' || nodeId === 'tidal') {
      var kind = providerStateKind(services[nodeId] || {});
      if (kind === 'online') return 'prism';
      if (kind === 'degraded' || kind === 'standby') return 'failover';
      return 'blocked';
    }
    return 'prism';
  }

  function evalNodeLeg(t, tStart, headDur, minS, maxS, beamLen, fadeInMs, fadeOutMs) {
    var span = Math.abs(maxS - minS);
    var drainDur = (beamLen / Math.max(1, span)) * headDur;
    var tEnd = tStart + headDur + drainDur;
    if (t < tStart || t > tEnd) return null;
    var frac = clamp((t - tStart) / headDur, 0, 1 + drainDur / headDur);
    var vPos = minS + frac * (maxS - minS);
    var op = 1;
    if (fadeInMs > 0 && t - tStart < fadeInMs) {
      op = Math.min(op, (t - tStart) / fadeInMs);
    }
    if (fadeOutMs > 0 && tEnd - t < fadeOutMs) {
      op = Math.min(op, (tEnd - t) / fadeOutMs);
    }
    return { vPos: vPos, op: clamp(op, 0, 1), localT: clamp((t - tStart) / headDur, 0, 1) };
  }

  function evalBusLeg(t, tStart, headDur, span, beamLen) {
    var s = clamp(span || 100, 20, 100);
    var drainDur = (beamLen / s) * headDur;
    var tEnd = tStart + headDur + drainDur;
    if (t < tStart || t > tEnd) return null;
    var travel = ((t - tStart) / headDur) * s;
    return { travel: travel, op: 1 };
  }

  function stepSignalFlow(dtMs) {
    var refs = state.flowRefs;
    if (!refs || !refs.rack || state.activeView !== 'status') return false;

    var approxRound = Math.floor(state.flowClockMs / CYCLE_MS);
    var flow = resolveFlowMode(approxRound);
    state.flowClockMs += dtMs * flow.pace;

    var roundIdx = Math.floor(state.flowClockMs / CYCLE_MS);
    var ms = ((state.flowClockMs % CYCLE_MS) / CYCLE_MS) * FLOW_MS;
    flow = resolveFlowMode(roundIdx);

    state.flowMode = flow.mode;
    state.flowTarget = flow.target;
    refs.rack.setAttribute('data-flow-mode', flow.mode);

    // Paused selection: smooth seamless perimeter orbit around the selected node
    if (state.selectedNode && refs.nodes[state.selectedNode]) {
      var idlePos = 50 + ((state.flowClockMs * 0.044) % 100);
      var selTone = selectedNodeTone(state.selectedNode);
      NODE_ORDER.forEach(function (id) {
        if (id === state.selectedNode) {
          setNodeSegment(refs.nodes[id], idlePos, 0, 200, 44, 0.96, selTone === 'stream' ? 'return' : 'req', selTone, true);
        } else {
          setNodeSegment(refs.nodes[id], null, 0, 100, 32, 0, 'req', 'prism', false);
        }
      });
      setBusSegment(refs.busListener, false, 0, 100, 40, 'req', 0, 'prism');
      setBusSegment(refs.busTop, false, 0, 100, 40, 'req', 0, 'prism');
      setBusSegment(refs.busQobuz, false, 0, 100, 40, 'req', 0, 'prism');
      setBusSegment(refs.busTidal, false, 0, 100, 40, 'req', 0, 'prism');
      return true;
    }

    var roundKey = flow.mode + ':' + flow.target;
    if (roundKey !== state.lastRoundKey) {
      state.lastRoundKey = roundKey;
      if (!state.hoveredNode && !state.selectedNode && !state.stages.status.dragging) {
        setText(document.getElementById('spatial-inspect-readout'), flow.readout);
      }
    }

    var nState = { listener: null, bitchord: null, confih: null, qobuz: null, tidal: null };
    var busListenerSt = null;
    var busTopSt = null;
    var busQSt = null;
    var busTSt = null;

    function putNode(id, res, minS, maxS, beamLen, dir, tone, showSpark) {
      if (!res) return;
      nState[id] = {
        vPos: res.vPos,
        minS: minS,
        maxS: maxS,
        beamLen: beamLen,
        op: res.op,
        dir: dir,
        tone: tone,
        spark: showSpark !== false,
      };
    }

    if (flow.mode === 'outage') {
      // OUTAGE: Listener -> BitChord -> Confih Gateway (Offline) blocks in crimson -> error rebounds UP BitChord -> Listener.
      // Downstream provider links and Qobuz/Tidal cards remain completely untouched while the gateway is offline.
      var lOut = evalNodeLeg(ms, 40, 440, 50, 150, 52, 90, 0);
      putNode('listener', lOut, 50, 150, 52, 'req', 'failover', true);

      var lBusDown = evalBusLeg(ms, 480, 150, 100, 54);
      if (lBusDown) busListenerSt = { active: true, travel: lBusDown.travel, span: 100, len: 54, dir: 'req', op: 0.96, tone: 'failover' };

      var bOut = evalNodeLeg(ms, 630, 720, 0, 150, 34, 0, 0);
      putNode('bitchord', bOut, 0, 150, 34, 'req', 'failover', true);

      var topDown = evalBusLeg(ms, 1350, 200, 100, 44);
      if (topDown) busTopSt = { active: true, travel: topDown.travel, span: 100, len: 44, dir: 'req', op: 0.96, tone: 'failover' };

      var cBlk = evalNodeLeg(ms, 1550, 1060, 0, 100, 36, 0, 0);
      putNode('confih', cBlk, 0, 100, 36, 'req', 'blocked', true);

      var topUpErr = evalBusLeg(ms, 2610, 210, 100, 46);
      if (topUpErr) busTopSt = { active: true, travel: topUpErr.travel, span: 100, len: 46, dir: 'return', op: 0.94, tone: 'blocked' };

      var bErr = evalNodeLeg(ms, 2820, 340, 50, 100, 34, 0, 0);
      putNode('bitchord', bErr, 50, 100, 34, 'return', 'blocked', true);

      var lBusUpErr = evalBusLeg(ms, 3160, 150, 100, 54);
      if (lBusUpErr) busListenerSt = { active: true, travel: lBusUpErr.travel, span: 100, len: 54, dir: 'return', op: 0.94, tone: 'blocked' };

      var lErr = evalNodeLeg(ms, 3310, 300, 50, 120, 50, 0, 180);
      putNode('listener', lErr, 50, 120, 50, 'return', 'blocked', true);
    } else if (flow.mode === 'failover') {
      // FAILOVER: Listener -> BitChord -> Confih -> failed Primary crimson bounce -> Backup reroute -> Emerald return to Listener
      var fIsQ = flow.failedPrimary === 'qobuz';

      var lReqF = evalNodeLeg(ms, 20, 380, 50, 150, 52, 80, 0);
      putNode('listener', lReqF, 50, 150, 52, 'req', 'prism', true);

      var lBusF1 = evalBusLeg(ms, 400, 140, 100, 54);
      if (lBusF1) busListenerSt = { active: true, travel: lBusF1.travel, span: 100, len: 54, dir: 'req', op: 1, tone: 'prism' };

      var bReqF = evalNodeLeg(ms, 540, 620, 0, 150, 32, 0, 0);
      putNode('bitchord', bReqF, 0, 150, 32, 'req', 'prism', true);

      var topF1 = evalBusLeg(ms, 1160, 170, 100, 44);
      if (topF1) busTopSt = { active: true, travel: topF1.travel, span: 100, len: 44, dir: 'req', op: 1, tone: 'prism' };

      var cF1 = evalNodeLeg(ms, 1330, 740, 0, 150, 34, 0, 0);
      if (cF1) putNode('confih', cF1, 0, 150, 34, 'req', cF1.localT > 0.5 ? 'failover' : 'prism', true);

      var failBusDown = evalBusLeg(ms, 2070, 190, 100, 44);
      if (failBusDown) {
        var fObj = { active: true, travel: failBusDown.travel, span: 100, len: 44, dir: 'req', op: 0.98, tone: 'failover' };
        if (fIsQ) busQSt = fObj; else busTSt = fObj;
      }

      var failChip = evalNodeLeg(ms, 2260, 230, 0, 38, 30, 0, 60);
      if (failChip) {
        putNode(flow.failedPrimary, failChip, 0, 38, 30, 'req', 'blocked', true);
      }

      var failBusUp = evalBusLeg(ms, 2440, 190, 100, 46);
      if (failBusUp) {
        var rObj = { active: true, travel: failBusUp.travel, span: 100, len: 46, dir: 'return', op: 0.96, tone: 'blocked' };
        if (fIsQ) busQSt = rObj; else busTSt = rObj;
      }

      var cF2 = evalNodeLeg(ms, 2630, 300, 100, 150, 32, 0, 0);
      if (cF2) putNode('confih', cF2, 100, 150, 32, 'req', cF2.localT < 0.45 ? 'failover' : 'prism', true);

      var okBusDown = evalBusLeg(ms, 2930, 190, 100, 44);
      if (okBusDown) {
        var dObj = { active: true, travel: okBusDown.travel, span: 100, len: 44, dir: 'req', op: 1, tone: 'prism' };
        if (flow.target === 'qobuz') busQSt = dObj; else busTSt = dObj;
      }

      var okChip = evalNodeLeg(ms, 3120, 520, 0, 100, 42, 0, 0);
      if (okChip) {
        var chipTone = okChip.localT >= 0.56 ? 'stream' : 'prism';
        var chipDir = okChip.localT >= 0.56 ? 'return' : 'req';
        putNode(flow.target, okChip, 0, 100, 42, chipDir, chipTone, true);
      }

      var okBusUp = evalBusLeg(ms, 3640, 190, 100, 46);
      if (okBusUp) {
        var uObj = { active: true, travel: okBusUp.travel, span: 100, len: 46, dir: 'return', op: 1, tone: 'stream' };
        if (flow.target === 'qobuz') busQSt = uObj; else busTSt = uObj;
      }

      var cF3 = evalNodeLeg(ms, 3830, 260, 50, 100, 32, 0, 0);
      putNode('confih', cF3, 50, 100, 32, 'return', 'stream', true);

      var topF2 = evalBusLeg(ms, 4090, 160, 100, 44);
      if (topF2) busTopSt = { active: true, travel: topF2.travel, span: 100, len: 44, dir: 'return', op: 1, tone: 'stream' };

      var bRetF = evalNodeLeg(ms, 4250, 220, 50, 100, 32, 0, 0);
      putNode('bitchord', bRetF, 50, 100, 32, 'return', 'stream', true);

      var lBusF2 = evalBusLeg(ms, 4470, 130, 100, 54);
      if (lBusF2) busListenerSt = { active: true, travel: lBusF2.travel, span: 100, len: 54, dir: 'return', op: 1, tone: 'stream' };

      var lRetF = evalNodeLeg(ms, 4600, 130, 50, 110, 48, 0, 120);
      putNode('listener', lRetF, 50, 110, 48, 'return', 'stream', true);
    } else {
      // DUAL / SINGLE / DEGRADED: Listener -> BitChord -> Confih -> Engine -> Stream returns all the way back to Listener
      var reqTone = flow.mode === 'degraded' ? 'failover' : 'prism';

      var lReq = evalNodeLeg(ms, 30, 420, 50, 150, 52, 90, 0);
      putNode('listener', lReq, 50, 150, 52, 'req', reqTone, true);

      var lBusDownN = evalBusLeg(ms, 450, 150, 100, 54);
      if (lBusDownN) busListenerSt = { active: true, travel: lBusDownN.travel, span: 100, len: 54, dir: 'req', op: 1, tone: reqTone };

      var bReq = evalNodeLeg(ms, 600, 720, 0, 150, 34, 0, 0);
      putNode('bitchord', bReq, 0, 150, 34, 'req', reqTone, true);

      var topDownN = evalBusLeg(ms, 1320, 190, 100, 44);
      if (topDownN) busTopSt = { active: true, travel: topDownN.travel, span: 100, len: 44, dir: 'req', op: 1, tone: reqTone };

      var cReq = evalNodeLeg(ms, 1510, 750, 0, 150, 34, 0, 0);
      putNode('confih', cReq, 0, 150, 34, 'req', reqTone, true);

      var brDown = evalBusLeg(ms, 2260, 220, 100, 46);
      if (brDown) {
        var bDownObj = { active: true, travel: brDown.travel, span: 100, len: 46, dir: 'req', op: 1, tone: reqTone };
        if (flow.target === 'qobuz') busQSt = bDownObj; else busTSt = bDownObj;
      }

      var prov = evalNodeLeg(ms, 2480, 640, 0, 100, 44, 0, 0);
      if (prov) {
        var switchPoint = flow.mode === 'degraded' ? 0.68 : 0.56;
        var pTone = prov.localT >= switchPoint ? 'stream' : reqTone;
        var pDir = prov.localT >= switchPoint ? 'return' : 'req';
        putNode(flow.target, prov, 0, 100, 44, pDir, pTone, true);
      }

      var brUp = evalBusLeg(ms, 3120, 220, 100, 46);
      if (brUp) {
        var bUpObj = { active: true, travel: brUp.travel, span: 100, len: 46, dir: 'return', op: 1, tone: 'stream' };
        if (flow.target === 'qobuz') busQSt = bUpObj; else busTSt = bUpObj;
      }

      var cRet = evalNodeLeg(ms, 3340, 320, 50, 100, 34, 0, 0);
      putNode('confih', cRet, 50, 100, 34, 'return', 'stream', true);

      var topUpN = evalBusLeg(ms, 3660, 190, 100, 44);
      if (topUpN) busTopSt = { active: true, travel: topUpN.travel, span: 100, len: 44, dir: 'return', op: 1, tone: 'stream' };

      var bRet = evalNodeLeg(ms, 3850, 320, 50, 100, 34, 0, 0);
      putNode('bitchord', bRet, 50, 100, 34, 'return', 'stream', true);

      var lBusUpN = evalBusLeg(ms, 4170, 150, 100, 54);
      if (lBusUpN) busListenerSt = { active: true, travel: lBusUpN.travel, span: 100, len: 54, dir: 'return', op: 1, tone: 'stream' };

      var lRet = evalNodeLeg(ms, 4320, 240, 50, 120, 50, 0, 160);
      putNode('listener', lRet, 50, 120, 50, 'return', 'stream', true);
    }

    NODE_ORDER.forEach(function (id) {
      var s = nState[id];
      if (s) {
        setNodeSegment(refs.nodes[id], s.vPos, s.minS, s.maxS, s.beamLen, s.op, s.dir, s.tone, s.spark);
      } else {
        setNodeSegment(refs.nodes[id], null, 0, 100, 32, 0, 'req', 'prism', false);
      }
    });

    if (busListenerSt) {
      setBusSegment(refs.busListener, true, busListenerSt.travel, busListenerSt.span, busListenerSt.len, busListenerSt.dir, busListenerSt.op, busListenerSt.tone);
    } else {
      setBusSegment(refs.busListener, false, 0, 100, 40, 'req', 0, 'prism');
    }

    if (busTopSt) {
      setBusSegment(refs.busTop, true, busTopSt.travel, busTopSt.span, busTopSt.len, busTopSt.dir, busTopSt.op, busTopSt.tone);
    } else {
      setBusSegment(refs.busTop, false, 0, 100, 40, 'req', 0, 'prism');
    }

    if (busQSt) {
      setBusSegment(refs.busQobuz, true, busQSt.travel, busQSt.span, busQSt.len, busQSt.dir, busQSt.op, busQSt.tone);
    } else {
      setBusSegment(refs.busQobuz, false, 0, 100, 40, 'req', 0, 'prism');
    }

    if (busTSt) {
      setBusSegment(refs.busTidal, true, busTSt.travel, busTSt.span, busTSt.len, busTSt.dir, busTSt.op, busTSt.tone);
    } else {
      setBusSegment(refs.busTidal, false, 0, 100, 40, 'req', 0, 'prism');
    }

    return true;
  }

  /* ── Tilt & Specular Sheen Surface Management ────────────────────────── */

  function scanTiltTargets() {
    var nodes = document.querySelectorAll(TILT_SELECTOR);
    for (var i = 0; i < nodes.length; i++) {
      var el = nodes[i];
      if (!el.hasAttribute('data-tilt')) {
        el.setAttribute('data-tilt', '');
      }
      if (!canPointerTilt()) {
        clearTiltVars(el);
      }
    }
  }

  function clearTiltVars(el) {
    if (!el || !el.style) return;
    el.style.removeProperty('--rx');
    el.style.removeProperty('--ry');
    el.style.setProperty('--tilt-active', '0');
  }

  function releaseHoveredTilt() {
    if (!state.hoveredTiltEl) return;
    var prev = state.tiltItems.get(state.hoveredTiltEl);
    if (prev) {
      prev.tx = 0;
      prev.ty = 0;
      prev.active = 0;
    }
    state.hoveredTiltEl = null;
  }

  function resetAllTiltImmediate() {
    state.hoveredTiltEl = null;
    state.tiltItems.forEach(function (_item, el) {
      clearTiltVars(el);
    });
    state.tiltItems.clear();
  }

  /* ── 60fps Animation Frame Loop ──────────────────────────────────────── */

  function stepFrame(nowMs) {
    if (state.frameBudgetMs > 0 && state.lastFrameMs) {
      /* Phones pay for every frame twice (higher DPR, weaker GPU). The beam
         clock is wall-time based, so running at 30fps changes the cost, not
         the choreography. */
      if (nowMs - state.lastFrameMs < state.frameBudgetMs - 1) {
        state.rafId = window.requestAnimationFrame(stepFrame);
        return;
      }
    }
    if (!canRunLoop()) {
      state.running = false;
      state.rafId = 0;
      resetAllTiltImmediate();
      snapStagesImmediate();
      clearFlowEffects();
      return;
    }

    var rawDtMs = state.lastFrameMs ? Math.min(50, nowMs - state.lastFrameMs) : 16;
    state.lastFrameMs = nowMs;
    var dt = rawDtMs / 1000;
    var kTilt = 1 - Math.exp(-15 * dt);
    var kRack = 1 - Math.exp(-12 * dt);
    var needsMore = false;

    state.tiltItems.forEach(function (item, el) {
      if (!el.isConnected) {
        state.tiltItems.delete(el);
        return;
      }
      item.cx += (item.tx - item.cx) * kTilt;
      item.cy += (item.ty - item.cy) * kTilt;
      item.curActive += (item.active - item.curActive) * kTilt;

      if (
        item.active === 0 &&
        Math.abs(item.cx) < 0.02 &&
        Math.abs(item.cy) < 0.02 &&
        item.curActive < 0.015
      ) {
        clearTiltVars(el);
        state.tiltItems.delete(el);
        return;
      }

      el.style.setProperty('--spot-x', item.sx.toFixed(1) + '%');
      el.style.setProperty('--spot-y', item.sy.toFixed(1) + '%');
      el.style.setProperty('--tilt-active', item.curActive.toFixed(3));

      if (
        Math.abs(item.tx - item.cx) >= 0.02 ||
        Math.abs(item.ty - item.cy) >= 0.02 ||
        Math.abs(item.active - item.curActive) >= 0.015
      ) {
        needsMore = true;
      }
    });

    Object.keys(state.stages).forEach(function (key) {
      var st = state.stages[key];
      if (!st.windowEl && !st.stackEl) return;
      var tgt = targetStageAngles(st);
      st.curRx += (tgt.rx - st.curRx) * kRack;
      st.curRy += (tgt.ry - st.curRy) * kRack;

      if (Math.abs(tgt.rx - st.curRx) < 0.02 && Math.abs(tgt.ry - st.curRy) < 0.02) {
        st.curRx = tgt.rx;
        st.curRy = tgt.ry;
      } else {
        needsMore = true;
      }
      writeStageVars(st, st.curRx, st.curRy);
    });

    if (stepSignalFlow(rawDtMs)) {
      needsMore = true;
    }

    state.framesRendered += 1;

    if (needsMore) {
      state.rafId = window.requestAnimationFrame(stepFrame);
    } else {
      state.running = false;
      state.rafId = 0;
      state.lastFrameMs = 0;
    }
  }

  function ensureRaf() {
    if (!canRunLoop()) {
      snapStagesImmediate();
      clearFlowEffects();
      return;
    }
    if (!state.running) {
      state.running = true;
      state.lastFrameMs = 0;
      state.rafId = window.requestAnimationFrame(stepFrame);
    }
  }

  function stopRaf() {
    if (state.rafId) {
      window.cancelAnimationFrame(state.rafId);
      state.rafId = 0;
    }
    state.running = false;
    state.lastFrameMs = 0;
  }

  /* ── Telemetry Copy & Live Hardware Rack Readouts ────────────────────── */

  function resolveProviderNode() {
    var stats = state.stats || {};
    var services = stats.services || {};
    var primary = String(stats.primary_provider || services.primary_provider || '').toLowerCase();
    var qOnline = !!(services.qobuz && (services.qobuz.online || services.qobuz.status === 'online'));
    var tOnline = !!(services.tidal && (services.tidal.online || services.tidal.status === 'online'));
    if (primary === 'tidal' && (tOnline || !qOnline)) return 'tidal';
    if (!qOnline && tOnline) return 'tidal';
    return 'qobuz';
  }

  function providerStateKind(svc) {
    if (!svc) return 'unconfigured';
    if (svc.configured === false && !svc.online) return 'unconfigured';
    var raw = String(svc.status || '').toLowerCase().replace(/[^a-z]/g, '');
    if (raw === 'online' || raw === 'active' || svc.online) return 'online';
    if (raw === 'standby') return 'standby';
    if (raw === 'degraded') return 'degraded';
    if (raw === 'offline' || raw === 'down') return 'offline';
    return svc.configured ? 'degraded' : 'unconfigured';
  }

  function nodeTelemetryText(nodeId) {
    var stats = state.stats;
    if (!stats) {
      if (nodeId === 'listener') return 'Listener · Lossless stream request';
      if (nodeId === 'bitchord') return 'BitChord Client · HTTP v2 search & stream';
      if (nodeId === 'confih') return 'Confih Router · Zero-transcode pass-through';
      if (nodeId === 'qobuz') return 'Qobuz Engine · Up to 24-bit / 192 kHz FLAC';
      if (nodeId === 'tidal') return 'Tidal Engine · Dolby Atmos & MAX FLAC';
      return 'Checking upstream connectivity…';
    }
    var services = stats.services || {};
    var traffic = stats.traffic || {};
    var cache = stats.cache || {};
    if (nodeId === 'listener') {
      var listeners = Number((stats.users && stats.users.active_now) || 0);
      var streams = Number(traffic.streams_served || traffic.total_streams || 0);
      return 'Listener · ' + listeners + ' active · ' + streams + ' streams served';
    }
    if (nodeId === 'bitchord') {
      var active = Number((stats.users && stats.users.active_now) || 0);
      var searches = Number(traffic.total_searches || 0);
      return 'BitChord Client · ' + active + ' active · ' + searches + ' searches';
    }
    if (nodeId === 'confih') {
      var hit = Math.round(Number(cache.hit_rate_percent || 0));
      var mode = stats.failover_active
        ? 'Failover Active'
        : String(stats.status || 'operational');
      mode = mode.charAt(0).toUpperCase() + mode.slice(1);
      return 'Confih Core · ' + mode + ' · ' + hit + '% cache hit';
    }
    if (nodeId === 'qobuz' || nodeId === 'tidal') {
      var svc = services[nodeId] || {};
      var label = nodeId === 'qobuz' ? 'Qobuz' : 'Tidal';
      var st = providerStateKind(svc);
      var stLabel = st.charAt(0).toUpperCase() + st.slice(1);
      var lat = svc.health_latency_ms || svc.avg_latency_ms;
      var latText = lat && Number(lat) > 0 ? Math.round(Number(lat)) + ' ms' : st;
      return label + ' · ' + stLabel + ' · ' + latText;
    }
    var r = resolveFlowMode(Math.floor(state.flowClockMs / CYCLE_MS));
    return r.readout;
  }

  function syncRackHardwareBadges() {
    var rack = state.stages.status.stackEl || document.getElementById('status-rack-3d');
    var stats = state.stats;
    if (!rack || !stats) return;

    var immediateFlow = resolveFlowMode(Math.floor(state.flowClockMs / CYCLE_MS));
    state.flowMode = immediateFlow.mode;
    state.flowTarget = immediateFlow.target;
    rack.setAttribute('data-flow-mode', immediateFlow.mode);

    var users = stats.users || {};
    var services = stats.services || {};
    var activeNow = Number(users.active_now || 0);
    var clientBadge = rack.querySelector('[data-rack-client-badge]');
    setText(clientBadge, activeNow > 0 ? activeNow + ' active · HTTP v2' : 'HTTP v2 Ready');

    var rawStatus = String(stats.status || '').toLowerCase();
    var failover = !!stats.failover_active;
    var coreNode = rack.querySelector('[data-rack-node="confih"]');
    var coreLed = rack.querySelector('[data-rack-core-led]');
    var coreBadge = rack.querySelector('[data-rack-core-badge]');
    if (coreNode) {
      coreNode.setAttribute(
        'data-node-health',
        rawStatus === 'outage' ? 'offline' : (failover || rawStatus === 'degraded' ? 'degraded' : 'online')
      );
    }
    if (coreLed) {
      var ledCls = rawStatus === 'outage'
        ? 'warn'
        : (failover || rawStatus === 'degraded' ? 'mid' : 'ok');
      coreLed.className = 'led ' + ledCls + ' pulse';
    }
    if (coreBadge) {
      var badgeWord = rawStatus === 'outage'
        ? 'Offline'
        : (failover ? 'Failover' : (rawStatus === 'degraded' ? 'Degraded' : 'Operational'));
      setText(coreBadge, badgeWord);
    }

    ['qobuz', 'tidal'].forEach(function (key) {
      var svc = services[key] || {};
      var kind = providerStateKind(svc);
      var chip = rack.querySelector('[data-rack-node="' + key + '"]');
      var ledEl = rack.querySelector('[data-rack-' + key + '-led]');
      var metaEl = rack.querySelector('[data-rack-' + key + '-meta]');
      if (chip) {
        chip.setAttribute('data-node-health', kind);
      }
      if (ledEl) {
        var c = kind === 'online'
          ? 'ok'
          : (kind === 'standby'
            ? 'standby'
            : (kind === 'degraded' ? 'mid' : (kind === 'offline' ? 'warn' : '')));
        ledEl.className = c ? 'led ' + c : 'led';
      }
      if (metaEl) {
        var lat = svc.health_latency_ms || svc.avg_latency_ms;
        var latStr = lat && Number(lat) > 0 ? Math.round(Number(lat)) + ' ms' : '';
        var fallbackTier = key === 'qobuz' ? 'FLAC Hi-Res' : 'Dolby Atmos · FLAC';
        var word = kind === 'online'
          ? ('Online' + (latStr ? ' · ' + latStr : ''))
          : (kind === 'standby'
            ? 'Standby · Ready'
            : (kind === 'unconfigured' ? fallbackTier : kind.charAt(0).toUpperCase() + kind.slice(1)));
        setText(metaEl, word);
      }
    });
  }

  function syncInspectUI() {
    var inspectNode = state.hoveredNode || state.selectedNode;
    var readout = document.getElementById('spatial-inspect-readout');
    if (readout) {
      setText(readout, nodeTelemetryText(inspectNode));
    }

    var rack = state.stages.status.stackEl || document.getElementById('status-rack-3d');
    if (rack) {
      if (inspectNode) {
        rack.setAttribute('data-active-node', inspectNode);
      } else {
        rack.removeAttribute('data-active-node');
      }
      var rackNodes = rack.querySelectorAll('[data-rack-node]');
      for (var r = 0; r < rackNodes.length; r++) {
        var rn = rackNodes[r];
        var rnId = rn.getAttribute('data-rack-node');
        rn.classList.toggle('is-active', rnId === inspectNode);
        if (rn.hasAttribute('aria-pressed')) {
          rn.setAttribute('aria-pressed', rnId === state.selectedNode ? 'true' : 'false');
        }
      }
    }
  }

  /* ── Interactive Stage Drag/Hover Orbit & Pointer Delegation ─────────── */

  function bindStageOrbit(winEl, stage) {
    if (!winEl || state.boundStages.has(winEl)) return;
    state.boundStages.add(winEl);

    winEl.addEventListener('pointerdown', function (e) {
      if (!canRunLoop() || e.button !== 0) return;
      if (e.target && e.target.closest && e.target.closest('a, input, select, textarea')) {
        return;
      }
      stage.dragging = true;
      stage.dragMoved = false;
      stage.startX = e.clientX;
      stage.startY = e.clientY;
    });

    winEl.addEventListener('pointermove', function (e) {
      if (!canRunLoop()) return;
      if (stage.dragging) {
        var dx = e.clientX - stage.startX;
        var dy = e.clientY - stage.startY;
        if (!stage.dragMoved && Math.hypot(dx, dy) > 3) {
          stage.dragMoved = true;
          winEl.classList.add('is-dragging');
          if (stage.stackEl) stage.stackEl.classList.add('is-dragging');
          if (winEl.setPointerCapture) {
            try {
              winEl.setPointerCapture(e.pointerId);
            } catch (err) {
              /* ignore */
            }
          }
        }
        if (!stage.dragMoved) return;
        stage.startX = e.clientX;
        stage.startY = e.clientY;
        stage.orbitRy = clamp(stage.orbitRy + dx * 0.3, -28, 28);
        stage.orbitRx = clamp(stage.orbitRx - dy * 0.22, -18, 18);
        if (stage.key === 'status' && !state.selectedNode) {
          var readout = document.getElementById('spatial-inspect-readout');
          var degX = Math.round(stage.baseRx + stage.orbitRx);
          var degY = Math.round(stage.baseRy + stage.orbitRy);
          setText(readout, '3D Orbit · ' + degX + '° pitch · ' + degY + '° yaw');
        }
        ensureRaf();
        return;
      }

      if (canPointerTilt()) {
        var rect = winEl.getBoundingClientRect();
        if (rect.width >= 20 && rect.height >= 20) {
          var nx = clamp(((e.clientX - rect.left) / rect.width) * 2 - 1, -1, 1);
          var ny = clamp(((e.clientY - rect.top) / rect.height) * 2 - 1, -1, 1);
          stage.hoverRx = -ny * 3.5;
          stage.hoverRy = nx * 5.0;
          ensureRaf();
        }
      }

      if (stage.key === 'status') {
        var nodeEl = e.target && e.target.closest ? e.target.closest('[data-rack-node]') : null;
        var hitId = nodeEl ? nodeEl.getAttribute('data-rack-node') : null;
        if (hitId !== state.hoveredNode) {
          state.hoveredNode = hitId;
          syncInspectUI();
          ensureRaf();
        }
      }
    });

    function finishDrag() {
      if (!stage.dragging) return;
      stage.dragging = false;
      stage.wasDragged = stage.dragMoved;
      winEl.classList.remove('is-dragging');
      if (stage.stackEl) stage.stackEl.classList.remove('is-dragging');
    }

    winEl.addEventListener('pointerup', finishDrag);
    winEl.addEventListener('pointercancel', function () {
      stage.dragging = false;
      stage.hoverRx = 0;
      stage.hoverRy = 0;
      winEl.classList.remove('is-dragging');
      if (stage.stackEl) stage.stackEl.classList.remove('is-dragging');
      ensureRaf();
    });
    winEl.addEventListener('pointerleave', function () {
      if (!stage.dragging) {
        stage.hoverRx = 0;
        stage.hoverRy = 0;
      }
      if (stage.key === 'status' && state.hoveredNode) {
        state.hoveredNode = null;
        syncInspectUI();
      }
      ensureRaf();
    });
    winEl.addEventListener('dblclick', function (e) {
      if (e.target && e.target.closest && e.target.closest('a')) return;
      stage.orbitRx = 0;
      stage.orbitRy = 0;
      if (stage.key === 'status') syncInspectUI();
      ensureRaf();
    });
  }

  function handleDocumentPointerMove(e) {
    if (!canPointerTilt()) return;

    var card = e.target && e.target.closest ? e.target.closest(TILT_SELECTOR) : null;
    if (card && card.closest('[hidden]')) card = null;

    if (card !== state.hoveredTiltEl) {
      releaseHoveredTilt();
      state.hoveredTiltEl = card;
    }

    if (card) {
      var r = card.getBoundingClientRect();
      if (r.width >= 16 && r.height >= 16) {
        var px = clamp((e.clientX - r.left) / r.width, 0, 1);
        var py = clamp((e.clientY - r.top) / r.height, 0, 1);
        var nx = px * 2 - 1;
        var ny = py * 2 - 1;
        var isLargeSurface = r.height > 440 || r.width > 780;
        var maxRx = isLargeSurface ? 1.35 : 3.5;
        var maxRy = isLargeSurface ? 1.75 : 4.5;
        var item = state.tiltItems.get(card);
        if (!item) {
          item = { cx: 0, cy: 0, tx: 0, ty: 0, sx: 50, sy: 50, active: 0, curActive: 0 };
          state.tiltItems.set(card, item);
        }
        item.tx = clamp(-ny * maxRx, -3.5, 3.5);
        item.ty = clamp(nx * maxRy, -4.5, 4.5);
        item.sx = px * 100;
        item.sy = py * 100;
        item.active = 1;
        ensureRaf();
      }
    }
  }

  /* ── Public Selection, Reset & Lifecycle API ─────────────────────────── */

  function selectNode(nodeId) {
    if (!nodeId) {
      state.selectedNode = null;
      syncInspectUI();
      ensureRaf();
      return;
    }
    var key = String(nodeId).toLowerCase();
    var alias = NODE_ALIASES[key] || null;
    if (alias === 'providers') {
      state.selectedNode = resolveProviderNode();
    } else {
      state.selectedNode = alias;
    }
    syncInspectUI();
    ensureRaf();
  }

  function focusNode(nodeId) {
    selectNode(nodeId);
  }

  function resetCamera() {
    Object.keys(state.stages).forEach(function (k) {
      var st = state.stages[k];
      st.orbitRx = 0;
      st.orbitRy = 0;
      st.hoverRx = 0;
      st.hoverRy = 0;
      st.dragging = false;
      if (st.windowEl) st.windowEl.classList.remove('is-dragging');
      if (st.stackEl) st.stackEl.classList.remove('is-dragging');
    });
    state.hoveredNode = null;
    state.selectedNode = null;
    syncInspectUI();
    ensureRaf();
  }

  function setView(view) {
    state.activeView = normalizeView(view);
    state.hoveredNode = null;
    releaseHoveredTilt();
    refreshStageElements();
    scanTiltTargets();
    syncInspectUI();
    snapStagesImmediate();
    if (state.activeView !== 'status') {
      clearFlowEffects();
    } else {
      ensureRaf();
    }
  }

  function setTheme(theme) {
    state.theme = theme === 'light' ? 'light' : 'dark';
  }

  function updateData(stats, plays) {
    if (stats) {
      state.stats = stats;
      state.hasStats = true;
      state.lastRoundKey = '';
      syncRackHardwareBadges();
    }
    if (plays) {
      state.plays = plays;
      state.hasPlays = true;
    }
    syncInspectUI();
  }

  function setTelemetry(stats, plays) {
    updateData(stats, plays);
  }

  function updateStats(stats) {
    updateData(stats, null);
  }

  function updatePlays(plays, tier) {
    state.activeTier = tier || '';
    updateData(null, plays);
  }

  function frameBudgetForViewport() {
    var narrow = window.innerWidth < 900;
    var coarse = !!fineMedia.matches === false;
    return narrow || coarse ? 1000 / 30 : 0;
  }

  function syncCapabilities() {
    state.reducedMotion = !!reduceMedia.matches;
    state.finePointer = !!fineMedia.matches;
    state.frameBudgetMs = frameBudgetForViewport();
    var legacyStage = document.getElementById('spatial-stage');
    if (legacyStage) {
      legacyStage.hidden = true;
      legacyStage.setAttribute('data-spatial-mode', 'css-3d');
      legacyStage.setAttribute('data-spatial-motion', state.reducedMotion ? 'reduced' : 'active');
    }
    if (!canRunLoop()) {
      stopRaf();
      resetAllTiltImmediate();
      snapStagesImmediate();
      clearFlowEffects();
    } else {
      ensureRaf();
    }
  }

  function init(options) {
    if (state.initialized) {
      if (options && options.view) setView(options.view);
      return;
    }

    state.reducedMotion = !!reduceMedia.matches;
    state.finePointer = !!fineMedia.matches;
    state.frameBudgetMs = frameBudgetForViewport();

    if (options && options.view) {
      state.activeView = normalizeView(options.view);
    } else if (document.body && document.body.dataset.initialView) {
      state.activeView = normalizeView(document.body.dataset.initialView);
    }

    var legacyStage = document.getElementById('spatial-stage');
    if (legacyStage) legacyStage.hidden = true;
    var legacyCanvas = document.getElementById('spatial-canvas');
    if (legacyCanvas) legacyCanvas.hidden = true;

    refreshStageElements();
    scanTiltTargets();
    snapStagesImmediate();
    syncInspectUI();

    document.addEventListener('pointermove', handleDocumentPointerMove, { passive: true });
    document.addEventListener('pointerleave', function () {
      releaseHoveredTilt();
      ensureRaf();
    });
    window.addEventListener('blur', function () {
      releaseHoveredTilt();
      ensureRaf();
    });

    document.addEventListener('visibilitychange', syncCapabilities);
    window.addEventListener('resize', syncAllPerimeters);
    document.addEventListener('confih:viewchange', function (e) {
      if (e && e.detail && e.detail.view) setView(e.detail.view);
    });
    document.addEventListener('confih:content', function () {
      refreshStageElements();
      scanTiltTargets();
    });
    document.addEventListener('confih:themechange', function (e) {
      if (e && e.detail && e.detail.theme) setTheme(e.detail.theme);
    });

    if (reduceMedia.addEventListener) {
      reduceMedia.addEventListener('change', syncCapabilities);
    }
    if (fineMedia.addEventListener) {
      fineMedia.addEventListener('change', syncCapabilities);
    }

    document.addEventListener('click', function (e) {
      if (state.stages.status.wasDragged) {
        state.stages.status.wasDragged = false;
        return;
      }
      var pill = e.target && e.target.closest ? e.target.closest('[data-spatial-node], [data-rack-node]') : null;
      if (pill) {
        var id = pill.getAttribute('data-spatial-node') || pill.getAttribute('data-rack-node');
        var next = state.selectedNode === id ? null : id;
        selectNode(next);
        return;
      }
      var resetBtn = e.target && e.target.closest ? e.target.closest('#stage-camera-reset') : null;
      if (resetBtn) {
        resetCamera();
      }
    });

    document.addEventListener('keydown', function (e) {
      var active = document.activeElement;
      var win = active && active.closest ? active.closest('.spatial-window') : null;
      if (!win) return;
      if (e.key === 'Escape') {
        var modal = document.getElementById('qr-modal');
        if (modal && modal.classList.contains('active')) return;
        if (
          state.selectedNode ||
          state.stages.status.orbitRx !== 0 ||
          state.stages.status.orbitRy !== 0 ||
          state.stages.info.orbitRx !== 0 ||
          state.stages.info.orbitRy !== 0
        ) {
          e.preventDefault();
          resetCamera();
        }
        return;
      }
      if (e.key !== 'ArrowRight' && e.key !== 'ArrowLeft') return;
      if (active && active.tagName === 'INPUT') return;
      e.preventDefault();
      var idx = NODE_ORDER.indexOf(state.selectedNode);
      var nextIdx = e.key === 'ArrowRight'
        ? (idx + 1) % NODE_ORDER.length
        : (idx <= 0 ? NODE_ORDER.length - 1 : idx - 1);
      selectNode(NODE_ORDER[nextIdx]);
    });

    state.initialized = true;
    syncCapabilities();
  }

  function destroy() {
    stopRaf();
    resetAllTiltImmediate();
    clearFlowEffects();
    state.initialized = false;
  }

  function getState() {
    var st = state.stages.status;
    return {
      backend: state.backend,
      activeView: state.activeView,
      theme: state.theme,
      reducedMotion: state.reducedMotion,
      finePointer: state.finePointer,
      running: state.running,
      offscreen: state.offscreen,
      framesRendered: state.framesRendered,
      selectedNode: state.selectedNode,
      flowMode: state.flowMode,
      flowTarget: state.flowTarget,
      hasStats: state.hasStats,
      hasPlays: state.hasPlays,
      camera: {
        rx: Number(st.curRx.toFixed(2)),
        ry: Number(st.curRy.toFixed(2)),
        orbitRx: Number(st.orbitRx.toFixed(2)),
        orbitRy: Number(st.orbitRy.toFixed(2)),
      },
    };
  }

  window.ConfihSpatial = {
    init: init,
    setView: setView,
    setTheme: setTheme,
    setTelemetry: setTelemetry,
    updateData: updateData,
    updateStats: updateStats,
    updatePlays: updatePlays,
    selectNode: selectNode,
    focusNode: focusNode,
    resetCamera: resetCamera,
    destroy: destroy,
    getState: getState,
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () {
      init();
    });
  } else {
    init();
  }
})();
