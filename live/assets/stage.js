/* The stage: one 16:9 picture, drawn the same way for the audience, the crew and the recorder,
   so what a producer arranges is exactly what goes out and what ends up in the recording. */
(function () {
  "use strict";

  function round(n) { return Math.round(n * 10000) / 10000; }

  /* Where each source goes, as fractions of the stage. Pure, so every browser agrees. */
  function computeRects(mode, keys, focus, active, custom) {
    var rects = {};
    if (!keys.length) { return rects; }
    var presentations = keys.filter(function (k) { return k.indexOf("cam:") !== 0; });
    var main = keys.indexOf(focus) >= 0 ? focus : presentations[0] || (keys.indexOf(active) >= 0 ? active : keys[0]);
    var others = keys.filter(function (k) { return k !== main; });
    var gap = 0.01;
    function grid(list) {
      var cols = Math.ceil(Math.sqrt(list.length));
      var rows = Math.ceil(list.length / cols);
      var w = (1 - gap * (cols + 1)) / cols;
      var h = (1 - gap * (rows + 1)) / rows;
      list.forEach(function (k, i) {
        var row = Math.floor(i / cols);
        var inRow = row === rows - 1 ? list.length - cols * (rows - 1) : cols;
        var pad = (1 - (inRow * w + (inRow + 1) * gap)) / 2;
        rects[k] = { x: pad + gap + (i % cols) * (w + gap), y: gap + row * (h + gap), w: w, h: h };
      });
    }
    if (mode === "custom") {
      grid(keys);
      keys.forEach(function (k) { if (custom && custom[k]) { rects[k] = custom[k]; } });
    } else if (mode === "single" || keys.length === 1) {
      rects[main] = { x: 0, y: 0, w: 1, h: 1 };
    } else if (mode === "split") {
      rects[main] = { x: gap, y: gap, w: 0.5 - gap * 1.5, h: 1 - gap * 2 };
      rects[others[0]] = { x: 0.5 + gap / 2, y: gap, w: 0.5 - gap * 1.5, h: 1 - gap * 2 };
    } else if (mode === "speaker") {
      rects[main] = { x: gap, y: gap, w: 0.79, h: 1 - gap * 2 };
      var h = Math.min(0.24, (1 - gap * (others.length + 1)) / others.length);
      var top = (1 - (others.length * h + (others.length - 1) * gap)) / 2;
      others.forEach(function (k, i) { rects[k] = { x: 0.81, y: top + i * (h + gap), w: 0.18, h: h }; });
    } else if (mode === "pip") {
      rects[main] = { x: 0, y: 0, w: 1, h: 1 };
      var small = others.filter(function (k) { return k.indexOf("cam:") === 0; })[0] || others[0];
      rects[small] = { x: 0.74, y: 0.72, w: 0.24, h: 0.24 };
    } else {
      grid(keys);
    }
    return rects;
  }

  function el(tag, cls) {
    var node = document.createElement(tag);
    if (cls) { node.className = cls; }
    return node;
  }

  window.VCStage = function (root, options) {
    var state = null;
    var active = "";
    var serverOffset = 0;
    var tracks = {};    // key -> video track
    var tiles = {};     // key -> tile elements
    var editing = false;
    var currentRects = {};
    var currentPoll = null;
    var mediaEl = null;
    var mediaId = "";
    var muted = false;

    var pic = el("div", "pic");
    var bg = el("img", "pic-bg"); bg.alt = ""; bg.hidden = true;
    var layer = el("div", "pic-tiles");
    var frame = el("img", "pic-frame"); frame.alt = ""; frame.hidden = true;
    var visual = el("img", "pic-visual"); visual.alt = ""; visual.hidden = true;
    var logo = el("img", "pic-logo"); logo.alt = ""; logo.hidden = true;
    var third = el("div", "pic-third"); third.hidden = true;
    var thirdTitle = el("strong"); var thirdSub = el("span");
    third.appendChild(thirdTitle); third.appendChild(thirdSub);
    var ticker = el("div", "pic-ticker"); ticker.hidden = true;
    var tickerText = el("span"); ticker.appendChild(tickerText);
    var poll = el("div", "pic-poll"); poll.hidden = true;
    var cta = el("a", "pic-cta"); cta.hidden = true; cta.target = "_blank"; cta.rel = "noopener noreferrer";
    var ctaText = el("span"), ctaSub = el("small"), ctaBtn = el("b");
    cta.appendChild(ctaText); cta.appendChild(ctaSub); cta.appendChild(ctaBtn);
    var layersEl = el("div", "pic-layers");
    var captionBox = el("div", "pic-captions"); captionBox.hidden = true;
    var captionLine = el("p"); captionBox.appendChild(captionLine);
    var captionsOn = false, captionTimer = null;
    var empty = el("p", "pic-empty");
    var floaters = el("div", "pic-floaters");
    [bg, layer, frame, visual, layersEl, logo, third, ticker, poll, cta, captionBox, empty, floaters].forEach(function (n) { pic.appendChild(n); });
    root.appendChild(pic);

    function toSrc(img, id) {
      if (id) {
        var src = options.fileUrl(id);
        if (img.getAttribute("src") !== src) { img.src = src; }
        img.hidden = false;
      } else {
        img.hidden = true;
        img.removeAttribute("src");
      }
    }

    function sourceKeys() {
      var keys = [];
      if (state.media) { keys.push("media"); }
      if (state.slides) { keys.push("slides"); }
      Object.keys(tracks).forEach(function (k) { if (k.indexOf("screen:") === 0) { keys.push(k); } });
      state.stage.forEach(function (m) { keys.push("cam:" + m.identity); });
      return keys;
    }

    function member(key) {
      var id = key.slice(4);
      return state.stage.filter(function (m) { return m.identity === id; })[0];
    }

    function tileFor(key) {
      if (tiles[key]) { return tiles[key]; }
      var node = el("div", "stile");
      node.dataset.key = key;
      var avatar = el("div", "stile-avatar");
      var label = el("div", "stile-label");
      var who = el("b"), title = el("span");
      label.appendChild(who); label.appendChild(title);
      var handle = el("div", "stile-handle");
      node.appendChild(avatar); node.appendChild(label); node.appendChild(handle);
      layer.appendChild(node);
      var tile = { node: node, avatar: avatar, label: label, who: who, title: title, video: null, track: null };
      tiles[key] = tile;
      attachEditing(tile, key, handle);
      return tile;
    }

    function bind(key, tile) {
      var track = tracks[key];
      if (tile.track === track) { return; }
      if (tile.video) { if (tile.track) { tile.track.detach(tile.video); } tile.video.remove(); tile.video = null; }
      tile.track = track || null;
      if (track) {
        var video = track.attach();
        video.muted = true;
        video.playsInline = true;
        tile.video = video;
        tile.node.insertBefore(video, tile.node.firstChild);
      }
    }

    function syncMedia() {
      var m = state.media;
      var show = !!m && options.showMedia !== false;
      if (!show) {
        if (mediaEl) { mediaEl.pause(); mediaEl.remove(); mediaEl = null; mediaId = ""; }
        return;
      }
      var tile = tileFor("media");
      if (!mediaEl || mediaId !== m.id) {
        if (mediaEl) { mediaEl.remove(); }
        mediaEl = el("video");
        mediaEl.src = options.fileUrl(m.id);
        mediaEl.playsInline = true;
        mediaEl.preload = "auto";
        mediaEl.muted = muted;
        // Someone arriving late starts where everyone else is, not at the beginning.
        mediaEl.addEventListener("loadedmetadata", function () { syncMedia(); });
        mediaId = m.id;
        tile.node.insertBefore(mediaEl, tile.node.firstChild);
      }
      var expected = m.position + (m.playing ? (Date.now() + serverOffset - m.atMs) / 1000 : 0);
      if (mediaEl.readyState >= 1 && Math.abs(mediaEl.currentTime - expected) > 0.8) {
        mediaEl.currentTime = Math.max(0, Math.min(expected, mediaEl.duration || expected));
      }
      if (m.playing && mediaEl.paused) {
        var played = mediaEl.play();
        if (played && played.catch) { played.catch(function () { if (options.onBlockedMedia) { options.onBlockedMedia(); } }); }
      } else if (!m.playing && !mediaEl.paused) {
        mediaEl.pause();
      }
    }

    var slideImg = null;
    function syncSlides() {
      if (!state.slides) { slideImg = null; return; }
      var tile = tiles.slides;
      if (!tile) { return; }
      if (!slideImg || !tile.node.contains(slideImg)) {
        slideImg = el("img", "stile-slide");
        slideImg.alt = "";
        tile.node.insertBefore(slideImg, tile.node.firstChild);
      }
      var src = options.pageUrl(state.slides.fileId, state.slides.page);
      if (slideImg.getAttribute("src") !== src) { slideImg.src = src; }
    }

    // Captions sit above whatever is along the bottom of the picture.
    function placeCaptions() {
      var bottom = 6;
      if (state.ticker) { bottom = Math.max(bottom, 8); }
      if (state.lowerThird) { bottom = Math.max(bottom, 24); }
      if (state.cta) { bottom = Math.max(bottom, 22); }
      (state.layers || []).forEach(function (l) {
        // A band along the bottom, not something tall down the side or in a corner.
        if (l.visible && l.w >= 0.4 && l.h < 0.5 && l.y + l.h > 0.7) { bottom = Math.max(bottom, Math.round((1 - l.y) * 100) + 1); }
      });
      captionBox.style.setProperty("bottom", Math.min(bottom, 60) + "%");
    }

    function paint() {
      if (!state) { return; }
      placeCaptions();
      var keys = sourceKeys();
      var rects = computeRects(state.layout.mode, keys, state.layout.focus, active, state.layout.tiles);
      var inset = state.layout.inset || 0;
      if (inset && state.layout.mode !== "custom") {
        Object.keys(rects).forEach(function (k) {
          var r = rects[k];
          rects[k] = { x: inset + r.x * (1 - 2 * inset), y: inset + r.y * (1 - 2 * inset), w: r.w * (1 - 2 * inset), h: r.h * (1 - 2 * inset) };
        });
      }
      if (editing && dragging) { rects = currentRects; }
      else { currentRects = rects; }
      Object.keys(tiles).forEach(function (key) {
        if (keys.indexOf(key) < 0) {
          var t = tiles[key];
          if (t.track && t.video) { t.track.detach(t.video); }
          t.node.remove();
          delete tiles[key];
        }
      });
      keys.forEach(function (key, i) {
        var tile = tileFor(key);
        var r = rects[key];
        tile.node.hidden = !r;
        if (!r) { return; }
        tile.node.style.setProperty("left", round(r.x * 100) + "%");
        tile.node.style.setProperty("top", round(r.y * 100) + "%");
        tile.node.style.setProperty("width", round(r.w * 100) + "%");
        tile.node.style.setProperty("height", round(r.h * 100) + "%");
        tile.node.style.setProperty("z-index", String(state.layout.mode === "pip" && r.w < 1 ? 5 : i + 1));
        var isCam = key.indexOf("cam:") === 0;
        tile.node.classList.toggle("is-screen", !isCam);
        tile.node.classList.toggle("is-speaking", isCam && key === active);
        tile.node.classList.toggle("editing", editing);
        if (isCam) {
          var m = member(key);
          tile.who.textContent = m ? m.name : "";
          tile.title.textContent = m ? m.title : "";
          tile.avatar.textContent = m ? m.name.split(/\s+/).map(function (w) { return w.charAt(0); }).slice(0, 2).join("").toUpperCase() : "";
        } else {
          tile.who.textContent = key === "media" || key === "slides" ? "" : (options.screenName ? options.screenName(key) : "");
          tile.title.textContent = "";
          tile.avatar.textContent = "";
        }
        tile.label.hidden = !tile.who.textContent;
        tile.avatar.hidden = !isCam || !!tracks[key];
        if (key !== "media" && key !== "slides") { bind(key, tile); }
      });
      syncMedia();
      syncSlides();
      toSrc(bg, state.backgroundId);
      toSrc(frame, state.frameId);
      toSrc(visual, state.visualId);
      toSrc(logo, state.logoId);
      third.hidden = !state.lowerThird;
      if (state.lowerThird) { thirdTitle.textContent = state.lowerThird.title; thirdSub.textContent = state.lowerThird.subtitle; }
      ticker.hidden = !state.ticker;
      if (state.ticker) { tickerText.textContent = state.ticker; }
      paintLayers();
      paintPoll();
      cta.hidden = !state.cta;
      if (state.cta) {
        ctaText.textContent = state.cta.title;
        ctaSub.textContent = state.cta.subtitle;
        ctaSub.hidden = !state.cta.subtitle;
        ctaBtn.textContent = state.cta.label || "Open";
        cta.href = state.cta.url;
      }
      empty.hidden = keys.length > 0 || !!state.visualId;
      empty.textContent = options.emptyText ? options.emptyText(state) : "";
    }

    // ---- graphics: GIFs, videos and words placed on the picture

    var layerEls = {};
    function paintLayers() {
      var seen = {};
      (state.layers || []).forEach(function (l, i) {
        if (!l.visible && !editing) { return; }
        seen[l.id] = true;
        var entry = layerEls[l.id];
        if (!entry || entry.fileId !== l.fileId) {
          if (entry) { entry.node.remove(); }
          var node = el("div", "pic-layer");
          node.dataset.layer = l.id;
          var media = null;
          if (l.fileId) {
            if (l.mime.indexOf("video/") === 0) {
              media = el("video");
              media.muted = true;
              media.loop = true;
              media.autoplay = true;
              media.playsInline = true;
            } else {
              media = el("img");
              media.alt = "";
            }
            media.src = options.fileUrl(l.fileId);
            node.appendChild(media);
          }
          var words = el("div", "pic-layer-words");
          var first = el("strong"), second = el("span");
          words.appendChild(first); words.appendChild(second);
          var handle = el("div", "stile-handle");
          node.appendChild(words);
          node.appendChild(handle);
          layersEl.appendChild(node);
          entry = layerEls[l.id] = { node: node, fileId: l.fileId, first: first, second: second, words: words };
          attachLayerEditing(entry, l.id, handle);
        }
        var n = entry.node;
        var r = layerDrag && layerDrag.id === l.id ? layerDrag.rect : l;
        n.style.setProperty("left", round(r.x * 100) + "%");
        n.style.setProperty("top", round(r.y * 100) + "%");
        n.style.setProperty("width", round(r.w * 100) + "%");
        n.style.setProperty("height", round(r.h * 100) + "%");
        n.style.setProperty("z-index", String(i + 1));
        n.classList.toggle("editing", editing);
        n.classList.toggle("off", !l.visible);
        entry.first.textContent = l.title;
        entry.second.textContent = l.subtitle;
        entry.second.hidden = !l.subtitle;
        entry.words.hidden = !l.title && !l.subtitle;
        entry.words.classList.toggle("scroll", !!l.scroll);
      });
      Object.keys(layerEls).forEach(function (id) {
        if (!seen[id]) { layerEls[id].node.remove(); delete layerEls[id]; }
      });
    }

    var layerDrag = null;
    function attachLayerEditing(entry, id, handle) {
      var node = entry.node;
      node.addEventListener("pointerdown", function (event) {
        if (!editing) { return; }
        var l = (state.layers || []).filter(function (x) { return x.id === id; })[0];
        if (!l) { return; }
        event.preventDefault();
        layerDrag = { id: id, layer: l, resize: event.target === handle, startX: event.clientX, startY: event.clientY, box: pic.getBoundingClientRect(), rect: { x: l.x, y: l.y, w: l.w, h: l.h } };
        node.setPointerCapture(event.pointerId);
      });
      node.addEventListener("pointermove", function (event) {
        if (!layerDrag || layerDrag.id !== id) { return; }
        var s = layerDrag.layer;
        var dx = (event.clientX - layerDrag.startX) / layerDrag.box.width;
        var dy = (event.clientY - layerDrag.startY) / layerDrag.box.height;
        layerDrag.rect = layerDrag.resize
          ? { x: s.x, y: s.y, w: Math.max(0.03, Math.min(1 - s.x, s.w + dx)), h: Math.max(0.03, Math.min(1 - s.y, s.h + dy)) }
          : { x: Math.max(0, Math.min(1 - s.w, s.x + dx)), y: Math.max(0, Math.min(1 - s.h, s.y + dy)), w: s.w, h: s.h };
        paintLayers();
      });
      function finish() {
        if (!layerDrag || layerDrag.id !== id) { return; }
        var r = layerDrag.rect, l = layerDrag.layer;
        layerDrag = null;
        if (options.onLayer) { options.onLayer(Object.assign({}, l, { x: round(r.x), y: round(r.y), w: round(r.w), h: round(r.h) })); }
      }
      node.addEventListener("pointerup", finish);
      node.addEventListener("pointercancel", finish);
    }

    function paintPoll() {
      var p = currentPoll;
      poll.hidden = !p;
      if (!p) { return; }
      poll.textContent = "";
      var q = el("strong"); q.textContent = p.question; poll.appendChild(q);
      var total = p.total || 0;
      p.options.forEach(function (text, i) {
        var row = el("div", "pic-poll-row");
        var count = p.tallies ? p.tallies[i] || 0 : null;
        var share = count === null || !total ? 0 : count / total;
        var bar = el("i"); bar.style.setProperty("width", Math.round(share * 100) + "%");
        var label = el("span"); label.textContent = text;
        var value = el("b");
        if (count !== null) { value.textContent = p.showCounts ? String(count) : Math.round(share * 100) + "%"; }
        row.appendChild(bar); row.appendChild(label); row.appendChild(value);
        if (p.correct === i) { row.classList.add("correct"); }
        poll.appendChild(row);
      });
    }

    // ---- moving and resizing windows

    var dragging = null;
    function attachEditing(tile, key, handle) {
      tile.node.addEventListener("pointerdown", function (event) {
        if (!editing) { return; }
        event.preventDefault();
        var resize = event.target === handle;
        var box = pic.getBoundingClientRect();
        var start = currentRects[key];
        if (!start) { return; }
        dragging = { key: key, resize: resize, startX: event.clientX, startY: event.clientY, start: { x: start.x, y: start.y, w: start.w, h: start.h }, box: box };
        tile.node.setPointerCapture(event.pointerId);
      });
      tile.node.addEventListener("pointermove", function (event) {
        if (!dragging || dragging.key !== key) { return; }
        var dx = (event.clientX - dragging.startX) / dragging.box.width;
        var dy = (event.clientY - dragging.startY) / dragging.box.height;
        var s = dragging.start;
        var next;
        if (dragging.resize) {
          next = { x: s.x, y: s.y, w: Math.max(0.08, Math.min(1 - s.x, s.w + dx)), h: Math.max(0.08, Math.min(1 - s.y, s.h + dy)) };
        } else {
          next = { x: Math.max(0, Math.min(1 - s.w, s.x + dx)), y: Math.max(0, Math.min(1 - s.h, s.y + dy)), w: s.w, h: s.h };
        }
        currentRects[key] = next;
        paint();
      });
      function finish() {
        if (!dragging || dragging.key !== key) { return; }
        dragging = null;
        var out = {};
        Object.keys(currentRects).forEach(function (k) {
          var r = currentRects[k];
          out[k] = { x: round(r.x), y: round(r.y), w: round(r.w), h: round(r.h) };
        });
        if (options.onLayout) { options.onLayout(out); }
      }
      tile.node.addEventListener("pointerup", finish);
      tile.node.addEventListener("pointercancel", finish);
    }

    return {
      setState: function (next, offset) { state = next; if (offset !== undefined) { serverOffset = offset; } paint(); },
      setTrack: function (key, track) { if (track) { tracks[key] = track; } else { delete tracks[key]; } paint(); },
      setActive: function (key) { if (active !== key) { active = key; paint(); } },
      setPoll: function (p) { currentPoll = p; paintPoll(); },
      setCaptionsOn: function (on) { captionsOn = on; if (!on) { captionBox.hidden = true; } },
      caption: function (name, text) {
        if (!captionsOn) { return; }
        captionLine.textContent = "";
        var who = el("b"); who.textContent = name + ": ";
        captionLine.appendChild(who);
        captionLine.appendChild(document.createTextNode(text));
        captionBox.hidden = false;
        clearTimeout(captionTimer);
        captionTimer = setTimeout(function () { captionBox.hidden = true; }, 6000);
      },
      setEditing: function (on) { editing = on; paint(); },
      stopMedia: function () { if (mediaEl) { mediaEl.pause(); mediaEl.remove(); mediaEl = null; mediaId = ""; } options.showMedia = false; },
      setMuted: function (on) { muted = on; if (mediaEl) { mediaEl.muted = on; } },
      repaint: paint,
      float: function (emoji) {
        var node = el("span", "pic-float");
        node.textContent = emoji;
        node.style.setProperty("left", 8 + Math.floor(Math.random() * 20) + "%");
        floaters.appendChild(node);
        setTimeout(function () { node.remove(); }, 2600);
      }
    };
  };
  window.VCStage.computeRects = computeRects;
})();
