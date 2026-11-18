/* ============================================================================
   Confih — History-API Spatial Router & Single-Flight Overview Bootstrap
   ----------------------------------------------------------------------------
   Provides seamless client-side navigation across Status, Plays, and
   About & Support views with epoch-guarded View Transitions + deterministic
   CSS 3D depth fallback, directional Z-choreography, clean scroll memory,
   zero stuck hidden views, and reduced-motion parity.
   ========================================================================= */
(function () {
  'use strict';

  var VIEW_ORDER = { status: 0, plays: 1, about: 2, support: 3 };
  var reduceMedia = window.matchMedia('(prefers-reduced-motion: reduce)');
  function reduced() {
    return !!reduceMedia.matches;
  }

  function readBoot() {
    var el = document.getElementById('confih-boot');
    if (el && el.textContent) {
      try {
        return JSON.parse(el.textContent);
      } catch (e) {
        /* fallback to dataset */
      }
    }
    var ds = document.body ? document.body.dataset : {};
    return {
      initial_view: ds.initialView || 'status',
      public_plays: ds.publicPlays === '1',
      path_secret: ds.pathSecret === '1',
      public_url: window.location.origin,
    };
  }

  var boot = readBoot();
  var publicPlays = !!boot.public_plays;
  var currentViewName = normalizeView(
    (document.body && document.body.dataset.initialView) || boot.initial_view || 'status'
  );
  var overviewPromise = null;
  var navEpoch = 0;
  var cleanupTimer = 0;
  var activeVT = null;
  var scrollMemory = Object.create(null);

  function normalizeView(v) {
    var s = String(v || '').toLowerCase();
    if (s === 'plays' && publicPlays) return 'plays';
    if (s === 'support') return 'support';
    if (s === 'about' || s === 'guide' || s === 'info') return 'about';
    return 'status';
  }

  function pageKey(loc) {
    return (loc.pathname || '/') + (loc.search || '');
  }

  // Passively remember scroll position per route for deterministic Back/Forward restoration.
  var scrollTick = false;
  window.addEventListener(
    'scroll',
    function () {
      if (scrollTick) return;
      scrollTick = true;
      window.requestAnimationFrame(function () {
        scrollTick = false;
        scrollMemory[pageKey(window.location)] = window.scrollY || 0;
      });
    },
    { passive: true }
  );

  function bootstrapOverview() {
    if (!overviewPromise) {
      overviewPromise = fetch('/api/overview', {
        cache: 'no-store',
        headers: { Accept: 'application/json' },
      })
        .then(function (res) {
          if (!res.ok) throw new Error('HTTP ' + res.status);
          return res.json();
        })
        .catch(function (err) {
          overviewPromise = null;
          throw err;
        });
    }
    return overviewPromise;
  }

  function resolveRoute(urlLike) {
    if (urlLike === null || urlLike === undefined || urlLike === '') return null;
    var raw = String(urlLike);
    if (raw === '#main-content') return null;

    var u;
    try {
      u = urlLike instanceof URL ? urlLike : new URL(raw, window.location.href);
    } catch (e) {
      return null;
    }
    if (u.origin !== window.location.origin) return null;
    var path = u.pathname.replace(/\/+$/, '') || '/';
    var hashId = u.hash ? decodeURIComponent(u.hash.slice(1)) : '';

    if (path === '/' || path === '/status' || path === '/monitor' || /^\/s\/[^/]+$/.test(path)) {
      return {
        view: 'status',
        navKey: 'status',
        anchor: hashId,
        title: 'Status — Confih Gateway',
        url: u,
      };
    }
    if (path === '/plays' && publicPlays) {
      return {
        view: 'plays',
        navKey: 'plays',
        anchor: hashId,
        title: 'Plays — Confih Gateway',
        url: u,
      };
    }
    if (path === '/about') {
      return {
        view: 'about',
        navKey: 'about',
        anchor: hashId,
        title: 'About — Confih Gateway',
        url: u,
      };
    }
    if (path === '/support') {
      return {
        view: 'support',
        navKey: 'support',
        /* The panel itself owns the anchor now, but old links still say
           #donations, so keep accepting it. */
        anchor: hashId === 'donations' ? 'donations-bay' : hashId,
        title: 'Support — Confih Gateway',
        url: u,
      };
    }
    return null;
  }

  function getPanels() {
    return Array.prototype.slice.call(document.querySelectorAll('.view-panel[data-view]'));
  }

  function getPanel(viewName) {
    return document.querySelector('.view-panel[data-view="' + viewName + '"]');
  }

  function updateNavState(navKey) {
    var links = document.querySelectorAll('.site-nav a[data-nav-view]');
    for (var i = 0; i < links.length; i++) {
      var a = links[i];
      if (a.getAttribute('data-nav-view') === navKey) {
        a.setAttribute('aria-current', 'page');
      } else {
        a.removeAttribute('aria-current');
      }
    }
  }

  function applyScrollAndFocus(route, savedScrollY, instantAnchor) {
    if (route.anchor) {
      var target = document.getElementById(route.anchor);
      if (target) {
        if (!target.hasAttribute('tabindex')) target.setAttribute('tabindex', '-1');
        var behavior = instantAnchor || reduced() ? 'auto' : 'smooth';
        try {
          target.scrollIntoView({ behavior: behavior, block: 'start' });
        } catch (e) {
          target.scrollIntoView();
        }
        try {
          target.focus({ preventScroll: true });
        } catch (e) {
          /* ignore */
        }
        scrollMemory[pageKey(window.location)] = window.scrollY || 0;
        return;
      }
    }
    var nextY =
      typeof savedScrollY === 'number' && isFinite(savedScrollY) && savedScrollY >= 0
        ? savedScrollY
        : 0;
    try {
      window.scrollTo({ top: nextY, behavior: 'auto' });
    } catch (e) {
      window.scrollTo(0, nextY);
    }
    scrollMemory[pageKey(window.location)] = nextY;
  }

  function clearTransientClasses(panel) {
    panel.classList.remove('is-leaving', 'is-entering');
    panel.removeAttribute('data-enter-dir');
    panel.style.opacity = '';
    panel.style.transform = '';
  }

  function resetTransientState(targetPane) {
    if (cleanupTimer) {
      window.clearTimeout(cleanupTimer);
      cleanupTimer = 0;
    }
    if (activeVT && typeof activeVT.skipTransition === 'function') {
      try {
        activeVT.skipTransition();
      } catch (e) {
        /* ignore */
      }
    }
    activeVT = null;
    getPanels().forEach(function (pane) {
      clearTransientClasses(pane);
      if (targetPane && pane !== targetPane && pane.hidden) {
        pane.hidden = true;
      }
    });
  }

  function showOnlyPane(targetPane) {
    getPanels().forEach(function (pane) {
      pane.hidden = pane !== targetPane;
      clearTransientClasses(pane);
    });
  }

  function finalize(epoch, targetPane) {
    if (epoch !== navEpoch) return;
    if (cleanupTimer) {
      window.clearTimeout(cleanupTimer);
      cleanupTimer = 0;
    }
    activeVT = null;
    document.documentElement.removeAttribute('data-nav-dir');
    document.documentElement.classList.remove('is-routing');
    showOnlyPane(targetPane);
    if (targetPane) {
      document.dispatchEvent(
        new CustomEvent('confih:viewsettled', {
          detail: { view: targetPane.getAttribute('data-view') || currentViewName },
        })
      );
    }
  }

  function emitViewEvents(route, direction) {
    if (window.ConfihSpatial && typeof window.ConfihSpatial.setView === 'function') {
      window.ConfihSpatial.setView(route.view);
    }
    document.dispatchEvent(
      new CustomEvent('confih:viewchange', {
        detail: {
          view: route.view,
          anchor: route.anchor,
          direction: direction || 'forward',
          path: route.url ? route.url.pathname : window.location.pathname,
        },
      })
    );
    window.requestAnimationFrame(function () {
      document.dispatchEvent(new CustomEvent('confih:content'));
    });
  }

  function transitionTo(route, opts) {
    opts = opts || {};
    var targetPane = getPanel(route.view);
    if (!targetPane) return;

    navEpoch += 1;
    var myEpoch = navEpoch;
    var prevViewName = currentViewName;
    var prevPane = getPanel(prevViewName);
    var samePane = prevPane === targetPane;
    var prevScrollY = window.scrollY || 0;
    var direction =
      (VIEW_ORDER[route.view] || 0) >= (VIEW_ORDER[prevViewName] || 0) ? 'forward' : 'back';

    scrollMemory[pageKey(window.location)] = prevScrollY;
    resetTransientState(targetPane);

    currentViewName = route.view;
    document.title = route.title;
    updateNavState(route.navKey);

    if (opts.push && route.url) {
      try {
        var curRel = window.location.pathname + window.location.search + window.location.hash;
        var nextRel = route.url.pathname + route.url.search + route.url.hash;
        window.history.replaceState(
          { view: prevViewName, scrollY: prevScrollY },
          '',
          window.location.href
        );
        if (nextRel !== curRel) {
          window.history.pushState({ view: route.view, scrollY: 0 }, '', nextRel);
        }
      } catch (e) {
        /* ignore */
      }
    }

    if (reduced() || document.visibilityState === 'hidden') {
      finalize(myEpoch, targetPane);
      applyScrollAndFocus(route, opts.scrollY, !samePane);
      emitViewEvents(route, direction);
      return;
    }

    if (samePane) {
      finalize(myEpoch, targetPane);
      if (route.anchor) {
        applyScrollAndFocus(route, opts.scrollY, false);
      } else if (prevScrollY > 40) {
        try {
          window.scrollTo({ top: 0, behavior: 'smooth' });
        } catch (e) {
          window.scrollTo(0, 0);
        }
        scrollMemory[pageKey(window.location)] = 0;
        document.dispatchEvent(
          new CustomEvent('confih:scrolltop', { detail: { view: route.view } })
        );
      }
      return;
    }

    document.documentElement.setAttribute('data-nav-dir', direction);
    document.documentElement.classList.add('is-routing');

    if (typeof document.startViewTransition === 'function') {
      try {
        var vtEmitted = false;
        activeVT = document.startViewTransition(function () {
          showOnlyPane(targetPane);
          targetPane.setAttribute('data-enter-dir', direction);
          targetPane.classList.add('is-entering');
          applyScrollAndFocus(route, opts.scrollY, true);
          if (myEpoch === navEpoch && !vtEmitted) {
            vtEmitted = true;
            emitViewEvents(route, direction);
          }
        });
        if (activeVT) {
          if (activeVT.ready && typeof activeVT.ready.catch === 'function') {
            activeVT.ready.catch(function () {});
          }
          if (activeVT.updateCallbackDone && typeof activeVT.updateCallbackDone.catch === 'function') {
            activeVT.updateCallbackDone.catch(function () {});
          }
          if (activeVT.finished && typeof activeVT.finished.then === 'function') {
            activeVT.finished.then(
              function () {
                finalize(myEpoch, targetPane);
                if (myEpoch === navEpoch && !vtEmitted) {
                  vtEmitted = true;
                  emitViewEvents(route, direction);
                }
              },
              function () {
                finalize(myEpoch, targetPane);
                if (myEpoch === navEpoch && !vtEmitted) {
                  vtEmitted = true;
                  emitViewEvents(route, direction);
                }
              }
            );
          }
        }
        cleanupTimer = window.setTimeout(function () {
          finalize(myEpoch, targetPane);
          if (myEpoch === navEpoch && !vtEmitted) {
            vtEmitted = true;
            emitViewEvents(route, direction);
          }
        }, 560);
        return;
      } catch (e) {
        activeVT = null;
      }
    }

    // Deterministic CSS 3D depth fallback: immediately hide inactive views so two
    // panels never stack vertically or flicker, then animate targetPane from Z-depth.
    showOnlyPane(targetPane);
    targetPane.setAttribute('data-enter-dir', direction);
    targetPane.classList.add('is-entering');
    applyScrollAndFocus(route, opts.scrollY, true);
    emitViewEvents(route, direction);

    function onDone(ev) {
      if (ev.target !== targetPane) return;
      targetPane.removeEventListener('animationend', onDone);
      targetPane.removeEventListener('transitionend', onDone);
      finalize(myEpoch, targetPane);
    }
    targetPane.addEventListener('animationend', onDone);
    targetPane.addEventListener('transitionend', onDone);
    cleanupTimer = window.setTimeout(function () {
      targetPane.removeEventListener('animationend', onDone);
      targetPane.removeEventListener('transitionend', onDone);
      finalize(myEpoch, targetPane);
    }, 520);
  }

  function navigate(href, opts) {
    var route = resolveRoute(href);
    if (!route) return false;
    transitionTo(route, Object.assign({ push: true }, opts || {}));
    return true;
  }

  document.addEventListener('click', function (event) {
    if (event.defaultPrevented || event.button !== 0) return;
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    var link = event.target && event.target.closest ? event.target.closest('a[href]') : null;
    if (!link) return;
    if (link.target && link.target !== '_self') return;
    if (link.hasAttribute('download')) return;

    var route = resolveRoute(link.getAttribute('href'));
    if (!route) return;
    event.preventDefault();
    transitionTo(route, { push: true });
  });

  window.addEventListener('popstate', function (event) {
    var route = resolveRoute(window.location.href);
    if (!route) return;
    var memY = scrollMemory[pageKey(window.location)];
    var savedY =
      event.state && typeof event.state.scrollY === 'number'
        ? event.state.scrollY
        : typeof memY === 'number'
          ? memY
          : 0;
    transitionTo(route, { push: false, scrollY: route.anchor ? null : savedY });
  });

  function initBootAnchor() {
    try {
      if (!window.history.state) {
        window.history.replaceState(
          { view: currentViewName, scrollY: window.scrollY || 0 },
          '',
          window.location.href
        );
      }
    } catch (e) {
      /* ignore */
    }

    var hashAnchor = window.location.hash ? decodeURIComponent(window.location.hash.slice(1)) : '';
    if (hashAnchor === 'donations') hashAnchor = 'donations-bay';
    var initialAnchor = hashAnchor || (document.body ? document.body.dataset.initialAnchor : '');
    if (initialAnchor) {
      var target = document.getElementById(initialAnchor);
      if (target) {
        if (!target.hasAttribute('tabindex')) target.setAttribute('tabindex', '-1');
        window.requestAnimationFrame(function () {
          try {
            target.scrollIntoView({ behavior: 'auto', block: 'start' });
          } catch (e) {
            target.scrollIntoView();
          }
          try {
            target.focus({ preventScroll: true });
          } catch (e) {
            /* ignore */
          }
          scrollMemory[pageKey(window.location)] = window.scrollY || 0;
        });
      }
    }
    if (window.ConfihSpatial && typeof window.ConfihSpatial.setView === 'function') {
      window.ConfihSpatial.setView(currentViewName);
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initBootAnchor);
  } else {
    initBootAnchor();
  }

  window.ConfihRouter = {
    currentView: function () {
      return currentViewName;
    },
    navigate: navigate,
    bootstrapOverview: bootstrapOverview,
    getBoot: function () {
      return boot;
    },
  };
})();
