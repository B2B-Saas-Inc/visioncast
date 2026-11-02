/* /login and /admin. What's on these pages comes from the API, which checks the session. */
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  function show(el, on) { el.hidden = !on; }
  function h(tag, attrs) {
    var node = document.createElement(tag);
    Object.keys(attrs || {}).forEach(function (key) {
      var value = attrs[key];
      if (key === "class") { node.className = value; }
      else if (key === "text") { node.textContent = value; }
      else if (key.indexOf("on") === 0) { node.addEventListener(key.slice(2), value); }
      else if (value !== false && value !== null && value !== undefined) { node.setAttribute(key, value === true ? "" : value); }
    });
    Array.prototype.slice.call(arguments, 2).forEach(function (child) { if (child) { node.appendChild(child); } });
    return node;
  }

  // ---- /login

  if ($("login-form")) {
    var setting = /^\/login\/set\/([^/]+)$/.exec(window.location.pathname);
    var fail = function (code) {
      var messages = {
        wrong_password: "That email and password don't match.",
        slow_down: "Too many tries. Wait a few minutes.",
        password_too_short: "Use at least 12 characters.",
        link_used: "This link has been used or has expired. Ask an owner for a new one."
      };
      $("login-error").textContent = messages[code] || "That didn't work. Try again.";
      show($("login-error"), true);
    };
    if (setting) {
      document.querySelector(".gate h2").textContent = "Set your password";
      document.querySelector(".gate .muted").textContent = "At least 12 characters. This link works once.";
      show($("email-field"), false);
      $("password-label").textContent = "New password";
      $("password").autocomplete = "new-password";
      $("login-button").textContent = "Set password and sign in";
      $("password").focus();
    }
    $("login-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var send = setting
        ? fetch(window.location.pathname, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: $("password").value }) })
        : fetch("/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email: $("email").value, password: $("password").value }) });
      send.then(function (res) {
        if (res.ok) { window.location.href = "/admin"; return; }
        return res.json().catch(function () { return {}; }).then(function (d) { fail(d.error); });
      }).catch(function () { fail(""); });
    });
    return;
  }

  // ---- /admin

  var MESSAGES = {
    invalid_destination: "Check the server: it has to start rtmp:// or rtmps://. A platform that's turned on needs a server and a key.",
    invalid_input: "Check what you entered and try again.",
    file_type_not_allowed: "Use a PNG.",
    file_too_large: "That file is over 2 MB."
  };

  function api(path, method, body, raw) {
    var options = { method: method || "GET" };
    if (raw) { options.body = raw; }
    else if (body !== undefined) { options.headers = { "Content-Type": "application/json" }; options.body = JSON.stringify(body); }
    return fetch("/admin/api" + path, options).then(function (res) {
      if (res.status === 401) { window.location.href = "/login"; throw new Error("signed out"); }
      return res.json().catch(function () { return {}; }).then(function (d) {
        if (!res.ok) { var err = new Error(d.error); err.code = d.error; throw err; }
        return d;
      });
    });
  }
  function fail(err) {
    if (err.message === "signed out") { return; }
    $("admin-error").textContent = MESSAGES[err.code] || "That didn't save. Try again.";
    show($("admin-error"), true);
    show($("admin-saved"), false);
  }
  function saved(text) {
    show($("admin-error"), false);
    $("admin-saved").textContent = text;
    show($("admin-saved"), true);
  }

  function go(page) {
    ["sessions", "session", "brand", "keys", "accounts", "activity", "me"].forEach(function (name) { show($("page-" + name), name === page); });
    document.querySelectorAll("[data-page]").forEach(function (b) { b.setAttribute("aria-pressed", String(b.dataset.page === page || (page === "session" && b.dataset.page === "sessions"))); });
    show($("admin-error"), false);
    show($("admin-saved"), false);
  }

  var publicUrl = "";

  function sessions() {
    go("sessions");
    api("/rooms").then(function (d) {
      publicUrl = d.publicUrl;
      var page = $("page-sessions");
      page.textContent = "";
      page.appendChild(h("h2", { text: "Sessions" }));
      if (!d.rooms.length) { page.appendChild(h("p", { class: "muted", text: "None yet. Sessions come from the VisionCast studio, when you create a room on a session's Hosting page." })); }
      d.rooms.forEach(function (r) {
        page.appendChild(h("button", { class: "box admin-row", type: "button", "data-room": r.id, onclick: function () { session(r.id); } },
          h("strong", { text: r.title }),
          h("span", { class: "muted", text: publicUrl + "/" + r.slug }),
          h("span", { class: "muted", text: new Date(r.startsAt).toLocaleString() + " · " + ({ not_live: "Not live", live: "Live", ended: "Ended" }[r.phase] || r.phase) })));
      });
    }).catch(fail);
  }

  function copyable(label, value) {
    var field = h("code", { text: value });
    return h("div", { class: "file" }, h("div", {}, h("strong", { text: label }), h("div", {}, field)),
      h("div", { class: "acts" }, h("button", { class: "mini", type: "button", text: "Copy", onclick: function () { navigator.clipboard.writeText(value); saved(label + " copied."); } })));
  }

  function session(id) {
    go("session");
    window.location.hash = id;
    api("/rooms/" + encodeURIComponent(id)).then(function (d) {
      var page = $("page-session");
      page.textContent = "";
      page.appendChild(h("button", { class: "mini", type: "button", text: "All sessions", onclick: function () { window.location.hash = ""; sessions(); } }));
      page.appendChild(h("h2", { text: d.title }));
      page.appendChild(h("div", { class: "box admin-box" }, h("h3", { text: "Where it lives" }),
        h("div", { class: "file" }, h("div", {}, h("strong", { text: "Attendees" }), h("div", {}, h("code", { text: publicUrl + "/" + d.slug }))), h("span", { class: "muted small", text: "Each registrant comes in on their own link, from the waiting room." })),
        h("div", { class: "file" }, h("div", {}, h("strong", { text: "Crew" }), h("div", {}, h("code", { text: publicUrl + "/" + d.slug + "/green-room" }))), h("span", { class: "muted small", text: "Opened with one of the links below." }))));
      page.appendChild(crewBox(id, d.crew));

      var form = h("form", {});
      d.destinations.forEach(function (row) {
        var on = h("input", { type: "checkbox", name: row.platform + "-armed", checked: row.armed });
        var server = h("input", { name: row.platform + "-server", value: row.server, placeholder: "rtmps://", spellcheck: "false" });
        var key = h("input", { type: "password", autocomplete: "off", name: row.platform + "-key", placeholder: row.key === "none" ? "Stream key" : "Leave empty to keep it" });
        var label = { own: "Set for this session", saved: "Using your saved key", none: "Not set" }[row.key];
        var fields = h("div", { class: "dest-fields" }, h("p", { class: "muted small", text: row.help }),
          h("label", { text: "Server" }), server,
          h("label", {}, document.createTextNode("Stream key "), h("span", { class: "muted small", text: "(" + label + ")" })), key,
          row.error ? h("p", { class: "error small", text: row.error }) : null);
        show(fields, row.armed);
        on.addEventListener("change", function () { show(fields, on.checked); });
        form.appendChild(h("div", { class: "dest", "data-dest": row.platform },
          h("label", { class: "check" }, on, document.createTextNode(" " + row.label)), fields));
      });
      form.appendChild(h("button", { class: "btn btn-primary-inline", type: "submit", text: "Save broadcast settings" }));
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        var rows = d.destinations.map(function (row) {
          return { platform: row.platform, armed: form.elements[row.platform + "-armed"].checked, server: form.elements[row.platform + "-server"].value.trim(), key: form.elements[row.platform + "-key"].value.trim() };
        });
        api("/rooms/" + encodeURIComponent(id) + "/destinations", "PUT", { destinations: rows }).then(function () { session(id); saved("Saved. Turned-on platforms start when you go live."); }).catch(fail);
      });
      page.appendChild(h("div", { class: "box admin-box" }, h("h3", { text: "Broadcast to other platforms" }),
        h("p", { class: "muted small", text: "Turned-on platforms start when you go live, from one encode. A platform that fails doesn't affect the others or your audience. Hosts and producers see each one's status in the room's Broadcast tab." }), form));

      var recordings = h("div", { class: "box admin-box" }, h("h3", { text: "Recordings and captions" }));
      if (!d.recordings.length) { recordings.appendChild(h("p", { class: "muted small", text: "Nothing recorded yet." })); }
      d.recordings.forEach(function (r) {
        var name = (r.filename || "").split("/").pop() || "Recording";
        recordings.appendChild(h("div", { class: "file" },
          r.publicUrl ? h("a", { href: r.publicUrl, rel: "noopener noreferrer", target: "_blank", text: name }) : h("span", { text: name }),
          h("span", { class: "muted small", text: r.status === "complete" ? Math.max(1, Math.round(r.durationSeconds / 60)) + " min" : r.status })));
        r.captions.forEach(function (f) {
          var label = f.speaker + " · " + (f.kind === "srt" ? "SRT" : "transcript");
          recordings.appendChild(h("div", { class: "file small" }, f.url ? h("a", { href: f.url, rel: "noopener noreferrer", target: "_blank", text: label }) : h("span", { text: label + ": " + f.filename })));
        });
      });
      page.appendChild(recordings);
    }).catch(function (err) { sessions(); fail(err); });
  }

  // For what can't be undone: the first press asks, the second does it.
  function armed(label, fn, cls) {
    var armedUntil = 0;
    var button = h("button", { class: "mini" + (cls ? " " + cls : ""), type: "button", text: label, onclick: function () {
      if (Date.now() < armedUntil) { armedUntil = 0; button.textContent = label; fn(); return; }
      armedUntil = Date.now() + 4000;
      button.textContent = "Click again to confirm";
      setTimeout(function () { button.textContent = label; }, 4000);
    } });
    return button;
  }

  var myEmail = "";
  var ROLE_HELP = { host: "On stage, runs the show", producer: "Runs the show, never seen or heard", guest: "Comes on stage when brought on" };

  function field(text, control, help) {
    var label = h("label", { for: control.id, text: text });
    return h("div", { class: "field" }, label, control, help ? h("p", { class: "muted small", text: help }) : null);
  }

  function crewActions(room, m, refresh) {
    return h("div", { class: "acts" },
      h("button", { class: "mini", type: "button", text: "Copy link", onclick: function () { navigator.clipboard.writeText(m.url).then(function () { saved("Link for " + m.name + " copied."); }, function () { saved(m.url); }); } }),
      armed("New link", function () {
        api("/rooms/" + encodeURIComponent(room) + "/crew/" + m.id + "/new-link", "POST").then(function () { refresh(); saved("The old link for " + m.name + " no longer works, and they've been put out of the room."); }).catch(fail);
      }),
      armed("Remove", function () {
        api("/rooms/" + encodeURIComponent(room) + "/crew/" + m.id, "DELETE").then(function () { refresh(); saved(m.name + " has been removed."); }).catch(fail);
      }, "danger"));
  }

  function crewBox(room, crew) {
    var box = h("div", { class: "box admin-box", id: "crew-box" }, h("h3", { text: "Crew" }),
      h("p", { class: "muted small", text: "Everyone gets their own link. Anyone with a link can act as that person, so send it only to them. You can replace or remove one without touching anyone else's." }));
    crew.forEach(function (m) {
      box.appendChild(h("div", { class: "file", "data-member": m.name },
        h("div", {}, h("strong", { text: m.name }), document.createTextNode(" "), h("span", { class: "badge " + m.role, text: m.role }),
          m.title ? h("div", { class: "muted small", text: m.title }) : null),
        crewActions(room, m, function () { session(room); })));
    });
    var name = h("input", { id: "crew-name", maxlength: "80", required: true });
    var title = h("input", { id: "crew-title", maxlength: "80" });
    var role = h("select", { id: "crew-role" });
    Object.keys(ROLE_HELP).forEach(function (r) { role.appendChild(h("option", { value: r, text: r.charAt(0).toUpperCase() + r.slice(1) })); });
    var roleHelp = h("p", { class: "muted small", text: ROLE_HELP[role.value] + "." });
    role.addEventListener("change", function () { roleHelp.textContent = ROLE_HELP[role.value] + "."; });
    var form = h("form", { id: "crew-form", class: "admin-form" }, h("h4", { text: "Add someone" }), field("Name", name), field("Title (optional)", title, "Shown on stage."), h("div", { class: "field" }, h("label", { for: "crew-role", text: "Role" }), role, roleHelp), h("button", { class: "btn btn-primary-inline", type: "submit", text: "Add" }));
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      api("/rooms/" + encodeURIComponent(room) + "/crew", "POST", { name: name.value, title: title.value, role: role.value }).then(function () { session(room); saved("Added. Copy their link and send it to them."); }).catch(fail);
    });
    box.appendChild(form);
    return box;
  }

  var PEOPLE_ROLES = [
    ["owner", "Owner", "Signs in here, and can also manage people."],
    ["admin", "Admin", "Signs in here, and manages sessions and settings."],
    ["host", "Host", ROLE_HELP.host + ". Joins one session by link."],
    ["producer", "Producer", ROLE_HELP.producer + ". Joins one session by link."],
    ["guest", "Guest", ROLE_HELP.guest + ". Joins one session by link."]
  ];

  function accountRow(a) {
    return h("div", { class: "file", "data-account": a.email },
      h("div", {}, h("strong", { text: a.email }), document.createTextNode(" "), h("span", { class: "badge " + (a.role === "owner" ? "host" : "guest"), text: a.role }),
        h("div", { class: "muted small", text: (a.hasPassword ? "" : "No password yet. ") + (a.lastLoginAt ? "Last signed in " + new Date(a.lastLoginAt).toLocaleString() : "Hasn't signed in yet") })),
      h("div", { class: "acts" },
        // Your own password is changed under Your account: this would sign you out and lose the link.
        a.email === myEmail ? null : armed("Reset password", function () {
          api("/accounts/" + a.id + "/reset", "POST").then(function (r) { accounts(); showLink(a.email, r.setPasswordUrl); }).catch(fail);
        }),
        a.email === myEmail ? null : armed("Remove", function () {
          api("/accounts/" + a.id, "DELETE").then(function () { accounts(); saved(a.email + " has been removed and signed out."); }).catch(function (err) {
            if (err.code === "last_owner") { $("admin-error").textContent = "There has to be at least one owner."; show($("admin-error"), true); } else { fail(err); }
          });
        }, "danger")));
  }

  function accounts() {
    go("accounts");
    Promise.all([api("/accounts"), api("/rooms")]).then(function (results) {
      var d = results[0], rooms = results[1].rooms;
      var page = $("page-accounts");
      page.textContent = "";
      var box = h("div", { class: "box admin-box" }, h("h2", { text: "People" }),
        h("p", { class: "muted", text: "Owners and admins sign in here. Hosts, producers and guests don't: each has their own link to one session. There's no sign-up, so add people here." }),
        h("h3", { text: "Sign in here" }));
      d.accounts.forEach(function (a) { box.appendChild(accountRow(a)); });
      box.appendChild(h("h3", { text: "On a session" }));
      if (!d.crew.length) { box.appendChild(h("p", { class: "muted small", text: "No hosts, producers or guests yet." })); }
      d.crew.forEach(function (m) {
        box.appendChild(h("div", { class: "file", "data-member": m.name },
          h("div", {}, h("strong", { text: m.name }), document.createTextNode(" "), h("span", { class: "badge " + m.role, text: m.role }),
            h("div", { class: "muted small", text: [m.title, m.roomTitle].filter(Boolean).join(" · ") })),
          crewActions(m.room, m, accounts)));
      });

      var role = h("select", { id: "person-role" });
      PEOPLE_ROLES.forEach(function (r) { role.appendChild(h("option", { value: r[0], text: r[1] })); });
      role.value = "admin";
      var roleHelp = h("p", { class: "muted small", id: "person-role-help" });
      var room = h("select", { id: "person-room" });
      rooms.forEach(function (r) { room.appendChild(h("option", { value: r.id, text: r.title })); });
      var email = h("input", { id: "account-email", type: "email", maxlength: "254" });
      var name = h("input", { id: "account-name", maxlength: "80" });
      var title = h("input", { id: "person-title", maxlength: "80" });
      var roomField = field("Session", room), titleField = field("Title (optional)", title, "Shown on stage.");
      var emailField = field("Email", email), nameField = field("Name", name);
      var noSessions = h("p", { class: "muted small", text: "There are no sessions yet. Create one in the studio first." });
      var submit = h("button", { class: "btn btn-primary-inline", type: "submit", text: "Add" });
      function sync() {
        var crew = !!ROLE_HELP[role.value];
        roleHelp.textContent = PEOPLE_ROLES.filter(function (r) { return r[0] === role.value; })[0][2];
        show(roomField, crew && rooms.length > 0);
        show(titleField, crew);
        show(noSessions, crew && !rooms.length);
        submit.disabled = crew && !rooms.length;
        email.required = !crew;
        name.required = crew;
        emailField.querySelector("label").textContent = crew ? "Email (optional)" : "Email";
        nameField.querySelector("label").textContent = crew ? "Name" : "Name (optional)";
      }
      role.addEventListener("change", sync);
      sync();
      var form = h("form", { class: "admin-form", id: "person-form" }, h("h3", { text: "Add someone" }), h("div", { class: "field" }, h("label", { for: "person-role", text: "Role" }), role, roleHelp), roomField, noSessions, nameField, emailField, titleField, submit);
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        if (ROLE_HELP[role.value]) {
          api("/rooms/" + encodeURIComponent(room.value) + "/crew", "POST", { name: name.value, title: title.value, email: email.value, role: role.value }).then(function (m) {
            accounts();
            saved("Send this link to " + m.name + ", and only to them: " + m.url);
          }).catch(fail);
          return;
        }
        api("/accounts", "POST", { email: email.value, name: name.value, role: role.value }).then(function (r) { accounts(); showLink(r.account.email, r.setPasswordUrl); }).catch(function (err) {
          if (err.code === "account_exists") { $("admin-error").textContent = "That person already has an account."; show($("admin-error"), true); } else { fail(err); }
        });
      });
      box.appendChild(form);
      page.appendChild(box);
    }).catch(fail);
  }

  function showLink(email, url) {
    saved("Send this link to " + email + ". It works once, for 3 days: " + url);
  }

  function activity() {
    go("activity");
    api("/activity").then(function (d) {
      var page = $("page-activity");
      page.textContent = "";
      var box = h("div", { class: "box admin-box" }, h("h2", { text: "Activity" }), h("p", { class: "muted", text: "Who did what, newest first." }));
      d.activity.forEach(function (row) {
        box.appendChild(h("div", { class: "file small", "data-activity": row.action }, h("div", {}, h("strong", { text: row.actor }), document.createTextNode(" " + row.action + (row.detail ? ": " + row.detail : ""))), h("span", { class: "muted", text: new Date(row.at).toLocaleString() })));
      });
      page.appendChild(box);
    }).catch(fail);
  }

  function me() {
    go("me");
    api("/me").then(function (d) {
      var page = $("page-me");
      page.textContent = "";
      var current = h("input", { id: "me-current", type: "password", autocomplete: "current-password" });
      var next = h("input", { id: "me-new", type: "password", autocomplete: "new-password", required: true });
      var form = h("form", {}, d.hasPassword ? h("label", { for: "me-current", text: "Current password" }) : null, d.hasPassword ? current : null,
        h("label", { for: "me-new", text: "New password, at least 12 characters" }), next, h("button", { class: "btn btn-primary-inline", type: "submit", text: "Change password" }));
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        api("/me/password", "POST", { current: current.value, newPassword: next.value }).then(function () { me(); saved("Changed. You've been signed out everywhere else."); }).catch(function (err) {
          $("admin-error").textContent = { wrong_password: "That's not your current password.", password_too_short: "Use at least 12 characters." }[err.code] || "That didn't save. Try again.";
          show($("admin-error"), true);
        });
      });
      page.appendChild(h("div", { class: "box admin-box" }, h("h2", { text: "Your account" }), h("p", { class: "muted", text: d.email + " · " + d.role }), form));
    }).catch(fail);
  }

  function brand() {
    go("brand");
    api("/brand").then(function (d) {
      $("b-accent").value = d.accent || "#c2462e";
      $("b-stage").value = d.stage || "#0d0c0a";
      $("b-mode").value = d.mode === "dark" ? "dark" : "";
      $("b-font").value = d.font || "";
      show($("b-logo"), !!d.logo);
      show($("b-logo-remove"), !!d.logo);
      show($("b-font-remove"), !!d.fontFile);
      show($("b-font-custom"), !!d.fontFile);
      $("b-font-state").textContent = d.fontFile ? "Your font is uploaded. Choose it above, under Font." : "No font uploaded.";
      if (d.logo) { $("b-logo").src = "/brand/logo?v=" + Date.now(); }
      $("brand-form").dataset.loaded = "1";
    }).catch(fail);
  }
  $("brand-form").addEventListener("submit", function (event) {
    event.preventDefault();
    api("/brand", "PUT", { accent: $("b-accent").value, stage: $("b-stage").value, mode: $("b-mode").value, font: $("b-font").value }).then(function () { saved("Saved. Open rooms have it now."); }).catch(fail);
  });
  $("b-logo-file").addEventListener("change", function () {
    var input = this;
    if (!input.files[0]) { return; }
    api("/brand/logo", "PUT", undefined, input.files[0]).then(function () { input.value = ""; brand(); saved("Logo saved."); }).catch(fail);
  });
  $("b-font-file").addEventListener("change", function () {
    var input = this;
    fetch("/admin/api/brand/font", { method: "PUT", body: input.files[0] }).then(function (res) {
      input.value = "";
      if (res.status === 401) { window.location.href = "/login"; return; }
      if (!res.ok) { $("admin-error").textContent = "That file isn't a font we can use, or it's over 8 MB."; show($("admin-error"), true); return; }
      brand();
      saved("Font uploaded. Choose it under Font and save.");
    }).catch(fail);
  });
  $("b-font-remove").addEventListener("click", function () { api("/brand/font/remove", "POST").then(function () { brand(); saved("Font removed."); }).catch(fail); });
  $("b-logo-remove").addEventListener("click", function () { api("/brand/logo/remove", "POST").then(function () { brand(); saved("Logo removed."); }).catch(fail); });

  function keys() {
    go("keys");
    api("/saved-destinations").then(function (d) {
      var page = $("page-keys");
      page.textContent = "";
      var form = h("form", {});
      d.destinations.forEach(function (row) {
        form.appendChild(h("div", { class: "dest", "data-saved": row.platform },
          h("strong", { text: row.label }), document.createTextNode(" "), h("span", { class: "muted small", text: row.keySet ? "(Key saved)" : "(No key)" }),
          h("p", { class: "muted small", text: row.help }),
          h("label", { text: "Server" }), h("input", { name: row.platform + "-server", value: row.server, placeholder: "rtmps://", spellcheck: "false" }),
          h("label", { text: "Stream key" }), h("input", { type: "password", autocomplete: "off", name: row.platform + "-key", placeholder: row.keySet ? "Leave empty to keep it" : "Stream key" }),
          row.keySet ? h("label", { class: "check" }, h("input", { type: "checkbox", name: row.platform + "-clear" }), document.createTextNode(" Remove the saved key")) : null));
      });
      form.appendChild(h("button", { class: "btn btn-primary-inline", type: "submit", text: "Save keys" }));
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        var rows = d.destinations.map(function (row) {
          var clear = form.elements[row.platform + "-clear"];
          return { platform: row.platform, server: form.elements[row.platform + "-server"].value.trim(), key: form.elements[row.platform + "-key"].value.trim(), clearKey: !!(clear && clear.checked) };
        });
        api("/saved-destinations", "PUT", { destinations: rows }).then(function () { keys(); saved("Saved."); }).catch(fail);
      });
      page.appendChild(h("div", { class: "box admin-box" }, h("h2", { text: "Saved stream keys" }),
        h("p", { class: "muted", text: "Optional. For keys you reuse across sessions. You still turn a platform on per session. LinkedIn issues a new key for each stream, so it's set per session." }), form));
    }).catch(fail);
  }

  document.querySelectorAll("[data-page]").forEach(function (b) {
    b.addEventListener("click", function () {
      window.location.hash = "";
      ({ sessions: sessions, brand: brand, keys: keys, accounts: accounts, activity: activity, me: me })[b.dataset.page]();
    });
  });
  $("sign-out").addEventListener("click", function () {
    fetch("/logout", { method: "POST" }).then(function () { window.location.href = "/login"; });
  });

  api("/me").then(function (who) {
    myEmail = who.email;
    $("who").textContent = who.email;
    show($("who"), true);
    show($("nav-accounts"), who.role === "owner");
    return api("/rooms");
  }).then(function (d) {
    publicUrl = d.publicUrl;
    show($("main"), true);
    var wanted = window.location.hash.slice(1);
    if (wanted) { session(wanted); } else { sessions(); }
  }).catch(function (err) {
    if (err.message === "signed out") { return; }
    show($("main"), true);
    $("admin-error").textContent = "This page couldn't load. Check your connection and reload it.";
    show($("admin-error"), true);
  });
})();
