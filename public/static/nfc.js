// Web NFC helper (Chrome on Android, HTTPS only). Falls back gracefully elsewhere.
(function () {
  "use strict";
  function normalize(serial) { return String(serial || "").replace(/[^0-9a-fA-F]/g, "").toUpperCase(); }
  var supported = "NDEFReader" in window && window.isSecureContext;
  async function start(onUid, onError) {
    if (!supported) { if (onError) onError("Web NFC is not available in this browser."); return null; }
    var abort = new AbortController(), last = "", lastAt = 0;
    try {
      var reader = new NDEFReader();
      await reader.scan({ signal: abort.signal });
      reader.onreading = function (ev) {
        var uid = normalize(ev.serialNumber), now = Date.now();
        if (!uid || (uid === last && now - lastAt < 2500)) return; // same card still near the phone
        last = uid; lastAt = now; onUid(uid);
      };
      reader.onreadingerror = function () { if (onError) onError("Couldn't read that tag — try again."); };
      return { stop: function () { abort.abort(); } };
    } catch (e) {
      if (onError) onError(e && e.name === "NotAllowedError" ? "NFC permission was denied. Allow it in the browser prompt." : "Couldn't start NFC: " + ((e && e.message) || e));
      return null;
    }
  }
  window.NFC = { supported: supported, start: start, normalize: normalize };
})();
