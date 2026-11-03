// BugLens reproduction driver (TRUSTED VERIFIER, BugLens-side).
//
// Runs inside the Modal sandbox against the fixture checked out at the exact
// pinned commit. Reads its contract from $BUGLENS_DIR/contract.json
// (default /buglens), collects evidence for the broken and control routes,
// and writes result.json plus screenshots/traces to the contract outDir.
//
// The driver records facts only; verdict classification happens server-side.
// It navigates exclusively to baseUrl + the two contracted routes.
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

const BUGSLENS_DIR = process.env.BUGLENS_DIR || '/buglens';

function describeElement(handle) {
  if (!handle) return null;
  return {
    tag: handle.tag.toLowerCase(),
    id: handle.id || null,
    testid: handle.testid,
    buglens: handle.buglens,
    rect: handle.rect,
    style: handle.style,
  };
}

async function snapshotElement(page, selector) {
  return page.evaluate((sel) => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return {
      tag: el.tagName,
      id: el.id || null,
      testid: el.getAttribute('data-testid'),
      buglens: el.getAttribute('data-buglens'),
      rect: { x: rect.x, y: rect.y, width: rect.width, height: rect.height },
      style: {
        position: style.position,
        zIndex: style.zIndex,
        opacity: style.opacity,
        pointerEvents: style.pointerEvents,
        display: style.display,
        visibility: style.visibility,
      },
    };
  }, selector);
}

async function hitTest(page, selector) {
  return page.evaluate((sel) => {
    const button = document.querySelector(sel);
    if (!button) return { present: false, points: [] };
    if (button.scrollIntoViewIfNeeded) button.scrollIntoViewIfNeeded();
    const rect = button.getBoundingClientRect();
    const points = [];
    for (const fx of [0.25, 0.5, 0.75]) {
      for (const fy of [0.25, 0.5, 0.75]) {
        const x = Math.floor(rect.x + rect.width * fx);
        const y = Math.floor(rect.y + rect.height * fy);
        const top = document.elementFromPoint(x, y);
        points.push({
          x,
          y,
          topTag: top ? top.tagName.toLowerCase() : null,
          topId: top && top.id ? top.id : null,
          topTestid: top ? top.getAttribute('data-testid') : null,
          hitsButton: top === button || (top !== null && button.contains(top)),
        });
      }
    }
    return { present: true, points };
  }, selector);
}

async function topElementAt(page, selector) {
  return page.evaluate((sel) => {
    const button = document.querySelector(sel);
    if (!button) return null;
    const rect = button.getBoundingClientRect();
    const top = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
    if (!top) return { none: true };
    const topRect = top.getBoundingClientRect();
    const style = window.getComputedStyle(top);
    return {
      tag: top.tagName.toLowerCase(),
      id: top.id || null,
      testid: top.getAttribute('data-testid'),
      buglens: top.getAttribute('data-buglens'),
      rect: { x: topRect.x, y: topRect.y, width: topRect.width, height: topRect.height },
      style: {
        position: style.position,
        zIndex: style.zIndex,
        opacity: style.opacity,
        pointerEvents: style.pointerEvents,
        display: style.display,
        visibility: style.visibility,
      },
    };
  }, selector);
}

async function runRoute(browser, contract, label, route) {
  const url = `${contract.baseUrl}${route}`;
  if (!url.startsWith(contract.baseUrl)) {
    throw new Error(`navigation outside approved application refused: ${route}`);
  }
  const context = await browser.newContext({ viewport: contract.viewport });
  const page = await context.newPage();
  const consoleErrors = [];
  const pageErrors = [];
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text().slice(0, 500));
  });
  page.on('pageerror', (error) => pageErrors.push(String(error && error.message ? error.message : error).slice(0, 500)));
  const result = {
    route,
    url,
    missingElements: [],
    buttonPresent: false,
    hitTest: null,
    occlusion: null,
    button: null,
    clicked: false,
    clickError: null,
    confirmationObserved: false,
    confirmationText: null,
    consoleErrors,
    pageErrors,
    artifacts: [],
  };
  try {
    await context.tracing.start({ screenshots: true, snapshots: true });
    await page.goto(url, { timeout: 15000 });
    for (const selector of contract.requiredElements) {
      const count = await page.locator(selector).count();
      if (count === 0) result.missingElements.push(selector);
    }
    result.buttonPresent = (await page.locator(contract.clickSelector).count()) > 0;
    if (result.buttonPresent) {
      result.hitTest = await hitTest(page, contract.clickSelector);
      result.occlusion = await topElementAt(page, contract.clickSelector);
      result.button = describeElement(await snapshotElement(page, contract.clickSelector));
    }
    const beforeName = `${label}-before.png`;
    await page.screenshot({ path: path.join(contract.outDir, beforeName) });
    result.artifacts.push(beforeName);
    if (result.buttonPresent) {
      try {
        await page.locator(contract.clickSelector).first().click({ timeout: contract.clickTimeoutMs });
        result.clicked = true;
      } catch (error) {
        result.clicked = false;
        result.clickError = String(error && error.message ? error.message : error).slice(0, 800);
      }
    }
    const expected = contract.expectedConfirmation;
    try {
      await page.locator(expected.selector).first().waitFor({ state: 'visible', timeout: 5000 });
      const text = (await page.locator(expected.selector).first().textContent()) || '';
      result.confirmationText = text;
      result.confirmationObserved = text.includes(expected.textContains);
    } catch {
      result.confirmationObserved = false;
    }
    const afterName = `${label}-after.png`;
    await page.screenshot({ path: path.join(contract.outDir, afterName) });
    result.artifacts.push(afterName);
  } finally {
    const traceName = `${label}-trace.zip`;
    try {
      await context.tracing.stop({ path: path.join(contract.outDir, traceName) });
      result.artifacts.push(traceName);
    } catch (error) {
      result.traceError = String(error && error.message ? error.message : error).slice(0, 300);
    }
    await context.close();
  }
  return result;
}

async function main() {
  const contract = JSON.parse(fs.readFileSync(path.join(BUGSLENS_DIR, 'contract.json'), 'utf-8'));
  fs.mkdirSync(contract.outDir, { recursive: true });
  const report = { routes: {}, driverErrors: [] };
  const browser = await chromium.launch({ headless: true });
  try {
    for (const [label, route] of Object.entries(contract.routes)) {
      try {
        report.routes[label] = await runRoute(browser, contract, label, route);
      } catch (error) {
        report.routes[label] = {
          route,
          url: `${contract.baseUrl}${route}`,
          fatal: String(error && error.message ? error.message : error).slice(0, 800),
        };
        report.driverErrors.push(`${label}: ${report.routes[label].fatal}`);
      }
    }
  } finally {
    await browser.close();
  }
  fs.writeFileSync(path.join(contract.outDir, 'result.json'), `${JSON.stringify(report, null, 2)}\n`);
  console.log(`driver done: ${Object.keys(report.routes).join(', ')}`);
}

main().catch((error) => {
  console.error(`driver fatal: ${error && error.message ? error.message : error}`);
  process.exitCode = 3;
});
