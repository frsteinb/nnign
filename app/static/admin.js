/* nnign Admin-Bedienung: Aktionen ohne Seitenwechsel, Ergebnis direkt am Knopf, Einstellungen bequem. */
(function () {
  'use strict';

  // ---------------------------------------------------------------- Hinweis-Leiste (Toast)
  function toast(msg, ok) {
    if (!msg) return;
    var box = document.getElementById('toast');
    if (!box) {
      box = document.createElement('div');
      box.id = 'toast';
      box.setAttribute('role', 'status');
      box.setAttribute('aria-live', 'polite');
      document.body.appendChild(box);
    }
    box.className = 'toast ' + (ok === false ? 'err' : 'ok') + ' show';
    box.textContent = msg;
    clearTimeout(box._t);
    box._t = setTimeout(function () { box.classList.remove('show'); }, ok === false ? 9000 : 5000);
  }
  try {
    var pending = sessionStorage.getItem('nnign-toast');
    if (pending) { sessionStorage.removeItem('nnign-toast'); var p = JSON.parse(pending); toast(p.msg, p.ok); }
  } catch (e) {}

  // ---------------------------------------------------------------- Ergebnis am Knopf
  function resultBox(form) {
    var host = form.closest('.actions') || form;
    var box = host.nextElementSibling;
    if (!box || !box.classList.contains('result')) {
      box = document.createElement('div');
      box.className = 'result';
      box.setAttribute('aria-live', 'polite');
      host.parentNode.insertBefore(box, host.nextSibling);
    }
    return box;
  }

  function showModels(box, data) {
    var list = document.createElement('div');
    list.className = 'chips';
    data.models.forEach(function (m) {
      var b = document.createElement('button');
      b.type = 'button';
      b.className = 'chip';
      b.textContent = m;
      b.addEventListener('click', function () {
        var fd = new FormData();
        fd.append('llm_model', m);
        fetch('/admin/settings', {method: 'POST', body: fd, credentials: 'same-origin',
                                  headers: {'X-Requested-With': 'fetch'}})
          .then(function (r) { return r.json(); })
          .then(function () {
            try {
              sessionStorage.setItem('nnign-toast', JSON.stringify(
                {msg: 'Modell „' + m + '“ gespeichert – jetzt „Verbindung testen“.', ok: true}));
            } catch (x) {}
            location.reload();
          });
      });
      list.appendChild(b);
    });
    box.appendChild(list);
  }

  // Formulare der Admin-Aktionen abfangen (Bestätigungen per onsubmit laufen vorher)
  document.addEventListener('submit', function (e) {
    var form = e.target;
    if (e.defaultPrevented || form.method.toLowerCase() !== 'post' || form.hasAttribute('data-nav')) return;
    var path = new URL(form.action, location.href).pathname;
    if (!/^\/admin\//.test(path) && path !== '/logout') return;
    if (path === '/logout') return;
    e.preventDefault();
    var btn = e.submitter || form.querySelector('button[type=submit], button:not([type])');
    var label = btn ? btn.textContent : '';
    if (btn) { btn.disabled = true; btn.classList.add('busy'); }
    var inline = !!form.closest('.actions') && !form.closest('#admin-status');
    var box = inline ? resultBox(form) : null;
    if (box) { box.className = 'result wait'; box.textContent = 'läuft …'; }
    fetch(form.action, {method: 'POST', body: new FormData(form), credentials: 'same-origin',
                        headers: {'X-Requested-With': 'fetch', 'Accept': 'application/json'}})
      .then(function (r) {
        return r.json().catch(function () { return {ok: r.ok, msg: r.ok ? 'erledigt' : 'Fehler ' + r.status}; });
      })
      .then(function (data) {
        if (data.location) { location.href = data.location; return; }
        if (box) {
          box.className = 'result ' + (data.ok === false ? 'err' : 'ok');
          box.textContent = data.msg || 'erledigt';
          if (data.models) showModels(box, data);
        }
        if (data.reload) {
          try { sessionStorage.setItem('nnign-toast', JSON.stringify({msg: data.msg, ok: data.ok})); } catch (x) {}
          if (form.id === 'settings-form') rememberGroups();
          location.reload();
          return;
        }
        if (!box) toast(data.msg, data.ok);
        if (window.nnignPoll) setTimeout(window.nnignPoll, 800);   // Status gleich auffrischen
      })
      .catch(function () {
        if (box) { box.className = 'result err'; box.textContent = 'Keine Verbindung zum Server.'; }
        else toast('Keine Verbindung zum Server.', false);
      })
      .finally(function () { if (btn) { btn.disabled = false; btn.classList.remove('busy'); btn.textContent = label; } });
  });

  // ---------------------------------------------------------------- Einstellungen
  var groups = Array.prototype.slice.call(document.querySelectorAll('details.setgroup'));
  function rememberGroups() {
    try {
      sessionStorage.setItem('nnign-groups', JSON.stringify(groups.filter(function (g) { return g.open; })
        .map(function (g) { return g.dataset.group; })));
    } catch (e) {}
  }
  try {
    var open = JSON.parse(sessionStorage.getItem('nnign-groups') || '[]');
    groups.forEach(function (g) { if (open.indexOf(g.dataset.group) >= 0) g.open = true; });
  } catch (e) {}
  groups.forEach(function (g) { g.addEventListener('toggle', rememberGroups); });

  function openGroup(name) {
    var g = document.querySelector('details.setgroup[data-group="' + name + '"]');
    if (g) { g.open = true; rememberGroups(); g.scrollIntoView({behavior: 'smooth', block: 'start'}); }
  }
  document.addEventListener('click', function (e) {
    var a = e.target.closest('[data-open-group]');
    if (a) { e.preventDefault(); openGroup(a.dataset.openGroup); }
    var row = e.target.closest('[data-toggle-row]');
    if (row) {
      var r = document.getElementById(row.dataset.toggleRow);
      if (r) { r.hidden = !r.hidden; row.textContent = r.hidden ? 'Bearbeiten' : 'Schließen'; }
    }
    var t = e.target.closest('[data-groups]');
    if (t) { groups.forEach(function (g) { g.open = t.dataset.groups === 'open'; }); rememberGroups(); }
  });
  if (location.hash.indexOf('#g-') === 0) openGroup(decodeURIComponent(location.hash.slice(3)));

  var filter = document.getElementById('settings-filter');
  if (filter) {
    filter.addEventListener('input', function () {
      var q = filter.value.trim().toLowerCase();
      groups.forEach(function (g) {
        var hits = 0;
        g.querySelectorAll('.setting').forEach(function (s) {
          var match = !q || s.textContent.toLowerCase().indexOf(q) >= 0;
          s.hidden = !match;
          if (match) hits++;
        });
        g.hidden = q && !hits;
        if (q) g.open = hits > 0;
      });
    });
  }

  var sform = document.getElementById('settings-form');
  if (sform) {
    sform.addEventListener('input', function () { sform.classList.add('dirty'); });
    sform.addEventListener('change', function () { sform.classList.add('dirty'); });
    window.addEventListener('beforeunload', function (e) {
      if (sform.classList.contains('dirty') && !sform.classList.contains('saving')) { e.preventDefault(); e.returnValue = ''; }
    });
    sform.addEventListener('submit', function () { sform.classList.add('saving'); });
  }

  // ---------------------------------------------------------------- Live-Status
  var live = document.querySelector('[data-poll]');
  if (live) {
    var timer = null;
    var poll = function () {
      clearTimeout(timer);
      fetch(live.dataset.poll, {credentials: 'same-origin'}).then(function (r) {
        var busy = r.headers.get('X-Busy') === '1';
        return r.text().then(function (html) { live.innerHTML = html; return busy; });
      }).catch(function () { return false; }).then(function (busy) {
        timer = setTimeout(poll, document.hidden ? 60000 : (busy ? 4000 : 30000));
      });
    };
    window.nnignPoll = poll;
    timer = setTimeout(poll, 4000);
  }
})();
