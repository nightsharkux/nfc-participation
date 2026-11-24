(function () {
  "use strict";
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  // ---- CSRF-aware fetch for JSON APIs
  window.api = function (url, opts) {
    opts = opts || {};
    var token = (document.querySelector('meta[name="csrf-token"]') || {}).content || "";
    var headers = { "Accept": "application/json", "X-CSRF-Token": token };
    var init = { method: opts.method || "GET", headers: headers, credentials: "same-origin" };
    if (opts.body !== undefined) { headers["Content-Type"] = "application/json"; init.body = JSON.stringify(opts.body); }
    return fetch(url, init).then(function (r) {
      return r.json().catch(function () { return { ok: false, message: "Unexpected response (" + r.status + ")" }; })
        .then(function (data) { if (r.status === 401) { location.href = "/login"; } return data; });
    });
  };

  // ---- theme toggle (light / dark / follow system)
  function currentTheme() {
    var t = document.documentElement.getAttribute("data-theme");
    if (t) return t;
    return window.matchMedia && matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  function paintThemeButtons() {
    var dark = currentTheme() === "dark";
    $$("[data-theme-toggle]").forEach(function (b) {
      b.textContent = dark ? "☀️" : "🌙";
      b.setAttribute("aria-label", dark ? "Switch to light mode" : "Switch to dark mode");
      b.setAttribute("aria-pressed", dark ? "true" : "false");
    });
  }
  $$("[data-theme-toggle]").forEach(function (b) {
    b.addEventListener("click", function () {
      var next = currentTheme() === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      try { localStorage.setItem("theme", next); } catch (e) {}
      paintThemeButtons();
    });
  });
  paintThemeButtons();

  // ---- mobile nav + dropdown menus (click to open, Esc / outside click to close)
  var navToggle = $("[data-nav-toggle]");
  if (navToggle) navToggle.addEventListener("click", function () {
    var open = $("#nav").classList.toggle("open");
    navToggle.setAttribute("aria-expanded", open ? "true" : "false");
  });
  function closeMenus(except) {
    $$("[data-menu]").forEach(function (m) {
      if (m === except) return;
      var p = $(".menu-panel", m); if (p) p.hidden = true;
      var b = $("[data-menu-btn]", m); if (b) b.setAttribute("aria-expanded", "false");
    });
  }
  $$("[data-menu]").forEach(function (m) {
    var btn = $("[data-menu-btn]", m), panel = $(".menu-panel", m);
    btn.addEventListener("click", function (e) {
      e.stopPropagation();
      var open = panel.hidden;
      closeMenus(m);
      panel.hidden = !open;
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      if (open && e.detail === 0) { var first = $("a,button", panel); if (first) first.focus(); }
    });
  });
  document.addEventListener("click", function (e) { if (!e.target.closest("[data-menu]")) closeMenus(); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") { closeMenus(); var b = $("[data-menu-btn][aria-expanded=true]"); if (b) b.focus(); }
  });

  // ---- confirm dialogs: <form data-confirm="Are you sure?">
  document.addEventListener("submit", function (e) {
    var msg = e.target.getAttribute && e.target.getAttribute("data-confirm");
    if (msg && !window.confirm(msg)) e.preventDefault();
  });

  // ---- copy to clipboard: <button data-copy="#selector">
  $$("[data-copy]").forEach(function (b) {
    b.addEventListener("click", function () {
      var el = $(b.getAttribute("data-copy"));
      if (!el || !navigator.clipboard) return;
      navigator.clipboard.writeText(el.textContent.trim()).then(function () {
        var t = b.textContent; b.textContent = "Copied ✓"; setTimeout(function () { b.textContent = t; }, 1500);
      });
    });
  });

  // ---- live text filter: <input data-filter="#list"> filters elements carrying data-search
  $$("[data-filter]").forEach(function (input) {
    input.addEventListener("input", function () {
      var q = input.value.trim().toLowerCase();
      $$("[data-search]", $(input.getAttribute("data-filter"))).forEach(function (row) {
        row.hidden = !!q && row.getAttribute("data-search").indexOf(q) === -1;
      });
    });
  });

  // ---- searchable <select data-search-select>: ranked filtering; still a normal select without JS
  function score(text, q) {
    text = text.toLowerCase(); q = q.toLowerCase().trim();
    if (!q) return 1000; if (text === q) return 0; if (text.indexOf(q) === 0) return 1;
    var i = text.indexOf(q); if (i > -1) return 2 + i;
    var qi = 0, s = 100; for (var k = 0; k < text.length && qi < q.length; k++) { if (text[k] === q[qi]) { qi++; s += k; } }
    return qi === q.length ? s : 9999;
  }
  $$("select[data-search-select]").forEach(function (sel) {
    var box = document.createElement("input");
    box.type = "search"; box.className = "select-search"; box.autocomplete = "off";
    box.placeholder = sel.getAttribute("data-search-select") || "Type to search…";
    box.setAttribute("aria-label", box.placeholder);
    sel.parentNode.insertBefore(box, sel);
    var all = $$("option", sel).map(function (o, i) { return { v: o.value, t: o.textContent, i: i }; });
    box.addEventListener("input", function () {
      var cur = sel.value, q = box.value;
      var ranked = all.map(function (o) { return { v: o.v, t: o.t, i: o.i, s: o.v === "" ? -1 : score(o.t, q) }; })
        .filter(function (o) { return o.s < 9999; }).sort(function (a, b) { return a.s - b.s || a.i - b.i; });
      sel.innerHTML = "";
      ranked.forEach(function (o) { var op = document.createElement("option"); op.value = o.v; op.textContent = o.t; sel.appendChild(op); });
      if (q && sel.options.length > 1) sel.selectedIndex = sel.options[0].value === "" ? 1 : 0;
      else if ($$("option", sel).some(function (o) { return o.value === cur; })) sel.value = cur;
    });
    box.addEventListener("keydown", function (e) { if (e.key === "Enter") e.preventDefault(); });
  });

  // ---- "scan card into field": Web NFC when available, otherwise wait for a tap on the USB reader bridge
  $$("[data-scan-into]").forEach(function (btn) {
    var field = $(btn.getAttribute("data-scan-into")), status = $("#scan-status");
    var ctl = null, poll = null;
    function say(m) { if (status) status.textContent = m; }
    function stop() { if (ctl) { ctl.stop(); ctl = null; } if (poll) { clearInterval(poll); poll = null; } btn.textContent = "Scan card"; }
    function got(uid) { field.value = uid; say("Card " + uid + " captured."); stop(); field.dispatchEvent(new Event("input")); }
    function viaBridge() {
      say("Waiting for a tap on your USB reader (bridge)…");
      var lastId = null;
      window.api("/api/scans/latest").then(function (r) { lastId = r.last_id; });
      poll = setInterval(function () {
        if (lastId === null) return;
        window.api("/api/scans/latest?since=" + lastId).then(function (r) { lastId = r.last_id; if (r.uid) got(r.uid); });
      }, 1500);
    }
    btn.addEventListener("click", function () {
      if (ctl || poll) { stop(); say(""); return; }
      btn.textContent = "Stop scanning";
      if (window.NFC && window.NFC.supported) {
        window.NFC.start(got, function (err) { say(err); stop(); }).then(function (c) {
          ctl = c; if (c) say("Hold the card against the back of your phone…"); else if (!poll) { stop(); }
        });
      } else { viaBridge(); }
    });
  });
})();
