import { launch, openPage, login, sleep } from './lib.mjs';
import fs from 'fs';
const base = process.env.BASE_URL || 'http://localhost:8771';
const ab = await launch(); const gb = await launch();
const D = await openPage(ab, base, { width: 1280, height: 900 });   // admin
const G = await openPage(gb, base);                                  // ordinary agent
for (const p of [D, G]) p.on('dialog', d => d.accept());
const results = [];
const check = (name, ok, extra = '') => { results.push(ok); console.log((ok ? 'PASS ' : 'FAIL ') + name + (extra ? '  ' + extra : '')); };
const waitFor = (page, fn, arg, ms = 8000) => page.waitForFunction(fn, { timeout: ms }, arg).then(() => true).catch(() => false);
const text = (page, sel) => page.$eval(sel, e => e.textContent.replace(/\s+/g, ' ').trim()).catch(() => null);
const nav = (page, action) => Promise.all([page.waitForNavigation({ waitUntil: 'networkidle0' }), action()]);

await login(D, base, 'admin@example.com');
await login(G, base, 'agent1@example.com');

// ---------------- users (admin)
check('admin sees the Users tab', !!(await D.$('a[href="/dashboard/users"]')));
check('agent does not see the Users tab', !(await G.$('a[href="/dashboard/users"]')));
await D.goto(base + '/dashboard/users', { waitUntil: 'networkidle0' });
await D.type('#n-name', 'Meera Support'); await D.type('#n-email', 'meera@example.com'); await D.type('#n-pass', 'welcome-123');
await D.click('form[hx-post="/dashboard/users"] button[type=submit]');
check('new team member appears in the table', await waitFor(D, () => document.querySelector('#users-wrap')?.textContent.includes('Meera Support')));
check('the form is cleared after creating the account (no password left behind)', await waitFor(D, () => ['#n-name', '#n-email', '#n-pass'].every(id => document.querySelector(id)?.value === '')));
check('toast confirms the account was created', (await D.$$eval('#toasts .toast', t => t.map(x => x.textContent))).some(t => t.includes('Account created')));
const meeraRow = () => D.evaluateHandle(() => [...document.querySelectorAll('#users-wrap tbody tr')].find(r => r.textContent.includes('Meera Support')));
await (await meeraRow()).$eval('button[hx-vals*="0"]', b => b.click());
check('disabling an account shows it as disabled', await waitFor(D, () => [...document.querySelectorAll('#users-wrap tbody tr')].find(r => r.textContent.includes('Meera Support'))?.textContent.includes('Disabled')));
check('you cannot change your own role', await D.$$eval('#users-wrap select', ss => ss.some(s => s.disabled)));
await D.screenshot({ path: '/tmp/shots/08-users.png' });

// agent is blocked from admin pages
await G.goto(base + '/dashboard/users', { waitUntil: 'networkidle0' });
check('agent gets an access-denied page for Users', (await text(G, 'main')).includes("don't have access"));
check('access-denied page offers a way back', !!(await G.$('main a[href="/dashboard"]')));

// ---------------- knowledge base
await D.goto(base + '/dashboard/kb', { waitUntil: 'networkidle0' });
await D.type('#ask-q', 'How do I reset my password?');
await D.click('form[hx-post="/dashboard/kb/ask"] button[type=submit]');
check('"Test the assistant" shows the answer customers would get', await waitFor(D, () => document.querySelector('#ask-result')?.textContent.includes('Customers would see')));
await D.$eval('#ask-q', e => e.value = ''); await D.type('#ask-q', 'What is the capital of France?');
await D.click('form[hx-post="/dashboard/kb/ask"] button[type=submit]');
check('off-topic question shows it would hand over', await waitFor(D, () => document.querySelector('#ask-result')?.textContent.includes('hand this to a person')));

await D.goto(base + '/dashboard/kb/new', { waitUntil: 'networkidle0' });
await D.type('#a-title', 'Parking and visitor passes');
await D.type('#a-content', 'Visitors can park in lot B for free for two hours. Ask reception for a visitor pass at the front desk.\n\nOvernight parking is not available.');
await D.click('form[hx-post="/dashboard/kb"] button[type=submit]');
check('creating an article opens its page', await waitFor(D, () => /\/dashboard\/kb\/\d+$/.test(location.pathname)));
await sleep(300);
check('article page shows the piece(s) the assistant reads', (await D.$$eval('.msg-bot', els => els.map(e => e.textContent)).then(t => t.join(' '))).includes('lot B'));
const kbUrl = D.url();
await D.goto(base + '/dashboard/kb', { waitUntil: 'networkidle0' });
await D.type('#ask-q', 'where can visitors park?');
await D.click('form[hx-post="/dashboard/kb/ask"] button[type=submit]');
check('the new article is used straight away', await waitFor(D, () => document.querySelector('#ask-result')?.textContent.includes('lot B')));

// agent can edit but not delete
await G.goto(kbUrl, { waitUntil: 'networkidle0' });
check('agent can open the article editor', !!(await G.$('#a-title')));
check('agent has no delete button', !(await G.$('button[hx-post$="/delete"]')));
// ... and the server refuses even if the button is forged
const forged = await G.evaluate(async (u) => {
  const token = document.querySelector('meta[name=csrf-token]').content;
  const r = await fetch(u + '/delete', { method: 'POST', headers: { 'X-CSRF-Token': token, 'HX-Request': 'true' } });
  return r.status;
}, new URL(kbUrl).pathname);
check('forged delete request from an agent is refused (403)', forged === 403, `(status ${forged})`);

// upload a markdown file
fs.writeFileSync('/tmp/gift_cards.md', '# Gift cards\n\nGift cards never expire and can be used on any order.\n\nThey cannot be exchanged for cash.');
await D.goto(base + '/dashboard/kb', { waitUntil: 'networkidle0' });
const input = await D.$('#up-file'); await input.uploadFile('/tmp/gift_cards.md');
await D.click('form[hx-post="/dashboard/kb/upload"] button[type=submit]');
check('uploading a file creates an article', await waitFor(D, () => /\/dashboard\/kb\/\d+$/.test(location.pathname)));
check('upload is titled from the file name', (await D.$eval('#a-title', e => e.value)) === 'gift cards');

// admin deletes
await D.goto(kbUrl, { waitUntil: 'networkidle0' });
await D.click('button[hx-post$="/delete"]');
check('admin can delete an article', await waitFor(D, () => location.pathname === '/dashboard/kb'));
check('deleted article is gone from the list', !(await text(D, 'main')).includes('Parking and visitor passes'));
await D.screenshot({ path: '/tmp/shots/09-kb.png' });

// ---------------- sessions
await G.goto(base + '/dashboard/tickets/1', { waitUntil: 'networkidle0' });
const cookies = await G.cookies(base); await G.deleteCookie(...cookies);          // the session "expires"
await G.click('#t-suggestions button.btn-small');
check('an action after the session expires sends you to sign in', await waitFor(G, () => location.pathname === '/login'));
await login(G, base, 'agent1@example.com');
await G.goto(base + '/dashboard/tickets/1', { waitUntil: 'networkidle0' });
await G.click('header button[hx-post="/logout"]');
check('sign out returns to the sign-in page', await waitFor(G, () => location.pathname === '/login'));
await G.goto(base + '/dashboard/tickets/1', { waitUntil: 'networkidle0' });
check('after sign out, a protected page redirects to sign in and remembers where you were', new URL(G.url()).pathname === '/login' && new URL(G.url()).searchParams.get('next') === '/dashboard/tickets/1', G.url());

console.log('\nproblems:', JSON.stringify([...D.problems, ...G.problems.filter(p => !p.includes('401') && !p.includes('403') && !p.includes('Failed to load resource'))].length ? { admin: D.problems, agent: G.problems } : 'none'));
console.log(results.filter(Boolean).length + '/' + results.length + ' checks passed');
await ab.close(); await gb.close();
