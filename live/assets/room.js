/* The webinar room. One page, four people: hosts and producers run the show, guests present,
   attendees watch. This page only ever asks: the service decides who may do what, and LiveKit
   enforces who may publish. Text from other people only reaches the page as textContent. */
(function () {
  "use strict";
  var LK = window.LivekitClient;
  var program = window.location.pathname.indexOf("/program/") === 0;
  // The signed link arrives in the fragment, which no server or referrer ever sees, and is taken out
  // of the address bar right away. This tab remembers it, so a reload works and the address stays clean.
  var slug = program ? "" : window.location.pathname.split("/")[1];
  var ticketKey = "vc-ticket-" + slug;
  var ticket = "";
  if (program) {
    ticket = window.location.pathname.replace(/^\/program\//, "").replace(/\/+$/, "");
  } else {
    // Only what a signed link looks like: nothing that could point these requests somewhere else.
    var fragment = /^#t=([A-Za-z0-9._~-]+)$/.exec(window.location.hash);
    if (fragment) {
      ticket = fragment[1];
      sessionStorage.setItem(ticketKey, ticket);
      history.replaceState(null, "", window.location.pathname);
    } else {
      ticket = sessionStorage.getItem(ticketKey) || "";
    }
  }
  var base = "/join/" + ticket;
  document.documentElement.dataset.base = base;
  var $ = function (id) { return document.getElementById(id); };

  var clientKey = "vc-client-" + ticket.slice(-12);
  var clientId = sessionStorage.getItem(clientKey);
  if (!clientId) {
    clientId = Array.from(crypto.getRandomValues(new Uint8Array(10))).map(function (b) { return b.toString(36).padStart(2, "0"); }).join("").slice(0, 16);
    sessionStorage.setItem(clientKey, clientId);
  }

  var info = null;
  var S = { me: null, state: null, chat: [], qa: {}, polls: {}, files: [], people: [], viewers: 0, myVotes: {}, recordingAvailable: false };
  var source = null, stageRoom = null, greenRoom = null, shareRoom = null, stage = null;
  var serverOffset = 0;
  var want = { mic: false, cam: false, micDevice: "", camDevice: "" };
  var onStageNow = false, switching = false;
  var greenAudio = {};
  var tab = "chat", channel = "event", unread = {};
  var scheduled = false;
  var myName = "", myTitle = "", previewStream = null, previewTurn = 0;

  var actionFailed = false;
  function isCrew() { return S.me && S.me.perms.publish; }
  function runs() { return S.me && S.me.perms.runShow; }
  function show(el, on) { el.hidden = !on; }
  function setError(el, text) { el.textContent = text || ""; show(el, !!text); }

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

  var MESSAGES = {
    not_open_yet: "This room isn't open yet. Come back closer to the start time.",
    session_over: "This session has ended.",
    room_unavailable: "The room couldn't be prepared. Try again in a moment.",
    recording_failed: "Nothing is being recorded: the recorder didn't start. This server may be too small or too busy.",
    recording_not_configured: "Recording isn't set up on this server.",
    not_allowed: "Your role can't do that.",
    removed: "You've been removed from this session.",
    slow_down: "That was quick. Wait a moment and try again.",
    chat_off: "Chat is off.",
    qa_off: "Questions are off.",
    poll_closed: "This poll is closed.",
    not_connected: "They've lost their connection. Wait for them to come back.",
    cannot_be_on_stage: "Producers can't go on stage.",
    not_on_stage: "You can share your screen once you're on stage.",
    file_type_not_allowed: "That file type can't be used here.",
    file_too_large: "That file is too large.",
    invalid_input: "Check what you entered and try again.",
    in_breakout: "They're in a breakout room. Ask them to come back first.",
    time_passed: "That time has already passed. Choose one in the future.",
    room_full: "That room is full. Try another one.",
    on_stage: "Leave the stage before going to a breakout room.",
    already_answered: "You've already answered this.",
    not_available: "That isn't available right now.",
    ingress_not_configured: "RTMP input isn't set up on this server."
  };
  function message(err) { return MESSAGES[err && err.code] || "Something went wrong. Check your connection and try again."; }

  function request(path, body, method) {
    var options = body === undefined ? {} : { method: method || "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
    return fetch(base + path, options).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) {
        if (!res.ok) { var err = new Error(data.error || "request_failed"); err.code = data.error; throw err; }
        return data;
      });
    });
  }
  function act(action) {
    return request("/act", { clientId: clientId, action: action }).then(function (data) {
      if (actionFailed) { actionFailed = false; setError($("room-error"), ""); }
      return data;
    }, function (err) {
      actionFailed = true;
      setError($("room-error"), message(err));
      throw err;
    });
  }
  function quietly(action) { act(action).catch(function () {}); }
  function fileUrl(id) { return base + "/files/" + encodeURIComponent(id) + (program ? "" : "?c=" + clientId); }

  function pageUrl(id, page) { return base + "/files/" + encodeURIComponent(id) + "/pages/" + page; }

  // A button replaced between pressing and letting go loses the click, so nothing is redrawn until then.
  var pointerDown = false, renderWaiting = false;
  document.addEventListener("pointerdown", function () { pointerDown = true; }, true);
  function pointerUp() {
    pointerDown = false;
    if (renderWaiting) { renderWaiting = false; setTimeout(schedule, 0); }
  }
  document.addEventListener("pointerup", pointerUp, true);
  document.addEventListener("pointercancel", pointerUp, true);
  function schedule() {
    if (scheduled) { return; }
    scheduled = true;
    requestAnimationFrame(function () {
      scheduled = false;
      if (pointerDown) { renderWaiting = true; return; }
      keepingFocus(render);
    });
  }
  // Lists are redrawn as things change. Someone on a keyboard stays on the same control.
  function keepingFocus(draw) {
    var active = document.activeElement;
    var holder = active && active.tagName === "BUTTON" ? active.closest("[data-id],[data-poll],[data-file],[data-person],[data-dest],[data-layer],[data-tab]") : null;
    var mark = holder ? { attrs: Array.from(holder.attributes).filter(function (a) { return a.name.indexOf("data-") === 0; })[0], text: active.textContent } : null;
    draw();
    if (!mark || !mark.attrs || document.activeElement === active && active.isConnected) { return; }
    var found = document.querySelector("[" + mark.attrs.name + '="' + CSS.escape(mark.attrs.value) + '"]');
    if (!found) { return; }
    var again = found.tagName === "BUTTON" ? found : Array.from(found.querySelectorAll("button")).filter(function (b) { return b.textContent === mark.text; })[0];
    if (again) { again.focus(); }
  }

  // ---- events from the service

  function open() {
    source = new EventSource(base + "/events");
    function on(name, fn, quiet) { source.addEventListener(name, function (e) { fn(JSON.parse(e.data)); if (!quiet) { schedule(); } }); }
    on("snapshot", function (d) {
      S.me = d.me;
      S.chat = d.chat;
      S.qa = {}; d.qa.forEach(function (q) { S.qa[q.id] = q; });
      S.polls = {}; d.polls.forEach(function (p) { S.polls[p.id] = p; });
      setState(d.state);
      S.files = d.files;
      S.broadcast = d.broadcast || [];
      S.people = d.people || [];
      S.viewers = d.viewers || 0;
      S.recordingAvailable = !!d.recordingAvailable;
      S.myVotes = {}; d.myVotes.forEach(function (v) { S.myVotes[v[0]] = v[1]; });
      S.chatReactions = d.chatReactions || {};
      S.breakoutCounts = d.breakoutCounts || {};
      S.surveyDone = !!d.surveyDone;
      S.assigned = d.assigned || null;
      S.removedPeople = d.removedPeople || [];
      eventFailures = 0;
      // The service restarted and has forgotten who this is, or which language they wanted.
      if (d.me.named === false && myName) { request("/enter", { name: myName, title: myTitle }).catch(function () {}); }
      if (captionLanguage) { chooseLanguage(captionLanguage); }
      if (d.me.invited) { show($("invite-modal"), true); }
      syncToggle($("hand"), !!d.me.hand, "Lower hand", "Raise hand");
    });
    on("state", setState);
    on("brand", function (b) { S.state.brand = b; applyTheme(); });
    on("broadcast", function (rows) { S.broadcast = rows; });
    on("chat", function (m) {
      var i = S.chat.findIndex(function (x) { return x.id === m.id; });
      if (i >= 0) { S.chat[i] = m; } else {
        S.chat.push(m);
        if (tab !== "chat") { unread.chat = true; }
        if (m.channel === channel && m.approved) { $("chat-announce").textContent = m.name + ": " + m.text; }
      }
    });
    on("chat_reactions", function (d) { S.chatReactions[d.id] = d.counts; });
    on("breakout_counts", function (d) { S.breakoutCounts = d; });
    on("chat_remove", function (d) { S.chat = S.chat.filter(function (m) { return m.id !== d.id; }); });
    on("pin", function (m) { S.pinnedMessage = m; });
    on("qa", function (q) { if (!S.qa[q.id] && tab !== "qa") { unread.qa = true; } S.qa[q.id] = q; });
    on("qa_remove", function (d) { delete S.qa[d.id]; });
    on("poll", function (p) {
      var previous = S.polls[p.id];
      if ((!previous || previous.status === "draft") && p.status === "open" && tab !== "polls") { unread.polls = true; }
      S.polls[p.id] = p;
      if (S.state && S.state.stagePoll === p.id) { stage.setPoll(p); }
    });
    on("poll_remove", function (d) { delete S.polls[d.id]; });
    on("files", function (f) { S.files = f; });
    on("file_stat", function (d) { S.files.forEach(function (f) { if (f.id === d.id) { f.downloads = d.downloads; } }); });
    on("people", function (p) { S.people = p; });
    on("viewers", function (n) { S.viewers = n; $("viewers-pill").textContent = n + (n === 1 ? " attendee" : " attendees"); }, true);
    on("react", function (d) { if (!program && stage) { stage.float(d.emoji); } }, true);
    on("caption", function (d) { if (stage && !captionLanguage) { stage.caption(d.name, d.text); } S.speakersStale = true; }, true);
    on("caption_tr", function (d) { if (stage && d.language === captionLanguage) { stage.caption(d.name, d.text); } }, true);
    on("me", function (d) {
      if (d.blocked !== undefined) { S.me.blocked = d.blocked; }
      if (d.invited !== undefined) { S.me.invited = d.invited; show($("invite-modal"), d.invited); }
      if (d.assigned !== undefined) { S.assigned = d.assigned; }
    });
    on("demoted", function (d) { if (S.me && d.identity === S.me.identity) { sessionStorage.setItem("vc-rejoin", "1"); window.location.reload(); } });
    on("removed", function (d) { if (S.me && d.identity === S.me.identity) { leave("You've been removed from this session."); } });
    on("invite_result", function () {});
    source.onerror = function () {
      if (source && source.readyState === EventSource.CLOSED) { reopenEvents(); }
    };
  }

  // A browser gives up on an event stream that gets one bad answer, like during an upgrade. This doesn't,
  // unless the link has really stopped working.
  var eventFailures = 0;
  function reopenEvents() {
    if (source) { source.close(); source = null; }
    eventFailures += 1;
    var wait = Math.min(30000, 1000 * Math.pow(2, eventFailures));
    setError($("room-error"), "Reconnecting to the session.");
    setTimeout(function () {
      fetch(base + "/info").then(function (res) {
        if (res.status === 404) { leave("This link no longer works."); return; }
        if (res.ok) { setError($("room-error"), ""); open(); } else { reopenEvents(); }
      }).catch(reopenEvents);
    }, wait);
  }

  function setState(next) {
    var previous = S.state;
    if (next.serverMs) { serverOffset = next.serverMs - Date.now(); }
    if (!next.brand && S.state) { next.brand = S.state.brand; }
    S.state = next;
    applyTheme();
    if (stage) { stage.setState(next, serverOffset); }
    if (S.me) { S.me.perms.goLive = S.me.role === "host" || (S.me.role === "producer" && next.settings.producersCanGoLive); }
    stage.setPoll(next.stagePoll ? S.polls[next.stagePoll] || null : null);
    if (program) {
      stage.setCaptionsOn(!!next.settings.captionsOnPicture);
      if (next.phase === "ended") { console.log("END_RECORDING"); }
      return;
    }
    if (breakoutRoom && !next.breakouts.some(function (r) { return r.id === breakoutId; })) { leaveBreakout("The breakout rooms have closed."); }
    if (!isCrew() && (!previous || previous.phase !== next.phase)) { audiencePhase(next.phase); }
    syncAddress();
    paintCaptionButton();
    if (isCrew()) { syncPosition(); syncLocalRecording(); }
  }

  // ---- the address, and captions

  function syncAddress() {
    if (program || !slug || !S.state || !S.me) { return; }
    var wanted = isCrew() ? "/" + slug + "/green-room" : S.state.phase === "live" ? "/" + slug : "/" + slug + "/lobby";
    if (window.location.pathname !== wanted) { history.replaceState(null, "", wanted); }
  }

  var captionsWanted = localStorage.getItem("vc-captions") === "on";
  function paintCaptionButton() {
    if (!S.state) { return; }
    var usable = !!S.state.captionsAvailable && S.state.settings.captions && S.state.phase === "live";
    var buttons = [$("cc"), $("cc-crew")];
    buttons.forEach(function (b) {
      show(b, usable);
      b.setAttribute("aria-pressed", String(captionsWanted));
      b.textContent = captionsWanted ? "Captions on" : "Captions off";
    });
    if (stage) { stage.setCaptionsOn(usable && (captionsWanted || S.state.settings.captionsOnPicture)); }
    paintLanguages();
  }
  var LANGUAGE_NAMES = { en: "English", es: "Spanish", fr: "French", de: "German", pt: "Portuguese", it: "Italian", nl: "Dutch", ja: "Japanese", ko: "Korean", zh: "Chinese", ar: "Arabic", hi: "Hindi", ru: "Russian" };
  var captionLanguage = "";
  function paintLanguages() {
    var select = $("cc-language");
    var languages = S.state.translateLanguages || [];
    var usable = languages.length > 0 && !$("cc").hidden && captionsWanted;
    show(select, usable);
    if (select.options.length !== languages.length + 1) {
      select.textContent = "";
      select.appendChild(h("option", { value: "", text: "Original language" }));
      languages.forEach(function (code) { select.appendChild(h("option", { value: code, text: LANGUAGE_NAMES[code] || code })); });
      var saved = localStorage.getItem("vc-caption-language") || "";
      if (languages.indexOf(saved) >= 0) { select.value = saved; chooseLanguage(saved); }
    }
  }
  function chooseLanguage(code) {
    captionLanguage = code;
    localStorage.setItem("vc-caption-language", code);
    if (code) { quietly({ type: "caption_language", language: code }); }
  }
  $("cc-language").addEventListener("change", function () { chooseLanguage(this.value); });
  [$("cc"), $("cc-crew")].forEach(function (b) {
    b.addEventListener("click", function () {
      captionsWanted = !captionsWanted;
      localStorage.setItem("vc-captions", captionsWanted ? "on" : "off");
      paintCaptionButton();
    });
  });

  // ---- the brand: this session's own choices, then what's set at /admin

  var FONTS = {
    system: ['system-ui, -apple-system, "Segoe UI", sans-serif', 'system-ui, -apple-system, "Segoe UI", sans-serif'],
    serif: ['Georgia, "Times New Roman", serif', '"Iowan Old Style", Palatino, Georgia, serif'],
    rounded: ['ui-rounded, "SF Pro Rounded", "Nunito", system-ui, sans-serif', 'ui-rounded, "SF Pro Rounded", "Nunito", system-ui, sans-serif'],
    mono: ['ui-monospace, Menlo, Consolas, monospace', 'ui-monospace, Menlo, Consolas, monospace']
  };

  function theme() {
    var own = (S.state && S.state.theme) || {};
    var brand = (S.state && S.state.brand) || {};
    return {
      accent: own.accent || brand.accent || "#c2462e",
      stage: own.stage || brand.stage || "#0d0c0a",
      mode: own.mode || brand.mode || "light",
      font: own.font || brand.font || "",
      logo: brand.logo ? "/brand/logo?b=1" : ""
    };
  }

  function readableOn(hex) {
    var n = parseInt(hex.slice(1), 16);
    var channel = function (v) { v /= 255; return v <= 0.03928 ? v / 12.92 : Math.pow((v + 0.055) / 1.055, 2.4); };
    var l = 0.2126 * channel(n >> 16 & 255) + 0.7152 * channel(n >> 8 & 255) + 0.0722 * channel(n & 255);
    return l > 0.45 ? "#1c1a17" : "#ffffff";
  }

  var appliedTheme = "", brandFont = null;
  function applyTheme() {
    var t = theme();
    var key = JSON.stringify(t) + isCrew() + !!(S.state && S.state.brand && S.state.brand.fontFile);
    if (key === appliedTheme) { return; }
    appliedTheme = key;
    var root = document.documentElement.style;
    root.setProperty("--coral", t.accent);
    root.setProperty("--coral-dark", t.accent);
    root.setProperty("--on-accent", readableOn(t.accent));
    root.setProperty("--stage-bg", t.stage);
    if (t.font === "custom" && S.state.brand && S.state.brand.fontFile) {
      if (!brandFont) {
        brandFont = new FontFace("VC Brand", "url(/brand/font)");
        brandFont.load().then(function (face) { document.fonts.add(face); }).catch(function () { brandFont = null; });
      }
      root.setProperty("--font", '"VC Brand", system-ui, sans-serif');
      root.setProperty("--font-head", '"VC Brand", system-ui, sans-serif');
    } else if (FONTS[t.font]) {
      root.setProperty("--font", FONTS[t.font][0]);
      root.setProperty("--font-head", FONTS[t.font][1]);
    } else {
      root.removeProperty("--font");
      root.removeProperty("--font-head");
    }
    document.body.classList.toggle("dark", !!isCrew() || t.mode === "dark");
    var logo = $("bar-logo");
    var wanted = t.logo || "/assets/mark.svg";
    if (logo.getAttribute("src") !== wanted) { logo.src = wanted; }
    var icon = document.querySelector('link[rel="icon"]');
    if (icon && icon.getAttribute("href") !== wanted) { icon.setAttribute("href", wanted); icon.setAttribute("type", t.logo ? "image/png" : "image/svg+xml"); }
  }

  // ---- LiveKit

  function roomOptions() {
    return {
      adaptiveStream: true,
      dynacast: true,
      videoCaptureDefaults: { resolution: LK.VideoPresets.h1080.resolution },
      publishDefaults: {
        simulcast: true,
        videoEncoding: LK.VideoPresets.h1080.encoding,
        videoSimulcastLayers: [LK.VideoPresets.h360, LK.VideoPresets.h720],
        audioPreset: LK.AudioPresets.musicHighQualityStereo,
        forceStereo: true
      }
    };
  }
  // Everyone in a breakout publishes, attendees included, so these stay smaller.
  function breakoutRoomOptions() {
    return { adaptiveStream: true, dynacast: true, videoCaptureDefaults: { resolution: LK.VideoPresets.h720.resolution }, publishDefaults: { simulcast: true } };
  }

  function keyFor(participant) {
    return participant.identity.indexOf("host-m-") === 0 ? "screen:" + participant.identity : "cam:" + participant.identity;
  }

  function wireStage(room) {
    function add(track, publication, participant) {
      if (track.kind === "video") {
        stage.setTrack(keyFor(participant), track);
      } else if (!participant.isLocal) {
        var el = track.attach();
        el.dataset.stage = "1";
        $("audio-sink").appendChild(el);
      }
    }
    function drop(track, publication, participant) {
      if (track.kind === "video") {
        stage.setTrack(keyFor(participant), null);
      } else {
        track.detach().forEach(function (el) { el.remove(); });
      }
    }
    room.on(LK.RoomEvent.TrackSubscribed, add);
    room.on(LK.RoomEvent.TrackUnsubscribed, drop);
    room.on(LK.RoomEvent.LocalTrackPublished, function (publication, participant) { if (publication.track) { add(publication.track, publication, participant); } if (!program) { localTracksChanged(); } });
    room.on(LK.RoomEvent.LocalTrackUnpublished, function (publication, participant) { if (publication.track) { drop(publication.track, publication, participant); } if (!program) { localTracksChanged(); } });
    room.on(LK.RoomEvent.ActiveSpeakersChanged, function (speakers) {
      var first = speakers.filter(function (p) { return p.identity.indexOf("host-m-") !== 0; })[0];
      if (first) { stage.setActive(keyFor(first)); }
    });
    room.on(LK.RoomEvent.AudioPlaybackStatusChanged, function () { show($("enable-sound"), !room.canPlaybackAudio); });
    room.on(LK.RoomEvent.Reconnecting, function () { $("state-pill").textContent = "Reconnecting"; show($("state-pill"), true); });
    room.on(LK.RoomEvent.Reconnected, function () { show($("state-pill"), false); });
    // A camera turned off is muted, not gone, so the window goes back to their initials.
    room.on(LK.RoomEvent.TrackMuted, function (publication, participant) {
      if (publication.kind === "video") { stage.setTrack(keyFor(participant), null); }
      if (!program && participant.isLocal) { localTracksChanged(); }
    });
    room.on(LK.RoomEvent.TrackUnmuted, function (publication, participant) {
      if (publication.kind === "video" && publication.track) { stage.setTrack(keyFor(participant), publication.track); }
      if (!program && participant.isLocal) { localTracksChanged(); }
    });
    room.on(LK.RoomEvent.Disconnected, function (reason) {
      if (reason === LK.DisconnectReason.DUPLICATE_IDENTITY) { leave("This personal link was opened in another window, so this one was closed."); return; }
      if (!program && room === stageRoom) { connectionLost(reason); }
    });
  }

  // LiveKit retries for less than a minute, then stops. Someone left on a dead picture gets back in.
  var recovering = false, leaving = false;
  function connectionLost(reason) {
    var expected = [LK.DisconnectReason.CLIENT_INITIATED, LK.DisconnectReason.PARTICIPANT_REMOVED, LK.DisconnectReason.ROOM_DELETED];
    if (leaving || recovering || expected.indexOf(reason) >= 0 || (S.state && S.state.phase === "ended")) { return; }
    recovering = true;
    show($("state-pill"), false);
    setError($("room-error"), "You lost the connection. Getting you back in.");
    if (isCrew()) {
      sessionStorage.setItem("vc-rejoin", "1");
      setTimeout(function () { window.location.reload(); }, 1500);
      return;
    }
    stageRoom = null;
    (function again(wait) {
      setTimeout(function () {
        if (leaving || stageRoom) { recovering = false; return; }
        request("/enter", { name: myName }).then(function (d) {
          if (!d.stageToken) { recovering = false; setError($("room-error"), ""); return null; }
          return connectStage(d.url, d.stageToken).then(function () { recovering = false; setError($("room-error"), ""); });
        }).catch(function () {
          if (stageRoom) { stageRoom.disconnect(); stageRoom = null; }
          again(Math.min(30000, wait * 2));
        });
      }, wait);
    })(2000);
  }

  function catchUp(room, add) {
    room.remoteParticipants.forEach(function (participant) {
      participant.trackPublications.forEach(function (publication) {
        if (publication.track) { add(publication.track, publication, participant); }
      });
    });
  }

  function connectStage(url, token) {
    stageRoom = new LK.Room(roomOptions());
    wireStage(stageRoom);
    return stageRoom.connect(url, token).then(function () {
      catchUp(stageRoom, function (track, publication, participant) {
        if (track.kind === "video") { stage.setTrack(keyFor(participant), track); }
        else { $("audio-sink").appendChild(track.attach()); }
      });
      return stageRoom.startAudio().catch(function () {});
    }).then(function () { show($("enable-sound"), !stageRoom.canPlaybackAudio); });
  }

  // ---- the audience's side

  var countdownTimer = null;
  function audiencePhase(phase) {
    var live = phase === "live";
    show($("prelive"), !live);
    clearInterval(countdownTimer);
    if (phase === "not_live") {
      $("prelive-title").textContent = info.title;
      $("prelive-note").textContent = "You can chat and ask questions while you wait.";
      paintBanner();
      var tick = function () {
        var left = new Date(info.startsAt).getTime() - Date.now();
        if (left <= 0) { $("countdown").textContent = "Starting soon"; return; }
        var s = Math.floor(left / 1000);
        var hours = Math.floor(s / 3600), mins = Math.floor((s % 3600) / 60), secs = s % 60;
        $("countdown").textContent = (hours ? hours + ":" : "") + String(mins).padStart(2, "0") + ":" + String(secs).padStart(2, "0");
      };
      tick();
      countdownTimer = setInterval(tick, 1000);
    }
    if (phase === "ended") {
      $("prelive-title").textContent = "This session has ended";
      $("countdown").textContent = "";
      $("prelive-note").textContent = "Thanks for coming.";
      paintBanner();
      if (stageRoom) { stageRoom.disconnect(); stageRoom = null; }
    }
    if (live && !stageRoom) {
      request("/enter", { name: myName, clientId: clientId }).then(function (d) {
        if (d.stageToken && !stageRoom) { return connectStage(d.url, d.stageToken); }
      }).catch(function (err) { setError($("room-error"), message(err)); });
    }
  }

  function paintBanner() {
    var img = $("prelive-banner");
    var id = S.state && S.state.phase === "not_live" ? S.state.bannerId : null;
    if (id) {
      if (img.dataset.id !== id) { img.dataset.id = id; img.src = fileUrl(id); }
    } else {
      img.dataset.id = "";
      img.removeAttribute("src");
    }
    show(img, !!id);
  }

  // ---- the crew's side: where my camera and microphone go

  function activeRoom() { return onStageNow ? stageRoom : greenRoom; }

  // A background is on the camera before it's published, so the real room is never seen.
  function cameraOptions() {
    var options = want.camDevice ? { deviceId: want.camDevice } : {};
    if (!background.kind) { return Promise.resolve(options); }
    return backgroundProcessor().then(function (processor) {
      options.processor = processor;
      return options;
    }).catch(function () {
      backgroundFailed();
      return options;
    });
  }

  function applyDevices(room) {
    var lp = room.localParticipant;
    return cameraOptions().then(function (camera) {
      return Promise.all([
        lp.setMicrophoneEnabled(want.mic, want.micDevice ? { deviceId: want.micDevice } : undefined),
        lp.setCameraEnabled(want.cam, camera)
      ]);
    }).then(function () {
      // A track that already existed in this room comes back on the device it had. Move it.
      return Promise.all([
        want.mic && want.micDevice ? room.switchActiveDevice("audioinput", want.micDevice).catch(function () {}) : null,
        want.cam && want.camDevice ? room.switchActiveDevice("videoinput", want.camDevice).catch(function () {}) : null
      ]);
    });
  }

  function waitToPublish(room) {
    return new Promise(function (resolve) {
      var tries = 0;
      (function check() {
        if (room.localParticipant.permissions && room.localParticipant.permissions.canPublish) { resolve(); return; }
        if (tries++ > 40) { resolve(); return; }
        setTimeout(check, 150);
      })();
    });
  }

  function syncPosition() {
    if (!greenRoom || !stageRoom || switching || !S.state || !S.me) { return; }
    var on = S.state.stage.some(function (m) { return m.identity === S.me.identity; });
    if (on === onStageNow) { return; }
    switching = true;
    var from = onStageNow ? stageRoom : greenRoom;
    var to = on ? stageRoom : greenRoom;
    var off = Promise.all([
      from.localParticipant.setMicrophoneEnabled(false).catch(function () {}),
      from.localParticipant.setCameraEnabled(false).catch(function () {})
    ]);
    off.then(function () { return on ? waitToPublish(to) : null; }).then(function () {
      onStageNow = on;
      return applyDevices(to);
    }).catch(function () {
      setError($("room-error"), "Your camera or microphone didn't switch over. Turn them off and on again.");
    }).then(function () {
      switching = false;
      syncGreenAudio();
      schedule();
      syncPosition();
      syncLocalRecording();
    });
  }

  // On stage you hear the stage, plus producers talking to you. Everyone off stage hears the green room.
  function syncGreenAudio() {
    Object.keys(greenAudio).forEach(function (identity) {
      var isProducer = identity.indexOf("host-p-") === 0;
      greenAudio[identity].forEach(function (el) { el.muted = onStageNow && !isProducer; });
    });
  }

  var greenVideo = {};
  function wireGreen(room) {
    function add(track, publication, participant) {
      if (track.kind === "video") {
        greenVideo[participant.identity] = track;
      } else if (!participant.isLocal) {
        var el = track.attach();
        el.dataset.from = participant.identity;
        (greenAudio[participant.identity] = greenAudio[participant.identity] || []).push(el);
        $("audio-sink").appendChild(el);
        syncGreenAudio();
      }
      schedule();
    }
    function drop(track, publication, participant) {
      if (track.kind === "video") {
        delete greenVideo[participant.identity];
      } else {
        track.detach().forEach(function (el) { el.remove(); });
        delete greenAudio[participant.identity];
      }
      schedule();
    }
    room.on(LK.RoomEvent.TrackSubscribed, add);
    room.on(LK.RoomEvent.TrackUnsubscribed, drop);
    room.on(LK.RoomEvent.LocalTrackPublished, function (publication, participant) { if (publication.track) { add(publication.track, publication, participant); } });
    room.on(LK.RoomEvent.LocalTrackUnpublished, function (publication, participant) {
      if (publication.track) { drop(publication.track, publication, participant); }
      else if (publication.kind === "video") { delete greenVideo[participant.identity]; schedule(); }
    });
    room.on(LK.RoomEvent.Disconnected, function (reason) {
      if (reason === LK.DisconnectReason.DUPLICATE_IDENTITY) { leave("This link was opened in another window, so this one was closed."); return; }
      connectionLost(reason);
    });
    room.on(LK.RoomEvent.TrackMuted, function (publication, participant) {
      if (publication.kind === "video") { delete greenVideo[participant.identity]; schedule(); }
    });
    room.on(LK.RoomEvent.TrackUnmuted, function (publication, participant) {
      if (publication.kind === "video" && publication.track) { greenVideo[participant.identity] = publication.track; schedule(); }
    });
    room.on(LK.RoomEvent.ParticipantConnected, schedule);
    room.on(LK.RoomEvent.ParticipantDisconnected, schedule);
    catchUp(room, add);
  }

  function connectCrew(d) {
    greenRoom = new LK.Room(roomOptions());
    return greenRoom.connect(d.url, d.greenToken).then(function () {
      wireGreen(greenRoom);
      return connectStage(d.url, d.stageToken);
    }).then(function () { return greenRoom.startAudio().catch(function () {}); }).then(function () {
      var waiting = setInterval(function () {
        if (!S.state) { return; }
        clearInterval(waiting);
        onStageNow = S.state.stage.some(function (m) { return m.identity === S.me.identity; });
        applyDevices(activeRoom()).catch(function () {
          setError($("room-error"), "Your browser didn't allow the camera or microphone. Check its permissions.");
        }).then(schedule);
      }, 50);
    });
  }

  function syncToggle(button, on, onLabel, offLabel) {
    button.setAttribute("aria-pressed", String(on));
    button.textContent = on ? onLabel : offLabel;
  }

  $("mic").addEventListener("click", function () {
    var button = this, on = !want.mic;
    activeRoom().localParticipant.setMicrophoneEnabled(on, want.micDevice ? { deviceId: want.micDevice } : undefined).then(function () {
      want.mic = on;
      syncToggle(button, on, "Mic on", "Mic off");
    }).catch(function () { setError($("room-error"), "Your browser didn't allow the microphone."); });
  });
  $("cam").addEventListener("click", function () {
    var button = this, on = !want.cam;
    cameraOptions().then(function (camera) { return activeRoom().localParticipant.setCameraEnabled(on, camera); }).then(function () {
      want.cam = on;
      syncToggle(button, on, "Camera on", "Camera off");
    }).catch(function () { setError($("room-error"), "Your browser didn't allow the camera."); });
  });

  function fillDevices() {
    return navigator.mediaDevices.enumerateDevices().then(function (devices) {
      var kinds = [["videoinput", "live-cam", want.camDevice], ["audioinput", "live-mic", want.micDevice], ["audiooutput", "live-speaker", ""]];
      kinds.forEach(function (row) {
        var select = $(row[1]), found = devices.filter(function (d) { return d.kind === row[0]; });
        var value = select.value || row[2];
        select.textContent = "";
        found.forEach(function (d, i) { select.appendChild(h("option", { value: d.deviceId, text: d.label || row[0] + " " + (i + 1) })); });
        if (found.some(function (d) { return d.deviceId === value; })) { select.value = value; }
      });
      show($("live-speaker-wrap"), devices.some(function (d) { return d.kind === "audiooutput"; }) && typeof HTMLMediaElement.prototype.setSinkId === "function");
      // What someone was using was unplugged: move to what's left, rather than going silent.
      [["videoinput", "camDevice"], ["audioinput", "micDevice"]].forEach(function (pair) {
        var current = want[pair[1]];
        if (current && !devices.some(function (d) { return d.kind === pair[0] && d.deviceId === current; })) {
          var next = devices.filter(function (d) { return d.kind === pair[0]; })[0];
          want[pair[1]] = next ? next.deviceId : "";
          if (next && activeRoom()) { activeRoom().switchActiveDevice(pair[0], next.deviceId).catch(function () {}); }
          setError($("room-error"), "A device was unplugged, so we've moved to " + (next ? next.label || "another one" : "none") + ".");
        }
      });
    });
  }
  function switchDevice(kind, id) {
    var rooms = kind === "audiooutput" ? [stageRoom, greenRoom] : [activeRoom()];
    return Promise.all(rooms.filter(Boolean).map(function (room) { return room.switchActiveDevice(kind, id); })).catch(function () {
      setError($("room-error"), "That device couldn't be used. Try another one.");
    });
  }
  $("devices-btn").addEventListener("click", function () {
    var on = this.getAttribute("aria-expanded") !== "true";
    this.setAttribute("aria-expanded", String(on));
    show($("device-panel"), on);
    if (on) { fillDevices(); }
  });
  $("live-cam").addEventListener("change", function () { want.camDevice = this.value; switchDevice("videoinput", this.value); });
  $("live-mic").addEventListener("change", function () { want.micDevice = this.value; switchDevice("audioinput", this.value); });
  $("live-speaker").addEventListener("change", function () { switchDevice("audiooutput", this.value); });

  // ---- virtual backgrounds, drawn in this browser. Everything is served from here, not a CDN.

  var backgrounds = null, background = { kind: "", url: "" };
  function loadBackgrounds() {
    if (!backgrounds) {
      backgrounds = new Promise(function (resolve, reject) {
        var script = document.createElement("script");
        script.src = "/assets/vbg/vbg.js";
        script.onload = function () { resolve(window.VCBackgrounds); };
        script.onerror = reject;
        document.head.appendChild(script);
      });
    }
    return backgrounds;
  }
  var ASSET_PATHS = { tasksVisionFileSet: "/assets/vbg", modelAssetPath: "/assets/vbg/selfie_segmenter.tflite" };
  function backgroundProcessor() {
    return loadBackgrounds().then(function (lib) {
      if (!lib.supportsBackgroundProcessors()) { throw new Error("unsupported"); }
      return background.kind === "blur"
        ? lib.BackgroundProcessor({ blurRadius: 12, assetPaths: ASSET_PATHS })
        : lib.BackgroundProcessor({ imagePath: background.url, assetPaths: ASSET_PATHS });
    });
  }
  function backgroundFailed() {
    setError($("room-error"), "This browser can't draw a background. Your camera is showing as normal.");
    background = { kind: "", url: "" };
    $("vbg").value = "";
    document.documentElement.dataset.background = "";
  }
  function applyBackground() {
    var room = activeRoom();
    var pub = room && room.localParticipant.getTrackPublication(LK.Track.Source.Camera);
    var mark = function () { document.documentElement.dataset.background = background.kind; };
    // No camera on yet: it starts with the background when it's turned on.
    if (!pub || !pub.track || pub.isMuted) { mark(); return Promise.resolve(); }
    if (!background.kind) { return (pub.track.getProcessor() ? pub.track.stopProcessor() : Promise.resolve()).then(mark); }
    return backgroundProcessor().then(function (processor) { return pub.track.setProcessor(processor); }).then(mark).catch(backgroundFailed);
  }
  $("vbg").addEventListener("change", function () {
    show($("vbg-file"), this.value === "image");
    if (this.value === "image") { return; }
    background = { kind: this.value, url: "" };
    applyBackground();
  });
  $("vbg-file").addEventListener("change", function () {
    if (!this.files[0]) { return; }
    background = { kind: "image", url: URL.createObjectURL(this.files[0]) };
    applyBackground();
  });
  if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
    navigator.mediaDevices.addEventListener("devicechange", function () { if (isCrew()) { fillDevices(); } });
  }

  // ---- each speaker's own recording: full quality, from their own browser, uploaded as it's made

  var localRec = null;
  function syncLocalRecording() {
    if (!S.state || !stageRoom) { return; }
    var wanted = S.state.settings.localRecordings && S.state.recording && onStageNow && !switching && typeof MediaRecorder === "function";
    if (wanted && !localRec) { startLocalRecording(); }
    if (!wanted && localRec) { stopLocalRecording(); }
  }
  function startLocalRecording() {
    var tracks = [];
    stageRoom.localParticipant.trackPublications.forEach(function (pub) {
      if (pub.track && !pub.isMuted && pub.track.mediaStreamTrack && pub.track.mediaStreamTrack.readyState === "live") { tracks.push(pub.track.mediaStreamTrack); }
    });
    if (!tracks.length) { return; }
    var mime = ["video/webm;codecs=vp9,opus", "video/webm", "video/mp4"].filter(function (m) { return MediaRecorder.isTypeSupported(m); })[0];
    var id = Array.from(crypto.getRandomValues(new Uint8Array(8))).map(function (b) { return b.toString(16).padStart(2, "0"); }).join("");
    var recorder = new MediaRecorder(new MediaStream(tracks), { mimeType: mime, videoBitsPerSecond: 10000000, audioBitsPerSecond: 128000 });
    var rec = { recorder: recorder, id: id, seq: 0, chain: Promise.resolve(), broken: false, tracks: tracks };
    var missed = function () {
      rec.broken = true;
      setError($("room-error"), "Your own copy of the recording missed a part, so a new file has started. The main recording isn't affected.");
      if (localRec === rec) { stopLocalRecording(); setTimeout(syncLocalRecording, 1000); }
    };
    var send = function (blob, seq, tries) {
      return fetch(base + "/local/" + id + "/" + seq, { method: "PUT", body: blob }).then(function (res) {
        if (res.status >= 500) { throw new Error("retry"); }
        if (!res.ok) { missed(); }
      }).catch(function () {
        if (rec.broken) { return null; }
        if (tries > 0) { return new Promise(function (r) { setTimeout(r, 3000); }).then(function () { return send(blob, seq, tries - 1); }); }
        missed();
      });
    };
    recorder.ondataavailable = function (event) {
      if (!event.data || !event.data.size || rec.broken) { return; }
      var seq = rec.seq++;
      rec.chain = rec.chain.then(function () { return rec.broken ? null : send(event.data, seq, 20); });
    };
    recorder.onstop = function () {
      rec.chain.then(function () { quietly({ type: "local_recording_done", id: id }); });
      if (localRec === rec) { localRec = null; schedule(); }
    };
    recorder.start(5000);
    localRec = rec;
  }
  // A camera or microphone turned on, off or changed is a new track, which a running recorder can't
  // follow. So each change starts a new file.
  function localTracksChanged() {
    if (!localRec) { syncLocalRecording(); return; }
    stopLocalRecording();
    setTimeout(syncLocalRecording, 300);
  }
  function stopLocalRecording() {
    var rec = localRec;
    localRec = null;
    if (rec && rec.recorder.state !== "inactive") { rec.recorder.stop(); }
  }

  // ---- breakout rooms and the networking hub

  var breakoutRoom = null, breakoutId = "", breakoutWasOn = null;
  function paintBreakout() {
    var grid = $("breakout-grid");
    grid.textContent = "";
    if (!breakoutRoom) { return; }
    var everyone = [breakoutRoom.localParticipant].concat(Array.from(breakoutRoom.remoteParticipants.values()));
    everyone.forEach(function (p) {
      var tile = h("div", { class: "br-tile", "data-person": p.identity }, h("b", { text: p.name || "Guest" }));
      p.trackPublications.forEach(function (pub) {
        if (pub.track && pub.kind === "video" && !pub.isMuted) {
          var video = pub.track.attach();
          video.muted = true;
          tile.insertBefore(video, tile.firstChild);
        }
      });
      grid.appendChild(tile);
    });
  }
  var joiningBreakout = false;
  function joinBreakout(row) {
    if (joiningBreakout) { return; }
    joiningBreakout = true;
    setError($("room-error"), "");
    // Nothing of theirs stays on the air while they're away.
    stopShare();
    act({ type: "breakout_join", id: row.id }).then(function (d) {
      var room = new LK.Room(breakoutRoomOptions());
      var audio = function (track, pub, participant) {
        if (track.kind === "audio" && !participant.isLocal) { var el = track.attach(); el.dataset.breakout = "1"; document.body.appendChild(el); }
        paintBreakout();
      };
      room.on(LK.RoomEvent.TrackSubscribed, audio);
      room.on(LK.RoomEvent.TrackUnsubscribed, function (track) { track.detach().forEach(function (el) { el.remove(); }); paintBreakout(); });
      room.on(LK.RoomEvent.LocalTrackPublished, paintBreakout);
      room.on(LK.RoomEvent.TrackMuted, paintBreakout);
      room.on(LK.RoomEvent.TrackUnmuted, paintBreakout);
      room.on(LK.RoomEvent.LocalTrackUnpublished, paintBreakout);
      room.on(LK.RoomEvent.ParticipantConnected, paintBreakout);
      room.on(LK.RoomEvent.ParticipantDisconnected, paintBreakout);
      room.on(LK.RoomEvent.Disconnected, function () { if (breakoutRoom === room) { leaveBreakout("The room has closed."); } });
      return room.connect(d.url, d.token).then(function () {
        return room.startAudio().catch(function () {});
      }).then(function () {
        breakoutRoom = room;
        breakoutId = row.id;
        muteShow(true);
        $("breakout-title").textContent = row.name;
        document.body.classList.add("in-breakout");
        show($("breakout"), true);
        syncToggle($("br-mic"), false, "Mic on", "Mic off");
        syncToggle($("br-cam"), false, "Camera on", "Camera off");
        paintBreakout();
        render();
      });
    }).catch(function (err) { setError($("room-error"), message(err)); }).then(function () { joiningBreakout = false; });
  }
  function muteShow(on) {
    if (stage) { stage.setMuted(on); }
    Array.from($("audio-sink").children).forEach(function (el) { el.muted = on; });
    if (!on) { syncGreenAudio(); }
    if (isCrew() && activeRoom()) {
      if (on) { breakoutWasOn = { mic: want.mic, cam: want.cam }; activeRoom().localParticipant.setMicrophoneEnabled(false).catch(function () {}); activeRoom().localParticipant.setCameraEnabled(false).catch(function () {}); }
      else if (breakoutWasOn) { applyDevices(activeRoom()).catch(function () {}); breakoutWasOn = null; }
    }
  }
  function leaveBreakout(note) {
    var room = breakoutRoom;
    breakoutRoom = null;
    breakoutId = "";
    if (room) { room.disconnect(); }
    document.querySelectorAll("audio[data-breakout]").forEach(function (el) { el.remove(); });
    document.body.classList.remove("in-breakout");
    show($("breakout"), false);
    muteShow(false);
    quietly({ type: "breakout_leave" });
    if (note) { setError($("room-error"), note); }
    schedule();
  }
  $("br-leave").addEventListener("click", function () { leaveBreakout(); });
  $("br-mic").addEventListener("click", function () {
    var button = this, on = button.getAttribute("aria-pressed") !== "true";
    breakoutRoom.localParticipant.setMicrophoneEnabled(on).then(function () { syncToggle(button, on, "Mic on", "Mic off"); }).catch(function () { setError($("room-error"), "Your browser didn't allow the microphone."); });
  });
  $("br-cam").addEventListener("click", function () {
    var button = this, on = button.getAttribute("aria-pressed") !== "true";
    cameraOptions().then(function (camera) { return breakoutRoom.localParticipant.setCameraEnabled(on, camera); }).then(function () { syncToggle(button, on, "Camera on", "Camera off"); paintBreakout(); }).catch(function () { setError($("room-error"), "Your browser didn't allow the camera."); });
  });

  function renderRooms() {
    var kind = S.state.breakoutKind;
    var box = $("rooms");
    // The form is only rebuilt when rooms open or close, so what's being typed in it stays.
    var form = $("rooms-form"), list = $("rooms-list");
    if (!form) {
      form = h("div", { id: "rooms-form" });
      list = h("div", { id: "rooms-list" });
      box.appendChild(form);
      box.appendChild(list);
    }
    var closing = arming["close-rooms"] && Date.now() < arming["close-rooms"];
    var formFor = String(runs()) + kind + (closing ? "-closing" : "");
    if (form.dataset.for !== formFor) {
      form.dataset.for = formFor;
      form.textContent = "";
      if (runs() && !kind) {
        var count = h("input", { id: "br-count", type: "number", min: "1", max: "50", value: "3" });
        var cap = h("input", { id: "br-cap", type: "number", min: "0", max: "500", value: "0" });
        var mode = h("select", { id: "br-kind" },
          h("option", { value: "open", text: "People choose a room" }),
          h("option", { value: "assigned", text: "Split the audience evenly" }),
          h("option", { value: "hub", text: "Networking hub: tables open, even after the session" }));
        form.appendChild(h("form", { class: "box", id: "br-form", onsubmit: function (event) {
          event.preventDefault();
          var attendees = S.people.filter(function (p) { return p.role === "attendee"; }).map(function (p) { return p.identity; });
          var n = Math.max(1, Math.min(50, Number(count.value) || 1));
          var word = mode.value === "hub" ? "Table " : "Room ";
          var rooms = [];
          for (var i = 0; i < n; i += 1) { rooms.push({ name: word + (i + 1), cap: Number(cap.value) || 0, members: mode.value === "assigned" ? attendees.filter(function (_, j) { return j % n === i; }) : [] }); }
          quietly({ type: "breakouts_open", kind: mode.value, rooms: rooms });
        } }, h("h3", { text: "Open breakout rooms" }),
          h("label", { for: "br-kind", text: "How people get in" }), mode,
          h("label", { for: "br-count", text: "How many" }), count,
          h("label", { for: "br-cap", text: "Most people per room (0 for no limit)" }), cap,
          h("button", { class: "btn", type: "submit", text: "Open them" })));
      } else if (runs() && kind) {
        form.appendChild(h("div", { class: "row end" }, armed("close-rooms", "Close all rooms", function () { quietly({ type: "breakouts_close" }); }, "danger")));
      }
    }
    box = list;
    box.textContent = "";
    if (kind && !runs() && !isCrew()) {
      box.appendChild(h("p", { class: "muted small", text: "Your camera and microphone stay off until you turn them on in the room." }));
    }
    S.state.breakouts.forEach(function (row) {
      var mine = kind !== "assigned" || isCrew() || S.assigned === row.id;
      if (!mine || S.me.blocked) { return; }
      var inside = S.breakoutCounts[row.id] || 0;
      var here = breakoutId === row.id;
      box.appendChild(h("div", { class: "file", "data-breakout-room": row.name },
        h("div", {}, h("strong", { text: row.name }), h("div", { class: "muted small", text: plural(inside, "person").replace("persons", "people") + (row.cap ? " of " + row.cap : "") })),
        here ? h("span", { class: "muted small", text: "You're here" }) : mini("Join", function () {
          if (breakoutRoom) { leaveBreakout(); }
          joinBreakout(row);
        }, "primary")));
    });
    if (!kind && !runs()) { box.appendChild(h("p", { class: "muted", text: "No rooms are open." })); }
  }

  // ---- polls that come to you, and the survey at the end

  var dismissedPolls = {};
  function renderPollPop() {
    var box = $("poll-pop");
    var due = null;
    if (!isCrew() && S.state.settings.pollsEnabled && !S.me.blocked) {
      Object.keys(S.polls).forEach(function (id) {
        var p = S.polls[id];
        if (!due && p.popup && p.status === "open" && S.myVotes["poll:" + id] === undefined && !dismissedPolls[id]) { due = p; }
      });
    }
    if (!due) { show(box, false); box.dataset.id = ""; return; }
    if (box.dataset.id === due.id && !box.hidden) { return; }
    box.dataset.id = due.id;
    box.textContent = "";
    box.appendChild(h("button", { class: "mini pop-close", type: "button", "aria-label": "Close", text: "Close", onclick: function () { dismissedPolls[due.id] = true; renderPollPop(); } }));
    box.appendChild(h("h3", { text: due.question }));
    due.options.forEach(function (text, i) {
      box.appendChild(h("button", { class: "poll-row", type: "button", text: text, onclick: function () {
        act({ type: "poll_vote", id: due.id, option: i }).then(function () { S.myVotes["poll:" + due.id] = i; schedule(); }).catch(function () {});
      } }));
    });
    show(box, true);
  }

  function renderSurveyForm() {
    var box = $("survey-box");
    var due = !isCrew() && S.state.survey && S.state.phase === "ended" && !S.surveyDone;
    if (S.surveyThanks) { return; }
    if (!due) { show(box, false); box.textContent = ""; return; }
    if (!box.hidden) { return; }
    box.textContent = "";
    var form = h("form", { id: "survey-form" }, h("h2", { text: S.state.survey.title || "How was it?" }));
    S.state.survey.questions.forEach(function (q) {
      var name = "survey-" + q.id;
      form.appendChild(h("p", { class: "survey-q", text: q.text }));
      if (q.kind === "text") { form.appendChild(h("textarea", { name: name, maxlength: "1000", rows: "3", "aria-label": q.text })); return; }
      var values = q.kind === "rating" ? ["1", "2", "3", "4", "5"] : q.options;
      var row = h("div", { class: "survey-opts" });
      values.forEach(function (v) { row.appendChild(h("label", { class: "check" }, h("input", { type: "radio", name: name, value: v }), document.createTextNode(" " + v))); });
      form.appendChild(row);
    });
    form.appendChild(h("button", { class: "btn btn-primary-inline", type: "submit", text: "Send" }));
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var answers = {};
      S.state.survey.questions.forEach(function (q) {
        var field = form.elements["survey-" + q.id];
        if (field && field.value && field.value.trim()) { answers[q.id] = field.value; }
      });
      act({ type: "survey_answer", answers: answers }).then(function () {
        S.surveyDone = true;
        S.surveyThanks = true;
        show(box, true);
        box.textContent = "";
        box.appendChild(h("p", { text: "Thanks. Your answers have been sent." }));
      }).catch(function () {});
    });
    box.appendChild(form);
    show(box, true);
  }

  var surveyResults = null;
  function renderSurveyEditor() {
    var box = $("survey-editor");
    show(box, runs());
    if (!runs()) { return; }
    // Only when something saved has changed: what someone is still typing isn't saved yet.
    var drawn = JSON.stringify([S.state.survey, surveyResults]);
    if (box.dataset.drawn === drawn) { return; }
    box.dataset.drawn = drawn;
    box.textContent = "";
    var survey = S.state.survey;
    box.appendChild(h("h3", { text: "Survey after the session" }));
    box.appendChild(h("p", { class: "muted small", text: "Attendees see this when the session ends. Answers go back to your studio and CRM." }));
    var title = h("input", { id: "survey-title", maxlength: "200", placeholder: "Title, like How was it?", value: survey ? survey.title : "" });
    var rows = h("div", { id: "survey-rows" });
    function addRow(q) {
      q = q || { id: "", kind: "rating", text: "", options: [] };
      var text = h("input", { class: "sq-text", maxlength: "500", placeholder: "Question", value: q.text });
      var kind = h("select", { class: "sq-kind", "aria-label": "Answer type" }, h("option", { value: "rating", text: "1 to 5" }), h("option", { value: "choice", text: "Choose one" }), h("option", { value: "text", text: "Free text" }));
      kind.value = q.kind;
      var options = h("input", { class: "sq-options", maxlength: "1000", placeholder: "Choices, separated by commas", value: q.options.join(", ") });
      var row = h("div", { class: "dest", "data-id": q.id }, text, kind, options, h("div", { class: "row end" }, mini("Remove", function () { row.remove(); }, "danger")));
      rows.appendChild(row);
    }
    (survey ? survey.questions : []).forEach(addRow);
    if (!survey) { addRow(); }
    box.appendChild(title);
    box.appendChild(rows);
    box.appendChild(h("div", { class: "row end" },
      mini("Add a question", function () { addRow(); }),
      survey ? mini("Remove the survey", function () { quietly({ type: "survey_save", survey: null }); }, "danger") : null,
      mini("Save survey", function () {
        var questions = Array.from(rows.children).map(function (row) {
          return { id: row.dataset.id || "", kind: row.querySelector(".sq-kind").value, text: row.querySelector(".sq-text").value, options: row.querySelector(".sq-options").value.split(",").map(function (o) { return o.trim(); }).filter(Boolean) };
        });
        act({ type: "survey_save", survey: { title: title.value, questions: questions } }).then(function () { setError($("room-error"), ""); document.activeElement.blur(); schedule(); }).catch(function () {});
      }, "primary")));
    if (survey) {
      box.appendChild(h("div", { class: "row end" }, mini("See answers", function () {
        act({ type: "survey_results" }).then(function (d) { surveyResults = d.answers; schedule(); }).catch(function () {});
      })));
    }
    if (surveyResults) {
      box.appendChild(h("p", { class: "muted small", "data-survey-count": String(surveyResults.length), text: plural(surveyResults.length, "answer") }));
      (survey ? survey.questions : []).forEach(function (q) {
        var given = surveyResults.map(function (a) { return a.answers[q.id]; }).filter(Boolean);
        var summary = q.kind === "rating" && given.length
          ? "Average " + (given.reduce(function (t, v) { return t + Number(v); }, 0) / given.length).toFixed(1)
          : given.slice(0, 20).join(" · ");
        box.appendChild(h("div", { class: "file small" }, h("strong", { text: q.text }), h("span", { text: summary })));
      });
    }
  }

  // ---- saved layouts

  function renderSavedLayouts() {
    var box = $("saved-layouts");
    var drawn = JSON.stringify((S.state.savedLayouts || []).map(function (l) { return l.id + l.name; }));
    if (box.dataset.drawn === drawn) { return; }
    box.dataset.drawn = drawn;
    box.textContent = "";
    (S.state.savedLayouts || []).forEach(function (l) {
      box.appendChild(h("span", { class: "saved-layout" },
        h("button", { class: "seg", type: "button", "data-saved-layout": l.name, text: l.name, onclick: function () { quietly({ type: "layout_apply", id: l.id }); } }),
        h("button", { class: "mini", type: "button", "aria-label": "Delete " + l.name, text: "×", onclick: function () { quietly({ type: "layout_delete", id: l.id }); } })));
    });
    var name = h("input", { id: "layout-name", maxlength: "60", placeholder: "Name this layout", "aria-label": "Name this layout" });
    box.appendChild(name);
    box.appendChild(mini("Save layout", function () {
      if (name.value.trim()) { act({ type: "layout_save", name: name.value }).then(function () { document.activeElement.blur(); schedule(); }).catch(function () {}); }
    }));
  }

  // ---- slides

  function renderSlideControls() {
    var show_ = S.state.slides;
    var onStage = S.state.stage.some(function (m) { return m.identity === S.me.identity; });
    var can = !!show_ && (runs() || (isCrew() && onStage));
    show($("slide-controls"), can);
    var deck = can ? S.files.filter(function (f) { return f.id === show_.fileId; })[0] : null;
    var note = deck && deck.notes ? deck.notes[show_.page - 1] : "";
    show($("slide-notes"), !!note);
    $("slide-notes").textContent = note || "";
    if (can) { $("slide-count").textContent = show_.page + " of " + show_.pages; }
  }
  $("slide-prev").addEventListener("click", function () { quietly({ type: "slides_page", page: S.state.slides.page - 1 }); });
  $("slide-next").addEventListener("click", function () { quietly({ type: "slides_page", page: S.state.slides.page + 1 }); });

  // ---- an encoder sending RTMP in

  var ingressRows = null, ingressShown = null;
  function loadIngress() {
    act({ type: "ingress_list" }).then(function (d) { ingressRows = d.inputs; schedule(); }).catch(function () {});
  }
  function renderIngress() {
    var box = $("ingress-box");
    var usable = runs() && S.state.ingressAvailable;
    show(box, usable);
    if (!usable) { return; }
    if (ingressRows === null) { ingressRows = []; loadIngress(); }
    var drawn = JSON.stringify(ingressRows) + ingressShown + JSON.stringify(arming);
    if (box.dataset.drawn === drawn) { return; }
    box.dataset.drawn = drawn;
    box.textContent = "";
    box.appendChild(h("h3", { text: "Bring in an encoder" }));
    box.appendChild(h("p", { class: "muted small", text: "For OBS, vMix or a hardware encoder. It goes on stage the moment its stream arrives and comes off when the stream stops, so start it when you want it seen." }));
    ingressRows.forEach(function (row) {
      box.appendChild(h("div", { class: "file", "data-ingress": row.name }, h("div", {}, h("strong", { text: row.name }),
        h("div", { class: "muted small", text: row.url }), h("div", { class: "muted small", text: "Stream key: " + (ingressShown === row.id ? row.key : "hidden") })),
        h("div", { class: "acts" },
          mini(ingressShown === row.id ? "Hide key" : "Show key", function () { ingressShown = ingressShown === row.id ? null : row.id; schedule(); }),
          armed("input-" + row.id, "Delete", function () { act({ type: "ingress_delete", id: row.id }).then(loadIngress).catch(function () {}); }, "danger"))));
    });
    var name = h("input", { id: "ingress-name", maxlength: "80", placeholder: "Name, like Main camera" });
    box.appendChild(name);
    box.appendChild(h("div", { class: "row end" }, mini("Create an input", function () {
      if (!name.value.trim()) { return; }
      act({ type: "ingress_create", name: name.value }).then(function (row) { ingressShown = row.id; document.activeElement.blur(); loadIngress(); }).catch(function () {});
    }, "primary")));
  }

  function stopShare() {
    if (shareRoom) { shareRoom.disconnect(); shareRoom = null; }
    syncToggle($("screen"), false, "Stop sharing", "Share screen");
  }
  $("screen").addEventListener("click", function () {
    if (shareRoom) { stopShare(); return; }
    var button = this;
    button.disabled = true;
    var room = new LK.Room();
    var tracks = [];
    // First, straight from the click: Safari only opens the screen picker from a click itself,
    // and waiting for the server or LiveKit before asking would lose it.
    room.localParticipant.createScreenTracks({ audio: true }).then(function (made) {
      tracks = made;
      return request("/share-token", { clientId: clientId });
    }).then(function (d) {
      return room.connect(d.url, d.token);
    }).then(function () {
      room.on(LK.RoomEvent.Disconnected, stopShare);
      room.on(LK.RoomEvent.LocalTrackUnpublished, stopShare);
      return Promise.all(tracks.map(function (track) { return room.localParticipant.publishTrack(track, { source: track.source }); }));
    }).then(function () {
      shareRoom = room;
      syncToggle(button, true, "Stop sharing", "Share screen");
    }).catch(function (err) {
      tracks.forEach(function (track) { track.stop(); });
      room.disconnect();
      // Ours have a word for a code. The browser's have a name, like NotAllowedError.
      if (err.name === "NotAllowedError") { setError($("room-error"), "The screen wasn't shared. If you didn't cancel, check that your browser is allowed to record the screen in your system's privacy settings."); return; }
      setError($("room-error"), typeof err.code === "string" ? message(err) : "Your browser couldn't share the screen.");
    }).then(function () { button.disabled = false; });
  });

  // ---- the top bar

  $("go-live").addEventListener("click", function () { quietly({ type: "go_live" }); });
  var endArmed = null;
  $("end").addEventListener("click", function () {
    var button = this;
    if (!endArmed) {
      button.textContent = "Click again to end for everyone";
      button.classList.add("armed");
      endArmed = setTimeout(function () { endArmed = null; button.textContent = "End session"; button.classList.remove("armed"); }, 4000);
      return;
    }
    clearTimeout(endArmed); endArmed = null;
    button.textContent = "End session"; button.classList.remove("armed");
    quietly({ type: "end_session" });
  });
  $("record").addEventListener("click", function () {
    var button = this;
    button.disabled = true;
    act({ type: "record", on: !S.state.recording }).catch(function () {}).then(function () { button.disabled = false; });
  });
  $("audience-view").addEventListener("click", function () {
    var on = this.getAttribute("aria-pressed") !== "true";
    this.setAttribute("aria-pressed", String(on));
    document.body.classList.toggle("audience-view", on);
  });
  $("enable-sound").addEventListener("click", function () {
    if (stageRoom) { stageRoom.startAudio(); }
    if (greenRoom) { greenRoom.startAudio(); }
    show(this, false);
  });
  document.querySelectorAll("[data-layout]").forEach(function (button) {
    button.addEventListener("click", function () { quietly({ type: "set_layout", mode: button.dataset.layout, focus: S.state.layout.focus || null }); });
  });
  $("edit-layout").addEventListener("click", function () {
    var on = this.getAttribute("aria-pressed") !== "true";
    this.setAttribute("aria-pressed", String(on));
    stage.setEditing(on);
  });

  // ---- render

  var TAB_LABELS = { chat: "Chat", qa: "Q&A", polls: "Polls", files: "Files", rooms: "Rooms", people: "People", look: "Look", broadcast: "Broadcast", settings: "Settings" };

  function tabs() {
    var st = S.state.settings;
    var open = S.state.breakouts && S.state.breakouts.length > 0;
    if (runs()) { return ["chat", "qa", "polls", "files", "rooms", "people", "look", "broadcast", "settings"]; }
    if (isCrew()) { return ["chat", "qa", "polls", "files"].concat(open ? ["rooms"] : []); }
    var out = [];
    if (st.chatEnabled) { out.push("chat"); }
    if (st.qaEnabled) { out.push("qa"); }
    if (st.pollsEnabled) { out.push("polls"); }
    if (st.resourcesEnabled) { out.push("files"); }
    if (open) { out.push("rooms"); }
    return out;
  }

  function renderTabs() {
    var list = tabs();
    if (list.indexOf(tab) < 0) { tab = list[0] || ""; }
    show($("side"), list.length > 0);
    var nav = $("tabs");
    nav.textContent = "";
    list.forEach(function (name) {
      var button = h("button", { class: "tab", type: "button", role: "tab", "data-tab": name, "aria-selected": String(tab === name), text: TAB_LABELS[name], onclick: function () { tab = name; unread[name] = false; render(); } });
      if (unread[name] && tab !== name) { button.appendChild(h("span", { class: "dot" })); }
      nav.appendChild(button);
    });
    ["chat", "qa", "polls", "files", "rooms", "people", "look", "broadcast", "settings"].forEach(function (name) { show($("panel-" + name), tab === name); });
  }

  function badge(role) { return h("span", { class: "badge " + role, text: role }); }

  function mini(label, fn, cls) { return h("button", { class: "mini" + (cls ? " " + cls : ""), type: "button", text: label, onclick: fn }); }

  // For what can't be undone: the first press asks, the second does it. Survives a redraw.
  var arming = {};
  function armed(key, label, fn, cls) {
    var ready = arming[key] && Date.now() < arming[key];
    return mini(ready ? "Click again to confirm" : label, function () {
      if (arming[key] && Date.now() < arming[key]) { delete arming[key]; fn(); return; }
      arming[key] = Date.now() + 4000;
      schedule();
      setTimeout(schedule, 4100);
    }, cls);
  }

  var CHAT_REACTIONS = ["👏", "❤️", "😂", "🎉", "👍", "🤔"];
  var reactingTo = null;
  function reactionRow(m) {
    var counts = S.chatReactions[m.id] || {};
    var mine = S.myVotes["chat:" + m.id];
    var row = h("div", { class: "chat-reacts" });
    var send = function (emoji) {
      reactingTo = null;
      act({ type: "chat_react", id: m.id, emoji: emoji }).then(function (r) {
        if (r.reaction === null || r.reaction === undefined) { delete S.myVotes["chat:" + m.id]; } else { S.myVotes["chat:" + m.id] = r.reaction; }
        schedule();
      }).catch(function () {});
    };
    var canReact = !S.me.blocked && (isCrew() || S.state.settings.reactionsEnabled);
    CHAT_REACTIONS.forEach(function (emoji, i) {
      if (!counts[emoji] && reactingTo !== m.id) { return; }
      row.appendChild(h("button", { class: "react-chip" + (mine === i ? " mine" : ""), type: "button", disabled: !canReact, "aria-label": "React " + emoji, "data-react": emoji, text: emoji + (counts[emoji] ? " " + counts[emoji] : ""), onclick: function () { send(emoji); } }));
    });
    if (canReact) {
      row.appendChild(h("button", { class: "react-chip more", type: "button", "aria-label": "Add a reaction", "data-react-open": m.id, text: reactingTo === m.id ? "×" : "+", onclick: function () { reactingTo = reactingTo === m.id ? null : m.id; schedule(); } }));
    }
    return row;
  }

  function renderChat() {
    var list = $("messages");
    var stick = list.scrollTop + list.clientHeight >= list.scrollHeight - 30;
    list.textContent = "";
    S.chat.filter(function (m) { return m.channel === channel; }).forEach(function (m) {
      var li = h("li", { class: m.approved ? "" : "held", "data-id": m.id },
        h("div", { class: "who" }, h("strong", { text: m.name }), m.role !== "attendee" ? badge(m.role) : null, !m.approved ? h("span", { text: runs() ? "Waiting for approval" : "Waiting for a moderator" }) : null),
        h("div", { text: m.text }));
      if (m.channel === "event" && m.approved) { li.appendChild(reactionRow(m)); }
      if (runs() && m.channel === "event") {
        li.appendChild(h("div", { class: "acts" },
          m.approved ? mini("On stage", function () { quietly({ type: "chat_overlay", id: m.id }); }) : null,
          !m.approved ? mini("Approve", function () { quietly({ type: "chat_approve", id: m.id }); }, "primary") : null,
          m.approved ? mini("Pin", function () { quietly({ type: "chat_pin", id: m.id }); }) : null,
          mini("To Q&A", function () { quietly({ type: "chat_to_qa", id: m.id }); }),
          m.identity && m.identity.indexOf("host-") !== 0 ? mini("Block", function () { quietly({ type: "block", identity: m.identity, on: true }); }) : null,
          mini("Delete", function () { quietly({ type: "chat_delete", id: m.id }); }, "danger")));
      }
      list.appendChild(li);
    });
    if (stick) { list.scrollTop = list.scrollHeight; }
    var pinned = S.state.pinned ? S.chat.filter(function (m) { return m.id === S.state.pinned; })[0] : null;
    var box = $("pinned");
    box.textContent = "";
    show(box, !!pinned && channel === "event");
    if (pinned) {
      box.appendChild(h("small", { text: "Pinned · " + pinned.name }));
      box.appendChild(h("span", { text: pinned.text }));
      if (runs()) { box.appendChild(document.createTextNode(" ")); box.appendChild(mini("Unpin", function () { quietly({ type: "chat_pin", id: null }); })); }
    }
    show($("chat-channels"), isCrew());
    document.querySelectorAll("[data-channel]").forEach(function (b) { b.setAttribute("aria-pressed", String(b.dataset.channel === channel)); });
    var canWrite = channel === "stage" || isCrew() || (S.state.settings.chatEnabled && !S.me.blocked);
    show($("chat-form"), canWrite);
    show($("chat-off"), !canWrite);
    $("chat-off").textContent = S.me.blocked ? "A moderator has stopped you posting here." : "Chat is off.";
    $("chat-input").placeholder = channel === "stage" ? "Message the crew" : "Say something";
  }

  document.querySelectorAll("[data-channel]").forEach(function (b) {
    b.addEventListener("click", function () { channel = b.dataset.channel; render(); });
  });
  $("chat-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var input = $("chat-input");
    var text = input.value.trim();
    if (!text) { return; }
    act({ type: "chat_send", text: text, channel: channel }).then(function () { input.value = ""; setError($("room-error"), ""); }).catch(function () {});
  });

  function renderQa() {
    var list = $("questions");
    // An answer being written stays where it is, until it's sent or put aside.
    if (list.querySelector("form")) { return; }
    list.textContent = "";
    var rows = Object.keys(S.qa).map(function (id) { return S.qa[id]; });
    var order = { pending: 0, approved: 1, hidden: 2, rejected: 3 };
    rows.sort(function (a, b) { return (order[a.status] - order[b.status]) || (a.answered - b.answered) || (b.votes - a.votes) || (a.at - b.at); });
    rows.forEach(function (q) {
      var voted = S.myVotes["qa:" + q.id] !== undefined;
      var li = h("li", { class: q.status === "approved" ? "" : "held", "data-id": q.id },
        h("div", { class: "who" }, h("strong", { text: q.name }),
          q.status !== "approved" ? h("span", { text: q.status === "pending" ? (runs() ? "Pending" : "Waiting for a moderator") : q.status }) : null,
          q.answered ? h("span", { class: "badge guest", text: "answered" }) : null,
          q.private ? h("span", { class: "badge", text: "private" }) : null),
        h("div", { text: q.text }));
      if (q.answer) { li.appendChild(h("div", { class: "answer" }, h("strong", { text: (q.answeredBy || "") + ": " }), document.createTextNode(q.answer))); }
      var acts = h("div", { class: "acts" });
      if (q.status === "approved" && (!q.private || isCrew())) {
        acts.appendChild(mini((voted ? "▲ " : "△ ") + q.votes, function () {
          var was = voted;
          act({ type: "qa_vote", id: q.id }).then(function (r) {
            if (r.voted) { S.myVotes["qa:" + q.id] = 1; } else { delete S.myVotes["qa:" + q.id]; }
            schedule();
          }).catch(function () { return was; });
        }, voted ? "primary" : ""));
      }
      if (isCrew()) {
        acts.appendChild(mini("Answer", function () {
          var input = h("input", { maxlength: "500", placeholder: "Type an answer" });
          var form = h("form", { class: "composer", onsubmit: function (event) {
            event.preventDefault();
            if (input.value.trim()) { act({ type: "qa_answer", id: q.id, text: input.value }).then(function () { form.remove(); schedule(); }).catch(function () {}); }
          } }, input, h("button", { class: "btn", type: "submit", text: "Send" }), h("button", { class: "btn btn-ghost", type: "button", text: "Cancel", onclick: function () { form.remove(); schedule(); } }));
          li.appendChild(form);
          input.focus();
        }));
      }
      if (runs()) {
        function mod(label, action, cls) { return mini(label, function () { quietly({ type: "qa_moderate", id: q.id, action: action }); }, cls); }
        if (q.status !== "approved") { acts.appendChild(mod("Approve", "approve", "primary")); }
        if (q.status === "pending") { acts.appendChild(mod("Reject", "reject")); }
        if (q.status === "approved") {
          acts.appendChild(mod(q.answered ? "Not answered" : "Mark answered", "answered"));
          acts.appendChild(mini("On stage", function () { quietly({ type: "qa_overlay", id: q.id }); }));
          acts.appendChild(q.private ? mod("Show to everyone", "public") : mod("Make private", "private"));
          acts.appendChild(mod("Hide", "hide"));
        }
        acts.appendChild(mod("Delete", "delete", "danger"));
      }
      if (acts.children.length) { li.appendChild(acts); }
      list.appendChild(li);
    });
    show($("qa-anon-wrap"), S.state.settings.qaAnonymous);
    var canAsk = isCrew() || (S.state.settings.qaEnabled && !S.me.blocked);
    show($("qa-form"), canAsk);
  }
  $("qa-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var input = $("qa-input");
    if (!input.value.trim()) { return; }
    act({ type: "qa_ask", text: input.value, anonymous: $("qa-anon").checked }).then(function () { input.value = ""; setError($("room-error"), ""); }).catch(function () {});
  });

  function renderPolls() {
    show($("poll-editor"), runs());
    var list = $("poll-list");
    list.textContent = "";
    var rows = Object.keys(S.polls).map(function (id) { return S.polls[id]; });
    rows.reverse();
    if (!rows.length) { list.appendChild(h("p", { class: "muted", text: "No polls yet." })); }
    rows.forEach(function (p) {
      var mine = S.myVotes["poll:" + p.id];
      var voted = mine !== undefined;
      var box = h("div", { class: "box", "data-poll": p.id }, h("h3", { text: p.question }));
      if (runs()) { box.appendChild(h("p", { class: "muted small", text: p.status === "draft" ? (p.openAfter !== null && p.openAfter !== undefined ? "Opens " + Math.round(p.openAfter / 60) + " min after going live" : "Draft") : p.status === "open" ? "Open · " + plural(p.total || 0, "vote") : "Closed · " + plural(p.total || 0, "vote") })); }
      p.options.forEach(function (text, i) {
        var count = p.tallies ? p.tallies[i] || 0 : null;
        var share = count === null || !p.total ? 0 : count / p.total;
        var canVote = p.status === "open" && !voted && !S.me.blocked;
        var seeResults = count !== null && (runs() || voted || p.status === "closed");
        var row = h(canVote ? "button" : "div", { class: "poll-row" + (mine === i ? " mine" : "") + (p.correct === i ? " correct" : ""), type: canVote ? "button" : null, onclick: canVote ? function () {
          act({ type: "poll_vote", id: p.id, option: i }).then(function () { S.myVotes["poll:" + p.id] = i; schedule(); }).catch(function () {});
        } : null });
        if (seeResults) { var bar = h("i"); bar.style.setProperty("width", Math.round(share * 100) + "%"); row.appendChild(bar); }
        row.appendChild(h("span", { text: text + (mine === i ? " (your vote)" : "") }));
        if (seeResults) { row.appendChild(h("b", { text: p.showCounts ? String(count) : Math.round(share * 100) + "%" })); }
        box.appendChild(row);
      });
      if (!runs() && voted && p.tallies === null) { box.appendChild(h("p", { class: "muted small", text: "Results appear when the poll closes." })); }
      if (runs()) {
        var acts = h("div", { class: "row" });
        var group = h("span", {});
        if (p.status === "draft") { group.appendChild(mini("Publish", function () { quietly({ type: "poll_status", id: p.id, status: "open" }); }, "primary")); }
        if (p.status === "open") { group.appendChild(mini("Close", function () { quietly({ type: "poll_status", id: p.id, status: "closed" }); })); }
        if (p.status === "closed") { group.appendChild(mini("Reopen", function () { quietly({ type: "poll_status", id: p.id, status: "open" }); })); }
        if (p.status !== "draft") {
          var staged = S.state.stagePoll === p.id;
          group.appendChild(mini(staged ? "Take off stage" : "Show on stage", function () { quietly({ type: "poll_stage", id: staged ? null : p.id }); }));
        }
        group.appendChild(armed("poll-" + p.id, "Delete", function () { quietly({ type: "poll_delete", id: p.id }); }, "danger"));
        acts.appendChild(group);
        box.appendChild(acts);
      }
      list.appendChild(box);
    });
  }
  $("poll-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var all = Array.from(document.querySelectorAll(".poll-opt")).map(function (i) { return i.value; });
    var options = all.filter(function (v) { return v.trim(); });
    // The right answer is chosen by its place in the form, and blanks don't count.
    var picked = $("poll-correct").value;
    var correct = picked === "" || !all[Number(picked)].trim() ? "" : options.indexOf(all[Number(picked)]);
    var openAfter = $("poll-open-after").value, closeAfter = $("poll-close-after").value;
    act({ type: "poll_save", id: null, question: $("poll-q").value, options: options, correct: correct === "" ? null : Number(correct), showResults: $("poll-results").value, showCounts: $("poll-counts").checked, popup: $("poll-popup").checked, openAfter: openAfter === "" ? null : Number(openAfter) * 60, closeAfter: closeAfter === "" ? null : Number(closeAfter) }).then(function () {
      $("poll-form").reset();
      $("poll-editor").open = false;
      setError($("room-error"), "");
    }).catch(function () {});
  });

  function plural(n, word) { return n + " " + word + (n === 1 ? "" : "s"); }
  function size(n) { return n > 1048576 ? (n / 1048576).toFixed(1) + " MB" : Math.max(1, Math.round(n / 1024)) + " KB"; }

  function upload(file, kind, input) {
    if (!file) { return; }
    setError($("room-error"), "");
    var url = base + "/files?c=" + clientId + "&kind=" + kind + "&name=" + encodeURIComponent(file.name);
    return fetch(url, { method: "PUT", body: file }).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (d) {
        if (!res.ok) { var err = new Error(d.error); err.code = d.error; throw err; }
        return d;
      });
    }).catch(function (err) { setError($("room-error"), message(err)); }).then(function (d) { if (input) { input.value = ""; } return d; });
  }

  var recordings = null, recordingSpeakers = null, localFiles = null;
  function loadRecordings() {
    if (!runs() || !S.recordingAvailable) { return; }
    act({ type: "list_recordings" }).then(function (d) { recordings = d.recordings; recordingSpeakers = d.speakers; localFiles = d.local; schedule(); }).catch(function () {});
  }

  function renderRecordings() {
    var box = $("recording-list");
    box.textContent = "";
    show(box, runs() && S.recordingAvailable);
    if (!runs() || !S.recordingAvailable) { return; }
    box.appendChild(h("div", { class: "row" }, h("h3", { text: "Recordings and captions" }), mini("Check again", loadRecordings)));
    var speakers = recordingSpeakers;
    if (speakers && speakers.length) {
      var captionFile = function (label, file, speaker) {
        return h("a", { href: base + "/captions/" + file + (speaker ? "?speaker=" + encodeURIComponent(speaker) : ""), download: "", text: label });
      };
      var line = h("div", { class: "file small", "data-captions": "all" }, h("span", { text: "Everyone, from going live" }), h("span", {}, captionFile("SRT", "all.srt"), document.createTextNode(" · "), captionFile("Transcript", "all.txt")));
      box.appendChild(line);
      speakers.forEach(function (who) {
        box.appendChild(h("div", { class: "file small", "data-captions": who.name }, h("span", { text: who.name }), h("span", {}, captionFile("SRT", "all.srt", who.identity), document.createTextNode(" · "), captionFile("Transcript", "all.txt", who.identity))));
      });
    }
    (localFiles || []).forEach(function (r) {
      box.appendChild(h("div", { class: "file small", "data-local-recording": r.name }, h("a", { href: base + "/local/" + r.id, download: "", text: r.name + "'s own recording" }), h("span", { class: "muted", text: size(r.size) + (r.finished ? "" : " · still uploading") })));
    });
    if (!recordings || !recordings.length) { box.appendChild(h("p", { class: "muted small", text: recordings ? "Nothing recorded yet." : "Press Check again to see them." })); return; }
    recordings.forEach(function (r) {
      var label = r.status === "complete" ? Math.max(1, Math.round(r.durationSeconds / 60)) + " min · " + size(r.sizeBytes) : r.status === "failed" ? "Failed" : "Still recording";
      box.appendChild(h("div", { class: "file" },
        r.publicUrl ? h("a", { href: r.publicUrl, rel: "noopener noreferrer", target: "_blank", text: r.filename.split("/").pop() }) : h("span", { text: r.filename.split("/").pop() || "Recording" }),
        h("span", { class: "muted small", text: label })));
      (r.captions || []).forEach(function (f) {
        var text = f.speaker + " · " + (f.kind === "srt" ? "SRT" : "transcript");
        box.appendChild(h("div", { class: "file small", "data-recording-caption": f.filename }, f.url ? h("a", { href: f.url, rel: "noopener noreferrer", target: "_blank", text: text }) : h("span", { text: text })));
      });
    });
  }

  function renderFiles() {
    renderRecordings();
    show($("file-upload"), runs());
    var list = $("file-list");
    list.textContent = "";
    var rows = S.files.filter(function (f) { return f.kind === undefined || f.kind === "resource"; });
    if (isCrew() && !runs()) { rows = rows.filter(function (f) { return f.public; }); }
    if (!rows.length) { list.appendChild(h("p", { class: "muted", text: runs() ? "Nothing here yet." : "Nothing to download yet." })); }
    rows.forEach(function (f) {
      var acts = h("div", { class: "acts" });
      if (runs()) {
        acts.appendChild(mini(f.public ? "Shared · stop" : "Share", function () { quietly({ type: "file_public", id: f.id, public: !f.public }); }, f.public ? "" : "primary"));
        acts.appendChild(armed("file-" + f.id, "Delete", function () { quietly({ type: "file_delete", id: f.id }); }, "danger"));
      }
      list.appendChild(h("div", { class: "file", "data-file": f.id },
        h("div", {}, h("a", { href: fileUrl(f.id), download: f.name, text: f.name }), h("div", { class: "muted small", text: size(f.size) + (runs() ? " · " + plural(f.downloads || 0, "download") : "") })),
        acts));
    });
  }
  $("resource-file").addEventListener("change", function () { upload(this.files[0], "resource", this); });

  function renderPeople() {
    var list = $("people-list");
    list.textContent = "";
    var term = $("people-search").value.trim().toLowerCase();
    var rank = { host: 0, producer: 1, guest: 2, attendee: 3 };
    var rows = S.people.filter(function (p) { return !term || p.name.toLowerCase().indexOf(term) >= 0; });
    rows.sort(function (a, b) { return (b.hand - a.hand) || (rank[a.role] - rank[b.role]) || a.name.localeCompare(b.name); });
    rows.forEach(function (p) {
      var acts = h("div", { class: "acts" });
      if (p.identity !== S.me.identity) {
        if (p.role === "attendee") {
          acts.appendChild(p.invited
            ? mini("Cancel invite", function () { quietly({ type: "invite", identity: p.identity, on: false }); })
            : mini("Invite on stage", function () { quietly({ type: "invite", identity: p.identity, on: true }); }, "primary"));
          acts.appendChild(mini(p.blocked ? "Unblock" : "Block", function () { quietly({ type: "block", identity: p.identity, on: !p.blocked }); }));
          acts.appendChild(armed("remove-" + p.identity, "Remove", function () { quietly({ type: "remove", identity: p.identity }); }, "danger"));
        } else if (p.role === "guest") {
          if (p.identity.indexOf("host-") !== 0) { acts.appendChild(mini("Back to audience", function () { quietly({ type: "demote", identity: p.identity }); })); }
          acts.appendChild(armed("remove-" + p.identity, "Remove", function () { quietly({ type: "remove", identity: p.identity }); }, "danger"));
        }
      }
      list.appendChild(h("div", { class: "person", "data-person": p.identity },
        h("div", {}, h("strong", { text: p.name }), document.createTextNode(" "), badge(p.role),
          p.hand ? h("span", { class: "badge on", text: "Hand up" }) : null,
          p.invited ? h("span", { class: "muted small", text: " Invite pending" }) : null,
          p.blocked ? h("span", { class: "muted small", text: " Blocked" }) : null),
        acts));
    });
    if (!rows.length) { list.appendChild(h("p", { class: "muted", text: "Nobody here yet." })); }
    (S.removedPeople || []).forEach(function (p) {
      list.appendChild(h("div", { class: "person", "data-removed": p.identity }, h("div", { class: "muted", text: (p.name || "Someone") + " was removed" }),
        mini("Let back in", function () {
          act({ type: "restore", identity: p.identity }).then(function () {
            S.removedPeople = S.removedPeople.filter(function (x) { return x.identity !== p.identity; });
            schedule();
          }).catch(function () {});
        })));
    });
  }
  $("people-search").addEventListener("input", schedule);

  // ---- look: frames, overlays, calls to action, video

  var ASSETS = [
    { slot: "frame", key: "frameId", title: "Frame", help: "A 16:9 PNG with a transparent middle. It sits on top of the windows." },
    { slot: "background", key: "backgroundId", title: "Background", help: "A 16:9 PNG behind the windows." },
    { slot: "logo", key: "logoId", title: "Logo", help: "A PNG in the top right." },
    { slot: "visual", key: "visualId", title: "Full-stage image", help: "Covers the stage. The audience sees this and not the people, until you turn it off." },
    { slot: "banner", key: "bannerId", title: "Banner before going live", help: "A PNG, JPEG or WebP attendees see while they wait.", accept: ".png,.jpg,.jpeg,.webp,image/png,image/jpeg,image/webp" }
  ];

  var lookBuilt = false;
  var lookInputs = {};
  function buildLook() {
    if (lookBuilt) { return; }
    lookBuilt = true;
    var look = $("look");
    function saveTheme(field, value) {
      var next = Object.assign({ accent: "", stage: "", mode: "", font: "" }, S.state.theme || {});
      next[field] = value;
      quietly({ type: "set_theme", theme: next });
    }
    look.appendChild(h("div", { class: "box", id: "look-brand" }, h("h3", { text: "Brand for this session" }),
      h("p", { class: "muted small", text: "Your company brand is set at /admin. Anything you change here applies to this session only." }),
      h("div", { class: "colours" },
        h("label", {}, h("input", { type: "color", id: "look-accent", onchange: function () { saveTheme("accent", this.value); } }), document.createTextNode("Brand colour")),
        h("label", {}, h("input", { type: "color", id: "look-stage", onchange: function () { saveTheme("stage", this.value); } }), document.createTextNode("Stage background"))),
      h("label", { for: "look-mode", class: "small", text: "Attendee page" }),
      h("select", { id: "look-mode", onchange: function () { saveTheme("mode", this.value); } },
        h("option", { value: "", text: "As set at /admin" }), h("option", { value: "light", text: "Light" }), h("option", { value: "dark", text: "Dark" })),
      h("label", { for: "look-font", class: "small", text: "Font" }),
      h("select", { id: "look-font", onchange: function () { saveTheme("font", this.value); } },
        h("option", { value: "", text: "As set at /admin" }), h("option", { value: "system", text: "Clean" }), h("option", { value: "serif", text: "Serif" }), h("option", { value: "rounded", text: "Rounded" }), h("option", { value: "mono", text: "Monospace" }), h("option", { value: "custom", text: "The font uploaded at /admin" })),
      h("div", { class: "row end", id: "look-reset" }, mini("Back to the company brand", function () { quietly({ type: "set_theme", theme: { accent: "", stage: "", mode: "", font: "" } }); }))));
    ASSETS.forEach(function (a) {
      var input = h("input", { type: "file", accept: a.accept || ".png,image/png", "data-upload": a.slot, onchange: function () {
        var el = this;
        upload(el.files[0], a.slot, el).then(function (d) { if (d && d.id) { quietly({ type: "set_asset", slot: a.slot, id: d.id }); } });
      } });
      look.appendChild(h("div", { class: "box" }, h("h3", { text: a.title }), h("p", { class: "muted small", text: a.help }), input, h("div", { class: "thumbs", id: "thumbs-" + a.slot })));
    });
    var frameBox = $("thumbs-frame").parentNode;
    frameBox.appendChild(h("label", { class: "small", for: "frame-inset", text: "Keep windows inside the frame" }));
    frameBox.appendChild(h("input", { type: "range", id: "frame-inset", min: "0", max: "25", step: "1", value: "0", onchange: function () {
      quietly({ type: "set_layout", mode: S.state.layout.mode, focus: S.state.layout.focus || null, inset: Number(this.value) / 100 });
    } }));
    var ltTitle = h("input", { maxlength: "200", placeholder: "Title", id: "lt-title" });
    var ltSub = h("input", { maxlength: "200", placeholder: "Subtitle", id: "lt-sub" });
    look.appendChild(h("div", { class: "box" }, h("h3", { text: "Lower third" }), ltTitle, ltSub, h("div", { class: "row end" },
      mini("Hide", function () { quietly({ type: "set_lower_third", title: null, subtitle: null }); }),
      mini("Show", function () { quietly({ type: "set_lower_third", title: ltTitle.value, subtitle: ltSub.value }); }, "primary"))));
    var ticker = h("input", { maxlength: "500", placeholder: "Scrolling text", id: "ticker-text" });
    look.appendChild(h("div", { class: "box" }, h("h3", { text: "Ticker" }), ticker, h("div", { class: "row end" },
      mini("Hide", function () { quietly({ type: "set_ticker", text: null }); }),
      mini("Show", function () { quietly({ type: "set_ticker", text: ticker.value }); }, "primary"))));
    var cTitle = h("input", { maxlength: "200", placeholder: "Title", id: "cta-title" });
    var cSub = h("input", { maxlength: "200", placeholder: "Subtitle (optional)", id: "cta-sub" });
    var cLabel = h("input", { maxlength: "60", placeholder: "Button text", id: "cta-label" });
    var cUrl = h("input", { maxlength: "1000", placeholder: "https://", id: "cta-url" });
    look.appendChild(h("div", { class: "box" }, h("h3", { text: "Call to action" }), cTitle, cSub, cLabel, cUrl, h("div", { class: "row end" },
      mini("Hide", function () { quietly({ type: "set_cta", title: null, subtitle: null, label: null, url: null }); }),
      mini("Show", function () { quietly({ type: "set_cta", title: cTitle.value, subtitle: cSub.value, label: cLabel.value, url: cUrl.value }); }, "primary"))));
    var graphicInput = h("input", { type: "file", accept: ".gif,.webp,.png,.mp4,.webm,image/gif,image/webp,image/png,video/mp4,video/webm", "data-upload": "graphic", onchange: function () {
      var el = this;
      var name = el.files[0] ? el.files[0].name : "";
      upload(el.files[0], "graphic", el).then(function (d) {
        if (d && d.id) { quietly({ type: "layer_save", layer: Object.assign({ id: "", fileId: d.id, name: name, visible: true, title: "", subtitle: "", scroll: false, mime: "" }, { x: PLACES.chyron.x, y: PLACES.chyron.y, w: PLACES.chyron.w, h: PLACES.chyron.h }) }); }
      });
    } });
    look.appendChild(h("div", { class: "box", id: "layers-box" }, h("h3", { text: "On-screen graphics" }),
      h("p", { class: "muted small", text: "A looping GIF or video, placed anywhere on the picture, with live words on top if you want them. It's how you make an animated chyron or ticker. Use a WebM with transparency for a shape that isn't a rectangle. Videos here play silently. Drag them with Move and resize." }),
      graphicInput,
      h("div", { class: "row end" }, mini("Add words with no graphic", function () { quietly({ type: "layer_save", layer: { id: "", fileId: null, name: "", visible: false, title: "Your words here", subtitle: "", scroll: false, mime: "", x: PLACES.chyron.x, y: PLACES.chyron.y, w: PLACES.chyron.w, h: PLACES.chyron.h } }); })),
      h("div", { id: "layer-list" })));
    var mediaInput = h("input", { type: "file", accept: ".mp4,video/mp4", "data-upload": "media", onchange: function () { upload(this.files[0], "media", this); } });
    look.appendChild(h("div", { class: "box" }, h("h3", { text: "Video" }), h("p", { class: "muted small", text: "An MP4 played right on the stage. It looks better than sharing your screen, and it's in the recording." }), mediaInput, h("div", { id: "media-list" })));
    var simFile = h("select", { id: "sim-file", "aria-label": "Which video" });
    var simAt = h("input", { id: "sim-at", type: "datetime-local", "aria-label": "When it goes live" });
    var simEnd = h("input", { id: "sim-end", type: "checkbox", checked: true });
    look.appendChild(h("div", { class: "box", id: "sim-box" }, h("h3", { text: "Play a recorded session as if live" }),
      h("p", { class: "muted small", text: "The session goes live by itself at this time and plays the video. Chat, questions and polls work as usual." }),
      h("div", { id: "sim-status" }),
      h("div", { id: "sim-form" }, simFile, simAt, h("label", { class: "check small" }, simEnd, document.createTextNode(" End the session when the video ends")),
        h("div", { class: "row end" }, mini("Schedule it", function () {
          if (!simFile.value || !simAt.value) { setError($("room-error"), "Choose a video and a time."); return; }
          if (new Date(simAt.value).getTime() <= Date.now()) { setError($("room-error"), MESSAGES.time_passed); return; }
          var probe = h("video", { preload: "metadata" });
          probe.onloadedmetadata = function () {
            quietly({ type: "simulive_set", fileId: simFile.value, atMs: new Date(simAt.value).getTime(), duration: probe.duration, autoEnd: simEnd.checked });
          };
          probe.onerror = function () { setError($("room-error"), "That video couldn't be read."); };
          probe.src = fileUrl(simFile.value);
        }, "primary")))));
    var deckInput = h("input", { type: "file", accept: ".pdf,.pptx,.ppt,.odp", "data-upload": "deck", onchange: function () { upload(this.files[0], "deck", this); } });
    look.appendChild(h("div", { class: "box", id: "deck-box" }, h("h3", { text: "Slides" }),
      h("p", { class: "muted small", text: "A PDF or PowerPoint, shown on the stage page by page. Animations and transitions aren't kept. Speaker notes show only to the crew." }),
      deckInput, h("div", { id: "deck-list" })));
    lookInputs = { ltTitle: ltTitle };
  }

  function renderLook() {
    buildLook();
    var t = theme();
    var own = S.state.theme || {};
    [["look-accent", t.accent], ["look-stage", t.stage]].forEach(function (pair) {
      if (document.activeElement !== $(pair[0])) { $(pair[0]).value = pair[1]; }
    });
    if (document.activeElement !== $("look-mode")) { $("look-mode").value = own.mode || ""; }
    if (document.activeElement !== $("look-font")) { $("look-font").value = own.font || ""; }
    show($("look-reset"), !!(own.accent || own.stage || own.mode || own.font));
    var insetInput = $("frame-inset");
    if (document.activeElement !== insetInput) { insetInput.value = String(Math.round((S.state.layout.inset || 0) * 100)); }
    ASSETS.forEach(function (a) {
      var row = $("thumbs-" + a.slot);
      var current = S.state[a.key];
      // Each thumbnail is a download. Only when something changed.
      var drawn = String(current) + S.files.filter(function (f) { return f.kind === a.slot; }).map(function (f) { return f.id; }).join();
      if (row.dataset.drawn === drawn) { return; }
      row.dataset.drawn = drawn;
      row.textContent = "";
      row.appendChild(h("button", { class: "thumb thumb-none" + (current ? "" : " on"), type: "button", text: "None", onclick: function () { quietly({ type: "set_asset", slot: a.slot, id: null }); } }));
      S.files.filter(function (f) { return f.kind === a.slot; }).forEach(function (f) {
        row.appendChild(h("button", { class: "thumb" + (current === f.id ? " on" : ""), type: "button", title: f.name, "aria-label": f.name, onclick: function () { quietly({ type: "set_asset", slot: a.slot, id: f.id }); } },
          h("img", { src: fileUrl(f.id), alt: "" })));
      });
    });
    renderLayers();
    renderDecks();
    renderSimulive();
    var media = $("media-list");
    media.textContent = "";
    S.files.filter(function (f) { return f.kind === "media"; }).forEach(function (f) {
      var playing = S.state.media && S.state.media.id === f.id;
      var acts = h("div", { class: "acts" });
      if (playing) {
        acts.appendChild(mini(S.state.media.playing ? "Pause" : "Resume", function () {
          var m = S.state.media;
          var position = m.position + (m.playing ? (Date.now() + serverOffset - m.atMs) / 1000 : 0);
          quietly({ type: "media_control", playing: !m.playing, position: position });
        }));
        acts.appendChild(mini("Stop", function () { quietly({ type: "media_stop" }); }, "danger"));
      } else {
        acts.appendChild(mini("Play on stage", function () { quietly({ type: "media_play", id: f.id }); }, "primary"));
      }
      media.appendChild(h("div", { class: "file" }, h("div", { text: f.name }), acts));
    });
  }

  function renderDecks() {
    show($("deck-box"), !!S.state.slidesAvailable);
    var list = $("deck-list");
    list.textContent = "";
    S.files.filter(function (f) { return f.kind === "deck"; }).forEach(function (f) {
      var showing = S.state.slides && S.state.slides.fileId === f.id;
      var status = f.status === "ready" ? plural(f.pages, "page") : f.status === "failed" ? "Couldn't be converted" : "Converting…";
      list.appendChild(h("div", { class: "file", "data-deck": f.name, "data-status": f.status || "converting" }, h("div", {}, h("div", { text: f.name }), h("div", { class: "muted small", text: status })),
        h("div", { class: "acts" },
          f.status === "ready" ? (showing ? mini("Take off stage", function () { quietly({ type: "slides_stop" }); }) : mini("Show on stage", function () { quietly({ type: "slides_show", id: f.id, page: 1 }); }, "primary")) : null,
          mini("Delete", function () { quietly({ type: "file_delete", id: f.id }); }, "danger"))));
    });
  }

  function renderSimulive() {
    var sim = S.state.simulive;
    var status = $("sim-status");
    status.textContent = "";
    show($("sim-form"), !sim && S.state.phase === "not_live");
    if (sim) {
      var file = S.files.filter(function (f) { return f.id === sim.fileId; })[0];
      status.appendChild(h("p", { class: "small", "data-sim": sim.started ? "started" : "waiting", text: (sim.started ? "Playing " : "Goes live at " + new Date(sim.atMs).toLocaleString() + " with ") + (file ? file.name : "the video") }));
      if (!sim.started) { status.appendChild(mini("Cancel", function () { quietly({ type: "simulive_clear" }); }, "danger")); }
    }
    var select = $("sim-file");
    if (document.activeElement !== select) {
      var value = select.value;
      select.textContent = "";
      S.files.filter(function (f) { return f.kind === "media"; }).forEach(function (f) { select.appendChild(h("option", { value: f.id, text: f.name })); });
      if (value) { select.value = value; }
    }
  }

  // ---- on-screen graphics: a GIF or video that loops, with live words on it if you want them

  var PLACES = {
    chyron: { label: "Lower third", x: 0.03, y: 0.74, w: 0.5, h: 0.16 },
    ticker: { label: "Ticker band", x: 0, y: 0.92, w: 1, h: 0.08 },
    bug: { label: "Corner", x: 0.84, y: 0.04, w: 0.13, h: 0.16 },
    side: { label: "Side panel", x: 0.72, y: 0.1, w: 0.26, h: 0.7 },
    full: { label: "Full stage", x: 0, y: 0, w: 1, h: 1 }
  };

  function layerText(l, patch) {
    quietly({ type: "layer_save", layer: Object.assign({}, l, patch) });
  }

  function renderLayers() {
    var list = $("layer-list");
    // Don't pull the text box out from under someone typing, unless a graphic came or went.
    var ids = (S.state.layers || []).map(function (l) { return l.id; }).join();
    var typing = list.contains(document.activeElement) && ["INPUT", "SELECT"].indexOf(document.activeElement.tagName) >= 0 && document.activeElement.type !== "checkbox";
    if (typing && list.dataset.ids === ids) { return; }
    list.dataset.ids = ids;
    list.textContent = "";
    (S.state.layers || []).forEach(function (l) {
      var title = h("input", { maxlength: "500", placeholder: "Words on it (optional)", value: l.title, "data-layer-title": l.id, onchange: function () { layerText(l, { title: this.value }); } });
      var subtitle = h("input", { maxlength: "200", placeholder: "Second line (optional)", value: l.subtitle, onchange: function () { layerText(l, { subtitle: this.value }); } });
      var place = h("select", { "aria-label": "Where it goes", onchange: function () { var p = PLACES[this.value]; if (p) { layerText(l, { x: p.x, y: p.y, w: p.w, h: p.h }); } } }, h("option", { value: "", text: "Where it goes" }));
      Object.keys(PLACES).forEach(function (key) { place.appendChild(h("option", { value: key, text: PLACES[key].label })); });
      list.appendChild(h("div", { class: "dest", "data-layer": l.id },
        h("div", { class: "dest-head" }, h("strong", { text: l.name || l.title || "Text" }),
          h("span", {}, mini(l.visible ? "Hide" : "Show", function () { layerText(l, { visible: !l.visible }); }, l.visible ? "" : "primary"), document.createTextNode(" "), mini("Delete", function () { quietly({ type: "layer_delete", id: l.id }); }, "danger"))),
        title, subtitle, place,
        h("label", { class: "check small" }, h("input", { type: "checkbox", checked: l.scroll, onchange: function () { layerText(l, { scroll: this.checked }); } }), document.createTextNode(" Scroll the words, like a ticker"))));
    });
  }

  var PLATFORM_NAMES = { youtube: "YouTube", meta: "Meta", x: "X", linkedin: "LinkedIn" };
  var PLATFORM_TIPS = {
    youtube: "On YouTube, the event starts by itself only if auto-start is on. Otherwise press Go live in YouTube Studio.",
    meta: "Press Go live in Meta's Live Producer once the stream shows there.",
    x: "Press Go live in X's Media Studio once the stream shows there.",
    linkedin: "Press Go live in LinkedIn's Live Studio once the preview shows there."
  };
  var STATE_LABELS = { off: "Off", starting: "Connecting", live: "Sending", failed: "Failed" };

  function renderBroadcast() {
    var rows = S.broadcast || [];
    var live = S.state.phase === "live";
    $("broadcast-intro").textContent = rows.length
      ? (live ? "Turn a platform on or off without affecting anyone else." : "Anything marked to go out starts when you go live.")
      : "Nothing is set up. Stream keys are added at /admin, on this session's page.";
    var list = $("broadcast-list");
    list.textContent = "";
    rows.forEach(function (d) {
      var going = d.status === "live" || d.status === "starting";
      var label = !live && d.armed && S.state.phase !== "ended" ? "Goes out when live" : STATE_LABELS[d.status] || d.status;
      var button = S.state.phase === "ended" ? null : live
        ? mini(going ? "Stop" : "Start", function () { quietly({ type: "broadcast_toggle", platform: d.platform, on: !going }); }, going ? "danger" : "primary")
        : mini(d.armed ? "Don't send" : "Send when live", function () { quietly({ type: "broadcast_toggle", platform: d.platform, on: !d.armed }); });
      list.appendChild(h("div", { class: "dest", "data-dest": d.platform },
        h("div", { class: "dest-head" }, h("strong", { text: PLATFORM_NAMES[d.platform] || d.platform }), h("span", { class: "dest-state " + d.status, text: label }), button),
        d.error ? h("p", { class: "error small", text: d.error }) : null,
        going || (!live && d.armed) ? h("p", { class: "muted small", text: PLATFORM_TIPS[d.platform] }) : null));
    });
  }

  var SETTINGS = [
    ["chatEnabled", "Chat", "Attendees can read and post in chat."],
    ["chatReview", "Review chat before it appears", "A host or producer approves each attendee message."],
    ["reactionsEnabled", "Reactions", "Attendees can send emoji over the stage."],
    ["qaEnabled", "Q&A", "Attendees can ask questions."],
    ["qaModerated", "Review questions before they appear", "New questions wait for approval."],
    ["qaAnonymous", "Allow anonymous questions", ""],
    ["pollsEnabled", "Polls", ""],
    ["resourcesEnabled", "Files", "Attendees can see and download what you've shared."],
    ["handRaising", "Raise hand", "Attendees can ask to come on stage."],
    ["profanityFilter", "Hide common swear words", "In chat and questions."],
    ["qaPrivate", "Keep questions private", "Attendees' questions are seen only by the crew and whoever asked. Answers too."],
    ["localRecordings", "Each speaker records their own copy", "While you're recording, each person on stage also records their own camera and microphone in full quality, uploaded as they go. It uses more of their computer and upload speed."],
    ["captions", "Live captions", "Transcribes the people on stage while live, per speaker, and saves SRT and transcript files with the recording. Each viewer turns the display on or off."],
    ["captionsOnPicture", "Put captions on the picture", "Everyone sees them, and they're in recordings and broadcasts."],
    ["producersCanGoLive", "Producers can go live and end the session", "Only a host can change this."]
  ];
  function renderSettings() {
    var box = $("settings");
    box.textContent = "";
    SETTINGS.forEach(function (s) {
      var locked = s[0] === "producersCanGoLive" && S.me.role !== "host";
      box.appendChild(h("label", { class: "setting" },
        h("input", { type: "checkbox", "data-setting": s[0], checked: S.state.settings[s[0]], disabled: locked, onchange: function () {
          var next = Object.assign({}, S.state.settings);
          next[s[0]] = this.checked;
          quietly({ type: "set_settings", settings: next });
        } }),
        h("span", {}, document.createTextNode(s[1]), s[2] ? h("small", { text: s[2] }) : null)));
    });
  }

  // ---- the green room strip

  var videoCache = {};
  function renderGreen() {
    var strip = $("green-strip");
    var crew = S.people.filter(function (p) { return p.role !== "attendee"; });
    if (S.me && !crew.some(function (p) { return p.identity === S.me.identity; })) { crew.push({ identity: S.me.identity, name: myName, role: S.me.role }); }
    var seen = {};
    crew.forEach(function (p) {
      seen[p.identity] = true;
      var onStage = S.state.stage.some(function (m) { return m.identity === p.identity; });
      var tile = strip.querySelector('[data-identity="' + CSS.escape(p.identity) + '"]');
      if (!tile) {
        tile = h("div", { class: "gtile", "data-identity": p.identity });
        tile.appendChild(h("div", { class: "gtile-meta" }));
        tile.appendChild(h("div", { class: "gtile-actions" }));
        strip.appendChild(tile);
      }
      tile.classList.toggle("on-stage", onStage);
      var meta = tile.querySelector(".gtile-meta");
      meta.textContent = "";
      meta.appendChild(h("b", { text: p.name + (p.identity === S.me.identity ? " (you)" : "") }));
      meta.appendChild(badge(p.role));
      if (onStage) { meta.appendChild(h("span", { class: "badge on", text: "On stage" })); }
      var track = onStage ? null : greenVideo[p.identity];
      var cached = videoCache[p.identity];
      if (cached && cached.track !== track) { cached.track.detach(cached.el); cached.el.remove(); delete videoCache[p.identity]; }
      if (track && !videoCache[p.identity]) {
        var el = track.attach();
        el.muted = true;
        tile.insertBefore(el, tile.firstChild);
        videoCache[p.identity] = { track: track, el: el };
      }
      var actions = tile.querySelector(".gtile-actions");
      actions.textContent = "";
      if (runs()) {
        if (onStage) {
          actions.appendChild(mini("To green room", function () { quietly({ type: "stage_remove", identity: p.identity }); }));
        } else if (p.role !== "producer") {
          actions.appendChild(mini("Put on stage", function () { quietly({ type: "stage_add", identity: p.identity }); }, "primary"));
        }
      }
    });
    Array.from(strip.children).forEach(function (tile) {
      var id = tile.dataset.identity;
      if (!seen[id]) {
        if (videoCache[id]) { videoCache[id].track.detach(videoCache[id].el); delete videoCache[id]; }
        tile.remove();
      }
    });
  }

  function renderBar() {
    var st = S.state;
    var crew = isCrew();
    $("role-pill").textContent = S.me.role;
    show($("role-pill"), crew);
    var pill = $("phase-pill");
    show(pill, crew || st.phase === "live");
    pill.classList.toggle("pill-live", st.phase === "live");
    pill.textContent = st.phase === "live" ? "Live" : st.phase === "ended" ? "Ended" : "Not live";
    show($("rec-pill"), st.recording);
    show($("viewers-pill"), crew);
    var casting = runs() ? (S.broadcast || []).filter(function (d) { return d.status === "live"; }).length : 0;
    var castFailed = runs() ? (S.broadcast || []).filter(function (d) { return d.status === "failed"; }).length : 0;
    show($("cast-pill"), casting > 0 || castFailed > 0);
    $("cast-pill").textContent = castFailed ? "Broadcast problem" : "Broadcasting to " + casting;
    $("viewers-pill").textContent = S.viewers + (S.viewers === 1 ? " attendee" : " attendees");
    show($("audience-view"), crew);
    show($("go-live"), S.me.perms.goLive && st.phase === "not_live");
    show($("end"), S.me.perms.goLive && st.phase === "live");
    show($("record"), runs() && S.recordingAvailable && st.phase !== "ended");
    syncToggle($("record"), st.recording, "Stop recording", "Start recording");
    show($("layouts"), runs());
    document.querySelectorAll("[data-layout]").forEach(function (b) { b.setAttribute("aria-pressed", String(st.layout.mode === b.dataset.layout)); });
    show($("my-controls"), crew);
    show($("viewer-controls"), !crew);
    show($("green"), crew);
    show($("screen"), crew && (runs() || onStageNow));
    show($("hand"), !crew && st.phase === "live" && st.settings.handRaising && !S.me.blocked);
    show($("reactions"), st.phase === "live" && st.settings.reactionsEnabled && !S.me.blocked);
    $("green-note").textContent = "· the audience can't see or hear anyone here";
    var note = $("notice");
    var text = "", air = false;
    if (crew) {
      if (st.phase === "ended") { text = "The session has ended."; }
      else if (S.me.role === "producer") { text = "You're producing. The audience can't see or hear you. On-stage speakers can hear you, so keep it short."; }
      else if (onStageNow && st.phase === "live") { text = "You're on air. The audience can see and hear you."; air = true; }
      else if (onStageNow) { text = "You're on stage, but the session isn't live yet. Only the crew can see you."; }
      else if (S.me.role === "guest") { text = "You're in the green room. A host or producer will bring you on stage."; }
      else { text = "You're in the green room. Put yourself on stage when you're ready."; }
    }
    note.textContent = text;
    note.classList.toggle("on-air", air);
    show(note, !!text);
  }

  function render() {
    if (!S.state || !S.me || program) { return; }
    renderBar();
    renderTabs();
    if (tab === "chat") { renderChat(); }
    if (tab === "qa") { renderQa(); }
    if (tab === "polls") { renderPolls(); renderSurveyEditor(); }
    if (tab === "rooms") { renderRooms(); }
    if (tab === "files") { renderFiles(); }
    if (tab === "people") { renderPeople(); }
    if (tab === "look") { renderLook(); }
    if (tab === "broadcast") { renderBroadcast(); renderIngress(); }
    if (tab === "settings" && !$("settings").contains(document.activeElement)) { renderSettings(); }
    if (isCrew()) { renderGreen(); }
    if (runs()) { renderSavedLayouts(); }
    renderPollPop();
    renderSurveyForm();
    renderSlideControls();
    if (!isCrew()) { paintBanner(); }
    if (breakoutRoom) { Array.from($("audio-sink").children).forEach(function (el) { el.muted = true; }); }
  }

  // ---- reactions, hand, invitations

  ["👏", "❤️", "😂", "🎉", "👍", "🤔"].forEach(function (emoji) {
    $("reactions").appendChild(h("button", { type: "button", "aria-label": "React " + emoji, text: emoji, onclick: function () { quietly({ type: "react", emoji: emoji }); } }));
  });
  $("hand").addEventListener("click", function () {
    var button = this;
    var up = button.getAttribute("aria-pressed") !== "true";
    act({ type: "hand", up: up }).then(function () { syncToggle(button, up, "Lower hand", "Raise hand"); }).catch(function () {});
  });
  $("invite-no").addEventListener("click", function () { show($("invite-modal"), false); syncToggle($("hand"), false, "Lower hand", "Raise hand"); quietly({ type: "invite_answer", accept: false }); });
  // Nobody has a link: tell them where to get one.
  function noLink() {
    fetch("/" + slug + "/public").then(function (res) { return res.ok ? res.json() : null; }).then(function (d) {
      if (!d) { return; }
      $("title").textContent = d.title;
      document.title = d.title;
      $("gate-title").textContent = d.title;
      $("gate-note").textContent = "You'll need your personal link to get in.";
      if (d.entryUrl) {
        var note = $("gate-note");
        note.textContent = "";
        note.appendChild(h("a", { href: d.entryUrl, text: "Go to the waiting room to get in" }));
      }
    });
  }

  $("invite-yes").addEventListener("click", function () {
    act({ type: "invite_answer", accept: true }).then(function () { window.location.reload(); }).catch(function () {});
  });

  // ---- entering and leaving

  function leave(note) {
    leaving = true;
    if (source) { source.close(); source = null; }
    if (stage) { stage.stopMedia(); }
    stopShare();
    stopLocalRecording();
    if (breakoutRoom) { var br = breakoutRoom; breakoutRoom = null; br.disconnect(); }
    [stageRoom, greenRoom].forEach(function (room) { if (room) { room.disconnect(); } });
    stageRoom = greenRoom = null;
    stopPreview();
    show($("room"), false);
    show($("gate"), true);
    show($("join-form"), false);
    $("gate-title").textContent = note ? "You're out of the session" : "You've left the session";
    $("gate-note").textContent = note || "";
    $("gate").appendChild(h("button", { class: "btn btn-primary", type: "button", text: "Join again", onclick: function () { window.location.reload(); } }));
    ["go-live", "end", "record", "audience-view", "viewers-pill", "phase-pill", "rec-pill", "role-pill"].forEach(function (id) { show($(id), false); });
  }
  $("leave").addEventListener("click", function () { leave(); });
  $("leave-crew").addEventListener("click", function () { leave(); });
  window.addEventListener("beforeunload", function () {
    [stageRoom, greenRoom, shareRoom].forEach(function (room) { if (room) { room.disconnect(); } });
  });

  function stopPreview() {
    previewTurn += 1;
    if (previewStream) { previewStream.getTracks().forEach(function (t) { t.stop(); }); previewStream = null; }
  }

  function preview() {
    stopPreview();
    var turn = previewTurn;
    var useCam = $("use-cam").checked, useMic = $("use-mic").checked;
    show($("preview-off"), !useCam);
    if (!useCam && !useMic) { $("preview").srcObject = null; return Promise.resolve(); }
    var constraints = {
      // The size the room will ask for: a camera still open at another size stays at it.
      video: useCam ? Object.assign({ width: { ideal: 1920 }, height: { ideal: 1080 } }, $("cam-select").value ? { deviceId: { exact: $("cam-select").value } } : {}) : false,
      audio: useMic ? ($("mic-select").value ? { deviceId: { exact: $("mic-select").value } } : true) : false
    };
    return navigator.mediaDevices.getUserMedia(constraints).then(function (stream) {
      // They joined while the camera was still starting: nothing is watching this one any more.
      if (turn !== previewTurn) { stream.getTracks().forEach(function (t) { t.stop(); }); return navigator.mediaDevices.enumerateDevices(); }
      previewStream = stream;
      $("preview").srcObject = stream;
      setError($("device-error"), "");
      return navigator.mediaDevices.enumerateDevices();
    }).then(function (devices) {
      [["videoinput", "cam-select"], ["audioinput", "mic-select"]].forEach(function (pair) {
        var select = $(pair[1]);
        var value = select.value;
        select.textContent = "";
        devices.filter(function (d) { return d.kind === pair[0]; }).forEach(function (d, i) {
          select.appendChild(h("option", { value: d.deviceId, text: d.label || (pair[0] === "videoinput" ? "Camera " : "Microphone ") + (i + 1) }));
        });
        if (value) { select.value = value; }
      });
    }).catch(function () {
      setError($("device-error"), "Your browser didn't allow the camera or microphone. You can still join and turn them on later.");
    });
  }
  ["use-cam", "use-mic", "cam-select", "mic-select"].forEach(function (id) { $(id).addEventListener("change", preview); });

  function enterRoom() {
    var name = $("name").value.trim();
    myName = name;
    myTitle = $("job-title").value.trim();
    sessionStorage.setItem("vc-name", name);
    return request("/enter", { name: name, title: $("job-title").value.trim(), clientId: clientId }).then(function (d) {
      want.mic = $("use-mic").checked && info.role !== "attendee";
      want.cam = $("use-cam").checked && info.role !== "attendee";
      want.micDevice = $("mic-select").value;
      want.camDevice = $("cam-select").value;
      stopPreview();
      document.body.classList.toggle("crew", d.role !== "attendee");
      document.body.classList.toggle("dark", d.role !== "attendee");
      show($("gate"), false);
      show($("room"), true);
      stage = window.VCStage($("stage"), {
        fileUrl: fileUrl,
        pageUrl: pageUrl,
        emptyText: function (st) { return d.role === "attendee" ? "" : st.phase === "ended" ? "The session has ended." : "Nobody is on stage yet."; },
        screenName: function (key) {
          var room = stageRoom;
          var p = room && room.remoteParticipants.get(key.slice(7));
          return p ? p.name : "";
        },
        onLayout: function (tiles) { quietly({ type: "set_layout", mode: "custom", focus: null, tiles: tiles }); },
        onLayer: function (layer) { quietly({ type: "layer_save", layer: layer }); }
      });
      open();
      syncToggle($("mic"), want.mic, "Mic on", "Mic off");
      syncToggle($("cam"), want.cam, "Camera on", "Camera off");
      if (d.serverCpus && d.role !== "guest") {
        setError($("size-warning"), "This server has " + d.serverCpus + " CPUs, which is too small to record or broadcast. If it's still at its small size, size it up before going live.");
      }
      if (d.greenToken) { return connectCrew(d); }
    });
  }

  $("join-form").addEventListener("submit", function (event) {
    event.preventDefault();
    var button = $("join");
    button.disabled = true;
    setError($("gate-error"), "");
    enterRoom().catch(function (err) {
      // Past the gate by now if it's the video that failed, and the gate is hidden.
      var where = $("gate").hidden ? "room-error" : "gate-error";
      setError($(where), err && err.code ? message(err) : "We couldn't connect you to the video. Check your connection, then reload this page.");
    }).then(function () { button.disabled = false; });
  });

  // ---- the recorder: the stage alone, from the address the recorder was given

  function startProgram() {
    document.body.classList.add("program");
    show($("gate"), false);
    show($("room"), true);
    S.me = { identity: "program", role: "recorder", perms: {} };
    stage = window.VCStage($("stage"), { fileUrl: fileUrl, pageUrl: pageUrl, emptyText: function () { return ""; } });
    open();
    var params = new URLSearchParams(window.location.search);
    var room = new LK.Room({ adaptiveStream: false });
    stageRoom = room;
    wireStage(room);
    room.connect(params.get("url"), params.get("token"), { autoSubscribe: true }).then(function () {
      catchUp(room, function (track, publication, participant) {
        if (track.kind === "video") { stage.setTrack(keyFor(participant), track); }
        else { $("audio-sink").appendChild(track.attach()); }
      });
      return room.startAudio().catch(function () {});
    }).then(function () { console.log("START_RECORDING"); });
  }

  if (program) { startProgram(); return; }

  if (!ticket) { noLink(); return; }

  request("/info").then(function (data) {
    info = data;
    $("title").textContent = data.title;
    document.title = data.title;
    if (data.window === "not_yet") {
      $("gate-title").textContent = data.title;
      $("gate-note").textContent = "This room opens closer to " + new Date(data.startsAt).toLocaleString() + ".";
      return;
    }
    if (data.window === "over") {
      $("gate-title").textContent = "This session has ended";
      return;
    }
    var crew = data.role !== "attendee";
    $("gate-title").textContent = data.title;
    var notes = {
      host: "You're joining as a host. You'll start in the green room, where the audience can't see or hear you.",
      producer: "You're joining as a producer. You run the show from the green room. The audience never sees or hears you.",
      guest: "You're joining as a guest. You'll wait in the green room until someone brings you on stage.",
      attendee: "Your camera and microphone stay off."
    };
    $("gate-note").textContent = notes[data.role];
    show($("devices"), crew);
    show($("title-field"), crew && data.role !== "producer");
    show($("join-form"), true);
    $("name").value = sessionStorage.getItem("vc-name") || (data.you ? data.you.name : "");
    $("job-title").value = data.you ? data.you.title : "";
    if (crew) { preview(); }
    $("name").focus();
    // An attendee sent back to the audience comes straight back in.
    // So does someone in the crew whose connection dropped.
    if (sessionStorage.getItem("vc-rejoin")) {
      sessionStorage.removeItem("vc-rejoin");
      $("join").click();
    }
  }).catch(function () {
    setError($("gate-error"), "This room couldn't load. Check your connection and try again.");
  });
})();
