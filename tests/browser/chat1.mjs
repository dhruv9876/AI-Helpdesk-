import { launch, openPage, login, sleep } from './lib.mjs';
const base = process.env.BASE_URL || 'http://localhost:8771';
const agentBrowser = await launch();
const custBrowser = await launch();
const A = await openPage(agentBrowser, base, { width: 1280, height: 860 });
const C = await openPage(custBrowser, base, { width: 900, height: 820 });
for (const p of [A, C]) p.on('dialog', d => d.accept());
const results = [];
const check = (name, ok, extra = '') => { results.push(ok); console.log((ok ? 'PASS ' : 'FAIL ') + name + (extra ? '  ' + extra : '')); };
const waitFor = (page, fn, arg, ms = 8000) => page.waitForFunction(fn, { timeout: ms }, arg).then(() => true).catch(() => false);
const msgs = page => page.$$eval('#chat .msg', els => els.map(e => ({ cls: e.className, text: e.textContent.replace(/\s+/g, ' ').trim() })));

await login(A, base, 'agent1@example.com');
await A.goto(base + '/dashboard/chats', { waitUntil: 'networkidle0' });
await sleep(800);   // let the staff queue socket connect so an agent counts as online
const badgeBefore = Number(await A.$eval('[data-nav-waiting]', e => e.dataset.n));   // the seed already has one waiting chat
check('seeded waiting chat is already counted', badgeBefore === 1);

await login(C, base, 'customer1@example.com');
check('customer is sent to the chat page after sign-in', C.url().endsWith('/chat'));
check('customer socket connects', await waitFor(C, () => document.querySelector('[data-conn]')?.dataset.state === 'live'));
check('assistant greets the customer', (await msgs(C))[0]?.text.includes('support assistant'));
check('customer sees no staff navigation', !(await C.$('a[href="/dashboard"]')));

// ---- the bot answers a real question
await C.type('#chat-input', 'How do I reset my password?');
await C.keyboard.press('Enter');
check('bot answers from the knowledge base', await waitFor(C, () => [...document.querySelectorAll('.msg-bot')].some(m => m.querySelector('.sources'))));
const botMsg = (await msgs(C)).find(m => m.cls.includes('msg-bot') && m.text.includes('Resetting your password'));
check('answer cites its article and offers a person', !!botMsg && botMsg.text.includes('agent'));
check('own message is drawn on the right', (await C.$$('.msg-mine')).length === 1);
check('input is cleared after sending', (await C.$eval('#chat-input', e => e.value)) === '');

// ---- hand off to a person
await C.click('[data-send-text="agent"]');
check('customer sees the hand-off notice', await waitFor(C, () => [...document.querySelectorAll('.msg-system')].some(m => m.textContent.includes('connecting you'))));
check('customer status line says waiting', await waitFor(C, () => document.querySelector('[data-status-text]').textContent.includes('Waiting for an agent')));
check('hand-off notice does NOT say no agents are online', !(await msgs(C)).some(m => m.text.includes('No agents appear')));

// ---- the agent is told in real time
check('agent gets a live toast', await waitFor(A, () => [...document.querySelectorAll('#toasts .toast')].some(t => t.textContent.includes('Amit Customer is waiting'))));
check('waiting chat appears in the agent queue without a reload', await waitFor(A, () => document.querySelector('#chat-lists')?.textContent.includes('Amit Customer')));
check('top-bar Chats badge goes up by one', await waitFor(A, n => Number(document.querySelector('[data-nav-waiting]')?.dataset.n) === n, badgeBefore + 1));
check('queue previews what the customer said, not the bot notice', await (async () => { const row = await A.evaluate(() => [...document.querySelectorAll('#chat-lists .person')].find(p => p.textContent.includes('Amit Customer'))?.textContent.replace(/\s+/g, ' ') || ''); return row.includes('agent') && !row.includes('connecting you'); })());
check('queue shows why it was handed off', (await A.$eval('#chat-lists', e => e.textContent)).includes('asked for a human'));
await A.screenshot({ path: '/tmp/shots/05-chats-queue.png' });

// ---- agent claims, customer is told
await A.evaluate(() => [...document.querySelectorAll('#chat-lists .person')].find(p => p.textContent.includes('Amit Customer')).querySelector('button').click());   // claim Amit's chat, not the seeded one
check('claiming opens the chat room', await waitFor(A, () => location.pathname.startsWith('/dashboard/chats/') && document.querySelector('#chat')));
check('agent socket connects', await waitFor(A, () => document.querySelector('[data-conn]')?.dataset.state === 'live'));
check('agent input is enabled after claiming', await waitFor(A, () => !document.querySelector('#chat-input').disabled));
check('customer sees the agent join', await waitFor(C, () => [...document.querySelectorAll('.msg-system')].some(m => m.textContent.includes('Rahul Agent joined'))));
check('customer status line names the agent', await waitFor(C, () => document.querySelector('[data-status-text]').textContent.includes('Rahul Agent')));
check('agent room shows the earlier conversation', (await msgs(A)).some(m => m.text.includes('How do I reset my password?')));

// ---- conversation both ways
await A.type('#chat-input', 'Hello Amit, how can I help?');
await A.keyboard.press('Enter');
check('customer receives the agent message live', await waitFor(C, () => [...document.querySelectorAll('.msg-other')].some(m => m.textContent.includes('Hello Amit'))));
await C.type('#chat-input', 'My invoice amount is wrong');
await C.keyboard.press('Enter');
check('agent receives the customer message live', await waitFor(A, () => [...document.querySelectorAll('.msg-other')].some(m => m.textContent.includes('invoice amount is wrong'))));
await sleep(600);
const afterClaim = (await msgs(C)).slice(-4);
check('the bot stays silent once a person has joined', !afterClaim.some(m => m.cls.includes('msg-bot')));

// ---- XSS attempt: must be shown as text and never run
await C.type('#chat-input', '<img src=x onerror="window.__pwned=1"> <b>bold?</b>');
await C.keyboard.press('Enter');
check('agent sees the markup as plain text', await waitFor(A, () => [...document.querySelectorAll('.msg-other .msg-body')].some(b => b.textContent.includes('<img src=x'))));
check('no injected element was created', (await A.$$('.msg-body img, .msg-body b')).length === 0);
check('injected script did not run', (await A.evaluate(() => window.__pwned)) === undefined && (await C.evaluate(() => window.__pwned)) === undefined);
await A.screenshot({ path: '/tmp/shots/06-agent-room.png' });
await C.screenshot({ path: '/tmp/shots/07-customer-chat.png' });

// ---- customer converts to a ticket
await C.click('button[hx-post$="/ticket"]');
check('customer sees a ticket confirmation', await waitFor(C, () => [...document.querySelectorAll('#toasts .toast')].some(t => t.textContent.includes('Your ticket is #'))));
check('agent room learns the chat has ended', await waitFor(A, () => document.querySelector('[data-status-text]').textContent.includes('ended')));
check('agent input is disabled after the chat ends', await A.$eval('#chat-input', e => e.disabled));
check('agent action panel links to the new ticket', await waitFor(A, () => document.querySelector('#chat-actions a[href^="/dashboard/tickets/"]')));
await Promise.all([A.waitForNavigation({ waitUntil: 'networkidle0' }), A.click('#chat-actions a[href^="/dashboard/tickets/"]')]);
check('ticket was created from the first customer message', (await A.$eval('h1', e => e.textContent)).startsWith('Chat: How do I reset my password?'));
check('ticket contains the transcript', (await A.$eval('.msg-customer .msg-body', e => e.textContent)).includes('Customer: My invoice amount is wrong'));

console.log('\nproblems:', JSON.stringify([...A.problems, ...C.problems].length ? { agent: A.problems, customer: C.problems } : 'none'));
console.log(results.filter(Boolean).length + '/' + results.length + ' checks passed');
await agentBrowser.close(); await custBrowser.close();
