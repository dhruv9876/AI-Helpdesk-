/* Staff pages: keeps the top-bar counts live and tells the server an agent is online.
   Opens the staff queue WebSocket (see app/api/chats.py for the protocol). */
(function () {
  var attention = document.querySelector('[data-nav-attention]');
  var waiting = document.querySelector('[data-nav-waiting]');
  var delay = 1000;

  function setBadge(el, n) { if (el) { el.textContent = n; el.dataset.n = n; } }

  function refreshCounts() {
    return fetch('/dashboard/nav-counts', { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (c) { if (c) { setBadge(attention, c.attention); setBadge(waiting, c.waiting_chats); } })
      .catch(function () {});
  }

  function connect() {
    fetch('/ws-token', { credentials: 'same-origin', headers: { Accept: 'application/json' } })
      .then(function (r) { if (r.status === 401) { window.location = '/login'; throw new Error('signed out'); } return r.json(); })
      .then(function (t) {
        var ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/api/chats/queue/ws');
        ws.onopen = function () { ws.send(JSON.stringify({ type: 'auth', token: t.token })); };
        ws.onmessage = function (m) {
          delay = 1000;
          var ev = JSON.parse(m.data);
          if (ev.type === 'chat_waiting' && window.helpdeskToast) {
            window.helpdeskToast(ev.chat.customer_name + ' is waiting for an agent.', 'info');
          }
          if (ev.type !== 'pong') {
            refreshCounts();
            document.body.dispatchEvent(new CustomEvent('chat-queue'));
          }
        };
        ws.onclose = function (e) {
          if (e.code === 4401 || e.code === 4403) return;   // not allowed: do not hammer the server
          setTimeout(connect, delay);
          delay = Math.min(delay * 2, 30000);
        };
      })
      .catch(function () {});
  }

  connect();
  setInterval(refreshCounts, 30000);   // ticket flags change without a socket event
})();
