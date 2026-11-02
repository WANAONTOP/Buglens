// Reproduction test for the storefront fixture (PROTECTED: patches must not
// alter this file). Reads the repo-root .buglens.json contract, starts the
// fixture server, and runs Playwright over the broken route and the control.
//
// Verdicts per route:
//   PASS         - confirmation state observed after the click.
//   FAIL_BLOCKED - the click was intercepted AND occlusion evidence proves a
//                  transparent element covers the button (the intended bug).
//   FAIL_SETUP   - anything else: server unreachable, required element
//                  missing, click failure without occlusion evidence, or a
//                  click that succeeds yet shows no confirmation.
//
// The matrix passes only when broken=FAIL_BLOCKED and control=PASS.
// Usage: node reproduce.mjs [--route=broken|control]
import { spawn } from 'node:child_process';
import fs from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium } from 'playwright';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, '..', '..');
const CLICK_TIMEOUT_MS = 5000;

function arg(name, fallback) {
  const hit = process.argv.find((entry) => entry.startsWith(`--${name}=`));
  return hit ? hit.slice(name.length + 3) : fallback;
}

async function waitForHealth(baseUrl, timeoutMs) {
  const started = Date.now();
  for (;;) {
    try {
      const response = await fetch(`${baseUrl}/healthz`);
      if (response.ok) return;
    } catch {
      // server not up yet
    }
    if (Date.now() - started > timeoutMs) {
      throw new Error(`fixture server never answered ${baseUrl}/healthz`);
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
}

async function occlusionProbe(page, selector) {
  return page.evaluate((sel) => {
    const button = document.querySelector(sel);
    if (!button) return { present: false };
    button.scrollIntoViewIfNeeded();
    const rect = button.getBoundingClientRect();
    const x = Math.floor(rect.x + rect.width / 2);
    const y = Math.floor(rect.y + rect.height / 2);
    const top = document.elementFromPoint(x, y);
    if (!top) return { present: true, x, y, top: null, occluded: true };
    return {
      present: true,
      x,
      y,
      top: {
        tag: top.tagName.toLowerCase(),
        id: top.id || null,
        testid: top.getAttribute('data-testid'),
      },
      occluded: top !== button && !button.contains(top),
    };
  }, selector);
}

async function runRoute(browser, manifest, baseUrl, route, label) {
  const viewport = manifest.viewport;
  const page = await browser.newPage({ viewport });
  const evidence = {};
  try {
    for (const step of manifest.actions) {
      if (step.action === 'goto') {
        try {
          await page.goto(`${baseUrl}${route}`, { timeout: 10000 });
        } catch (error) {
          return fail('FAIL_SETUP', `navigation to ${route} failed: ${message(error)}`, evidence);
        }
      } else if (step.action === 'assertVisible') {
        try {
          await page.locator(step.selector).first().waitFor({ state: 'visible', timeout: 5000 });
        } catch {
          return fail('FAIL_SETUP', `required element missing or hidden: ${step.selector}`, evidence);
        }
      } else if (step.action === 'click') {
        const probe = await occlusionProbe(page, step.selector);
        evidence.occlusion = probe;
        if (!probe.present) {
          return fail('FAIL_SETUP', `click target vanished: ${step.selector}`, evidence);
        }
        try {
          await page.locator(step.selector).first().click({ timeout: CLICK_TIMEOUT_MS });
          evidence.clicked = true;
        } catch (error) {
          evidence.clicked = false;
          evidence.clickError = message(error).slice(0, 300);
          if (probe.occluded) {
            const top = probe.top ? `${probe.top.tag}#${probe.top.id ?? '?'}[${probe.top.testid ?? '?'}]` : 'nothing';
            return fail('FAIL_BLOCKED', `click intercepted; top element at button center is ${top}`, evidence);
          }
          return fail('FAIL_SETUP', `click failed without occlusion evidence: ${evidence.clickError}`, evidence);
        }
      } else if (step.action === 'assertConfirmation') {
        const expected = manifest.expectedConfirmation;
        const locator = page.locator(expected.selector).first();
        try {
          await locator.waitFor({ state: 'visible', timeout: 5000 });
        } catch {
          if (evidence.clicked) {
            return fail('FAIL_SETUP', 'click succeeded but no confirmation appeared', evidence);
          }
          return fail('FAIL_SETUP', 'confirmation missing (click did not succeed)', evidence);
        }
        const text = (await locator.textContent()) ?? '';
        evidence.confirmationText = text;
        if (!text.includes(expected.textContains)) {
          return fail('FAIL_SETUP', `confirmation text mismatch: ${JSON.stringify(text)}`, evidence);
        }
      } else {
        return fail('FAIL_SETUP', `unknown manifest action: ${step.action}`, evidence);
      }
    }
    // Required-element audit: every manifest selector must exist in the DOM.
    for (const selector of manifest.requiredElements) {
      const count = await page.locator(selector).count();
      if (count === 0) {
        return fail('FAIL_SETUP', `required element absent from DOM: ${selector}`, evidence);
      }
    }
    return { route, label, verdict: 'PASS', reason: 'confirmation observed', evidence };
  } finally {
    await page.close();
  }

  function fail(verdict, reason, evidence) {
    return { route, label, verdict, reason, evidence };
  }
}

function message(error) {
  return error instanceof Error ? error.message : String(error);
}

async function main() {
  const manifest = JSON.parse(await fs.readFile(path.join(ROOT, '.buglens.json'), 'utf-8'));
  const only = arg('route', 'both');
  const routes =
    only === 'broken'
      ? [{ route: manifest.route, label: 'broken' }]
      : only === 'control'
        ? [{ route: manifest.controlRoute, label: 'control' }]
        : [
            { route: manifest.route, label: 'broken' },
            { route: manifest.controlRoute, label: 'control' },
          ];
  const baseUrl = `http://127.0.0.1:${manifest.port}`;
  const server = spawn(process.execPath, ['server.js'], {
    cwd: HERE,
    env: { ...process.env, STOREFRONT_PORT: String(manifest.port) },
    stdio: 'ignore',
    windowsHide: true,
  });
  const report = { manifest: '.buglens.json', baseUrl, results: [], matrixPass: false };
  try {
    try {
      await waitForHealth(baseUrl, 15000);
    } catch (error) {
      for (const entry of routes) {
        report.results.push({
          route: entry.route,
          label: entry.label,
          verdict: 'FAIL_SETUP',
          reason: message(error),
          evidence: {},
        });
      }
      return report;
    }
    const launchOptions = { headless: true };
    if (process.env.PLAYWRIGHT_CHANNEL) {
      launchOptions.channel = process.env.PLAYWRIGHT_CHANNEL;
    }
    const browser = await chromium.launch(launchOptions);
    try {
      for (const entry of routes) {
        report.results.push(await runRoute(browser, manifest, baseUrl, entry.route, entry.label));
      }
    } finally {
      await browser.close();
    }
    const byLabel = Object.fromEntries(report.results.map((result) => [result.label, result]));
    report.matrixPass =
      (!byLabel.broken || byLabel.broken.verdict === 'FAIL_BLOCKED') &&
      (!byLabel.control || byLabel.control.verdict === 'PASS');
    return report;
  } finally {
    server.kill();
    await new Promise((resolve) => setTimeout(resolve, 500));
    if (server.exitCode === null) {
      try {
        server.kill('SIGKILL');
      } catch {
        // already gone
      }
    }
    await fs.writeFile(path.join(HERE, 'last-run.json'), `${JSON.stringify(report, null, 2)}\n`);
  }
}

const report = await main();
console.log(JSON.stringify(report, null, 2));
process.exit(report.matrixPass ? 0 : 1);
