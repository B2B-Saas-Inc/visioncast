/*
 * Registration form builder enhancements. Without JavaScript the server renders the
 * existing rows plus two empty rows (empty rows are ignored on save). With it, editors
 * can add, remove and reorder rows; every change renumbers the indexed input names
 * (rows-N-key, rows-N-label, ...) and updates the hidden rows-count, and is announced
 * through window.studioAnnounce.
 */
(function () {
  "use strict";

  var NAME_RE = /rows-(\d+|__N__)-/g;
  var ROW_ID_RE = /^row-(\d+|__N__)$/;
  var ATTRS = ["id", "name", "for", "aria-describedby", "aria-labelledby"];

  function announce(text) { if (window.studioAnnounce) window.studioAnnounce(text); }

  function init(form) {
    var list = form.querySelector("[data-rows]");
    var countInput = form.querySelector("[data-rows-count]");
    var template = form.querySelector("[data-row-template]");
    var addButton = form.querySelector("[data-add-row]");
    if (!list || !countInput || !template || !addButton) return; // read-only page

    var hint = form.querySelector("[data-no-js-hint]");
    if (hint) hint.hidden = true;
    addButton.hidden = false;

    function rows() { return Array.prototype.slice.call(list.querySelectorAll(":scope > [data-row]")); }

    function renumber() {
      var all = rows();
      all.forEach(function (row, index) {
        var n = index + 1;
        if (ROW_ID_RE.test(row.id)) row.id = "row-" + index;
        Array.prototype.forEach.call(row.querySelectorAll("*"), function (el) {
          ATTRS.forEach(function (attr) {
            var value = el.getAttribute(attr);
            if (value && value.indexOf("rows-") !== -1) el.setAttribute(attr, value.replace(NAME_RE, "rows-" + index + "-"));
          });
        });
        var title = row.querySelector("[data-row-title]");
        if (title) title.textContent = "Field " + n;
        var up = row.querySelector('[data-row-action="up"]');
        var down = row.querySelector('[data-row-action="down"]');
        var remove = row.querySelector('[data-row-action="remove"]');
        if (up) { up.setAttribute("aria-label", "Move field " + n + " up"); up.disabled = index === 0; }
        if (down) { down.setAttribute("aria-label", "Move field " + n + " down"); down.disabled = index === all.length - 1; }
        if (remove) remove.setAttribute("aria-label", "Remove field " + n);
      });
      countInput.value = String(all.length);
    }

    function syncCustom(row) {
      var select = row.querySelector("[data-row-key]");
      var custom = row.querySelector("[data-row-custom]");
      var options = row.querySelector("[data-row-options]");
      var type = row.querySelector('select[name$="-type"]');
      var isCustom = !!select && select.value === "__custom__";
      if (custom) custom.hidden = !isCustom;
      if (options && select) {
        options.hidden = !(select.value === "companySize" || (isCustom && type && type.value === "select"));
      }
    }

    function label(row) {
      var input = row.querySelector('input[name$="-label"]');
      return (input && input.value.trim()) || "empty field";
    }

    function wire(row) {
      var tools = row.querySelector("[data-row-tools]");
      if (tools) tools.hidden = false;
      syncCustom(row);
    }

    rows().forEach(wire);
    renumber();

    list.addEventListener("change", function (event) {
      if (event.target.matches('[data-row-key], select[name$="-type"]')) syncCustom(event.target.closest("[data-row]"));
    });

    list.addEventListener("click", function (event) {
      var button = event.target.closest("[data-row-action]");
      if (!button) return;
      var row = button.closest("[data-row]");
      var action = button.getAttribute("data-row-action");
      var name = label(row);
      if (action === "up" && row.previousElementSibling) {
        list.insertBefore(row, row.previousElementSibling);
        renumber();
        announce("Moved " + name + " up to position " + (rows().indexOf(row) + 1));
        button.disabled ? row.querySelector('[data-row-action="down"]').focus() : button.focus();
      } else if (action === "down" && row.nextElementSibling) {
        list.insertBefore(row.nextElementSibling, row);
        renumber();
        announce("Moved " + name + " down to position " + (rows().indexOf(row) + 1));
        button.disabled ? row.querySelector('[data-row-action="up"]').focus() : button.focus();
      } else if (action === "remove") {
        var next = row.nextElementSibling || row.previousElementSibling;
        row.parentNode.removeChild(row);
        renumber();
        announce("Removed " + name);
        var focusTarget = next ? next.querySelector("[data-row-key]") : addButton;
        if (focusTarget) focusTarget.focus();
      }
    });

    addButton.addEventListener("click", function () {
      var fragment = template.content.cloneNode(true);
      var row = fragment.querySelector("[data-row]");
      list.appendChild(fragment);
      wire(row);
      renumber();
      announce("Added field " + rows().length);
      var select = row.querySelector("[data-row-key]");
      if (select) select.focus();
    });
  }

  function start() {
    Array.prototype.forEach.call(document.querySelectorAll("[data-form-builder]"), init);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
