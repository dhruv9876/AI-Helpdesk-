import { launch, openPage, login, sleep } from './lib.mjs';
const base = process.env.BASE_URL || 'http://localhost:8771';
const ab = await launch(); const cb = await launch();
const A = await openPage(ab, base, { width: 1280, height: 900 });
const C = await openPage(cb, base, { width: 1280, height: 900 });
await login(A, base, 'admin@example.com');
await login(C, base, 'customer1@example.com');

const audit = () => {
  const out = [];
  const label = el => el.getAttribute('aria-label') || (el.id && document.querySelector(`label[for="${el.id}"]`)) || el.closest('label') || el.getAttribute('aria-labelledby') || el.getAttribute('title');
  document.querySelectorAll('input:not([type=hidden]), select, textarea').forEach(el => { if (!label(el)) out.push('control without a label: ' + el.outerHTML.slice(0, 80)); });
  document.querySelectorAll('button, a[href]').forEach(el => { if (!el.textContent.trim() && !el.getAttribute('aria-label')) out.push('empty ' + el.tagName + ': ' + el.outerHTML.slice(0, 80)); });
  const ids = [...document.querySelectorAll('[id]')].map(e => e.id); const dup = ids.filter((id, i) => ids.indexOf(id) !== i);
  if (dup.length) out.push('duplicate ids: ' + [...new Set(dup)].join(', '));
  if (!document.documentElement.lang) out.push('missing <html lang>');
  if (!document.querySelector('main')) out.push('no <main> landmark');
  if (document.querySelectorAll('h1').length !== 1) out.push('expected exactly one <h1>, found ' + document.querySelectorAll('h1').length);
  const wide = document.documentElement.scrollWidth - window.innerWidth;
  if (wide > 1) out.push('horizontal overflow of ' + wide + 'px');
  // anything that is tappable should be at least ~28px tall (WCAG 2.2 target size minimum is 24)
  document.querySelectorAll('button:not([hidden]), a.btn, select, input:not([type=hidden]):not([type=checkbox])').forEach(el => { const r = el.getBoundingClientRect(); if (r.width && r.height && r.height < 28) out.push('small tap target (' + Math.round(r.height) + 'px): ' + (el.textContent || el.name || '').trim().slice(0, 30)); });
  return out;
};

const staffPages = ['/dashboard', '/dashboard/tickets', '/dashboard/tickets/13', '/dashboard/chats', '/dashboard/kb', '/dashboard/kb/new', '/dashboard/users'];
let total = 0;
for (const w of [390, 768, 1280]) {
  await A.setViewport({ width: w, height: 900 }); await C.setViewport({ width: w, height: 900 });
  for (const [page, paths] of [[A, staffPages], [C, ['/chat']]]) {
    for (const path of paths) {
      await page.goto(base + path, { waitUntil: 'networkidle0' });
      const issues = await page.evaluate(audit);
      total += issues.length;
      console.log(`${String(w).padStart(4)}px ${path.padEnd(24)} ${issues.length ? 'ISSUES: ' + issues.join(' | ') : 'ok'}`);
      if (w === 390 && ['/dashboard', '/dashboard/tickets/13', '/chat'].includes(path)) await page.screenshot({ path: `/tmp/shots/m-${path.replace(/\W+/g, '_')}.png`, fullPage: true });
    }
  }
}
await A.goto(base + '/logout', { waitUntil: 'networkidle0' }).catch(() => {});
console.log('\ntotal issues:', total);
await ab.close(); await cb.close();
