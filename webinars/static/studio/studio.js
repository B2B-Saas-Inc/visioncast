/*
 * Studio shell behaviour shared by every page. No dependencies.
 *
 *  - Mobile navigation drawer (button[data-nav-toggle]).
 *  - Copy buttons: <button data-copy="#element-id">Copy</button> copies the element's
 *    text (or value) and announces the result in a polite live region.
 *  - Confirmations: <form data-confirm="Cancel this session?"> asks before submitting.
 *  - Character counters: <textarea data-maxlength="300"> gets a live count beneath it.
 */
(function () {
  "use strict";

  var live = document.createElement("div");
  live.className = "visually-hidden";
  live.setAttribute("aria-live", "polite");
  live.setAttribute("role", "status");
  document.addEventListener("DOMContentLoaded", function () { document.body.appendChild(live); });
  function announce(text) { live.textContent = ""; window.setTimeout(function () { live.textContent = text; }, 30); }
  window.studioAnnounce = announce;

  // Navigation drawer
  document.addEventListener("click", function (event) {
    var toggle = event.target.closest("[data-nav-toggle]");
    var nav = document.getElementById("studio-nav");
    var scrim = document.querySelector("[data-scrim]");
    if (!nav) return;
    if (toggle) {
      var open = nav.getAttribute("data-open") !== "true";
      nav.setAttribute("data-open", String(open));
      toggle.setAttribute("aria-expanded", String(open));
      if (scrim) { scrim.hidden = !open; scrim.setAttribute("data-open", String(open)); }
      if (open) { var first = nav.querySelector("a"); if (first) first.focus(); }
    } else if (event.target.closest("[data-scrim]")) {
      nav.setAttribute("data-open", "false");
      if (scrim) { scrim.hidden = true; scrim.setAttribute("data-open", "false"); }
      var t = document.querySelector("[data-nav-toggle]");
      if (t) { t.setAttribute("aria-expanded", "false"); t.focus(); }
    }
  });
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    var nav = document.getElementById("studio-nav");
    if (nav && nav.getAttribute("data-open") === "true") {
      nav.setAttribute("data-open", "false");
      var scrim = document.querySelector("[data-scrim]");
      if (scrim) { scrim.hidden = true; scrim.setAttribute("data-open", "false"); }
      var t = document.querySelector("[data-nav-toggle]");
      if (t) { t.setAttribute("aria-expanded", "false"); t.focus(); }
    }
  });

  // Copy to clipboard
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-copy]");
    if (!button) return;
    var target = document.querySelector(button.getAttribute("data-copy"));
    if (!target) return;
    var text = "value" in target && target.value !== undefined ? target.value : target.textContent;
    text = (text || "").trim();
    var done = function () { announce("Copied to clipboard"); var old = button.textContent; button.textContent = "Copied"; window.setTimeout(function () { button.textContent = old; }, 1600); };
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(done, function () { announce("Copy failed. Select the text and copy it manually."); });
    } else {
      var range = document.createRange();
      range.selectNodeContents(target);
      var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
      try { document.execCommand("copy"); done(); } catch (e) { announce("Select the text and copy it manually."); }
    }
  });

  // Integration form: choosing a native provider hides the webhook-only fields at once; on the
  // create page (form[data-provider-reload]) the page reloads with ?provider= so the provider's
  // settings and credential boxes appear before anything is saved.
  document.addEventListener("change", function (event) {
    var select = event.target;
    if (!select || select.id !== "f-provider") return;
    var form = select.form;
    var native = !!select.value;
    Array.prototype.forEach.call(document.querySelectorAll("[data-webhook-only]"), function (el) { el.hidden = native; });
    Array.prototype.forEach.call(document.querySelectorAll("[data-native-only]"), function (el) { el.hidden = !native; });
    if (form && form.hasAttribute("data-provider-reload")) {
      var params = new URLSearchParams();
      if (select.value) params.set("provider", select.value);
      var name = form.querySelector("[name=name]");
      if (name && name.value) params.set("name", name.value);
      var query = params.toString();
      window.location.href = window.location.pathname + (query ? "?" + query : "");
    }
  });

  // Confirm before destructive submits
  document.addEventListener("submit", function (event) {
    var form = event.target;
    var message = form.getAttribute && form.getAttribute("data-confirm");
    if (message && !window.confirm(message)) event.preventDefault();
  }, true);

  // Character counters
  function wireCounter(field) {
    var max = parseInt(field.getAttribute("data-maxlength"), 10);
    if (!max || field.dataset.counterWired) return;
    field.dataset.counterWired = "1";
    var counter = document.createElement("p");
    counter.className = "char-count";
    counter.setAttribute("aria-live", "polite");
    field.insertAdjacentElement("afterend", counter);
    var update = function () {
      var left = max - field.value.length;
      counter.textContent = left >= 0 ? left + " characters left" : Math.abs(left) + " characters over";
      counter.style.color = left < 0 ? "var(--danger-700)" : "";
    };
    field.addEventListener("input", update);
    update();
  }
  document.addEventListener("DOMContentLoaded", function () {
    Array.prototype.forEach.call(document.querySelectorAll("[data-maxlength]"), wireCounter);
    var summary = document.querySelector(".error-summary");
    if (summary) { if (!summary.hasAttribute("tabindex")) summary.setAttribute("tabindex", "-1"); summary.focus(); }
  });
})();
