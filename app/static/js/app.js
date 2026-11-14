/* Toasts and global htmx error handling. No inline scripts anywhere: the page CSP forbids them. */
(function () {
  var box = document.getElementById('toasts');

  function toast(message, level) {
    if (!box || !message) return;
    var el = document.createElement('div');
    el.className = 'toast';
    el.dataset.level = level || 'success';
    el.setAttribute('role', level === 'error' ? 'alert' : 'status');
    el.textContent = message;           // textContent: messages can contain user-written text
    box.appendChild(el);
    setTimeout(function () { el.remove(); }, level === 'error' ? 9000 : 4500);
  }
  window.helpdeskToast = toast;

  // The server asks for a toast with the HX-Trigger header: {"toast": {"message": "...", "level": "..."}}
  document.body.addEventListener('toast', function (e) {
    var d = e.detail || {};
    toast(d.message, d.level);
  });
  document.body.addEventListener('htmx:sendError', function () {
    toast('Could not reach the server. Check your connection and try again.', 'error');
  });
  document.body.addEventListener('htmx:responseError', function (e) {
    var xhr = e.detail.xhr;
    if (!xhr.getResponseHeader('HX-Trigger')) {
      toast('Something went wrong (error ' + xhr.status + '). Reload the page and try again.', 'error');
    }
  });
  // Ask before destructive actions: <button data-confirm="Delete this article?">
  document.body.addEventListener('htmx:confirm', function (e) {
    var q = e.detail.elt.getAttribute('data-confirm');
    if (!q) return;
    e.preventDefault();
    if (window.confirm(q)) e.detail.issueRequest(true);
  });
})();
