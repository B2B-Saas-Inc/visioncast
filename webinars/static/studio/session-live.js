/*
 * Session page live status: polls the session's live.json every 10 seconds and
 * updates the counts and room state in place. The JSON never contains personal
 * data or the join link. No animation; polling pauses while the tab is hidden and
 * stops once the session has ended or was cancelled.
 */
(function () {
  "use strict";

  var MODES = { auto: "Automatic", hold: "Hold", open: "Open now", ended: "Ended" };
  var INTERVAL = 10000;

  function start() {
    var panel = document.getElementById("live-panel");
    if (!panel || !window.fetch) return;
    var url = panel.getAttribute("data-live-url");
    var timer = null;
    var lastState = null;

    function el(name) { return panel.querySelector('[data-live="' + name + '"]'); }
    function setText(name, value) {
      var node = el(name);
      if (node && node.textContent !== String(value)) node.textContent = String(value);
    }
    function time(iso) {
      if (!iso) return "";
      try { return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); } catch (e) { return iso; }
    }

    function apply(data) {
      setText("registered", data.registered);
      setText("checkedIn", data.checkedIn);
      setText("admissionMode", MODES[data.admissionMode] || data.admissionMode);
      var badge = el("stateBadge");
      if (badge) {
        badge.textContent = data.stateLabel || data.state;
        badge.className = "badge badge-" + data.state;
      }
      var summary;
      if (!data.waitingRoomEnabled) summary = "Waiting room is off. Starts at " + time(data.startsAt) + ".";
      else if (data.state === "waiting") summary = "Doors open at " + time(data.opensAt) + ". Session starts at " + time(data.startsAt) + ".";
      else if (data.state === "open") summary = "Doors are open. Session ends at " + time(data.endsAt) + ".";
      else if (data.state === "hold") summary = "Doors are on hold. Attendees keep waiting.";
      else if (data.state === "preparing") summary = "Doors should be open, but there is no join URL yet. Add one in Schedule.";
      else if (data.state === "ended") summary = "The session has ended.";
      else if (data.state === "cancelled") summary = "This session was cancelled.";
      else summary = "";
      // Only announce real changes of state, not every poll.
      if (data.state !== lastState) setText("summary", summary);
      lastState = data.state;
      return data.state !== "ended" && data.state !== "cancelled";
    }

    function poll() {
      timer = null;
      if (document.hidden) return;
      fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" }, cache: "no-store" })
        .then(function (r) { if (!r.ok) throw new Error(r.status); return r.json(); })
        .then(function (data) { if (apply(data)) schedule(); })
        .catch(function () { schedule(INTERVAL * 3); });
    }
    function schedule(delay) {
      if (timer) window.clearTimeout(timer);
      timer = window.setTimeout(poll, delay || INTERVAL);
    }
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden && !timer && lastState !== "ended" && lastState !== "cancelled") poll();
    });
    schedule();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
