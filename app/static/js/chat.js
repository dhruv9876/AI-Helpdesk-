/* Live chat client, used by the customer page and the agent's chat room.
   Protocol: see the docstring of app/api/chats.py.
   Messages are built with textContent only: a customer's text must never be treated as HTML. */
(function () {
  var root = document.getElementById('chat');
  if (!root) return;

  var chatId = root.dataset.chatId;
  var me = Number(root.dataset.me);
  var role = root.dataset.role;               // "customer" or "agent"
  var log = root.querySelector('[data-log]');
  var form = root.querySelector('form[data-send]');
  var input = form.querySelector('textarea');
  var sendBtn = form.querySelector('button[type=submit]');
  var conn = root.querySelector('[data-conn]');
  var statusText = root.querySelector('[data-status-text]');
  var newChat = document.querySelector('[data-new-chat]');
  var botOnly = document.querySelectorAll('[data-bot-only]');

  var status = root.dataset.status;
  var agentName = root.dataset.agent || '';
  var agentId = root.dataset.agentId || '';
  var seen = new Set();                       // message ids already on screen (server-rendered ones too)
  var ws = null, delay = 1000, stopped = false;

  log.querySelectorAll('[data-id]').forEach(function (el) { seen.add(Number(el.dataset.id)); });
  log.scrollTop = log.scrollHeight;

  function setConn(state, text) { conn.dataset.state = state; conn.textContent = text; }

  function textFor(s) {
    var key = 't' + s.charAt(0).toUpperCase() + s.slice(1);   // data-t-waiting -> dataset.tWaiting
    return (root.dataset[key] || '').replace('{agent}', agentName || 'an agent');
  }

  function canSend() {
    if (role === 'customer') return status !== 'closed';
    return status === 'active' && agentId === String(me);     // agents must claim first
  }

  function applyStatus(next, agent) {
    status = next;
    if (agent) { agentName = agent.name; agentId = String(agent.id); }
    root.dataset.status = next;
    if (statusText) statusText.textContent = textFor(next);
    var ok = canSend();
    input.disabled = !ok;
    sendBtn.disabled = !ok;
    botOnly.forEach(function (el) { el.hidden = next !== 'bot'; });   // "Talk to a person" only makes sense with the bot
    if (next === 'closed') {
      input.placeholder = 'This chat has ended.';
      if (newChat) newChat.hidden = false;
    }
    // Lets htmx panels (the agent's action buttons) refresh themselves.
    document.body.dispatchEvent(new CustomEvent('chat-status', { detail: { status: next } }));
  }

  function parse(iso) { return new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : iso + 'Z'); }
  function ago(iso) {
    var s = Math.max(0, (Date.now() - parse(iso).getTime()) / 1000);
    if (s < 60) return 'just now';
    if (s < 3600) return Math.floor(s / 60) + ' min ago';
    if (s < 86400) return Math.floor(s / 3600) + ' h ago';
    return Math.floor(s / 86400) + ' d ago';
  }

  function kindOf(m) {
    if (m.sender_type === 'system' || m.sender_type === 'bot') return m.sender_type;
    return m.sender_id === me ? 'mine' : 'other';
  }

  function build(m) {
    var kind = kindOf(m);
    var el = document.createElement('div');
    el.className = 'msg msg-' + kind;
    el.dataset.id = m.id;
    if (kind !== 'system') {
      var meta = document.createElement('div');
      meta.className = 'msg-meta';
      var who = document.createElement('b'); who.textContent = m.sender_name;
      var when = document.createElement('span'); when.textContent = ago(m.created_at);
      meta.appendChild(who); meta.appendChild(when);
      el.appendChild(meta);
    }
    var body = document.createElement('div');
    body.className = 'msg-body';
    body.textContent = m.body;
    el.appendChild(body);
    if (m.sources && m.sources.length) {
      var wrap = document.createElement('div');
      wrap.className = 'sources';
      m.sources.forEach(function (s) {
        var chip = document.createElement('span');
        chip.className = 'chip chip-teal';
        chip.textContent = s.title;
        wrap.appendChild(chip);
      });
      el.appendChild(wrap);
    }
    return el;
  }

  function add(m) {
    if (seen.has(m.id)) return;
    seen.add(m.id);
    var nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 90;
    log.appendChild(build(m));
    if (nearBottom || m.sender_id === me) log.scrollTop = log.scrollHeight;
  }

  function onFrame(f) {
    if (f.type === 'ready') {
      f.messages.forEach(add);                 // fills any gap after a reconnect
      applyStatus(f.chat.status, f.chat.agent_id ? { id: f.chat.agent_id, name: f.chat.agent_name } : null);
      setConn('live', 'Connected');
    } else if (f.type === 'message') {
      add(f.message);
    } else if (f.type === 'status') {
      applyStatus(f.status, f.agent);
    } else if (f.type === 'error') {
      if (window.helpdeskToast) window.helpdeskToast(f.detail, 'error');
    }
  }

  function connect() {
    if (stopped) return;
    setConn('connecting', 'Connecting...');
    fetch('/ws-token', { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (r) {
        if (r.status === 401) { stopped = true; window.location = '/login'; throw new Error('signed out'); }
        return r.json();
      })
      .then(function (t) {
        ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/api/chats/' + chatId + '/ws');
        ws.onopen = function () { ws.send(JSON.stringify({ type: 'auth', token: t.token })); };
        ws.onmessage = function (e) { delay = 1000; onFrame(JSON.parse(e.data)); };
        ws.onclose = function (e) {
          ws = null;
          if (stopped) return;
          if (e.code === 4401 || e.code === 4403 || e.code === 4404) { setConn('offline', 'Not allowed'); return; }
          if (status === 'closed') { setConn('offline', 'Disconnected'); return; }
          setConn('offline', 'Reconnecting...');
          setTimeout(connect, delay);
          delay = Math.min(delay * 2, 15000);
        };
      })
      .catch(function () {
        if (stopped) return;
        setConn('offline', 'Reconnecting...');
        setTimeout(connect, delay);
        delay = Math.min(delay * 2, 15000);
      });
  }

  function send(text) {
    text = (text || '').trim();
    if (!text || !ws || ws.readyState !== 1) return false;
    ws.send(JSON.stringify({ type: 'message', body: text }));
    return true;
  }

  form.addEventListener('submit', function (e) {
    e.preventDefault();
    if (send(input.value)) input.value = '';
  });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
  });
  document.querySelectorAll('[data-send-text]').forEach(function (btn) {
    btn.addEventListener('click', function () { send(btn.dataset.sendText); });
  });
  window.addEventListener('pagehide', function () { stopped = true; if (ws) ws.close(); });

  connect();
})();
