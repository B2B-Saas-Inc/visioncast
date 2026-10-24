/*
 * Waiting room behaviour: countdown, server-authoritative polling and admission.
 * The page never learns the join link: when the server says "admit", the page
 * submits the CSRF-protected enter form and the browser follows the 303.
 * Without JavaScript the page still works (reload + Join button).
 */
(function () {
  "use strict";
  var box = document.querySelector("[data-room-status]");
  if (!box) return;
  var reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var url = box.getAttribute("data-status-url");
  var form = document.querySelector("[data-enter-form]");
  var countdown = box.querySelector("[data-countdown]");
  var opensAt = Date.parse(box.getAttribute("data-opens-at") || "");
  var skew = 0;
  var submitted = false;
  var timer = null;

  var focusMe = document.querySelector("[data-focus-me]");
  if (focusMe) focusMe.focus();

  Array.prototype.forEach.call(document.querySelectorAll("[data-local-time]"), function (el) {
    var d = new Date(el.getAttribute("datetime"));
    if (!isNaN(d)) el.textContent = d.toLocaleString([], { weekday: "long", hour: "numeric", minute: "2-digit", timeZoneName: "short" });
  });

  function fmt(seconds) {
    seconds = Math.max(0, Math.round(seconds));
    var h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
    if (h > 0) return h + " h " + m + " min";
    if (m > 0) return m + " min " + (reduced ? "" : s + " s");
    return s + " seconds";
  }

  function tick() {
    if (!countdown || isNaN(opensAt)) return;
    countdown.textContent = fmt((opensAt - (Date.now() + skew)) / 1000);
  }

  function admit() {
    if (submitted || !form) return;
    submitted = true;
    form.hidden = false;
    form.submit();
  }

  function poll() {
    fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" }, cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (data) {
        if (!data) { schedule(30); return; }
        if (data.serverTime) skew = Date.parse(data.serverTime) - Date.now();
        if (data.opensAt) opensAt = Date.parse(data.opensAt);
        if (data.state !== box.getAttribute("data-state")) {
          if (data.admit) { admit(); return; }
          window.location.reload();
          return;
        }
        if (data.admit) { admit(); return; }
        if (data.pollAfterSeconds) schedule(data.pollAfterSeconds);
      })
      .catch(function () { schedule(30); });
  }

  function schedule(seconds) {
    window.clearTimeout(timer);
    timer = window.setTimeout(poll, Math.max(2, seconds) * 1000);
  }

  var state = box.getAttribute("data-state");
  if (state === "open" && form && !form.hidden) {
    // Late arrival with a valid registration: enter immediately.
    admit();
    return;
  }
  if (state !== "ended" && state !== "cancelled") {
    tick();
    if (countdown) window.setInterval(tick, reduced ? 15000 : 1000);
    schedule(2);
  }
})();
