/* tokenscope live — board + session view. Vanilla JS, no dependencies.
   Data comes from a "source" (default: the HTTP server; demo.html injects a fake one via window.TS_SOURCE). */
(function () {
  "use strict";

  // ---------- helpers ----------
  var GROUPS = ["investigate", "change", "verify", "ship", "delegate", "research", "wait", "plan", "report", "overhead", "other", "respond"];
  var GCOL = {investigate: "#4c6ef5", change: "#f0645a", verify: "#f08c00", ship: "#7950f2", delegate: "#d4a017", research: "#12a594",
    wait: "#8d6e63", plan: "#e64980", report: "#adb5bd", overhead: "#ced4da", other: "#b8bec6", respond: "#c5cad1"};
  function gcol(g) { return GCOL[g] || "#b8bec6"; }
  function $(sel, root) { return (root || document).querySelector(sel); }
  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function esc(s) { return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]; }); }
  function ts(v) { // -> seconds (accepts epoch s, epoch ms, ISO string)
    if (v == null) return null;
    if (typeof v === "number") return v > 1e12 ? v / 1000 : v;
    var n = Date.parse(v);
    return isNaN(n) ? null : n / 1000;
  }
  function fmtK(n) {
    n = +n || 0;
    if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
    if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 1 : 2) + "M";
    if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "k";
    return String(Math.round(n));
  }
  function fmtUsd(n) { n = +n || 0; return "$" + (n >= 100 ? n.toFixed(0) : n >= 10 ? n.toFixed(1) : n.toFixed(2)); }
  function fmtDur(s) {
    s = Math.max(0, Math.floor(s || 0));
    var h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), x = s % 60;
    return h ? h + ":" + (m < 10 ? "0" : "") + m + "h" : m + ":" + (x < 10 ? "0" : "") + x;
  }
  var STATUS_TXT = {working: "working", waiting: "waiting for you", idle: "idle"};
  function pill(status) {
    var p = el("span", "pill " + (status || "idle"));
    p.appendChild(el("span", "dot"));
    p.appendChild(document.createTextNode(STATUS_TXT[status] || status || "idle"));
    return p;
  }
  function detailOf(f) {
    var d = f.detail;
    if (d && typeof d === "object") return d;
    return {value: d};
  }
  var FLAG_NAME = {reread_file: "re-read file", context_large: "context", retry_loop: "retry loop", dead_tool: "dead tool",
    same_intent_after_error: "same intent after error", browser_loop: "browser loop", compaction: "compacted"};
  // detail (session payloads): "<what> x<n>" string or a number; board rows carry only {flag, count}
  function flagText(f, ctx) {
    var c = +f.count || 0, d = f.detail, name = FLAG_NAME[f.flag] || String(f.flag).replace(/_/g, " ");
    if (f.flag === "context_large") return name + " " + fmtK(typeof d === "number" ? d : (ctx || 0));
    if (typeof d === "string" && d) return name + " · " + d.replace(/ x(\d+)$/, " ×$1");
    if (typeof d === "number" && d > 1) return name + " ×" + d;
    if (d == null && c > 1) return name + " ×" + c;
    return name;
  }
  function flagChip(f, ctx, amber) {
    var bad = !amber && (f.flag === "retry_loop" || f.flag === "dead_tool" || f.flag === "same_intent_after_error");
    var c = el("span", "chip " + (bad ? "bad" : "warn"), flagText(f, ctx));
    if (f.detail != null) c.title = String(f.flag) + " · count " + (f.count || 0);
    return c;
  }
  function goalName(g) { return g && typeof g === "object" ? g.name : g; }
  function goalNode(node, g) {
    var n = goalName(g);
    node.textContent = n || "(no prompt yet)";
    node.title = n || "";
    if (g && g.source === "haiku") node.appendChild(el("span", "haiku", "✦ haiku"));
  }
  function intentChip(intent, group) {
    var c = el("span", "chip intent", (intent || group || "—").replace(/_/g, " "));
    c.style.background = gcol(group);
    return c;
  }
  function stackBar(mix, cls) {
    var b = el("div", cls || "stack");
    fillStack(b, mix);
    return b;
  }
  function fillStack(b, mix) {
    var kids = b.children, i;
    mix = (mix || []).filter(function (m) { return m[1] > 0; });
    for (i = 0; i < mix.length; i++) {
      var s = kids[i] || b.appendChild(el("i"));
      s.style.background = gcol(mix[i][0]);
      s.style.flexGrow = mix[i][1];
      s.title = mix[i][0] + " · " + fmtK(mix[i][1]) + " CW";
    }
    while (b.children.length > mix.length) b.removeChild(b.lastChild);
  }

  // Animated number (counts smoothly to the target)
  function Counter(node, fmt) { this.node = node; this.fmt = fmt; this.v = null; this.raf = 0; }
  Counter.prototype.set = function (target) {
    target = +target || 0;
    var self = this;
    if (this.v == null) { this.v = target; this.node.textContent = this.fmt(target); return; }
    var from = this.v, t0 = performance.now(), dur = 700;
    if (from === target) return;
    cancelAnimationFrame(this.raf);
    (function step(now) {
      var k = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - k, 3);
      self.v = from + (target - from) * e;
      self.node.textContent = self.fmt(self.v);
      if (k < 1) self.raf = requestAnimationFrame(step); else self.v = target;
    })(t0);
  };

  // ---------- data source ----------
  function httpSource() {
    function getJSON(url) {
      return fetch(url, {cache: "no-store"}).then(function (r) {
        if (r.status === 404) { var e = new Error("not found"); e.status = 404; throw e; }
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      });
    }
    return {
      board: function () { return getJSON("/api/board"); },
      session: function (id, since) {
        return getJSON("/api/session/" + encodeURIComponent(id) + (since != null ? "?since=" + since : ""));
      },
      sessionHref: function (id) { return "/s/" + encodeURIComponent(id); },
      boardHref: "/",
      // handlers: {board(data), step(msg), up(), down()}; returns false if SSE unsupported
      subscribe: function (h) {
        if (!window.EventSource) return false;
        var es = new EventSource("/events");
        es.addEventListener("board", function (e) { h.up && h.up(); try { h.board && h.board(JSON.parse(e.data)); } catch (x) {} });
        es.addEventListener("step", function (e) { h.up && h.up(); try { h.step && h.step(JSON.parse(e.data)); } catch (x) {} });
        es.onopen = function () { h.up && h.up(); };
        es.onerror = function () { h.down && h.down(); };
        return true;
      }
    };
  }
  var SRC = window.TS_SOURCE || httpSource();

  // Live link: SSE first, polling fallback while SSE is down.
  function liveLink(opts) {
    var pollT = 0, sseOk = false, connEl = opts.connEl, lastMsg = 0;
    function show(mode) {
      if (!connEl) return;
      connEl.innerHTML = '<span class="dot" style="background:' + (mode === "live" ? "#2fb344" : mode === "poll" ? "#f59f00" : "#adb5bd") + '"></span>' +
        (mode === "live" ? "live" : mode === "poll" ? "polling" : "offline");
    }
    function startPoll() {
      if (pollT) return;
      show("poll");
      var tick = function () {
        opts.poll().then(function () { if (!sseOk) show("poll"); }, function () { if (!sseOk) show("off"); });
      };
      tick();
      pollT = setInterval(tick, opts.every);
    }
    function stopPoll() { if (pollT) { clearInterval(pollT); pollT = 0; } }
    var ok = SRC.subscribe({
      board: function (d) { lastMsg = Date.now(); opts.board && opts.board(d); },
      step: function (m) { lastMsg = Date.now(); opts.step && opts.step(m); },
      up: function () { if (!sseOk) { sseOk = true; stopPoll(); show("live"); opts.resync && opts.resync(); } },
      down: function () { sseOk = false; startPoll(); }
    });
    if (!ok) startPoll();
    else setTimeout(function () { if (!sseOk) startPoll(); }, 3000);
    // SSE "open" but silent for a long time (proxy buffering?) -> also poll
    setInterval(function () { if (sseOk && Date.now() - lastMsg > 20000) { sseOk = false; startPoll(); } }, 5000);
  }

  // ---------- board ----------
  function initBoard(root) {
    root = root || document;
    var grid = $("#grid", root), cards = {}, prev = {}, clockOff = 0, data = null;
    var k = {
      sessions: new Counter($("#k-sessions", root), function (v) { return Math.round(v); }),
      working: new Counter($("#k-working", root), function (v) { return Math.round(v); }),
      cw: new Counter($("#k-cw", root), fmtK),
      usd: new Counter($("#k-usd", root), fmtUsd)
    };
    function nowS() { return Date.now() / 1000 + clockOff; }

    function buildCard(s) {
      var a = el("a", "scard enter");
      a.href = SRC.sessionHref(s.id);
      a.innerHTML = '<div class="r1"><span class="proj"></span><span class="pillw"></span></div>' +
        '<div class="goal"></div><div class="r2"><span class="ic"></span><span class="last"></span></div>' +
        '<div class="r3"><span><b class="el"></b> <span class="mut small elx"></span></span><span><b class="cw"></b> CW · <b class="usd"></b></span></div>' +
        '<div class="stack"></div><div class="r1" style="margin-top:6px"><span class="ag"></span><span class="tc"></span></div><div class="flags"></div>';
      a._cw = new Counter($(".cw", a), fmtK);
      a._usd = new Counter($(".usd", a), fmtUsd);
      return a;
    }
    function updCard(a, s) {
      $(".proj", a).textContent = s.project || "—";
      goalNode($(".goal", a), s.goal);
      var pw = $(".pillw", a);
      if (a._st !== s.status) { pw.innerHTML = ""; pw.appendChild(pill(s.status)); }
      var ic = $(".ic", a), key = (s.current_intent || "") + "|" + (s.current_group || "");
      if (a._ic !== key) { ic.innerHTML = ""; if (s.current_intent || s.current_group) ic.appendChild(intentChip(s.current_intent, s.current_group)); a._ic = key; }
      var ls = s.last_step || {}, last = $(".last", a);
      last.textContent = ls.label ? (ls.err ? "✗ " : "") + ls.label : "";
      last.className = "last" + (ls.err ? " err" : "");
      a._cw.set(s.cw); a._usd.set(s.usd);
      fillStack($(".stack", a), s.intent_mix);
      $(".ag", a).textContent = s.agents_total ? "agents " + (s.agents_running || 0) + "/" + s.agents_total + (s.agents_running ? " running" : "") : "no subagents";
      $(".tc", a).textContent = (s.turns || 0) + " turns · " + (s.tool_calls || 0) + " tools" + (s.errors ? " · " + s.errors + " err" : "");
      var fl = $(".flags", a); fl.innerHTML = "";
      (s.flags || []).forEach(function (f) { fl.appendChild(flagChip(f, s.context_tokens)); });
      a._s = s;
      a._st = s.status;
    }
    function tickElapsed() {
      Object.keys(cards).forEach(function (id) {
        var a = cards[id], s = a._s;
        if (!s) return;
        var st = ts(s.started), lt = ts(s.last_ts), e;
        if (st != null) e = (s.status === "idle" ? (lt || nowS()) : nowS()) - st;
        else e = s.elapsed_s;
        $(".el", a).textContent = fmtDur(e);
        $(".elx", a).textContent = s.status === "idle" && lt ? "· " + fmtDur(nowS() - lt) + " ago" : "";
      });
    }

    function render(d) {
      data = d;
      if (d.now != null) clockOff = ts(d.now) - Date.now() / 1000;
      var ss = d.sessions || [];
      $("#redacted", root).style.display = d.redacted ? "" : "none";
      var tot = {cw: 0, usd: 0, w: 0};
      ss.forEach(function (s) { tot.cw += +s.cw || 0; tot.usd += +s.usd || 0; if (s.status === "working") tot.w++; });
      k.sessions.set(ss.length); k.working.set(tot.w); k.cw.set(tot.cw); k.usd.set(tot.usd);
      $("#empty", root).style.display = ss.length ? "none" : "";

      // FLIP: remember positions
      var first = {};
      Object.keys(cards).forEach(function (id) { first[id] = cards[id].getBoundingClientRect(); });
      var seen = {};
      ss.forEach(function (s, i) {
        seen[s.id] = 1;
        var a = cards[s.id], isNew = !a;
        if (isNew) { a = cards[s.id] = buildCard(s); }
        if (!isNew && prev[s.id] && prev[s.id] !== s.status) {
          a.classList.remove("flash"); void a.offsetWidth; a.classList.add("flash");
        }
        prev[s.id] = s.status;
        updCard(a, s);
        var at = grid.children[i];
        if (at !== a) grid.insertBefore(a, at || null);
        if (isNew) requestAnimationFrame(function () { requestAnimationFrame(function () { a.classList.remove("enter"); }); });
      });
      // leaving cards: fade out, then remove (FLIP the rest again)
      Object.keys(cards).forEach(function (id) {
        if (seen[id]) return;
        var a = cards[id];
        delete cards[id]; delete prev[id];
        a.classList.add("leave");
        setTimeout(function () {
          var f2 = {};
          Object.keys(cards).forEach(function (i2) { f2[i2] = cards[i2].getBoundingClientRect(); });
          if (a.parentNode) a.parentNode.removeChild(a);
          flip(f2);
        }, 320);
      });
      flip(first);
      tickElapsed();
    }
    function flip(first) {
      Object.keys(first).forEach(function (id) {
        var a = cards[id];
        if (!a) return;
        var r = a.getBoundingClientRect(), dx = first[id].left - r.left, dy = first[id].top - r.top;
        if (!dx && !dy) return;
        a.style.transition = "none";
        a.style.transform = "translate(" + dx + "px," + dy + "px)";
        void a.offsetWidth;
        a.style.transition = "";
        a.style.transform = "";
      });
    }

    setInterval(tickElapsed, 1000);
    SRC.board().then(render, function () { $("#empty", root).textContent = "Cannot reach the tokenscope server."; $("#empty", root).style.display = ""; });
    liveLink({
      connEl: $("#conn", root), every: 2000,
      board: render,
      poll: function () { return SRC.board().then(render); },
      resync: function () {}
    });
  }

  // ---------- session ----------
  var MAX_ROWS = 160;          // rows rendered at once (older rows behind a "show earlier" button)
  var RIBBON_MAX = 500;        // above this many turns, merge adjacent same-group ribbon segments

  function toolName(t) { return t.label || t.name || "?"; }
  function toolSummary(turns) {
    var c = {}, order = [];
    turns.forEach(function (t) { (t.tools || []).forEach(function (x) { var n = toolName(x); if (!c[n]) { c[n] = 0; order.push(n); } c[n]++; }); });
    order.sort(function (a, b) { return c[b] - c[a]; });
    var out = order.slice(0, 4).map(function (n) { return n + (c[n] > 1 ? " ×" + c[n] : ""); }).join(", ");
    if (order.length > 4) out += ", +" + (order.length - 4);
    return out;
  }
  function turnErr(t) { return (t.tools || []).some(function (x) { return x.err; }); }

  function turnRow(t, prevTurn) {
    var r = el("div", "turn");
    r.appendChild(intentChip(t.intent, t.group));
    var prevLabels = {};
    if (prevTurn && turnErr(prevTurn)) (prevTurn.tools || []).forEach(function (x) { if (x.err) prevLabels[toolName(x) + "|" + (x.target || "")] = 1; });
    (t.tools || []).forEach(function (x) {
      var retry = prevLabels[toolName(x) + "|" + (x.target || "")];
      var c = el("span", "tchip" + (x.err ? " err" : "") + (retry ? " retry" : ""));
      c.textContent = (retry ? "↻ " : "") + (x.err ? "✗ " : "") + toolName(x);
      if (x.target) c.appendChild(el("span", "tg", x.target));
      if (x.dur_ms) c.title = (x.dur_ms / 1000).toFixed(1) + " s";
      r.appendChild(c);
    });
    r.appendChild(el("span", "num", fmtK(t.cw) + " CW · " + fmtUsd(t.usd)));
    return r;
  }
  function reasonRow(s) {
    var what = s.kind === "text" ? "text" : "thinking";
    if (!(+s.chars > 0) && !s.text) return el("div", "reason", "💭 " + what + (s.signed ? " (content not in transcript)" : ""));
    return el("div", "reason", "💭 " + what + " · " + fmtK(s.chars) + " chars" + (s.text ? " — " + s.text : ""));
  }

  // Groups a step list into rows: segments of consecutive same-group turns; other steps are their own rows.
  function RowModel() { this.rows = []; }
  RowModel.prototype.add = function (s) {
    var last = this.rows[this.rows.length - 1];
    if (s.k === "turn") {
      if (last && last.type === "seg" && last.group === s.group) { last.items.push(s); last.turns.push(s); last.cw += +s.cw || 0; return {row: last, changed: true}; }
      var row = {type: "seg", group: s.group, items: [s], turns: [s], cw: +s.cw || 0};
      this.rows.push(row);
      return {row: row, changed: false};
    }
    if (s.k === "reason" && last && last.type === "seg") { last.items.push(s); return {row: last, changed: true}; }
    var r2 = {type: s.k, step: s};
    this.rows.push(r2);
    return {row: r2, changed: false};
  };

  function initSession(root) {
    root = root || document;
    var id = window.TS_SESSION_ID || decodeURIComponent((location.pathname.match(/\/s\/([^/?#]+)/) || [])[1] || "") ||
      new URLSearchParams(location.search).get("id");
    var S = null, maxSeq = -1, model = new RowModel(), turns = [], clockOff = 0;
    var tl = $("#timeline", root), ribbon = $("#ribbon", root), follow = $("#follow", root), jump = $("#jump", root);
    var showFrom = 0; // index of first rendered row
    var cw = new Counter($("#h-cw", root), fmtK), usd = new Counter($("#h-usd", root), fmtUsd);
    var openSegs = {}; // row index -> true
    var openFans = {};
    $("#back", root).href = SRC.boardHref;

    function nowS() { return Date.now() / 1000 + clockOff; }
    function atBottom() { return window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - 40; }

    function agentsMap() {
      var a = S.agents || {};
      if (Array.isArray(a)) { var m = {}; a.forEach(function (x) { m[x.id] = x; }); S.agents = a = m; }
      return a;
    }
    // index of the step with this seq (searching from the end: updates mostly hit recent steps), or -1
    function findSeq(list, seq) {
      if (seq == null) return -1;
      for (var i = list.length - 1; i >= 0; i--) if (list[i].seq === seq) return i;
      return -1;
    }
    // put s into list (replacing an older version of the same seq); "new" | "update" | "insert" | null (stale)
    function upsert(list, s) {
      var i = findSeq(list, s.seq);
      if (i >= 0) {
        var o = list[i];
        if (o === s || (o.useq != null && s.useq != null && s.useq <= o.useq)) return null;
        list[i] = s;
        return "update";
      }
      var last = list[list.length - 1];
      if (last && s.seq != null && last.seq != null && s.seq < last.seq) {
        var j = list.length;
        while (j > 0 && list[j - 1].seq > s.seq) j--;
        list.splice(j, 0, s);
        return "insert";
      }
      list.push(s);
      return "new";
    }

    // ---- header ----
    function renderHead() {
      goalNode($("#h-goal", root), S.goal);
      document.title = "tokenscope · " + (goalName(S.goal) || "session");
      $("#h-proj", root).textContent = (S.project || "—") + (S.model ? " · " + S.model : "") + " · " + String(S.id || id).slice(0, 8);
      var pw = $("#h-pill", root);
      if (pw._st !== S.status) { pw.innerHTML = ""; pw.appendChild(pill(S.status)); pw._st = S.status; }
      var T = S.totals || {};
      cw.set(T.cw != null ? T.cw : S.cw); usd.set(T.usd != null ? T.usd : S.usd);
      $("#h-turns", root).textContent = (T.turns || turns.length) + " turns · " + (T.tool_calls || 0) + " tools" + (T.errors ? " · " + T.errors + " errors" : "");
      var ctx = +S.context_tokens || 0, pct = Math.min(100, ctx / 2000);
      var g = $("#h-gauge", root);
      g.style.width = pct + "%";
      g.className = pct >= 75 ? "hot" : pct >= 50 ? "warm" : "";
      $("#h-ctx", root).textContent = fmtK(ctx) + " / 200k";
      var fl = $("#h-flags", root); fl.innerHTML = "";
      (S.flags || []).forEach(function (f) { fl.appendChild(flagChip(f, ctx, true)); });
      tick();
      updateAuto();
      scheduleMap();
    }
    function tick() {
      if (!S) return;
      var st = ts(S.started), lt = ts(S.last_event_ts || S.last_ts);
      var e = st != null ? ((S.status === "idle" ? (lt || nowS()) : nowS()) - st) : S.elapsed_s;
      $("#h-el", root).textContent = fmtDur(e);
    }

    // ---- ribbon ----
    function ribbonAdd(t, rb, merge) {
      var last = rb.lastElementChild;
      if (merge && last && last._g === t.group) { last._cw += +t.cw || 0; last.style.flexGrow = Math.max(1, last._cw); last.title = t.group + " · " + fmtK(last._cw) + " CW"; return; }
      var i = el("i");
      i._g = t.group; i._cw = +t.cw || 0;
      i.style.background = gcol(t.group);
      i.style.flexGrow = Math.max(1, i._cw);
      i.title = (t.intent || t.group) + " · " + fmtK(t.cw) + " CW";
      rb.appendChild(i);
    }
    function renderLegend() {
      var used = {}; turns.forEach(function (t) { used[t.group] = (used[t.group] || 0) + (+t.cw || 0); });
      var lg = $("#legend", root); lg.innerHTML = "";
      GROUPS.filter(function (g) { return used[g]; }).forEach(function (g) {
        var s = el("span"); s.innerHTML = '<span class="dot" style="background:' + gcol(g) + '"></span>' + g + " " + fmtK(used[g]);
        lg.appendChild(s);
      });
    }

    // ---- timeline rows ----
    function renderRow(row, idx) {
      var node;
      if (row.type === "seg") {
        node = el("div", "blk");
        node.style.borderLeftColor = gcol(row.group);
        var h = el("div", "bh");
        node.appendChild(h);
        var bb = el("div", "bb");
        node.appendChild(bb);
        h.onclick = function () {
          openSegs[idx] = !isOpen(idx);
          fillSeg(node, curRow(row, idx), idx);
        };
        fillSeg(node, row, idx);
      } else if (row.type === "prompt") {
        var s = row.step;
        node = el("div", "prompt");
        node.appendChild(el("span", "pn", "prompt" + (s.n ? " " + s.n : "")));
        node.appendChild(document.createTextNode(s.text ? s.text : "[prompt, " + (s.chars || 0) + " chars]"));
      } else if (row.type === "reason") {
        node = reasonRow(row.step);
      } else if (row.type === "compact") {
        node = el("div", "compact", "⇣ context compacted");
      } else if (row.type === "flag") {
        node = el("div", "callout", "⚑ " + flagText({flag: row.step.flag, detail: row.step.detail}, S.context_tokens));
      } else if (row.type === "fanout") {
        node = el("div", "blk fan");
        var fh = el("div", "bh"), fb = el("div", "bb");
        node.appendChild(fh); node.appendChild(fb);
        fh.onclick = function () { var k = fanKey(row.step); openFans[k] = !openFans[k]; fillFan(node, curRow(row, idx)); };
        fillFan(node, row);
      } else {
        node = el("div", "small mut", row.type);
      }
      node.classList.add("row");
      row.node = node;
      return node;
    }
    // rows are rebuilt when a step changes: event handlers look up the current row object by index
    function curRow(row, idx) { var r = typeof idx === "number" && model.rows[idx]; return r && r.type === row.type ? r : row; }
    function fanKey(s) { return s.seq != null ? "s" + s.seq : s.agent_id || "?"; }
    function fillSeg(node, row, idx) {
      var open = isOpen(idx), err = row.turns.some(turnErr);
      node.classList.toggle("open", open);
      node.classList.toggle("err", err);
      var h = node.firstChild;
      h.innerHTML = '<span class="caret">▸</span><b style="color:' + gcol(row.group) + '">' + esc((row.group || "other").toUpperCase()) + "</b>" +
        '<span class="mut">' + row.turns.length + " turn" + (row.turns.length > 1 ? "s" : "") + "</span>" +
        '<span>' + esc(toolSummary(row.turns)) + "</span>" +
        (err ? '<span class="chip bad">✗ ' + row.turns.filter(turnErr).length + " err</span>" : "") +
        '<span class="mut" style="margin-left:auto">' + fmtK(row.cw) + " CW</span>";
      var bb = node.lastChild;
      bb.style.display = open ? "" : "none";
      bb.innerHTML = "";
      if (!open) return; // keep DOM small: build children only when expanded
      var pt = null;
      row.items.forEach(function (s) {
        if (s.k === "turn") { bb.appendChild(turnRow(s, pt)); pt = s; } else bb.appendChild(reasonRow(s));
      });
    }
    function fillFan(node, row) {
      var s = row.step, ag = agentsMap()[s.agent_id] || {}, steps = ag.steps || [];
      var open = !!openFans[fanKey(s)];
      var aturns = steps.filter(function (x) { return x.k === "turn"; });
      node.classList.toggle("open", open);
      var h = node.firstChild;
      h.innerHTML = '<span class="caret">▸</span><b style="color:#a07a00">↳ ' + esc(s.agent_type || ag.type || "agent") + "</b>" +
        '<span>' + esc(s.description || ag.description || "") + "</span>" +
        '<span class="pillw"></span>' +
        '<span class="mut" style="margin-left:auto">' + aturns.length + " turns · " + fmtK(ag.cw || 0) + " CW · " + fmtUsd(ag.usd || 0) + "</span>";
      if (ag.status) { var ap = pill(ag.status === "running" ? "working" : "idle"); ap.lastChild.textContent = ag.status; $(".pillw", h).appendChild(ap); }
      var bb = node.lastChild;
      bb.innerHTML = "";
      var mini = el("div", "ribbon mini");
      aturns.forEach(function (t) { ribbonAdd(t, mini, aturns.length > RIBBON_MAX); });
      bb.appendChild(mini);
      bb.style.display = "";
      if (!open) { if (!aturns.length) bb.appendChild(el("div", "small mut", steps.length ? "" : "waiting for subagent transcript…")); return; }
      var m = new RowModel();
      steps.forEach(function (x) { m.add(x); });
      m.rows.forEach(function (r, i) {
        if (r.type === "seg") {
          r.items.forEach(function (x, j) { bb.appendChild(x.k === "turn" ? turnRow(x, j ? r.items[j - 1] : null) : reasonRow(x)); });
        } else bb.appendChild(renderRow(r, "a" + i));
      });
    }
    function rerenderFanouts(agentId) {
      model.rows.forEach(function (r) { if (r.type === "fanout" && r.step.agent_id === agentId && r.node) fillFan(r.node, r); });
    }

    // ---- flow map: goal -> phases (same segments as the timeline) -> tools, subagent lanes below ----
    var fmSeen = {}, fmT = 0, fmEl = $("#fmap", root);
    function scheduleMap() { if (fmEl && !fmT) fmT = setTimeout(function () { fmT = 0; renderMap(); }, 150); }
    function tw(str, fs) { return String(str).length * fs * 0.58; } // cheap text-width estimate
    function clip(str, w, fs) { str = String(str); var n = Math.floor(w / (fs * 0.58)); return str.length > n ? str.slice(0, Math.max(1, n - 1)) + "…" : str; }
    function chipName(x) {
      var n = x.name || "?";
      if (/^mcp__/.test(n)) { var p = n.split("__"); return "mcp:" + p[1] + (p[2] ? "." + p[2] : ""); }
      if (n === "Skill") return "skill:" + (x.target || "?");
      return n;
    }
    function phaseInfo(r) {
      var im = {}, tc = {}, te = {}, order = [];
      r.turns.forEach(function (t) {
        var k = (t.intent || t.group || "?").replace(/_/g, " ");
        im[k] = (im[k] || 0) + (+t.cw || 0) + 1;
        (t.tools || []).forEach(function (x) { var n = chipName(x); if (!tc[n]) { tc[n] = 0; te[n] = 0; order.push(n); } tc[n]++; if (x.err) te[n]++; });
      });
      var intents = Object.keys(im).sort(function (a, b) { return im[b] - im[a]; }).slice(0, 2).join(" · ");
      order.sort(function (a, b) { return (te[b] - te[a]) || (tc[b] - tc[a]); });
      var chips = order.slice(0, 4).map(function (n) { return {t: n + (tc[n] > 1 ? " ×" + tc[n] : "") + (te[n] ? "  ✗ " + te[n] : ""), err: !!te[n]}; });
      if (order.length > 4) chips[3] = {t: "+" + (order.length - 3) + " more", err: false};
      return {group: r.group || "other", intents: intents, n: r.turns.length, cw: r.cw, chips: chips};
    }
    function renderMap() {
      if (!S || !fmEl) return;
      var W = Math.max(600, fmEl.parentNode.clientWidth - 32), NW = 158, GAP = 38, PX = 22;
      var cap = Math.max(3, Math.floor((W - PX * 2 + GAP) / (NW + GAP)));
      NW = Math.floor((W - PX * 2 - GAP * (cap - 1)) / cap);
      // logical rows: goal + phases; a new prompt starts a new row
      var lrows = [], cur, last = null, pn = 0, goal = {kind: "goal", key: "goal", fans: [], flags: []};
      function newRow(label) { cur = {label: label, items: []}; lrows.push(cur); }
      newRow(null); cur.items.push(goal);
      model.rows.forEach(function (r, i) {
        if (r.type === "prompt") { pn++; if (pn > 1 && last) { newRow("↩ prompt " + (r.step.n || pn)); } return; }
        if (r.type === "seg") { last = {kind: "phase", key: "m" + i, row: r, info: phaseInfo(r), fans: [], flags: []}; cur.items.push(last); return; }
        if (r.type === "fanout") (last || goal).fans.push(r.step);
        else if (r.type === "flag") (last || goal).flags.push(r.step);
      });
      lrows = lrows.filter(function (r) { return r.items.length; });
      var rows = [];
      lrows.forEach(function (r) { for (var i = 0; i < r.items.length; i += cap) rows.push({label: i ? null : r.label, wrap: i > 0, items: r.items.slice(i, i + cap)}); });
      var maxCw = 1, all = [];
      rows.forEach(function (r) { r.items.forEach(function (it) { all.push(it); if (it.info) maxCw = Math.max(maxCw, it.info.cw); }); });
      var lastItem = all[all.length - 1], working = S.status === "working";
      var out = [], links = [], lanesSvg = [], y = 6, CH = 17, am = agentsMap();
      function isNew(k) { var n = !fmSeen[k]; fmSeen[k] = 1; return n; }
      function chipsLayout(chips, w) { // -> [{x,y,w,t,err}], height
        var x = 0, yy = 0, res = [];
        chips.forEach(function (c) {
          var cw = Math.min(w, tw(c.t, 10) + 12);
          if (x && x + cw > w) { x = 0; yy += CH + 3; }
          res.push({x: x, y: yy, w: cw, t: c.t, err: c.err}); x += cw + 4;
        });
        return {list: res, h: res.length ? yy + CH : 0};
      }
      rows.forEach(function (r, ri) {
        var top = y + (r.label ? 18 : 0);
        if (r.label) out.push('<text class="fm-pl" x="' + PX + '" y="' + (y + 11) + '">' + esc(r.label) + "</text>");
        r.items.forEach(function (it) {
          if (it.kind === "goal") { it.h = 46; return; }
          it.cl = chipsLayout(it.info.chips, NW - 16);
          it.h = 62 + (it.cl.h ? it.cl.h + 8 : 0);
        });
        var H = Math.max.apply(null, r.items.map(function (it) { return it.h; }));
        r.items.forEach(function (it, i) { it.x = PX + i * (NW + GAP); it.y = top + (it.kind === "goal" ? (Math.min(H, 70) - it.h) / 2 : 0); it.cy = it.kind === "goal" ? it.y + it.h / 2 : top + 30; it.row = ri; });
        // subagent lanes, greedily packed into lines under the row
        var lines = [], laneTop = top + H + 14, LH = 60;
        r.items.forEach(function (it) {
          it.fans.forEach(function (f, k) {
            var ag = am[f.agent_id] || {}, m = new RowModel();
            (ag.steps || []).forEach(function (x) { m.add(x); });
            var segs = m.rows.filter(function (x) { return x.type === "seg"; });
            var head = "↳ " + (f.agent_type || ag.type || "agent") + (f.description || ag.description ? " · " + (f.description || ag.description) : "");
            var lx = it.x + 14, maxW = W - lx - 4;
            var want = Math.max(150, Math.min(tw(head, 11) + 80, 260), 12 + segs.length * 78);
            var lw = Math.min(maxW, want), li = 0;
            while (lines[li] != null && lines[li] > lx - 10) li++;
            lines[li] = lx + lw;
            var ly = laneTop + li * LH, st = ag.status || (segs.length ? "running" : "starting");
            var lk = "a" + f.agent_id + (k ? ":" + k : ""), nw = isNew(lk);
            // connector: dashed elbow from the node's bottom-left into the lane
            links.push('<path class="fm-fan' + (nw ? " fm-fade" : "") + '" d="M' + (it.x + 10) + " " + (it.y + it.h) + " V" + (ly + 18) + " H" + lx + '"/>');
            var g = ['<g transform="translate(' + lx + "," + ly + ')"><g class="' + (nw ? "fm-in" : "") + '">'];
            g.push('<rect class="fm-lane" width="' + lw + '" height="' + (LH - 10) + '" rx="8"/>');
            var stw = tw(st, 10) + 14, run = st === "running";
            g.push('<text class="fm-lh" x="8" y="13">' + esc(clip(head, lw - stw - 20, 11)) + "</text>");
            g.push('<g transform="translate(' + (lw - stw - 6) + ',3)"><rect class="fm-st' + (run ? " run" : "") + '" width="' + stw + '" height="14" rx="7"/><text class="fm-stt' + (run ? " run" : "") + '" x="' + stw / 2 + '" y="10.5">' + st + "</text></g>");
            var fit = Math.max(1, Math.floor((lw - 12 + 6) / 78)), shown = segs.length > fit ? segs.slice(segs.length - fit + 1) : segs;
            var mx = 8;
            if (segs.length > fit) { g.push('<text class="fm-more" x="' + mx + '" y="37">+' + (segs.length - shown.length) + "</text>"); mx += 24; }
            shown.forEach(function (sg, j) {
              var c = gcol(sg.group), mk = lk + ":" + (segs.length - shown.length + j), mn = isNew(mk), err = sg.turns.some(turnErr);
              if (j) g.push('<path class="fm-ma" d="M' + (mx - 6) + " 37 h4" + '" stroke="' + c + '"/>');
              g.push('<g transform="translate(' + mx + ',21)"><g class="' + (mn ? "fm-in" : "") + '"><rect class="fm-mini" width="72" height="24" rx="5" fill="' + c + '" fill-opacity=".14" stroke="' + (err ? "#d9480f" : c) + '"/>' +
                '<text class="fm-mg" x="5" y="10" fill="' + c + '">' + esc(clip(sg.group.toUpperCase(), 64, 8.5)) + "</text>" +
                '<text class="fm-mt" x="5" y="20">' + sg.turns.length + "t · " + fmtK(sg.cw) + (err ? " ✗" : "") + "</text></g></g>");
              mx += 78;
            });
            if (!segs.length) g.push('<text class="fm-more" x="8" y="37">waiting for transcript…</text>');
            g.push("</g></g>");
            lanesSvg.push(g.join(""));
          });
        });
        r.bottom = laneTop - 14 + H * 0 + (lines.length ? 14 + lines.length * LH - 10 : 0);
        r.top = top; r.H = H;
        r.laneRight = lines.length ? Math.max.apply(null, lines) : 0;
        y = Math.max(top + H, r.bottom) + 22;
      });
      // arrows between consecutive nodes; width ∝ phase CW
      function sw(it) { return it.info ? 1.5 + 9 * Math.sqrt(it.info.cw / maxCw) : 2; }
      for (var i = 1; i < all.length; i++) {
        var a = all[i - 1], b = all[i], w = sw(b), c = b.info ? gcol(b.info.group) : "#adb5bd", nw = !fmSeen["e" + b.key], d, hx = b.x - 2, hy = b.cy;
        if (a.row === b.row) d = "M" + (a.x + NW + 2) + " " + a.cy + " H" + (hx - 7 - w / 2);
        else {
          var ra = rows[a.row], rb = rows[b.row];
          var gx = Math.min(W - 2, Math.max(a.x + NW + 14, ra.laneRight + 10)), gy = rb.top - (rb.label ? 22 : 10), lx = b.x - 16;
          d = "M" + (a.x + NW + 2) + " " + a.cy + " H" + gx + " V" + gy + " H" + lx + " V" + hy + " H" + (hx - 7 - w / 2);
        }
        fmSeen["e" + b.key] = 1;
        links.push('<g class="' + (nw ? "fm-fade" : "") + '"><path class="fm-edge" d="' + d + '" stroke="' + c + '" stroke-width="' + w.toFixed(1) + '"/>' +
          '<path d="M' + hx + " " + hy + " l" + (-8 - w / 2) + " " + (-(w / 2 + 4)) + " v" + (w + 8) + 'z" fill="' + c + '"/></g>');
      }
      // nodes
      all.forEach(function (it) {
        var nw = isNew(it.key), pulse = working && it === lastItem;
        var g = ['<g transform="translate(' + it.x + "," + it.y + ')"><g class="' + (nw ? "fm-in" : "") + '">'];
        if (it.kind === "goal") {
          var gn = goalName(S.goal) || "(no prompt yet)", l1 = gn, l2 = "", per = Math.floor((NW - 24) / (12.5 * 0.58));
          if (gn.length > per) { var cut = gn.lastIndexOf(" ", per); if (cut < per * 0.5) cut = per; l1 = gn.slice(0, cut); l2 = clip(gn.slice(cut).trim(), NW - 24, 12.5); }
          g.push('<rect class="fm-goal' + (pulse ? " fm-pulse" : "") + '" width="' + NW + '" height="' + it.h + '" rx="' + it.h / 2 + '"/>');
          g.push('<text class="fm-gt" x="' + NW / 2 + '" y="' + (l2 ? 19 : 27) + '">' + esc(l1) + "</text>");
          if (l2) g.push('<text class="fm-gt" x="' + NW / 2 + '" y="35">' + esc(l2) + "</text>");
        } else {
          var f = it.info, c = gcol(f.group), err = it.row && it.info.chips.some(function (x) { return x.err; });
          if (pulse) g.push('<rect class="fm-pulse" width="' + NW + '" height="' + it.h + '" rx="10" fill="none" stroke="' + c + '"/>');
          g.push('<rect width="' + NW + '" height="' + it.h + '" rx="10" fill="#fff"/><rect class="fm-node" width="' + NW + '" height="' + it.h + '" rx="10" fill="' + c + '" fill-opacity=".1" stroke="' + c + '"/>');
          g.push('<rect width="4" height="' + (it.h - 16) + '" x="0" y="8" rx="2" fill="' + c + '"/>');
          g.push('<text class="fm-g" x="12" y="19" fill="' + c + '">' + esc(clip(f.group.toUpperCase(), NW - 20, 11)) + "</text>");
          g.push('<text class="fm-i" x="12" y="36">' + esc(clip(f.intents, NW - 20, 11.5)) + "</text>");
          g.push('<text class="fm-n" x="12" y="52">' + f.n + " turn" + (f.n > 1 ? "s" : "") + " · " + fmtK(f.cw) + " CW</text>");
          it.cl.list.forEach(function (ch) {
            g.push('<g transform="translate(' + (8 + ch.x) + "," + (62 + ch.y) + ')"><rect class="fm-chip' + (ch.err ? " err" : "") + '" width="' + ch.w + '" height="' + CH + '" rx="8.5"/>' +
              '<text class="fm-ct' + (ch.err ? " err" : "") + '" x="6" y="12">' + esc(clip(ch.t, ch.w - 10, 10)) + "</text></g>");
          });
          void err;
        }
        // flag badges (amber) hanging off the node's top-right corner
        var fx = NW - 4;
        it.flags.slice(-2).forEach(function (fl) {
          var t = "⚑ " + flagText({flag: fl.flag, detail: fl.detail}, S.context_tokens), bw = Math.min(NW - 10, tw(t, 10) + 12);
          fx -= bw;
          g.push('<g class="fm-flag" transform="translate(' + fx + ',-9)"><rect width="' + bw + '" height="16" rx="8"/><text x="6" y="11.5">' + esc(clip(t, bw - 10, 10)) + "</text></g>");
          fx -= 4;
        });
        g.push("</g></g>");
        out.push(g.join(""));
      });
      fmEl.setAttribute("viewBox", "0 0 " + W + " " + y);
      fmEl.setAttribute("width", W); fmEl.setAttribute("height", y);
      fmEl.innerHTML = links.join("") + lanesSvg.join("") + out.join("");
      var meta = $("#fm-meta", root);
      if (meta) meta.textContent = (all.length - 1) + " phase" + (all.length === 2 ? "" : "s") + " · " + Object.keys(am).length + " subagent" + (Object.keys(am).length === 1 ? "" : "s");
    }
    window.addEventListener("resize", scheduleMap);

    // ---- auto-expand the last phase while working ----
    var autoIdx = -1;
    function isOpen(idx) { return Object.prototype.hasOwnProperty.call(openSegs, idx) ? openSegs[idx] : idx === autoIdx; }
    function calcAuto() {
      if (!S || S.status !== "working") return -1;
      for (var i = model.rows.length - 1; i >= 0; i--) if (model.rows[i].type === "seg") return i;
      return -1;
    }
    function updateAuto() {
      var n = calcAuto(), o = autoIdx;
      if (n === o) return;
      autoIdx = n;
      [o, n].forEach(function (i) { var r = model.rows[i]; if (r && r.type === "seg" && r.node) fillSeg(r.node, r, i); });
    }

    function renderAll() {
      tl.innerHTML = "";
      autoIdx = calcAuto();
      scheduleMap();
      var n = model.rows.length;
      showFrom = Math.max(0, n - MAX_ROWS);
      if (showFrom > 0) {
        var b = el("button", "more", "▴ show " + Math.min(MAX_ROWS, showFrom) + " earlier rows (" + showFrom + " hidden)");
        b.onclick = function () {
          var y = document.documentElement.scrollHeight - window.scrollY;
          var from = Math.max(0, showFrom - MAX_ROWS), frag = document.createDocumentFragment();
          for (var i = from; i < showFrom; i++) { var nd = renderRow(model.rows[i], i); nd.classList.remove("row"); frag.appendChild(nd); }
          tl.insertBefore(frag, b.nextSibling);
          showFrom = from;
          if (!showFrom) b.remove(); else b.textContent = "▴ show " + Math.min(MAX_ROWS, showFrom) + " earlier rows (" + showFrom + " hidden)";
          window.scrollTo(0, document.documentElement.scrollHeight - y);
        };
        tl.appendChild(b);
      }
      for (var i = showFrom; i < n; i++) { var nd = renderRow(model.rows[i], i); nd.classList.remove("row"); tl.appendChild(nd); }
      renderRibbon();
      renderLegend();
      $("#s-empty", root).style.display = n ? "none" : "";
    }

    function rebuild() {
      model = new RowModel(); turns = [];
      S.steps.forEach(function (x) { model.add(x); if (x.k === "turn") turns.push(x); });
    }
    function rowSig(r) { return r.type + ":" + (r.type === "seg" ? r.group + "/" + r.items.length : r.step.seq); }
    function rowHas(r, seq) { return r.type === "seg" ? r.items.some(function (x) { return x.seq === seq; }) : r.step.seq === seq; }
    function renderRibbon() {
      ribbon.innerHTML = "";
      var merge = turns.length > RIBBON_MAX;
      turns.forEach(function (t) { ribbonAdd(t, ribbon, merge); });
      Array.prototype.forEach.call(ribbon.children, function (x) { x.style.animation = "none"; });
    }
    // a step already shown changed (tool err, more tools / CW on the same turn, fanout linked): re-render in place
    function updateMain(s) {
      var old = model.rows;
      rebuild();
      var same = old.length === model.rows.length && old.every(function (r, i) { return rowSig(r) === rowSig(model.rows[i]); });
      if (!same) { renderAll(); return; }
      model.rows.forEach(function (r, i) {
        r.node = old[i].node;
        if (!rowHas(r, s.seq) || !r.node || !r.node.parentNode) return;
        if (r.type === "seg") fillSeg(r.node, r, i);
        else if (r.type === "fanout") fillFan(r.node, r);
        else { var nn = renderRow(r, i); nn.classList.remove("row"); old[i].node.parentNode.replaceChild(nn, old[i].node); }
      });
      if (s.k === "turn") {
        var ti = turns.indexOf(s), seg = ribbon.children[ti];
        if (turns.length <= RIBBON_MAX && seg && ribbon.children.length === turns.length) {
          seg._g = s.group; seg._cw = +s.cw || 0;
          seg.style.background = gcol(s.group);
          seg.style.flexGrow = Math.max(1, seg._cw);
          seg.title = (s.intent || s.group) + " · " + fmtK(s.cw) + " CW";
        } else renderRibbon();
        renderLegend();
      }
    }
    function ingest(s, agent) {
      if (s.useq != null && s.useq > maxSeq) maxSeq = s.useq;
      if (s.seq != null && s.seq > maxSeq) maxSeq = s.seq;
      if (agent) {
        var am = agentsMap(), a = am[agent] || (am[agent] = {id: agent, steps: []});
        if (!Array.isArray(a.steps)) a.steps = [];
        if (!upsert(a.steps, s)) return false;
        var tcw = 0, tusd = 0;
        a.steps.forEach(function (x) { if (x.k === "turn") { tcw += +x.cw || 0; tusd += +x.usd || 0; } });
        a.cw = Math.max(+a.cw || 0, tcw); a.usd = Math.max(+a.usd || 0, tusd);
        rerenderFanouts(agent);
        scheduleMap();
        return true;
      }
      var how = upsert(S.steps, s);
      if (!how) return false;
      $("#s-empty", root).style.display = "none";
      if (how === "update") { updateMain(s); updateAuto(); scheduleMap(); return true; }
      if (how === "insert") { rebuild(); renderAll(); return true; }
      var stick = follow.checked && atBottom();
      var res = model.add(s);
      if (s.k === "turn") {
        turns.push(s);
        ribbonAdd(s, ribbon, turns.length > RIBBON_MAX);
        renderLegend();
      }
      var idx = model.rows.indexOf(res.row);
      if (res.changed) { if (res.row.node) fillSeg(res.row.node, res.row, idx); }
      else tl.appendChild(renderRow(res.row, idx));
      updateAuto();
      scheduleMap();
      // keep DOM bounded for live sessions too
      var rendered = model.rows.length - showFrom;
      if (rendered > MAX_ROWS * 1.5) renderAll();
      if (stick) requestAnimationFrame(function () { window.scrollTo({top: document.documentElement.scrollHeight, behavior: "smooth"}); });
      else if (!atBottom() && !REPLAY) jump.style.display = "block";
      return true;
    }
    // subagent steps carry `agent` (fanout steps in the main thread carry `agent_id` = the branch they open)
    function agentOf(s, msg) { return s.agent || (msg && msg.agent) || null; }

    function applySummary(d) { // totals/status/flags/agents summary from ?since or board events
      ["status", "flags", "context_tokens", "totals", "goal", "project", "last_event_ts", "last_ts", "started"].forEach(function (k) { if (d[k] != null) S[k] = d[k]; });
      if (d.cw != null && !d.totals) { S.totals = S.totals || {}; S.totals.cw = d.cw; S.totals.usd = d.usd; S.totals.turns = d.turns; S.totals.tool_calls = d.tool_calls; S.totals.errors = d.errors; }
      if (d.agents) {
        var am = agentsMap(), list = Array.isArray(d.agents) ? d.agents : Object.keys(d.agents).map(function (k) { var x = d.agents[k]; x.id = x.id || k; return x; });
        list.forEach(function (x) {
          var a = am[x.id] || (am[x.id] = {id: x.id, steps: []});
          Object.keys(x).forEach(function (k) { if (k !== "steps" && !((k === "cw" || k === "usd") && x[k] < (a[k] || 0))) a[k] = x[k]; });
          if (Array.isArray(x.steps)) x.steps.forEach(function (st) { ingest(st, x.id); });
          rerenderFanouts(x.id);
        });
      }
      renderHead();
    }

    // ?replay=1: fetch the session once, then feed its steps (main + subagents) in seq order — for demos/recordings
    var REPLAY = new URLSearchParams(location.search).get("replay");
    function replayOf(d) {
      var full = JSON.parse(JSON.stringify(d)), feed = [], ags = Array.isArray(full.agents) ? full.agents : Object.keys(full.agents || {}).map(function (k) { var x = full.agents[k]; x.id = x.id || k; return x; });
      (full.steps || []).forEach(function (x) { feed.push({s: x}); });
      var lastOf = {};
      ags.forEach(function (a) { (a.steps || []).forEach(function (x) { feed.push({s: x, a: a.id}); lastOf[a.id] = x; }); });
      feed.sort(function (p, q) { return ((p.s.seq || 0) - (q.s.seq || 0)) || ((ts(p.s.ts) || 0) - (ts(q.s.ts) || 0)); });
      d.steps = []; d.flags = []; d.status = "working"; d.totals = {cw: 0, usd: 0, turns: 0, tool_calls: 0, errors: 0}; d.context_tokens = 0;
      d.agents = ags.map(function (a) { var m = {}; Object.keys(a).forEach(function (k) { if (k !== "steps") m[k] = a[k]; }); m.steps = []; m.cw = 0; m.usd = 0; m.status = "running"; return m; });
      var step0 = feed.length && ts(feed[0].s.ts);
      if (step0) { d.now = step0; d.started = step0 - 1; }
      var k = 0, T = d.totals, ctxMax = +full.context_tokens || 0, nturns = (full.steps || []).filter(function (x) { return x.k === "turn"; }).length || 1;
      setTimeout(function next() {
        if (!S) return;
        if (k >= feed.length) {
          ["status", "flags", "totals", "context_tokens", "last_event_ts"].forEach(function (x) { S[x] = full[x]; });
          ags.forEach(function (a) { var m = agentsMap()[a.id]; if (m) { m.status = a.status; m.cw = a.cw; m.usd = a.usd; } rerenderFanouts(a.id); });
          renderHead(); return;
        }
        var f = feed[k++], x = f.s;
        if (x.ts != null) { clockOff = ts(x.ts) - Date.now() / 1000; S.last_event_ts = x.ts; }
        if (!f.a && x.k === "turn") {
          T.cw += +x.cw || 0; T.usd += +x.usd || 0; T.turns++; T.tool_calls += (x.tools || []).length;
          T.errors += (x.tools || []).filter(function (t) { return t.err; }).length;
          S.context_tokens = Math.round(ctxMax * Math.min(1, T.turns / nturns));
        }
        if (!f.a && x.k === "flag") S.flags = (full.flags || []).filter(function (fl) { return fl.flag === x.flag; }).concat((S.flags || []).filter(function (fl) { return fl.flag !== x.flag; }));
        ingest(x, f.a || null);
        if (f.a && lastOf[f.a] === x) { var m = agentsMap()[f.a]; if (m) { m.status = "done"; rerenderFanouts(f.a); } }
        renderHead();
        setTimeout(next, 400);
      }, 900);
      return d;
    }

    function load() {
      return SRC.session(id).then(function (d) {
        if (REPLAY) { d = replayOf(d); follow.checked = false; }
        S = d; S.steps = S.steps || [];
        if (d.now != null) clockOff = ts(d.now) - Date.now() / 1000;
        maxSeq = d.seq != null ? d.seq : -1; openSegs = {}; openFans = {};
        S.steps.sort(function (a, b) { return (a.seq || 0) - (b.seq || 0); });
        rebuild();
        var am = agentsMap();
        Object.keys(am).forEach(function (k) { if (!Array.isArray(am[k].steps)) am[k].steps = []; });
        $("#notfound", root).style.display = "none";
        $("#body", root).style.display = "";
        renderHead();
        renderAll();
        if (follow.checked) window.scrollTo(0, document.documentElement.scrollHeight);
      }, function (e) {
        $("#body", root).style.display = "none";
        var nf = $("#notfound", root);
        nf.style.display = "";
        nf.textContent = e && e.status === 404 ? "Unknown session “" + id + "” — it may have been idle for more than 6 h, or the id is wrong." : "Cannot reach the tokenscope server.";
        throw e;
      });
    }
    function poll() {
      if (!S) return load();
      return SRC.session(id, maxSeq).then(function (d) {
        (d.steps || []).forEach(function (s) { ingest(s, agentOf(s)); });
        applySummary(d);
      });
    }

    window.addEventListener("scroll", function () { if (atBottom()) jump.style.display = "none"; }, {passive: true});
    jump.onclick = function () { follow.checked = true; window.scrollTo({top: document.documentElement.scrollHeight, behavior: "smooth"}); jump.style.display = "none"; };
    setInterval(tick, 1000);

    if (!id) { $("#body", root).style.display = "none"; $("#notfound", root).style.display = ""; $("#notfound", root).textContent = "No session id in the URL."; return; }
    function start() { load().then(REPLAY ? function () { $("#conn", root).textContent = "▶ replay"; } : connect, function (e) { if (e && e.status === 404) setTimeout(start, 2000); else setTimeout(start, 5000); }); }
    function connect() {
      liveLink({
        connEl: $("#conn", root), every: 1000,
        poll: poll,
        resync: function () { if (S) poll(); }, // catch up on anything missed while SSE was down
        step: function (m) { if (!S || m.session !== id || !m.step) return; ingest(m.step, agentOf(m.step, m)); },
        board: function (b) {
          if (!S) return;
          if (b.now != null) clockOff = ts(b.now) - Date.now() / 1000;
          (b.sessions || []).forEach(function (x) {
            if (x.id !== id) return;
            var y = {}; Object.keys(x).forEach(function (k) { if (k !== "flags" || !(S.flags && S.flags.length)) y[k] = x[k]; });
            applySummary(y);
          });
        }
      });
    }
    start(); // a session that has not written anything yet 404s: keep retrying until it shows up
  }

  window.TSLive = {initBoard: initBoard, initSession: initSession, GCOL: GCOL, fmtK: fmtK, fmtDur: fmtDur};
  document.addEventListener("DOMContentLoaded", function () {
    var page = document.body.getAttribute("data-page");
    if (page === "board") initBoard();
    else if (page === "session") initSession();
  });
})();
