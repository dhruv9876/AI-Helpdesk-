import { launch, openPage, login, sleep } from './lib.mjs';
const base = process.env.BASE_URL || 'http://localhost:8771';
const browser = await launch();
const page = await openPage(browser, base);
page.on('dialog', d => d.accept());
const results = [];
const check = (name, ok, extra = '') => { results.push(ok); console.log((ok ? 'PASS ' : 'FAIL ') + name + (extra ? '  ' + extra : '')); };
const text = sel => page.$eval(sel, e => e.textContent.replace(/\s+/g, ' ').trim()).catch(() => null);
const toasts = () => page.$$eval('#toasts .toast', els => els.map(e => e.textContent));
const waitFor = (fn, arg, ms = 6000) => page.waitForFunction(fn, { timeout: ms }, arg).then(() => true).catch(() => false);

await login(page, base, 'agent1@example.com');

// ---------- ticket #13: change priority, header chips follow
await page.goto(base + '/dashboard/tickets/13', { waitUntil: 'networkidle0' });
await page.select('#c-priority', 'high');
check('priority change updates the header chip', await waitFor(() => document.querySelector('#t-chips')?.textContent.includes('High')));
check('toast says the ticket was updated', (await toasts()).some(t => t.includes('Ticket updated')));
check('select shows the saved value', await page.$eval('#c-priority', e => e.value) === 'high');

// ---------- assign to me
await page.click('#t-sidebar button.btn-small[hx-vals]');
check('assign to me selects the agent', await waitFor(() => document.querySelector('#c-assignee')?.selectedOptions[0]?.textContent.includes('(you)')));

// ---------- regression: "Unassigned" and category "None" must really clear the fields
// htmx attaches handlers to swapped-in elements ~20 ms after the swap; a person is never that fast, a script is.
await sleep(150);
await page.select('#c-assignee', 'none');
await waitFor(() => document.querySelector('#c-assignee')?.value === 'none' && !document.querySelector('#c-assignee').selectedOptions[0].textContent.includes('(you)'));
await sleep(700);
await page.reload({ waitUntil: 'networkidle0' });
const afterAssignee = await page.$eval('#c-assignee', e => e.value + ' / ' + e.selectedOptions[0].textContent.trim());
check('choosing "Unassigned" really unassigns (still unassigned after a reload)', afterAssignee.startsWith('none'), afterAssignee);
await page.select('#c-category', 'none');
await sleep(900);
await page.reload({ waitUntil: 'networkidle0' });
const afterCategory = await page.$eval('#c-category', e => e.value);
check('category "None" really clears the category (after a reload)', afterCategory === 'none', afterCategory);

// ---------- AI triage: apply suggestion (priority back to urgent), with confirm dialog
await page.click('button[hx-post$="triage?apply=1"]');
check('"Use these values" applies the AI priority', await waitFor(() => document.querySelector('#c-priority')?.value === 'urgent' && document.querySelector('#t-chips')?.textContent.includes('Urgent')));

// ---------- internal note + public reply
await page.type('#reply-body', 'Checking the login logs first.');
await page.click('#internal');
await page.click('form.reply button[type=submit]');
check('internal note appears with its label', await waitFor(() => document.querySelector('.msg-note')?.textContent.includes('Checking the login logs')));
check('note label warns it is hidden', (await text('.msg-note')).includes('hidden from the customer'));
check('reply box is cleared after sending', (await page.$eval('#reply-body', e => e.value)) === '');
await page.type('#reply-body', 'Hi Amit, we have secured your account.');
await page.click('form.reply button[type=submit]');
check('public reply appears on the agent side', await waitFor(() => document.querySelector('.msg-agent')?.textContent.includes('secured your account')));

// ---------- suggested reply: a ticket the knowledge base cannot answer
await page.click('#t-suggestions button.btn-small');
check('no matching article: says so and asks the agent to write it', await waitFor(() => document.querySelector('#t-suggestions')?.textContent.includes('Write the reply yourself')) && !(await page.$('.draft')));

// ---------- suggested reply on a ticket it CAN answer (#1 password reset): edit then post
await page.goto(base + '/dashboard/tickets/1', { waitUntil: 'networkidle0' });
await page.click('#t-suggestions button.btn-small');
const gotDraft = await waitFor(() => document.querySelector('.draft textarea')?.value.length > 20);
check('suggest a reply produces an editable draft', gotDraft);
if (gotDraft) {
  const draft = await page.$eval('.draft textarea', e => e.value);
  check('draft is addressed to the customer', draft.startsWith('Hi Amit'));
  check('draft shows which article it came from', (await text('.draft')).includes('Resetting your password'));
  await page.focus('.draft textarea'); await page.keyboard.press('End'); await page.keyboard.type(' (edited by agent)');
  await page.click('.draft button[type=submit]');
  check('posting the draft adds it to the thread', await waitFor(() => [...document.querySelectorAll('.msg-agent')].some(m => m.textContent.includes('(edited by agent)'))));
  check('draft panel clears (out-of-band swap)', await waitFor(() => !document.querySelector('.draft')));
  // a second draft, this time discarded
  await page.click('#t-suggestions button.btn-small');
  await waitFor(() => document.querySelector('.draft textarea'));
  await page.click('.draft button[type=button]');
  check('discarding a draft removes it and posts nothing', await waitFor(() => !document.querySelector('.draft')) && (await page.$$('.msg-agent')).length === 1);
}

// ---------- duplicates and merge (#16 duplicates #15, same customer)
await page.goto(base + '/dashboard/tickets/16', { waitUntil: 'networkidle0' });
check('ticket 16 lists ticket 15 as a possible duplicate', (await text('#t-duplicates')).includes('#15'));
await page.click('#t-duplicates button[hx-post$="/merge"]');
check('merge redirects to the original ticket', await waitFor(() => location.pathname === '/dashboard/tickets/15', 8000));
await sleep(300);
check('original ticket has the merge note', (await text('#t-thread')).includes('was merged into this ticket'));
await page.goto(base + '/dashboard/tickets/16', { waitUntil: 'networkidle0' });
check('merged ticket is closed and links back', (await text('main')).includes('merged into') && (await text('#t-chips')).includes('Closed'));
check('merged ticket no longer offers a reply box', !(await page.$('#reply-body')));

// ---------- queue: htmx filtering and history
await page.goto(base + '/dashboard/tickets', { waitUntil: 'networkidle0' });
const before = (await page.$$('table.queue tbody tr')).length;
await page.type('#q', 'password');
const filtered = await waitFor(n => document.querySelectorAll('table.queue tbody tr').length < n, before);
const rows = (await page.$$('table.queue tbody tr')).length;
check('typing in search filters the queue without a reload', filtered && rows === 2, `(${before} -> ${rows} rows)`);
check('URL is clean (no empty parameters)', page.url().endsWith('/dashboard/tickets?q=password'), page.url());
await page.goBack();
check('back button restores the unfiltered queue', await waitFor(n => document.querySelectorAll('table.queue tbody tr').length === n, before));
await page.select('#f-priority', 'urgent');
check('priority filter narrows the queue', await waitFor(() => document.querySelectorAll('table.queue tbody tr').length === 1));
await page.select('#f-priority', '');
await page.click('#f-attn');
check('"needs attention only" shows just flagged tickets', await waitFor(() => document.querySelectorAll('table.queue tbody tr').length === 3));
await page.type('#q', 'zzzzzz');
check('no results shows a helpful empty state with a way out', await waitFor(() => document.querySelector('#queue .empty')?.textContent.includes('No tickets match')) );

console.log('\nconsole/network problems:', page.problems.length ? page.problems : 'none');
console.log(results.filter(Boolean).length + '/' + results.length + ' checks passed');
await browser.close();
