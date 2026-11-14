import chromium from '@sparticuz/chromium';
import puppeteer from 'puppeteer-core';
// By default this uses the Chromium bundled in @sparticuz/chromium (works in containers/CI with no
// browser installed). Set CHROME_PATH to use your own Chrome/Chromium instead.
export async function launch() {
  if (process.env.CHROME_PATH) {
    return puppeteer.launch({ executablePath: process.env.CHROME_PATH, headless: true, args: ['--no-sandbox'] });
  }
  const path = await chromium.executablePath();
  return puppeteer.launch({ args: [...chromium.args, '--no-sandbox'], executablePath: path, headless: 'shell' });
}
// Open a page that records every console message, page error and failed request.
export async function openPage(browser, base, { width = 1360, height = 900, dark = false } = {}) {
  const page = await browser.newPage();   // single-process Chromium: no extra contexts, use one browser per session
  await page.setViewport({ width, height });
  if (dark) await page.emulateMediaFeatures([{ name: 'prefers-color-scheme', value: 'dark' }]);
  page.problems = [];
  page.on('console', m => { if (['error', 'warning'].includes(m.type())) page.problems.push(`console.${m.type()}: ${m.text()}`); });
  page.on('pageerror', e => page.problems.push(`pageerror: ${e.message}`));
  page.on('requestfailed', r => page.problems.push(`requestfailed: ${r.url()} ${r.failure()?.errorText}`));
  page.on('response', r => { if (r.status() >= 400 && !r.url().includes('/api/')) page.problems.push(`http ${r.status()}: ${r.url()}`); });
  return page;
}
export async function login(page, base, email, password = 'password123') {
  const old = await page.cookies(base);
  if (old.length) await page.deleteCookie(...old);   // pages share one cookie jar here
  await page.goto(base + '/login', { waitUntil: 'networkidle0' });
  await page.type('#email', email);
  await page.type('#password', password);
  await Promise.all([page.waitForNavigation({ waitUntil: 'networkidle0' }), page.click('button[type=submit]')]);
}
export const sleep = ms => new Promise(r => setTimeout(r, ms));
