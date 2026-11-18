import { createRequire } from 'node:module';
import readline from 'node:readline';
import zlib from 'node:zlib';

const require = createRequire(import.meta.url);
const PLAYWRIGHT_MODULE =
  process.env.PLAYWRIGHT_MODULE ||
  require.resolve('playwright-core');
const { chromium } = require(PLAYWRIGHT_MODULE);

const CHROMIUM_EXECUTABLE =
  process.env.CHROMIUM_EXECUTABLE ||
  require('playwright-core').chromium.executablePath();

const MAX_CONTEXTS_PER_BROWSER = 10;
let browserPromise = null;
let contextCount = 0;

async function resetBrowser() {
  const existing = browserPromise;
  browserPromise = null;
  contextCount = 0;
  if (existing) {
    try {
      const b = await existing;
      await b.close();
    } catch {
      // Ignore cleanup errors on crashed/disconnected browser.
    }
  }
}

async function getBrowser() {
  if (browserPromise) {
    try {
      const b = await browserPromise;
      if (!b.isConnected()) {
        await resetBrowser();
      } else {
        return b;
      }
    } catch {
      await resetBrowser();
    }
  }
  if (!browserPromise) {
    contextCount = 0;
    browserPromise = chromium.launch({
      executablePath: CHROMIUM_EXECUTABLE,
      headless: true,
      args: [
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--enable-webgl',
        '--ignore-gpu-blocklist',
      ],
    });
  }
  return browserPromise;
}

// ── Compositor-true PNG decoder (zero external deps) ────────────────────────
function paethPredictor(a, b, c) {
  const p = a + b - c;
  const pa = Math.abs(p - a);
  const pb = Math.abs(p - b);
  const pc = Math.abs(p - c);
  if (pa <= pb && pa <= pc) return a;
  if (pb <= pc) return b;
  return c;
}

function analyzePngBuffer(buf) {
  const sig = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
  if (buf.length < 24 || !buf.subarray(0, 8).equals(sig)) {
    throw new Error('Invalid PNG signature');
  }
  let offset = 8;
  let width = 0;
  let height = 0;
  let bitDepth = 0;
  let colorType = 0;
  let interlace = 0;
  const idatChunks = [];

  while (offset + 8 <= buf.length) {
    const length = buf.readUInt32BE(offset);
    const type = buf.subarray(offset + 4, offset + 8).toString('ascii');
    const dataStart = offset + 8;
    const dataEnd = dataStart + length;
    if (dataEnd + 4 > buf.length) break;
    const chunkData = buf.subarray(dataStart, dataEnd);
    if (type === 'IHDR') {
      if (chunkData.length < 13) {
        throw new Error(`Invalid PNG IHDR length: ${chunkData.length}`);
      }
      width = chunkData.readUInt32BE(0);
      height = chunkData.readUInt32BE(4);
      bitDepth = chunkData[8];
      colorType = chunkData[9];
      interlace = chunkData[12];
    } else if (type === 'IDAT') {
      idatChunks.push(chunkData);
    } else if (type === 'IEND') {
      break;
    }
    offset = dataEnd + 4;
  }

  if (!width || !height || bitDepth !== 8) {
    throw new Error(`Unsupported PNG IHDR: ${width}x${height} depth=${bitDepth} type=${colorType}`);
  }
  if (interlace !== 0) {
    throw new Error(`Unsupported interlaced PNG (interlace=${interlace})`);
  }
  const bppMap = { 0: 1, 2: 3, 4: 2, 6: 4 };
  const bpp = bppMap[colorType];
  if (!bpp) {
    throw new Error(`Unsupported PNG colorType: ${colorType}`);
  }

  const inflated = zlib.inflateSync(Buffer.concat(idatChunks));
  const stride = width * bpp;
  const expectedLen = height * (stride + 1);
  if (inflated.length < expectedLen) {
    throw new Error(`Truncated PNG IDAT stream: got ${inflated.length} bytes, expected >= ${expectedLen}`);
  }
  const prevRow = new Uint8Array(stride);
  const curRow = new Uint8Array(stride);
  const distinct = new Set();

  let bgKey = null;
  let nonBgCount = 0;
  let totalPixels = 0;

  let pos = 0;
  for (let y = 0; y < height; y++) {
    const filter = inflated[pos++];
    if (filter > 4) {
      throw new Error(`Unsupported PNG scanline filter: ${filter} at row ${y}`);
    }
    for (let x = 0; x < stride; x++) {
      const filt = inflated[pos++];
      const a = x >= bpp ? curRow[x - bpp] : 0;
      const b = prevRow[x];
      const c = x >= bpp ? prevRow[x - bpp] : 0;
      let val = filt;
      if (filter === 1) val = (filt + a) & 0xff;
      else if (filter === 2) val = (filt + b) & 0xff;
      else if (filter === 3) val = (filt + ((a + b) >> 1)) & 0xff;
      else if (filter === 4) val = (filt + paethPredictor(a, b, c)) & 0xff;
      curRow[x] = val;
    }

    // Sample every pixel (or stride by 2 if very large)
    const step = width * height > 250000 ? 2 : 1;
    if (y % step === 0) {
      for (let px = 0; px < width; px += step) {
        const i = px * bpp;
        let r = 0;
        let g = 0;
        let bl = 0;
        let al = 255;
        if (colorType === 6) {
          r = curRow[i];
          g = curRow[i + 1];
          bl = curRow[i + 2];
          al = curRow[i + 3];
        } else if (colorType === 2) {
          r = curRow[i];
          g = curRow[i + 1];
          bl = curRow[i + 2];
        } else if (colorType === 4) {
          r = g = bl = curRow[i];
          al = curRow[i + 1];
        } else {
          r = g = bl = curRow[i];
        }
        const qKey = ((r >> 3) << 15) | ((g >> 3) << 10) | ((bl >> 3) << 5) | (al >> 3);
        distinct.add(qKey);
        if (bgKey === null) bgKey = qKey;
        if (qKey !== bgKey) nonBgCount++;
        totalPixels++;
      }
    }
    prevRow.set(curRow);
  }

  return {
    width,
    height,
    distinctColors: distinct.size,
    nonBgFraction: totalPixels > 0 ? nonBgCount / totalPixels : 0,
  };
}

// ── Runtime Hygiene & Context Management ────────────────────────────────────
function attachRuntimeGuards(page, allowedOrigin, options = {}) {
  const allowStatuses = new Set(options.allowStatuses || []);
  const errors = {
    consoleErrors: [],
    pageErrors: [],
    failedRequests: [],
    httpErrors: [],
    thirdPartyRequests: [],
  };

  page.on('console', (msg) => {
    if (msg.type() === 'error') {
      errors.consoleErrors.push(msg.text());
    }
  });
  page.on('pageerror', (err) => {
    errors.pageErrors.push(String(err));
  });
  page.on('requestfailed', (req) => {
    const reason = req.failure()?.errorText || 'failed';
    if (reason !== 'net::ERR_ABORTED') {
      errors.failedRequests.push(`${req.url()} (${reason})`);
    }
  });
  page.on('request', (req) => {
    const url = req.url();
    if (url.startsWith('data:') || url.startsWith('blob:') || url.startsWith('about:')) return;
    try {
      const origin = new URL(url).origin;
      if (origin !== allowedOrigin) {
        errors.thirdPartyRequests.push(url);
      }
    } catch {
      errors.thirdPartyRequests.push(url);
    }
  });
  page.on('response', (res) => {
    const status = res.status();
    if (status >= 400 && !allowStatuses.has(status)) {
      errors.httpErrors.push(`${status} ${res.url()}`);
    }
  });
  return errors;
}

async function withContext(params, fn) {
  const viewport = params.viewport || { width: 1440, height: 900 };
  const colorScheme = params.colorScheme || 'dark';
  const reducedMotion = params.reducedMotion || 'no-preference';
  const forcedColors = params.forcedColors || 'none';
  const hasTouch = Boolean(params.hasTouch);
  const isMobile = Boolean(params.isMobile);

  for (let attempt = 0; attempt < 2; attempt++) {
    if (contextCount >= MAX_CONTEXTS_PER_BROWSER) {
      await resetBrowser();
    }
    const browser = await getBrowser();
    contextCount++;
    const context = await browser.newContext({
      viewport,
      colorScheme,
      reducedMotion,
      forcedColors,
      hasTouch,
      isMobile,
    });
    let page = null;
    try {
      await context.addInitScript(() => {
        const canvasMap = new WeakMap();
        const wrappedCtx = new WeakSet();
        const origGetContext = HTMLCanvasElement.prototype.getContext;
        const GL_DRAW_FNS = [
          'drawArrays',
          'drawElements',
          'drawArraysInstanced',
          'drawElementsInstanced',
          'drawRangeElements',
        ];
        const GL_CLEAR_FNS = ['clear'];
        const C2D_DRAW_FNS = [
          'fillRect',
          'strokeRect',
          'fill',
          'stroke',
          'drawImage',
          'putImageData',
          'fillText',
          'strokeText',
        ];
        const C2D_CLEAR_FNS = ['clearRect'];

        function wrapMethods(ctx, names, counterFn) {
          for (const name of names) {
            const orig = ctx[name];
            if (typeof orig === 'function') {
              ctx[name] = function (...args) {
                counterFn();
                return orig.apply(this, args);
              };
            }
          }
        }

        HTMLCanvasElement.prototype.getContext = function (type, ...rest) {
          const ctx = origGetContext.call(this, type, ...rest);
          if (!ctx) return ctx;
          const normType = String(type || '').toLowerCase();
          let entry = canvasMap.get(this);
          if (!entry) {
            entry = { contextType: normType, drawCalls: 0, clearCalls: 0 };
            canvasMap.set(this, entry);
          }
          if (!wrappedCtx.has(ctx)) {
            wrappedCtx.add(ctx);
            if (normType === 'webgl2' || normType === 'webgl' || normType === 'experimental-webgl') {
              wrapMethods(ctx, GL_DRAW_FNS, () => {
                entry.drawCalls++;
              });
              wrapMethods(ctx, GL_CLEAR_FNS, () => {
                entry.clearCalls++;
              });
            } else if (normType === '2d') {
              wrapMethods(ctx, C2D_DRAW_FNS, () => {
                entry.drawCalls++;
              });
              wrapMethods(ctx, C2D_CLEAR_FNS, () => {
                entry.clearCalls++;
              });
            }
          }
          return ctx;
        };

        let pageRafCount = 0;
        const origRaf = window.requestAnimationFrame.bind(window);
        window.requestAnimationFrame = function (cb) {
          pageRafCount++;
          return origRaf(cb);
        };

        Object.defineProperty(window, '__confihGetCanvasAudit', {
          value: (canvas) => canvasMap.get(canvas) || null,
          enumerable: false,
          configurable: false,
        });
        Object.defineProperty(window, '__confihGetRafCount', {
          value: () => pageRafCount,
          enumerable: false,
          configurable: false,
        });
      });

      if (params.baseUrl) {
        await context.grantPermissions(['clipboard-read', 'clipboard-write'], {
          origin: params.baseUrl,
        });
      }
      page = await context.newPage();
      page.setDefaultTimeout(10000);
      return await fn(page, context);
    } catch (err) {
      const msg = err && err.message ? err.message : String(err);
      if (attempt === 0 && /Page crashed|Target crashed|Target closed/i.test(msg)) {
        await resetBrowser();
        continue;
      }
      throw err;
    } finally {
      if (page) await page.close().catch(() => {});
      await context.close().catch(() => {});
    }
  }
}

async function waitForPageHydration(page, path) {
  await page.waitForLoadState('domcontentloaded');
  await page.waitForLoadState('networkidle').catch(() => {});
  if (path.startsWith('/plays')) {
    await page
      .waitForFunction(() => {
        const top = document.getElementById('plays-top');
        const chart = document.getElementById('plays-chart');
        const status = document.getElementById('plays-status');
        const hasTracks = top && top.children.length > 0;
        const hasChart = chart && chart.children.length > 0;
        const hasEmptyStatus = status && !status.hidden && status.textContent.trim().length > 0;
        return (hasTracks && hasChart) || hasEmptyStatus;
      }, { timeout: 6000 })
      .catch(() => {});
  } else if (path.startsWith('/support') || path.startsWith('/about')) {
    await page
      .waitForFunction(() => {
        const assets = document.getElementById('support-assets');
        const status = document.getElementById('support-status');
        const about = document.getElementById('about-headline');
        const hasAssets = assets && !assets.hidden && assets.children.length > 0;
        const hasStatus = status && !status.hidden && status.textContent.trim().length > 0;
        return hasAssets || hasStatus || Boolean(about && about.textContent.trim().length > 0);
      }, { timeout: 6000 })
      .catch(() => {});
  } else {
    await page
      .waitForFunction(() => {
        const headline = document.getElementById('status-headline');
        return !headline || headline.textContent.trim().length > 0;
      }, { timeout: 4000 })
      .catch(() => {});
  }
  await page.waitForTimeout(250);
}

async function clickNavDestination(page, selector, nameRegex) {
  let loc = page.locator(selector).first();
  if ((await loc.count()) === 0) {
    loc = page.getByRole('navigation').getByRole('link', { name: nameRegex }).first();
  }
  await loc.click();
}

// ── Shared Audits ───────────────────────────────────────────────────────────
function mergeBrokenImages(baseImages, extraBroken) {
  if (!extraBroken || extraBroken.length === 0) return baseImages;
  const seen = new Set(baseImages.broken.map((b) => `${b.id || ''}|${b.src || ''}`));
  const merged = [...baseImages.broken];
  for (const item of extraBroken) {
    const key = `${item.id || ''}|${item.src || ''}`;
    if (!seen.has(key)) {
      seen.add(key);
      merged.push(item);
    }
  }
  return { ...baseImages, broken: merged };
}

async function checkVisibleImages(page) {
  return page.evaluate(async () => {
    const imgs = [...document.querySelectorAll('img')];
    const inViewUnloaded = imgs.filter((img) => {
      if (img.closest('[hidden], [aria-hidden="true"], dialog:not([open])')) return false;
      const cs = getComputedStyle(img);
      if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) <= 0.01) return false;
      const r = img.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) return false;
      const inViewport = r.bottom > 0 && r.top < window.innerHeight && r.right > 0 && r.left < window.innerWidth;
      return inViewport && !img.complete;
    });
    if (inViewUnloaded.length > 0) {
      await Promise.all(
        inViewUnloaded.map(
          (img) =>
            new Promise((resolve) => {
              const t = setTimeout(resolve, 1500);
              img.addEventListener('load', () => { clearTimeout(t); resolve(); }, { once: true });
              img.addEventListener('error', () => { clearTimeout(t); resolve(); }, { once: true });
            })
        )
      );
    }
    const broken = [];
    let visibleCount = 0;
    for (const img of imgs) {
      if (img.closest('[hidden], [aria-hidden="true"], dialog:not([open])')) continue;
      const cs = getComputedStyle(img);
      if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) <= 0.01) continue;
      const r = img.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) continue;
      const inViewport = r.bottom > 0 && r.top < window.innerHeight && r.right > 0 && r.left < window.innerWidth;
      // Chromium intentionally defers off-screen loading="lazy" images until scrolled into view.
      if (img.loading === 'lazy' && !inViewport && !img.complete) continue;
      if (img.complete && img.naturalWidth > 0 && typeof img.decode === 'function') {
        await img.decode().catch(() => {});
      }
      visibleCount++;
      if (!img.complete || img.naturalWidth <= 0 || img.naturalHeight <= 0) {
        broken.push({
          id: img.id || null,
          src: img.getAttribute('src') || '',
          complete: img.complete,
          naturalWidth: img.naturalWidth,
        });
      }
    }
    return { visibleCount, broken };
  });
}

async function auditScrollVisibilityAndOverflow(page) {
  const stops = [0, 0.25, 0.5, 0.75, 1.0, 0.7, 0.35, 0];
  const overflowFailures = [];
  const strandedFailures = [];
  const brokenImages = [];
  const seenBroken = new Set();

  for (const frac of stops) {
    await page.evaluate((f) => {
      const maxScroll = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
      window.scrollTo({ top: Math.round(maxScroll * f), behavior: 'instant' });
    }, frac);
    // Allow .rise / reveal animations (worst-case 590ms) and lazy images in-viewport to settle.
    await page.waitForTimeout(650);
    await page.evaluate(async () => {
      const imgs = [...document.querySelectorAll('img')].filter((img) => {
        if (img.closest('[hidden], [aria-hidden="true"], dialog:not([open])')) return false;
        const r = img.getBoundingClientRect();
        return r.bottom > 0 && r.top < window.innerHeight && r.width > 0 && r.height > 0;
      });
      await Promise.all(
        imgs.map(async (img) => {
          if (!img.complete) {
            await new Promise((resolve) => {
              const t = setTimeout(resolve, 1500);
              img.addEventListener('load', () => { clearTimeout(t); resolve(); }, { once: true });
              img.addEventListener('error', () => { clearTimeout(t); resolve(); }, { once: true });
            });
          }
          if (img.complete && img.naturalWidth > 0 && typeof img.decode === 'function') {
            await img.decode().catch(() => {});
          }
        })
      );
    });

    const stepResult = await page.evaluate((f) => {
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const docOverflow = document.documentElement.scrollWidth > vw;
      const bodyOverflow = document.body.scrollWidth > vw;
      const main = document.querySelector('main') || document.body;
      const stranded = [];
      const candidates = main.querySelectorAll(
        'h1, h2, h3, h4, p, section, article, li, a, button, table, canvas, dl, details, div'
      );
      for (const el of candidates) {
        if (el.closest('[hidden], [aria-hidden="true"], template, noscript, dialog:not([open])')) continue;
        if (el.closest('details:not([open])') && !el.closest('summary')) continue;
        // Skip intentionally disabled buttons and chart bar segments (#timeline-bars, #timeline-30d-bars)
        if (el.matches(':disabled') || el.closest('#timeline-bars, #timeline-30d-bars')) continue;
        if (el.tagName === 'DIV') {
          const hasText = (el.textContent || '').trim().length > 0;
          if (!hasText || el.children.length > 0) continue;
        }
        const cs = getComputedStyle(el);
        if (cs.display === 'none') continue;
        const r = el.getBoundingClientRect();
        if (r.width <= 2 && r.height <= 2 && cs.overflow === 'hidden') continue;
        if (r.width === 0 || r.height === 0) continue;

        let cur = el;
        let effOpacity = 1;
        let hiddenAncestor = false;
        let effVis = cs.visibility;
        while (cur && cur.nodeType === 1) {
          const ccs = cur === el ? cs : getComputedStyle(cur);
          if (ccs.display === 'none') {
            hiddenAncestor = true;
            break;
          }
          if (ccs.visibility === 'hidden') effVis = 'hidden';
          const cop = parseFloat(ccs.opacity);
          if (!Number.isNaN(cop)) effOpacity *= cop;
          if (cur === main || cur === document.body) break;
          cur = cur.parentElement;
        }
        if (hiddenAncestor) continue;

        if (r.bottom > 8 && r.top < vh - 8) {
          if (effOpacity < 0.95 || effVis === 'hidden') {
            stranded.push({
              stop: f,
              tag: el.tagName,
              id: el.id || null,
              text: (el.textContent || '').trim().slice(0, 40),
              opacity: Number(effOpacity.toFixed(3)),
              visibility: effVis,
              top: Math.round(r.top),
            });
          }
        }
      }

      const inViewBroken = [];
      for (const img of document.querySelectorAll('img')) {
        if (img.closest('[hidden], [aria-hidden="true"], dialog:not([open])')) continue;
        const cs = getComputedStyle(img);
        if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) <= 0.01) continue;
        const r = img.getBoundingClientRect();
        if (r.width === 0 || r.height === 0) continue;
        if (r.bottom <= 0 || r.top >= vh || r.right <= 0 || r.left >= vw) continue;
        if (!img.complete || img.naturalWidth <= 0 || img.naturalHeight <= 0) {
          inViewBroken.push({
            id: img.id || null,
            src: img.getAttribute('src') || '',
            complete: img.complete,
            naturalWidth: img.naturalWidth,
            stop: f,
          });
        }
      }

      const clippedNav = [];
      if (vw <= 430) {
        for (const a of document.querySelectorAll('nav a')) {
          const cs = getComputedStyle(a);
          if (cs.display === 'none' || cs.visibility === 'hidden') continue;
          const r = a.getBoundingClientRect();
          if (r.width === 0 || r.height === 0) continue;
          if (r.right > vw + 1) {
            clippedNav.push({ text: (a.textContent || '').trim(), right: Math.round(r.right), vw });
          }
        }
      }

      return {
        stop: f,
        docOverflow,
        bodyOverflow,
        scrollWidth: document.documentElement.scrollWidth,
        innerWidth: vw,
        stranded,
        inViewBroken,
        clippedNav,
      };
    }, frac);

    if (stepResult.docOverflow || stepResult.bodyOverflow || stepResult.clippedNav.length > 0) {
      overflowFailures.push(stepResult);
    }
    if (stepResult.stranded.length > 0) {
      strandedFailures.push(...stepResult.stranded);
    }
    for (const img of stepResult.inViewBroken) {
      const key = `${img.id || ''}|${img.src || ''}`;
      if (!seenBroken.has(key)) {
        seenBroken.add(key);
        brokenImages.push(img);
      }
    }
  }

  return { overflowFailures, strandedFailures, brokenImages };
}

async function auditSpatial3D(page, { checkDrag = false, reducedMotion = 'no-preference' } = {}) {
  const spatial = await page.evaluate(() => {
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    const getEffectiveVisibility = (el) => {
      let cur = el;
      let effOpacity = 1;
      while (cur && cur.nodeType === 1) {
        const ccs = getComputedStyle(cur);
        if (ccs.display === 'none' || ccs.visibility === 'hidden') {
          return { visible: false, opacity: 0 };
        }
        const cop = parseFloat(ccs.opacity);
        if (!Number.isNaN(cop)) effOpacity *= cop;
        if (cur === document.body) break;
        cur = cur.parentElement;
      }
      return { visible: effOpacity > 0.05, opacity: effOpacity };
    };

    const canvases = [...document.querySelectorAll('canvas')]
      .map((c, domIndex) => {
        const r = c.getBoundingClientRect();
        const vis = getEffectiveVisibility(c);
        const audit = typeof window.__confihGetCanvasAudit === 'function'
          ? window.__confihGetCanvasAudit(c)
          : null;
        return {
          domIndex,
          id: c.id || null,
          x: r.left,
          y: r.top,
          w: r.width,
          h: r.height,
          attrW: c.width,
          attrH: c.height,
          visible: vis.visible,
          contextType: audit ? audit.contextType : null,
          drawCalls: audit ? audit.drawCalls : 0,
          clearCalls: audit ? audit.clearCalls : 0,
        };
      })
      .filter(
        (c) =>
          c.visible &&
          c.w >= 120 &&
          c.h >= 120 &&
          c.attrW > 0 &&
          c.attrH > 0 &&
          c.drawCalls > 0
      );

    const css3dVisible = [...document.querySelectorAll('body *')].filter((el) => {
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return false;
      const vis = getEffectiveVisibility(el);
      if (!vis.visible) return false;
      const cs = getComputedStyle(el);
      return (
        (cs.perspective && cs.perspective !== 'none') ||
        cs.transformStyle === 'preserve-3d' ||
        (cs.transform && cs.transform.startsWith('matrix3d('))
      );
    });
    const css3dTarget = css3dVisible[0] ? css3dVisible[0].getBoundingClientRect() : null;

    return {
      canvas: canvases[0] || null,
      css3dCount: css3dVisible.length,
      css3dTargetRect: css3dTarget
        ? { x: css3dTarget.left, y: css3dTarget.top, w: css3dTarget.width, h: css3dTarget.height }
        : null,
      viewport: { vw, vh },
    };
  });

  const active3d = Boolean(spatial.canvas || spatial.css3dCount > 0);
  let canvasStats = null;
  let dragChanged = checkDrag ? false : null;
  let dragLayoutStable = checkDrag ? true : null;
  let reducedStatic = null;
  let reducedTransitionsOk = null;
  let rafFrames = null;

  const clampClip = (rect) => {
    if (!rect) return null;
    const x = Math.max(0, Math.floor(rect.x));
    const y = Math.max(0, Math.floor(rect.y));
    const right = Math.min(spatial.viewport.vw, Math.ceil(rect.x + rect.w));
    const bottom = Math.min(spatial.viewport.vh, Math.ceil(rect.y + rect.h));
    const width = right - x;
    const height = bottom - y;
    if (width < 20 || height < 20) return null;
    return { x, y, width, height };
  };

  if (spatial.canvas) {
    const clip = clampClip(spatial.canvas);
    if (clip) {
      const png1 = await page.screenshot({ clip });
      canvasStats = analyzePngBuffer(png1);

      if (reducedMotion === 'reduce') {
        const readCanvasAudit = () =>
          page.evaluate((idx) => {
            const canvas = document.querySelectorAll('canvas')[idx];
            const audit =
              canvas && typeof window.__confihGetCanvasAudit === 'function'
                ? window.__confihGetCanvasAudit(canvas)
                : null;
            const rafCount =
              typeof window.__confihGetRafCount === 'function' ? window.__confihGetRafCount() : null;
            return {
              hasAudit: Boolean(audit),
              drawCalls: audit ? audit.drawCalls : 0,
              clearCalls: audit ? audit.clearCalls : 0,
              rafCount,
            };
          }, spatial.canvas.domIndex);

        const auditBefore = await readCanvasAudit();
        await page.waitForTimeout(350);
        const auditAfter = await readCanvasAudit();
        if (auditBefore.hasAudit && auditAfter.hasAudit) {
          const drawDelta = auditAfter.drawCalls - auditBefore.drawCalls;
          const clearDelta = auditAfter.clearCalls - auditBefore.clearCalls;
          const rafDelta =
            auditBefore.rafCount !== null && auditAfter.rafCount !== null
              ? auditAfter.rafCount - auditBefore.rafCount
              : 0;
          rafFrames = rafDelta;
          reducedStatic = drawDelta === 0 && clearDelta === 0 && rafDelta === 0;
        } else {
          const png2 = await page.screenshot({ clip });
          reducedStatic = Buffer.compare(png1, png2) === 0;
        }
      } else {
        rafFrames = await page.evaluate(
          () =>
            new Promise((resolve) => {
              let count = 0;
              const start = performance.now();
              function tick() {
                count++;
                if (performance.now() - start >= 380) resolve(count);
                else requestAnimationFrame(tick);
              }
              requestAnimationFrame(tick);
            })
        );
      }
    }
  }

  if (checkDrag && reducedMotion !== 'reduce' && active3d) {
    const targetRect = spatial.canvas || spatial.css3dTargetRect;
    const clip = clampClip(targetRect);
    if (clip) {
      const scrollYBefore = await page.evaluate(() => window.scrollY);
      const beforeDrag = await page.screenshot({ clip });
      const cx = clip.x + clip.width * 0.5;
      const cy = clip.y + clip.height * 0.5;
      await page.mouse.move(cx - 70, cy - 20);
      await page.mouse.down();
      await page.mouse.move(cx + 90, cy + 30, { steps: 10 });
      await page.mouse.up();
      await page.waitForTimeout(200);
      const afterDrag = await page.screenshot({ clip });
      dragChanged = Buffer.compare(beforeDrag, afterDrag) !== 0;
      dragLayoutStable = await page.evaluate((y0) => {
        const vw = window.innerWidth;
        const noOverflow =
          document.documentElement.scrollWidth <= vw && document.body.scrollWidth <= vw;
        return noOverflow && Math.abs(window.scrollY - y0) <= 2;
      }, scrollYBefore);
    }
  }

  if (reducedMotion === 'reduce') {
    reducedTransitionsOk = await page.evaluate(() => {
      const matches = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      if (!matches) return { matches: false, slowEls: [] };
      const maxDuration = (raw) =>
        Math.max(0, ...String(raw || '0').split(',').map((v) => parseFloat(v) || 0));
      const slowEls = [];
      for (const el of document.querySelectorAll(
        'main, main *, header, header *, nav, nav *, footer, footer *, dialog, dialog *'
      )) {
        const cs = getComputedStyle(el);
        const td = maxDuration(cs.transitionDuration);
        const ad = maxDuration(cs.animationDuration);
        const animActive = Boolean(cs.animationName && cs.animationName !== 'none');
        const isInfinite =
          animActive &&
          cs.animationIterationCount.split(',').some((v) => v.trim() === 'infinite') &&
          ad > 0;
        if (td > 0.05 || (animActive && (ad > 0.05 || isInfinite))) {
          slowEls.push({
            tag: el.tagName,
            id: el.id || null,
            td,
            ad,
            anim: cs.animationName,
            iter: cs.animationIterationCount,
          });
          if (slowEls.length >= 5) break;
        }
      }
      return { matches: true, slowEls };
    });
  }

  return {
    active3d,
    hasCanvas: Boolean(spatial.canvas),
    canvasRect: spatial.canvas,
    css3dCount: spatial.css3dCount,
    canvasStats,
    dragChanged,
    dragLayoutStable,
    rafFrames,
    reducedStatic,
    reducedTransitionsOk,
  };
}

async function checkQrContainer(rootLocator) {
  return rootLocator.evaluate((root) => {
    for (const svg of root.querySelectorAll('svg')) {
      const r = svg.getBoundingClientRect();
      const shapes = svg.querySelectorAll('rect, path, polygon').length;
      let pathDataLen = 0;
      let subpathCount = 0;
      for (const p of svg.querySelectorAll('path')) {
        const d = p.getAttribute('d') || '';
        pathDataLen += d.length;
        const moves = d.match(/[Mm]/g);
        if (moves) subpathCount += moves.length;
      }
      if (
        r.width >= 90 &&
        r.height >= 90 &&
        (shapes >= 5 || (pathDataLen >= 100 && subpathCount >= 20))
      ) {
        return {
          kind: 'svg',
          shapes,
          pathDataLen,
          subpathCount,
          w: Math.round(r.width),
          h: Math.round(r.height),
        };
      }
    }
    for (const canvas of root.querySelectorAll('canvas')) {
      const r = canvas.getBoundingClientRect();
      if (r.width >= 90 && r.height >= 90 && canvas.width > 0) {
        return { kind: 'canvas', w: Math.round(r.width), h: Math.round(r.height) };
      }
    }
    for (const img of root.querySelectorAll('img')) {
      const r = img.getBoundingClientRect();
      if (
        img.complete &&
        img.naturalWidth > 0 &&
        r.width >= 90 &&
        (img.src.startsWith('data:') || img.src.startsWith('blob:'))
      ) {
        return { kind: 'img', naturalWidth: img.naturalWidth };
      }
    }
    return null;
  });
}

// ── Command Handlers ────────────────────────────────────────────────────────
async function handleAuditRoute(params) {
  return withContext(params, async (page) => {
    const errors = attachRuntimeGuards(page, params.baseUrl);
    const resp = await page.goto(`${params.baseUrl}${params.path}`, { waitUntil: 'domcontentloaded' });
    await waitForPageHydration(page, params.path);

    const spatial = await auditSpatial3D(page, {
      checkDrag: Boolean(params.checkDrag),
      reducedMotion: params.reducedMotion || 'no-preference',
    });
    const scrollAudit =
      params.checkScroll !== false
        ? await auditScrollVisibilityAndOverflow(page)
        : { overflowFailures: [], strandedFailures: [], brokenImages: [] };
    const images = mergeBrokenImages(await checkVisibleImages(page), scrollAudit.brokenImages);

    const domChecks = await page.evaluate((p) => {
      const navLinks = [...document.querySelectorAll('nav a')].map((a) => ({
        href: a.getAttribute('href') || '',
        text: (a.textContent || '').trim(),
        ariaCurrent: a.getAttribute('aria-current'),
      }));
      const h1 = [...document.querySelectorAll('main h1, h1')].find((el) => {
        if (el.closest('[hidden], [aria-hidden="true"]')) return false;
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden') return false;
        const r = el.getBoundingClientRect();
        return r.width > 0 && r.height > 0;
      });
      const playsIds = {};
      for (const id of [
        'plays-status',
        'plays-stats',
        'plays-chart',
        'plays-axis',
        'plays-readout',
        'plays-legend',
        'plays-top',
        'plays-recent',
        'plays-mix',
      ]) {
        playsIds[id] = Boolean(document.getElementById(id));
      }
      return {
        title: document.title,
        theme: document.documentElement.getAttribute('data-theme'),
        h1Text: h1 ? h1.textContent.trim() : '',
        navLinks,
        playsIds,
        bodyHasGateway: document.body.textContent.includes('Confih'),
        path: p,
      };
    }, params.path);

    return {
      ok: true,
      status: resp ? resp.status() : 0,
      url: page.url(),
      errors,
      images,
      spatial,
      scrollAudit,
      domChecks,
    };
  });
}

async function handleCheckSpaNav(params) {
  return withContext(params, async (page) => {
    const errors = attachRuntimeGuards(page, params.baseUrl);
    await page.goto(`${params.baseUrl}${params.startPath || '/'}`, { waitUntil: 'domcontentloaded' });
    await waitForPageHydration(page, params.startPath || '/');

    await page.evaluate(() => {
      window.__spaAlive = true;
    });

    const navSummary = await page.evaluate(() =>
      [...document.querySelectorAll('nav a')].map((a) => ({
        text: (a.textContent || '').trim(),
        href: a.getAttribute('href') || '',
      }))
    );

    const readSpaNavState = async (includeTitle = false) =>
      page.evaluate((withTitle) => {
        const headingEl = [...document.querySelectorAll('main h1, main h2, h1')].find((el) => {
          if (el.closest('[hidden], [aria-hidden="true"]')) return false;
          const cs = getComputedStyle(el);
          if (cs.display === 'none' || cs.visibility === 'hidden') return false;
          const r = el.getBoundingClientRect();
          return r.width > 0 && r.height > 0;
        });
        const out = {
          pathname: window.location.pathname,
          spaAlive: window.__spaAlive === true,
          ariaCurrentHref:
            document.querySelector('nav a[aria-current="page"]')?.getAttribute('href') || null,
          heading: (headingEl?.textContent || '').trim(),
        };
        if (withTitle) out.title = document.title;
        return out;
      }, includeTitle);

    const steps = [];
    const clickNav = async (selector, nameRegex, hydrationHint) => {
      await clickNavDestination(page, selector, nameRegex);
      await page.waitForTimeout(350);
      await waitForPageHydration(page, hydrationHint);
      const state = await readSpaNavState(true);
      steps.push(state);
      return state;
    };

    const playsStep = await clickNav('nav a[href^="/plays"]', /plays/i, '/plays');
    const supportStep = await clickNav(
      'nav a[href^="/support"], nav a[href^="/about"]',
      /about|support/i,
      '/support'
    );
    const statusStep = await clickNav(
      'nav a[href^="/status"], nav a[href="/"]',
      /status|gateway/i,
      '/status'
    );

    await page.goBack();
    await page.waitForTimeout(350);
    const back1 = await readSpaNavState(false);

    await page.goBack();
    await page.waitForTimeout(350);
    const back2 = await readSpaNavState(false);

    await page.goForward();
    await page.waitForTimeout(350);
    const forward1 = await readSpaNavState(false);

    return {
      ok: true,
      errors,
      navSummary,
      playsStep,
      supportStep,
      statusStep,
      back1,
      back2,
      forward1,
    };
  });
}

async function handleCheckThemeToggle(params) {
  return withContext(params, async (page) => {
    const errors = attachRuntimeGuards(page, params.baseUrl);
    await page.goto(`${params.baseUrl}${params.path || '/'}`, { waitUntil: 'domcontentloaded' });
    await waitForPageHydration(page, params.path || '/');

    const before = await page.evaluate(() => ({
      theme: document.documentElement.getAttribute('data-theme'),
      stored: localStorage.getItem('bc-theme'),
      bg: getComputedStyle(document.body).backgroundColor,
      toggleCount: document.querySelectorAll('#theme-toggle').length,
    }));

    await page.reload({ waitUntil: 'domcontentloaded' });
    await waitForPageHydration(page, params.path || '/');
    const afterReload = await page.evaluate(() => ({
      theme: document.documentElement.getAttribute('data-theme'),
      stored: localStorage.getItem('bc-theme'),
      bg: getComputedStyle(document.body).backgroundColor,
    }));

    return { ok: true, errors, before, afterReload };
  });
}

async function handleCheckGatewayQrCopy(params) {
  return withContext(params, async (page) => {
    const errors = attachRuntimeGuards(page, params.baseUrl);
    const targetPath = params.path || '/';
    await page.goto(`${params.baseUrl}${targetPath}`, { waitUntil: 'domcontentloaded' });
    await waitForPageHydration(page, targetPath);

    const pathSecret = Boolean(params.pathSecret);
    let copyBtn = page.locator('#copy-btn').first();
    if ((await copyBtn.count()) === 0) {
      copyBtn = page.getByRole('button', { name: /copy.*url/i }).first();
    }

    let copyBtnTextAfter = '';
    let clipboardText = '';
    if (await copyBtn.isEnabled()) {
      await copyBtn.click();
      await page.waitForTimeout(150);
      copyBtnTextAfter = (await copyBtn.textContent()) || '';
      clipboardText = await page.evaluate(() => navigator.clipboard.readText().catch(() => ''));
    }

    let qrTrigger = page.locator('#open-qr-btn').first();
    if ((await qrTrigger.count()) === 0) {
      qrTrigger = page.getByRole('button', { name: /qr/i }).first();
    }
    await qrTrigger.click();
    await page.waitForTimeout(200);

    let dialog = page.locator('#qr-modal').first();
    if ((await dialog.count()) === 0) {
      dialog = page.getByRole('dialog').first();
    }
    const dialogVisible = await dialog.isVisible();
    const dialogText = await dialog.innerText();
    const qrGraphic = pathSecret ? null : await checkQrContainer(dialog);

    await page.keyboard.press('Escape');
    await page.waitForTimeout(300);
    const dialogClosed = !(await dialog.isVisible());

    const addonUrlText = await page
      .locator('#addon-url')
      .first()
      .textContent()
      .catch(() => '');

    return {
      ok: true,
      errors,
      copyBtnDisabled: !(await copyBtn.isEnabled()),
      copyBtnTextAfter: copyBtnTextAfter.trim(),
      clipboardText,
      dialogVisible,
      dialogText,
      addonUrlText: (addonUrlText || '').trim(),
      qrGraphic,
      dialogClosed,
    };
  });
}

async function handleCheckSupportControls(params) {
  return withContext(params, async (page) => {
    const errors = attachRuntimeGuards(page, params.baseUrl);
    await page.goto(`${params.baseUrl}/support`, { waitUntil: 'domcontentloaded' });
    await waitForPageHydration(page, '/support');

    const assetCount = await page.locator('#support-assets > *').count();
    const demoVisible = await page.locator('#support-demo').isVisible().catch(() => false);

    let copyTextAfter = '';
    let clipboardText = '';
    let qrGraphic = null;
    let expandedAfterOpen = null;
    let expandedAfterClose = null;

    if (assetCount > 0) {
      const copyBtn = page.locator('#support-assets').getByRole('button', { name: /copy/i }).first();
      await copyBtn.click();
      await page.waitForTimeout(150);
      copyTextAfter = ((await copyBtn.textContent()) || '').trim();
      clipboardText = await page.evaluate(() => navigator.clipboard.readText().catch(() => ''));

      const qrBtn = page.locator('#support-assets').getByRole('button', { name: /show qr|qr/i }).first();
      await qrBtn.click();
      await page.waitForTimeout(200);
      expandedAfterOpen = await qrBtn.getAttribute('aria-expanded');
      const controlsId = await qrBtn.getAttribute('aria-controls');
      const qrBox = controlsId ? page.locator(`#${controlsId}`) : page.locator('#support-assets').first();
      qrGraphic = await checkQrContainer(qrBox);

      await qrBtn.click();
      await page.waitForTimeout(150);
      expandedAfterClose = await qrBtn.getAttribute('aria-expanded');
    }

    const visibleDetails = page.locator('details:visible');
    const faqCount = await visibleDetails.count();
    let faqOpened = null;
    if (faqCount > 0) {
      const firstDetails = visibleDetails.first();
      const firstSummary = firstDetails.locator('summary').first();
      await firstSummary.scrollIntoViewIfNeeded();
      await firstSummary.click();
      faqOpened = await firstDetails.evaluate((d) => d.open);
    }

    return {
      ok: true,
      errors,
      assetCount,
      demoVisible,
      copyTextAfter,
      clipboardText,
      expandedAfterOpen,
      expandedAfterClose,
      qrGraphic,
      faqCount,
      faqOpened,
    };
  });
}

async function handleCheckPlaysControls(params) {
  return withContext(params, async (page) => {
    const errors = attachRuntimeGuards(page, params.baseUrl);
    await page.goto(`${params.baseUrl}/plays`, { waitUntil: 'domcontentloaded' });
    await waitForPageHydration(page, '/plays');

    const scrollPre = params.checkScroll === true
      ? await auditScrollVisibilityAndOverflow(page)
      : { overflowFailures: [], strandedFailures: [], brokenImages: [] };
    const images = mergeBrokenImages(await checkVisibleImages(page), scrollPre.brokenImages);
    const counts = await page.evaluate(() => ({
      topCount: document.getElementById('plays-top')?.children.length || 0,
      recentCount: document.getElementById('plays-recent')?.children.length || 0,
      chartCols: document.getElementById('plays-chart')?.children.length || 0,
      legendBtns: document.querySelectorAll('#plays-legend button').length,
      artImages: [...document.querySelectorAll('img')]
        .map((i) => i.getAttribute('src') || '')
        .filter((s) => s.startsWith('/art/')).length,
    }));

    const readoutBefore = (await page.locator('#plays-readout').textContent()) || '';
    const firstCol = page.locator('#plays-chart button, #plays-chart [tabindex]').first();
    let readoutAfterFocus = readoutBefore;
    let readoutAfterClick = readoutBefore;
    let readoutAfterArrow = readoutBefore;
    if ((await firstCol.count()) > 0) {
      await firstCol.focus();
      await page.waitForTimeout(80);
      readoutAfterFocus = (await page.locator('#plays-readout').textContent()) || '';
      await page.keyboard.press('ArrowRight');
      await page.waitForTimeout(100);
      readoutAfterArrow = (await page.locator('#plays-readout').textContent()) || '';
      await firstCol.click();
      await page.waitForTimeout(100);
      readoutAfterClick = (await page.locator('#plays-readout').textContent()) || '';
      await page.keyboard.press('Escape');
    }

    const legendBtn = page.locator('#plays-legend button').first();
    let pressedOn = null;
    let pressedOff = null;
    if ((await legendBtn.count()) > 0) {
      await legendBtn.click();
      await page.waitForTimeout(100);
      pressedOn = await legendBtn.getAttribute('aria-pressed');
      await legendBtn.click();
      await page.waitForTimeout(100);
      pressedOff = await legendBtn.getAttribute('aria-pressed');
    }

    const searchInput = page.locator('input[type="search"], input[placeholder*="search" i]').first();
    let searchTested = false;
    let searchTopText = '';
    if ((await searchInput.count()) > 0 && (await searchInput.isVisible())) {
      searchTested = true;
      const searchRespPromise = page
        .waitForResponse(
          (r) => r.url().includes('/api/plays') && r.url().includes('q=Daft') && r.status() === 200,
          { timeout: 2500 }
        )
        .catch(() => null);
      await searchInput.fill('Daft');
      await searchRespPromise;
      await page.waitForTimeout(150);
      searchTopText = (await page.locator('#plays-top').textContent()) || '';

      const clearRespPromise = page
        .waitForResponse(
          (r) => r.url().includes('/api/plays') && !r.url().includes('q=') && r.status() === 200,
          { timeout: 2500 }
        )
        .catch(() => null);
      await searchInput.fill('');
      await clearRespPromise;
      await page.waitForTimeout(150);
    }

    const refreshBtn = page.locator('#plays-refresh').first();
    let refreshOk = false;
    if ((await refreshBtn.count()) > 0 && (await refreshBtn.isVisible())) {
      const [apiResp] = await Promise.all([
        page.waitForResponse((r) => r.url().includes('/api/plays') && r.status() === 200),
        refreshBtn.click(),
      ]);
      refreshOk = apiResp.ok();
    }

    return {
      ok: true,
      errors,
      scrollPre,
      images,
      counts,
      readoutBefore: readoutBefore.trim(),
      readoutAfterFocus: readoutAfterFocus.trim(),
      readoutAfterClick: readoutAfterClick.trim(),
      readoutAfterArrow: readoutAfterArrow.trim(),
      pressedOn,
      pressedOff,
      searchTested,
      searchTopText,
      refreshOk,
    };
  });
}

async function handleCheckNoplaysUi(params) {
  return withContext(params, async (page) => {
    const errors = attachRuntimeGuards(page, params.baseUrl);
    const routes = params.routes || ['/', '/status', '/about', '/support'];
    const routesChecked = [];
    for (const p of routes) {
      await page.goto(`${params.baseUrl}${p}`, { waitUntil: 'domcontentloaded' });
      await waitForPageHydration(page, p);
      const info = await page.evaluate(() => {
        const navPlaysHref = document.querySelectorAll('nav a[href^="/plays"]').length;
        const footerPlaysHref = document.querySelectorAll('footer a[href^="/plays"]').length;
        const navPlaysText = [...document.querySelectorAll('nav a, footer a')].filter(
          (a) => (a.textContent || '').trim().toLowerCase() === 'plays'
        ).length;
        return { navPlaysHref, footerPlaysHref, navPlaysText };
      });
      routesChecked.push({ path: p, ...info });
    }
    const playsHtmlRes = await page.request.get(`${params.baseUrl}/plays`);
    const playsApiRes = await page.request.get(`${params.baseUrl}/api/plays`);
    return {
      ok: true,
      errors,
      routesChecked,
      playsHtmlStatus: playsHtmlRes.status(),
      playsApiStatus: playsApiRes.status(),
    };
  });
}

async function handleRunJourney(params) {
  return withContext(params, async (page) => {
    const errors = attachRuntimeGuards(page, params.baseUrl, {
      allowStatuses: params.allowStatuses || [],
    });
    const scenario = params.scenario;

    if (scenario === 'E1_desktop_listener_setup') {
      await page.goto(`${params.baseUrl}/`, { waitUntil: 'domcontentloaded' });
      await waitForPageHydration(page, '/');
      await page.evaluate(() => {
        window.__spaAlive = true;
      });
      const spatial = await auditSpatial3D(page, { checkDrag: true, reducedMotion: 'no-preference' });

      const themeBefore = await page.evaluate(() => document.documentElement.getAttribute('data-theme'));

      const copyBtn = page.locator('#copy-btn').first();
      await copyBtn.click();
      await page.waitForTimeout(120);
      const clipboard = await page.evaluate(() => navigator.clipboard.readText().catch(() => ''));

      await page.locator('#open-qr-btn').first().click();
      await page.waitForTimeout(150);
      const modal = page.locator('#qr-modal').first();
      const qrGraphic = await checkQrContainer(modal);
      await page.keyboard.press('Escape');
      await page.waitForTimeout(150);

      await clickNavDestination(
        page,
        'nav a[href^="/support"], nav a[href^="/about"]',
        /about|support/i
      );
      await page.waitForTimeout(350);
      await waitForPageHydration(page, '/support');
      const spaAliveOnSupport = await page.evaluate(() => window.__spaAlive === true);

      const supportScroll = await auditScrollVisibilityAndOverflow(page);
      const visibleDetails = page.locator('details:visible');
      let faqOpened = true;
      if ((await visibleDetails.count()) > 0) {
        const firstDetails = visibleDetails.first();
        const firstSummary = firstDetails.locator('summary').first();
        await firstSummary.scrollIntoViewIfNeeded();
        await firstSummary.click();
        faqOpened = await firstDetails.evaluate((d) => d.open);
      }

      await page.goBack();
      await page.waitForTimeout(350);
      const backState = await page.evaluate(() => ({
        pathname: window.location.pathname,
        theme: document.documentElement.getAttribute('data-theme'),
        spaAlive: window.__spaAlive === true,
      }));
      const statusScroll = await auditScrollVisibilityAndOverflow(page);

      return {
        ok: true,
        errors,
        spatial,
        themeBefore,
        themeAfter: backState.theme,
        clipboard,
        qrGraphic,
        spaAliveOnSupport,
        faqOpened,
        backState,
        supportScroll,
        statusScroll,
      };
    }

    if (scenario === 'E2_live_audio_ledger') {
      await page.goto(`${params.baseUrl}/plays`, { waitUntil: 'domcontentloaded' });
      await waitForPageHydration(page, '/plays');
      const topCount = await page.locator('#plays-top > *').count();
      const recentCount = await page.locator('#plays-recent > *').count();

      const col = page.locator('#plays-chart button, #plays-chart [tabindex]').first();
      const readout0 = ((await page.locator('#plays-readout').textContent()) || '').trim();
      await col.focus();
      await page.waitForTimeout(80);
      await page.keyboard.press('ArrowRight');
      await page.waitForTimeout(100);
      const readoutArrow = ((await page.locator('#plays-readout').textContent()) || '').trim();
      await col.click();
      await page.waitForTimeout(80);
      const readout1 = ((await page.locator('#plays-readout').textContent()) || '').trim();
      await page.keyboard.press('Escape');

      const legendBtn = page.locator('#plays-legend button').first();
      await legendBtn.click();
      const tierPressed = await legendBtn.getAttribute('aria-pressed');
      await legendBtn.click();

      const refreshBtn = page.locator('#plays-refresh').first();
      let refreshOk = false;
      if ((await refreshBtn.count()) > 0) {
        const [resp] = await Promise.all([
          page.waitForResponse((r) => r.url().includes('/api/plays') && r.status() === 200),
          refreshBtn.click(),
        ]);
        refreshOk = resp.ok();
      }
      const scroll = await auditScrollVisibilityAndOverflow(page);
      const images = mergeBrokenImages(await checkVisibleImages(page), scroll.brokenImages);

      return {
        ok: true,
        errors,
        images,
        topCount,
        recentCount,
        readout0,
        readoutArrow,
        readout1,
        tierPressed,
        refreshOk,
        scroll,
      };
    }

    if (scenario === 'E3_mobile_commuter_support') {
      await page.goto(`${params.baseUrl}/support`, { waitUntil: 'domcontentloaded' });
      await waitForPageHydration(page, '/support');
      await page.evaluate(() => {
        window.__spaAlive = true;
      });
      const supportScroll = await auditScrollVisibilityAndOverflow(page);
      const assetCount = await page.locator('#support-assets > *').count();
      const qrBtn = page.locator('#support-assets').getByRole('button', { name: /show qr|qr/i }).first();
      await qrBtn.click();
      await page.waitForTimeout(150);
      const controlsId = await qrBtn.getAttribute('aria-controls');
      const qrGraphic = await checkQrContainer(page.locator(`#${controlsId}`));
      await page.locator('#support-assets').getByRole('button', { name: /copy/i }).first().click();

      await clickNavDestination(
        page,
        'nav a[href^="/status"], nav a[href="/"]',
        /status|gateway/i
      );
      await page.waitForTimeout(350);
      await waitForPageHydration(page, '/status');
      const statusScroll = await auditScrollVisibilityAndOverflow(page);

      await clickNavDestination(page, 'nav a[href^="/plays"]', /plays/i);
      await page.waitForTimeout(350);
      await waitForPageHydration(page, '/plays');
      const playsScroll = await auditScrollVisibilityAndOverflow(page);

      await page.goBack();
      await page.waitForTimeout(300);
      const backState = await page.evaluate(() => ({
        pathname: window.location.pathname,
        spaAlive: window.__spaAlive === true,
      }));

      return {
        ok: true,
        errors,
        assetCount,
        qrGraphic,
        supportScroll,
        statusScroll,
        playsScroll,
        backState,
      };
    }

    if (scenario === 'E4_reduced_motion_accessibility') {
      await page.goto(`${params.baseUrl}/`, { waitUntil: 'domcontentloaded' });
      await waitForPageHydration(page, '/');
      await page.evaluate(() => {
        window.__spaAlive = true;
      });
      const spatial = await auditSpatial3D(page, { checkDrag: false, reducedMotion: 'reduce' });

      await page.locator('#open-qr-btn').first().click();
      await page.waitForTimeout(150);
      const qrGraphic = await checkQrContainer(page.locator('#qr-modal').first());
      await page.keyboard.press('Escape');
      await page.waitForTimeout(100);

      const statusScroll = await auditScrollVisibilityAndOverflow(page);
      await clickNavDestination(page, 'nav a[href^="/plays"]', /plays/i);
      await page.waitForTimeout(350);
      await waitForPageHydration(page, '/plays');
      const playsScroll = await auditScrollVisibilityAndOverflow(page);

      await clickNavDestination(
        page,
        'nav a[href^="/support"], nav a[href^="/about"]',
        /about|support/i
      );
      await page.waitForTimeout(350);
      await waitForPageHydration(page, '/support');
      const supportScroll = await auditScrollVisibilityAndOverflow(page);

      await page.goBack();
      await page.waitForTimeout(300);
      const backState = await page.evaluate(() => ({
        pathname: window.location.pathname,
        spaAlive: window.__spaAlive === true,
      }));
      const spaAliveAfterBack = backState.spaAlive;

      return {
        ok: true,
        errors,
        spatial,
        qrGraphic,
        statusScroll,
        playsScroll,
        supportScroll,
        backState,
        spaAliveAfterBack,
      };
    }

    if (scenario === 'E5_hardened_private_deployment') {
      await page.goto(`${params.baseUrl}/status`, { waitUntil: 'domcontentloaded' });
      await waitForPageHydration(page, '/status');
      const statusScroll = await auditScrollVisibilityAndOverflow(page);
      const addonUrlText = ((await page.locator('#addon-url').textContent()) || '').trim();
      await page.locator('#open-qr-btn').first().click();
      await page.waitForTimeout(150);
      const modalText = await page.locator('#qr-modal').innerText();
      await page.keyboard.press('Escape');

      await page.goto(`${params.baseUrl}/support`, { waitUntil: 'domcontentloaded' });
      await waitForPageHydration(page, '/support');
      const supportScroll = await auditScrollVisibilityAndOverflow(page);

      return {
        ok: true,
        errors,
        addonUrlText,
        modalText,
        statusScroll,
        supportScroll,
      };
    }

    throw new Error(`Unknown journey scenario: ${scenario}`);
  });
}

async function handleRunTier5Probe(params) {
  const probe = params.probe;
  const origin = new URL(params.baseUrl).origin;

  if (probe === 'plays_debounce_clear_and_stale_abort') {
    return withContext(params, async (page) => {
      const errors = attachRuntimeGuards(page, origin);

      // (a) Abort initial /api/plays load before loadedOnce is true, switch to /status, then return to /plays
      let initialPlaysReqCount = 0;
      let seenInitialDelayed = false;
      const overviewMatcher = (u) => u.pathname === '/api/overview';
      const initialPlaysMatcher = (u) => u.pathname === '/api/plays' && !u.search;
      await page.route(overviewMatcher, async (route) => {
        const resp = await route.fetch();
        const body = await resp.json();
        body.plays = null;
        await route.fulfill({
          status: resp.status(),
          contentType: 'application/json',
          body: JSON.stringify(body),
        });
      });
      await page.route(initialPlaysMatcher, async (route) => {
        initialPlaysReqCount += 1;
        if (initialPlaysReqCount === 1) {
          seenInitialDelayed = true;
          await new Promise((r) => setTimeout(r, 450));
        }
        await route.continue().catch(() => {});
      });

      const firstPlaysReq = page.waitForRequest((req) => {
        const u = new URL(req.url());
        return u.pathname === '/api/plays' && !u.search;
      });
      await page.goto(`${params.baseUrl}/plays`, { waitUntil: 'domcontentloaded' });
      await firstPlaysReq;
      await page.waitForTimeout(80);
      await clickNavDestination(page, 'nav a[data-nav-view="status"]', /status/i);
      await page.waitForTimeout(550);
      await clickNavDestination(page, 'nav a[data-nav-view="plays"]', /plays/i);
      await waitForPageHydration(page, '/plays');

      const reentryInitialState = await page.evaluate(() => {
        const st = document.getElementById('plays-status');
        return {
          currentView: window.ConfihRouter ? window.ConfihRouter.currentView() : null,
          statusText: st ? st.textContent : '',
          statusHidden: st ? st.hidden : null,
          topCount: document.querySelectorAll('#plays-top .track-row').length,
        };
      });

      await page.unroute(overviewMatcher);
      await page.unroute(initialPlaysMatcher);

      // 1. Debounce vs clear button race
      await page.locator('#plays-search').fill('Instant');
      await page.waitForTimeout(45);
      await page.locator('#plays-search-clear').click();
      await page.waitForTimeout(420);

      const clearState = await page.evaluate(() => ({
        inputValue: document.getElementById('plays-search')?.value ?? null,
        clearHidden: document.getElementById('plays-search-clear')?.hidden ?? null,
        filterNoteHidden: document.getElementById('plays-filter-note')?.hidden ?? null,
        topCount: document.querySelectorAll('#plays-top .track-row').length,
      }));

      // 2. Stale delayed /api/plays?q=Billie aborted/ignored when navigating to Status,
      //    then re-fetched cleanly when returning to /plays
      let seenDelayedQuery = false;
      let billieReqCount = 0;
      const queryMatcher = (u) => u.pathname === '/api/plays' && u.search.includes('q=Billie');
      await page.route(queryMatcher, async (route) => {
        billieReqCount += 1;
        seenDelayedQuery = true;
        if (billieReqCount === 1) {
          await new Promise((r) => setTimeout(r, 450));
        }
        await route.continue().catch(() => {});
      });

      await page.locator('#plays-search').fill('Billie');
      await page.waitForTimeout(310);
      await clickNavDestination(page, 'nav a[data-nav-view="status"]', /status/i);
      await page.waitForTimeout(600);

      const afterAbortState = await page.evaluate(() => {
        const note = document.getElementById('plays-filter-note');
        return {
          currentView: window.ConfihRouter ? window.ConfihRouter.currentView() : null,
          filterNoteHidden: note ? note.hidden : null,
          filterNoteText: note ? note.textContent : '',
          topCount: document.querySelectorAll('#plays-top .track-row').length,
          spatialHasPlays: window.ConfihSpatial ? window.ConfihSpatial.getState().hasPlays : false,
        };
      });

      const [reentryResp] = await Promise.all([
        page.waitForResponse(
          (r) => r.url().includes('/api/plays') && r.url().includes('q=Billie') && r.status() === 200
        ),
        clickNavDestination(page, 'nav a[data-nav-view="plays"]', /plays/i),
      ]);
      await page.waitForTimeout(150);

      const reentrySearchState = await page.evaluate(() => {
        const note = document.getElementById('plays-filter-note');
        return {
          currentView: window.ConfihRouter ? window.ConfihRouter.currentView() : null,
          inputValue: document.getElementById('plays-search')?.value ?? null,
          clearHidden: document.getElementById('plays-search-clear')?.hidden ?? null,
          filterNoteHidden: note ? note.hidden : null,
          filterNoteText: note ? note.textContent : '',
          topCount: document.querySelectorAll('#plays-top .track-row').length,
          firstTitle: document.querySelector('#plays-top .track-title')?.textContent?.trim() ?? '',
        };
      });

      await page.unroute(queryMatcher);

      return {
        ok: true,
        errors,
        seenInitialDelayed,
        initialPlaysReqCount,
        reentryInitialState,
        clearState,
        seenDelayedQuery,
        billieReqCount,
        afterAbortState,
        reentrySearchStatus: reentryResp.status(),
        reentrySearchState,
      };
    });
  }

  if (probe === 'admin_vault_mobile_populated_and_auth') {
    return withContext({ ...params, hasTouch: true, isMobile: true }, async (page) => {
      const errors = attachRuntimeGuards(page, origin, { allowStatuses: [401] });
      await page.goto(`${params.baseUrl}/admin`, { waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(200);

      // 1. Invalid token -> 401 restores aria-invalid="true"
      await page.locator('#vault-key').fill('wrong_token');
      await page.locator('#unlock-btn').click();
      await page.waitForFunction(() => {
        const err = document.getElementById('login-err');
        return err && err.textContent.includes('not accepted');
      }, { timeout: 5000 });

      const authErrState = await page.evaluate(() => ({
        ariaInvalid: document.getElementById('vault-key')?.getAttribute('aria-invalid') ?? null,
        errText: document.getElementById('login-err')?.textContent ?? '',
      }));

      // 2. Mock populated summary with long unspaced error string
      await page.route('**/api/summary', async (route) => {
        const key = route.request().headers()['x-vault-key'];
        if (key === params.statsToken) {
          await route.fulfill({
            status: 200,
            contentType: 'application/json',
            body: JSON.stringify({
              primary_provider: 'tidal',
              auto_failover: true,
              users: { active_now: 2, total_all_time: 14 },
              qobuz_accounts: [
                {
                  id: 'qobuz-long-err-1',
                  label: 'QobuzStudioPoolAccountWithLongIdentifier',
                  enabled: true,
                  configured: true,
                  token_ok: false,
                  geo_ok: true,
                  subscription: 'Studio',
                  error: 'HTTP_401_UNAUTHORIZED_INVALID_USER_AUTH_TOKEN_LONGER_STRING_TEST_NO_SPACES_WRAP',
                },
              ],
              tidal_accounts: [
                {
                  id: 'tidal-1',
                  label: 'Tidal MAX Main',
                  enabled: true,
                  configured: true,
                  token_ok: true,
                  subscription: 'MAX',
                  requests_served: 42,
                  error: null,
                },
              ],
              device_auth: { status: 'idle', last_account: null, pending_code: null, pending_url: null },
            }),
          });
        } else {
          await route.continue();
        }
      });

      await page.route('**/api/routing', async (route) => {
        await route.fulfill({
          status: 200,
          contentType: 'application/json',
          body: JSON.stringify({ ok: true, primary_provider: 'qobuz', auto_failover: true }),
        });
      });

      await page.locator('#vault-key').fill(params.statsToken);
      await page.locator('#unlock-btn').click();
      await page.waitForFunction(() => {
        const con = document.getElementById('admin-console');
        return con && !con.classList.contains('hidden');
      }, { timeout: 5000 });
      await page.waitForTimeout(200);

      const layoutMetrics = await page.evaluate(() => {
        const btns = [...document.querySelectorAll('.acc-actions .button, .segmented button')].map((b) => {
          const r = b.getBoundingClientRect();
          return { text: b.textContent.trim(), width: r.width, height: r.height };
        });
        return {
          docScrollWidth: document.documentElement.scrollWidth,
          innerWidth: window.innerWidth,
          qobuzCards: document.querySelectorAll('#qobuz-list .acc-item').length,
          tidalPressed: document.getElementById('btn-pri-tidal')?.getAttribute('aria-pressed'),
          btns,
        };
      });

      await page.locator('#btn-lock').click();
      await page.waitForTimeout(100);
      const lockState = await page.evaluate(() => ({
        loginVisible: !document.getElementById('login-box')?.classList.contains('hidden'),
        consoleHidden: document.getElementById('admin-console')?.classList.contains('hidden'),
        activeId: document.activeElement ? document.activeElement.id : '',
      }));

      return {
        ok: true,
        errors,
        authErrState,
        layoutMetrics,
        lockState,
      };
    });
  }

  if (probe === 'forced_colors_accessibility') {
    return withContext({ ...params, forcedColors: 'active' }, async (page) => {
      const errors = attachRuntimeGuards(page, origin);
      await page.goto(`${params.baseUrl}/`, { waitUntil: 'domcontentloaded' });
      await waitForPageHydration(page, '/');

      await page.locator('.spatial-node-pill[data-spatial-node="confih"]').click();
      await page.waitForTimeout(100);

      const statusMetrics = await page.evaluate(() => {
        const logo = document.querySelector('.brand-logo');
        const pillOn = document.querySelector('.spatial-node-pill[aria-pressed="true"]');
        const pillOff = document.querySelector('.spatial-node-pill[aria-pressed="false"]');
        const tBar = document.querySelector('.t-bar');
        const dBar = document.querySelector('.d-bar');
        const logoRect = logo ? logo.getBoundingClientRect() : null;
        return {
          logoAdjust: logo ? getComputedStyle(logo).forcedColorAdjust : null,
          logoLoaded: Boolean(logo && logo.complete && logo.naturalWidth > 0 && logoRect && logoRect.width > 40),
          pillOnBg: pillOn ? getComputedStyle(pillOn).backgroundColor : null,
          pillOffBg: pillOff ? getComputedStyle(pillOff).backgroundColor : null,
          pillOnAdjust: pillOn ? getComputedStyle(pillOn).forcedColorAdjust : null,
          tBarAdjust: tBar ? getComputedStyle(tBar).forcedColorAdjust : null,
          tBarBorder: tBar ? getComputedStyle(tBar).borderTopWidth : null,
          dBarAdjust: dBar ? getComputedStyle(dBar).forcedColorAdjust : null,
          dBarBorder: dBar ? getComputedStyle(dBar).borderTopWidth : null,
        };
      });

      await page.goto(`${params.baseUrl}/plays`, { waitUntil: 'domcontentloaded' });
      await waitForPageHydration(page, '/plays');
      await page.locator('#plays-search').focus();
      await page.keyboard.press('Shift+Tab');
      await page.keyboard.press('Tab');

      const playsMetrics = await page.evaluate(() => {
        const inp = document.getElementById('plays-search');
        const cs = inp ? getComputedStyle(inp) : null;
        return {
          activeId: document.activeElement ? document.activeElement.id : '',
          outlineStyle: cs ? cs.outlineStyle : null,
          outlineWidth: cs ? cs.outlineWidth : null,
        };
      });

      return {
        ok: true,
        errors,
        statusMetrics,
        playsMetrics,
      };
    });
  }

  if (probe === 'webgl_context_loss_and_fallbacks') {
    return withContext(params, async (page) => {
      const errors = attachRuntimeGuards(page, origin);
      await page.goto(`${params.baseUrl}/`, { waitUntil: 'domcontentloaded' });
      await waitForPageHydration(page, '/');

      const restingCamera = await page.evaluate(() => window.ConfihSpatial.getState().camera);

      // 1. Verify CSS preserve-3d spatial architecture & zero WebGL canvas overlay
      const stageBox = await page.locator('.hero-stage-slot[data-spatial-slot="status"]').boundingBox();
      if (stageBox) {
        await page.mouse.move(stageBox.x + stageBox.width * 0.35, stageBox.y + stageBox.height * 0.35);
        await page.waitForTimeout(80);
        await page.mouse.move(stageBox.x + stageBox.width * 0.72, stageBox.y + stageBox.height * 0.65);
        await page.waitForTimeout(100);
      }

      const css3dStatus = await page.evaluate(() => {
        const rack = document.getElementById('status-rack-3d');
        const canvas = document.getElementById('spatial-canvas');
        const stage = document.getElementById('spatial-stage');
        const st = window.ConfihSpatial.getState();
        return {
          backend: st.backend,
          framesRendered: st.framesRendered,
          canvasHidden: canvas ? (canvas.hidden || getComputedStyle(canvas).display === 'none') : true,
          stageHidden: stage ? (stage.hidden || getComputedStyle(stage).display === 'none') : true,
          rackTransformStyle: rack ? getComputedStyle(rack).transformStyle : null,
          rackPlanes: document.querySelectorAll('#status-rack-3d .rack-plane').length,
          restingCamera: null,
          camera: st.camera,
        };
      });
      css3dStatus.restingCamera = restingCamera;

      // 2. Node selection updates active state & readout cleanly
      await page.locator('.spatial-node-pill[data-spatial-node="qobuz"]').click();
      await page.waitForTimeout(120);
      const selectedState = await page.evaluate(() => ({
        selectedNode: window.ConfihSpatial.getState().selectedNode,
        activeNodeAttr: document.getElementById('status-rack-3d')?.getAttribute('data-active-node') ?? null,
        qobuzActive: document.querySelector('[data-rack-node="qobuz"]')?.classList.contains('is-active') ?? false,
      }));

      // 3. Live reduced-motion toggle stops rAF immediately and resumes on pointer move when restored
      await page.emulateMedia({ reducedMotion: 'reduce' });
      await page.waitForTimeout(100);
      const redA = await page.evaluate(() => window.ConfihSpatial.getState().framesRendered);
      if (stageBox) {
        await page.mouse.move(stageBox.x + stageBox.width * 0.5, stageBox.y + stageBox.height * 0.5);
      }
      await page.waitForTimeout(140);
      const redB = await page.evaluate(() => ({
        frames: window.ConfihSpatial.getState().framesRendered,
        running: window.ConfihSpatial.getState().running,
        reducedMotion: window.ConfihSpatial.getState().reducedMotion,
      }));

      await page.emulateMedia({ reducedMotion: 'no-preference' });
      await page.waitForTimeout(80);
      if (stageBox) {
        await page.mouse.move(stageBox.x + stageBox.width * 0.25, stageBox.y + stageBox.height * 0.25);
        await page.waitForTimeout(120);
      }
      const resC = await page.evaluate(() => ({
        frames: window.ConfihSpatial.getState().framesRendered,
        reducedMotion: window.ConfihSpatial.getState().reducedMotion,
      }));

      // 3b. Dynamic functional flow-mode switching (outage -> dual -> failover -> degraded -> single)
      await page.locator('.spatial-node-pill[data-spatial-node="qobuz"]').click();
      await page.waitForTimeout(80);
      const flowModes = await page.evaluate(() => {
        const rack = document.getElementById('status-rack-3d');
        const readout = document.getElementById('spatial-inspect-readout');
        const apply = (payload) => {
          window.ConfihSpatial.updateStats(payload);
          const st = window.ConfihSpatial.getState();
          return {
            mode: st.flowMode,
            target: st.flowTarget,
            attr: rack ? rack.getAttribute('data-flow-mode') : null,
            readout: readout ? readout.textContent : '',
          };
        };
        const out = apply({
          status: 'outage',
          primary_provider: 'qobuz',
          failover_active: false,
          services: {
            qobuz: { configured: true, online: false, status: 'offline' },
            tidal: { configured: true, online: false, status: 'offline' },
          },
        });
        const dual = apply({
          status: 'operational',
          primary_provider: 'qobuz',
          failover_active: false,
          services: {
            qobuz: { configured: true, online: true, status: 'online', health_latency_ms: 40 },
            tidal: { configured: true, online: true, status: 'online', health_latency_ms: 48 },
          },
        });
        const failover = apply({
          status: 'degraded',
          primary_provider: 'tidal',
          primary_online: false,
          failover_active: true,
          services: {
            qobuz: { configured: true, online: true, status: 'online', health_latency_ms: 44 },
            tidal: { configured: true, online: false, status: 'offline' },
          },
        });
        const degraded = apply({
          status: 'degraded',
          primary_provider: 'qobuz',
          failover_active: false,
          services: {
            qobuz: { configured: true, online: true, status: 'degraded', health_latency_ms: 320 },
            tidal: { configured: false, online: false, status: 'unconfigured' },
          },
        });
        const single = apply({
          status: 'operational',
          primary_provider: 'qobuz',
          failover_active: false,
          services: {
            qobuz: { configured: true, online: true, status: 'online', health_latency_ms: 38 },
            tidal: { configured: false, online: false, status: 'unconfigured' },
          },
        });
        return { out, dual, failover, degraded, single };
      });

      // 4. Verify 3D vinyl sleeve on /plays and 3D protocol stack on /about, plus canvas-disabled resilience
      await clickNavDestination(page, 'nav a[data-nav-view="plays"]', /plays/i);
      await waitForPageHydration(page, '/plays');
      const plays3d = await page.evaluate(() => {
        const unit = document.querySelector('.sleeve-3d-unit');
        const disc = document.querySelector('.sleeve-3d-disc');
        return {
          hasUnit: Boolean(unit),
          hasDisc: Boolean(disc),
          transformStyle: unit ? getComputedStyle(unit).transformStyle : null,
        };
      });

      await clickNavDestination(page, 'nav a[data-nav-view="about"]', /about/i);
      await waitForPageHydration(page, '/about');
      const about3d = await page.evaluate(() => {
        const stack = document.querySelector('.arch-3d-stack');
        return {
          hasStack: Boolean(stack),
          plates: document.querySelectorAll('.arch-3d-stack .arch-plate').length,
          transformStyle: stack ? getComputedStyle(stack).transformStyle : null,
        };
      });

      const pageNoCanvas = await page.context().newPage();
      const errorsNoCanvas = attachRuntimeGuards(pageNoCanvas, origin);
      await pageNoCanvas.addInitScript(() => {
        HTMLCanvasElement.prototype.getContext = function () {
          return null;
        };
      });
      await pageNoCanvas.goto(`${params.baseUrl}/`, { waitUntil: 'domcontentloaded' });
      await waitForPageHydration(pageNoCanvas, '/');
      const noCanvasBackend = await pageNoCanvas.evaluate(() => window.ConfihSpatial.getState().backend);
      await pageNoCanvas.close();

      return {
        ok: true,
        errors,
        errorsNoCanvas,
        css3dStatus,
        selectedState,
        motionToggle: { redA, redB, resC },
        flowModes,
        plays3d,
        about3d,
        noCanvasBackend,
      };
    });
  }

  if (probe === 'mobile_touch_anchors_30d_and_polling') {
    return withContext({ ...params, viewport: { width: 390, height: 844 }, hasTouch: true, isMobile: true }, async (page) => {
      const errors = attachRuntimeGuards(page, origin);
      await page.goto(`${params.baseUrl}/`, { waitUntil: 'domcontentloaded' });
      await waitForPageHydration(page, '/');

      const quietBox = await page.locator('.quiet-link').first().boundingBox();

      // 30d keyboard ArrowRight & focusout readout reset
      const first30d = page.locator('#timeline-30d-bars .d-bar').first();
      await first30d.focus();
      const readoutOnFirst = await page.locator('#timeline-30d-readout').textContent();
      await page.keyboard.press('ArrowRight');
      const afterArrow = await page.evaluate(() => ({
        activeIsSecond: document.activeElement === document.querySelectorAll('#timeline-30d-bars .d-bar')[1],
        readout: document.getElementById('timeline-30d-readout')?.textContent ?? '',
      }));
      await page.locator('#copy-btn').focus();
      await page.waitForTimeout(80);
      const readoutAfterFocusout = await page.locator('#timeline-30d-readout').textContent();

      // Background polling pauses on /about and resumes on /
      let statsRequestsOnAbout = 0;
      let statsRequestsOnReturn = 0;
      let phase = 'idle';
      page.on('request', (req) => {
        if (req.url().includes('/api/public-stats')) {
          if (phase === 'about') statsRequestsOnAbout++;
          if (phase === 'return') statsRequestsOnReturn++;
        }
      });

      await clickNavDestination(page, 'nav a[data-nav-view="about"]', /about/i);
      await page.waitForTimeout(450);
      phase = 'about';
      await page.waitForTimeout(5600);
      phase = 'return';
      await clickNavDestination(page, 'nav a[data-nav-view="status"]', /status/i);
      await page.waitForTimeout(500);
      phase = 'done';

      // Direct /support#donations anchor + focus
      await page.goto(`${params.baseUrl}/support#donations`, { waitUntil: 'domcontentloaded' });
      await waitForPageHydration(page, '/support');
      await page.waitForTimeout(150);
      const donationsState = await page.evaluate(() => {
        const bay = document.getElementById('donations-bay');
        const rect = bay ? bay.getBoundingClientRect() : null;
        return {
          activeId: document.activeElement ? document.activeElement.id : '',
          bayTop: rect ? rect.top : null,
          scrollY: window.scrollY,
        };
      });

      return {
        ok: true,
        errors,
        quietBox,
        chart30d: {
          readoutOnFirst,
          afterArrow,
          readoutAfterFocusout,
        },
        polling: {
          statsRequestsOnAbout,
          statsRequestsOnReturn,
        },
        donationsState,
      };
    });
  }

  throw new Error(`Unknown tier5 probe: ${probe}`);
}

const HANDLERS = {
  ping: async () => {
    await getBrowser();
    return { ok: true };
  },
  audit_route: handleAuditRoute,
  check_spa_nav: handleCheckSpaNav,
  check_theme_toggle: handleCheckThemeToggle,
  check_gateway_qr_copy: handleCheckGatewayQrCopy,
  check_support_controls: handleCheckSupportControls,
  check_plays_controls: handleCheckPlaysControls,
  check_noplays_ui: handleCheckNoplaysUi,
  run_journey: handleRunJourney,
  run_tier5_probe: handleRunTier5Probe,
};

async function main() {
  const rl = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
  for await (const line of rl) {
    if (!line.trim()) continue;
    let req;
    try {
      req = JSON.parse(line);
    } catch (err) {
      process.stdout.write(`${JSON.stringify({ id: null, ok: false, error: `Invalid JSON: ${err}` })}\n`);
      continue;
    }

    if (req.cmd === 'shutdown') {
      await resetBrowser();
      process.stdout.write(`${JSON.stringify({ id: req.id, ok: true })}\n`);
      process.exit(0);
    }

    const handler = HANDLERS[req.cmd];
    if (!handler) {
      process.stdout.write(`${JSON.stringify({ id: req.id, ok: false, error: `Unknown cmd: ${req.cmd}` })}\n`);
      continue;
    }

    try {
      const result = await handler(req);
      process.stdout.write(`${JSON.stringify({ id: req.id, ...result })}\n`);
    } catch (err) {
      process.stdout.write(
        `${JSON.stringify({
          id: req.id,
          ok: false,
          error: err && err.stack ? err.stack : String(err),
        })}\n`
      );
    }
  }
  await resetBrowser();
}

main().catch((err) => {
  process.stderr.write(`${err && err.stack ? err.stack : String(err)}\n`);
  process.exit(1);
});
