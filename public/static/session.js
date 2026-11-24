(function () {
  "use strict";
  var root = document.getElementById("session-root");
  if (!root) return;
  var $ = function (s) { return root.querySelector(s); };
  var $$ = function (s) { return Array.prototype.slice.call(root.querySelectorAll(s)); };
  var id = root.getAttribute("data-lecture");
  var state = JSON.parse(document.getElementById("initial-state").textContent);
  var lastId = state.last_event_id || 0;
  var filter = "all", query = "";
  var rosterEl = $("#roster"), feedEl = $("#feed"), banner = $("#banner");
  var muted = false; try { muted = localStorage.getItem("mute") === "1"; } catch (e) {}
  var seen = {};

  // ---------- audio / haptics feedback
  var ctx;
  function tone(freq, dur, when) {
    if (muted) return;
    try {
      ctx = ctx || new (window.AudioContext || window.webkitAudioContext)();
      var o = ctx.createOscillator(), g = ctx.createGain();
      o.frequency.value = freq; o.type = "sine"; g.gain.value = 0.08;
      o.connect(g); g.connect(ctx.destination);
      var t = ctx.currentTime + (when || 0); o.start(t); o.stop(t + dur);
    } catch (e) {}
  }
  function buzz(p) { if (!muted && navigator.vibrate) navigator.vibrate(p); }
  function feedback(outcome) {
    if (outcome === "checked_in" || outcome === "manual") { tone(880, 0.12); tone(1175, 0.14, 0.13); buzz(60); }
    else if (outcome === "checked_out") { tone(660, 0.12); tone(520, 0.14, 0.13); buzz(60); }
    else if (outcome === "duplicate" || outcome === "already_out" || outcome === "removed") { tone(440, 0.15); }
    else { tone(220, 0.25); buzz([80, 60, 80]); }
  }
  var bannerClass = { checked_in: "in", manual: "in", checked_out: "out", duplicate: "warn", already_out: "warn", removed: "warn" };
  function showBanner(outcome, msg, uid) {
    banner.className = "status-banner " + (bannerClass[outcome] || "err");
    banner.textContent = "";
    var strong = document.createElement("span"); strong.textContent = msg; banner.appendChild(strong);
    if (outcome === "unknown" && uid) {
      var small = document.createElement("small"), a = document.createElement("a");
      a.href = "/students?uid=" + encodeURIComponent(uid); a.textContent = "Register this card →";
      small.appendChild(a); banner.appendChild(small);
    }
  }

  // ---------- rendering (always textContent: names are untrusted input)
  function renderCounts() {
    var c = state.counts;
    $("#c-present").textContent = c.present; $("#c-enrolled").textContent = c.enrolled; $("#c-left").textContent = c.left;
    var pct = c.enrolled ? Math.round(c.present / c.enrolled * 100) : 0;
    $("#c-pct").textContent = pct + "%"; $("#c-meter").style.setProperty("--w", pct + "%");
  }
  function renderRoster(changedIds) {
    rosterEl.textContent = "";
    var shown = 0;
    state.roster.forEach(function (r) {
      var hay = (r.name + " " + r.moodle_id).toLowerCase();
      if (query && hay.indexOf(query) === -1) return;
      if (filter === "present" && !r.present) return;
      if (filter === "absent" && r.present) return;
      shown++;
      var li = document.createElement("li");
      li.className = (r.present ? "present" : "") + (r.left ? " left" : "") + (changedIds && changedIds[r.id] ? " flash-new" : "");
      var dot = document.createElement("span"); dot.className = "dot"; dot.textContent = r.left ? "↗" : "✓"; dot.setAttribute("aria-hidden", "true");
      var name = document.createElement("div");
      var n = document.createElement("strong"); n.textContent = r.name; name.appendChild(n);
      var sub = document.createElement("div"); sub.className = "small muted";
      sub.textContent = (r.moodle_id ? r.moodle_id + " · " : "") + (r.present
        ? ("in " + r.in + (r.left ? " · out " + r.out + (r.minutes != null ? " (" + r.minutes + " min)" : "") : "") + (r.method === "manual" ? " · manual" : ""))
        : "absent");
      name.appendChild(sub);
      var st = document.createElement("span"); st.className = "sr-only"; st.textContent = r.present ? (r.left ? "Present, left" : "Present") : "Absent";
      var btn = document.createElement("button"); btn.type = "button"; btn.className = "btn secondary small";
      btn.textContent = r.present ? "Undo" : "Mark present";
      btn.setAttribute("aria-label", (r.present ? "Remove " : "Mark present: ") + r.name);
      btn.addEventListener("click", function () { mark(r.id, !r.present); });
      li.appendChild(dot); li.appendChild(name); li.appendChild(st); li.appendChild(btn);
      rosterEl.appendChild(li);
    });
    if (!shown) {
      var e = document.createElement("li"); e.className = "muted";
      e.textContent = state.roster.length ? "No students match." : "No students are enrolled in this subject yet.";
      rosterEl.appendChild(e);
    }
  }
  function addEvents(events, announce) {
    events.forEach(function (ev) {
      if (seen[ev.id]) return; seen[ev.id] = 1;
      var li = document.createElement("li"); li.className = "o-" + ev.outcome;
      var t = document.createElement("time"); t.textContent = ev.time;
      var m = document.createElement("span"); m.textContent = ev.message;
      li.appendChild(t); li.appendChild(m); feedEl.insertBefore(li, feedEl.firstChild);
      if (announce) { showBanner(ev.outcome, ev.message, ev.uid); feedback(ev.outcome); }
    });
    while (feedEl.children.length > 60) feedEl.removeChild(feedEl.lastChild);
  }
  function setOpen(open) {
    state.lecture.open = open;
    var b = $("#open-badge"); b.className = "badge " + (open ? "live" : "off"); b.textContent = open ? "Session open" : "Session closed";
    var s = $("#scan-tools"); if (s) s.hidden = !open;
  }

  // ---------- state sync (poll every 2.5 s while the tab is visible)
  function apply(next, announce) {
    var changed = {}, prev = {};
    state.roster.forEach(function (r) { prev[r.id] = r.present + "|" + r.left; });
    next.roster.forEach(function (r) { if (prev[r.id] !== undefined && prev[r.id] !== r.present + "|" + r.left) changed[r.id] = true; });
    state.roster = next.roster; state.counts = next.counts;
    if (state.lecture.open !== next.lecture.open) setOpen(next.lecture.open);
    lastId = Math.max(lastId, next.last_event_id || 0);
    renderCounts(); renderRoster(changed); addEvents(next.events, announce);
  }
  var busy = false;
  function refresh(announce) {
    if (busy) return Promise.resolve();
    busy = true;
    return window.api("/api/session/" + id + "/state?since=" + lastId)
      .then(function (s) { if (s && s.roster) apply(s, announce); })
      .catch(function () {}).then(function () { busy = false; });
  }
  setInterval(function () { if (!document.hidden) refresh(true); }, 2500);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) refresh(true); });

  // ---------- actions
  function sendUid(uid, source) {
    return window.api("/api/session/" + id + "/scan", { method: "POST", body: { uid: uid, source: source } }).then(function (res) {
      if (!res) return;
      showBanner(res.outcome || "invalid", res.message || "Done", res.uid);
      feedback(res.outcome || "invalid");
      return refresh(false);
    });
  }
  function mark(studentId, present) {
    window.api("/api/session/" + id + "/mark", { method: "POST", body: { student_id: studentId, present: present } }).then(function (r) {
      if (r && r.message) showBanner(present ? "manual" : "removed", r.message);
      refresh(false);
    });
  }

  var manual = $("#manual-form");
  if (manual) manual.addEventListener("submit", function (e) {
    e.preventDefault();
    var input = $("#manual-uid"), v = input.value.trim(); if (!v) return;
    sendUid(v, "manual").then(function () { input.value = ""; input.focus(); });
  });

  var nfcBtn = $("#nfc-btn"), nfcNote = $("#nfc-note"), ctl = null;
  if (nfcBtn) {
    if (!(window.NFC && window.NFC.supported)) {
      nfcBtn.disabled = true;
      nfcNote.textContent = "This browser has no Web NFC. Use Chrome on an NFC-capable Android phone, or connect a USB reader with the bridge (Account → Reader bridge).";
    }
    nfcBtn.addEventListener("click", function () {
      if (ctl) { ctl.stop(); ctl = null; nfcBtn.textContent = "Scan with this device"; nfcNote.textContent = ""; return; }
      window.NFC.start(function (uid) { sendUid(uid, "webnfc"); }, function (m) { nfcNote.textContent = m; }).then(function (c) {
        if (!c) return;
        ctl = c; nfcBtn.textContent = "Stop device scanning"; nfcNote.textContent = "Ready — hold a student card to the back of this phone.";
      });
    });
  }

  $$("[data-filter-chip]").forEach(function (chip) {
    chip.addEventListener("click", function () {
      filter = chip.getAttribute("data-filter-chip");
      $$("[data-filter-chip]").forEach(function (c) { c.setAttribute("aria-pressed", c === chip ? "true" : "false"); });
      renderRoster();
    });
  });
  var search = $("#roster-search");
  if (search) search.addEventListener("input", function () { query = search.value.trim().toLowerCase(); renderRoster(); });
  var muteBtn = $("#mute-btn");
  if (muteBtn) {
    var paint = function () { muteBtn.textContent = muted ? "🔇 Sound off" : "🔊 Sound on"; muteBtn.setAttribute("aria-pressed", muted ? "false" : "true"); };
    paint();
    muteBtn.addEventListener("click", function () { muted = !muted; try { localStorage.setItem("mute", muted ? "1" : "0"); } catch (e) {} paint(); });
  }

  // initial paint: events present at page load are history (no sound)
  addEvents(state.events, false); renderCounts(); renderRoster(); setOpen(state.lecture.open);
})();
