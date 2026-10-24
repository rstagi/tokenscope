/* tokenscope live — board + session view. Vanilla JS, no dependencies.
   Data comes from a "source" (default: the HTTP server; demo.html injects a fake one via window.TS_SOURCE). */
(function () {
  "use strict";

  // ---------- helpers ----------
  var GROUPS = ["investigate", "change", "verify", "ship", "delegate", "research", "wait", "plan", "report", "overhead", "other", "respond"];
  var GCOL = {investigate: "#4c6ef5", change: "#f0645a", verify: "#f08c00", ship: "#7950f2", delegate: "#d4a017", research: "#12a594",
    wait: "#8d6e63", plan: "#e64980", report: "#adb5bd", overhead: "#ced4da", other: "#b8bec6", respond: "#c5cad1"};
  function gcol(g) { return GCOL[g] || "#b8bec6"; }
  // text colour of a [CATEGORY] tag: the pale greys of the bars are too light to read as text
  var TCOL = {overhead: "#868e96", other: "#868e96", respond: "#868e96", report: "#868e96"};
  function tcol(g) { return TCOL[g] || gcol(g); }
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
  var STATUS_TXT = {working: "working", waiting: "waiting for you", idle: "idle", done: "done"};
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
  // alerts (tokenscope/alerts.py): a chip per active one (▲ + its name, the message on hover), then how many more
  var ALERT_NAMES = {session_usd: "session $", intent_usd: "intent $", jump: "cost jump", unrelated: "off-task",
    repeat_read: "read loop", repeat_search: "search loop", repeat_command: "command loop", repeat_call: "call loop",
    retry: "retry loop", edit_revert: "edit revert"};
  function alertName(x) { return ALERT_NAMES[x.rule] || String(x.rule || x.kind || "alert").replace(/_/g, " "); }
  // (board cards: "top" holds the active ones not acknowledged; "new" counts every one not acknowledged)
  function alertChips(node, al) {
    var n = al ? (al.new != null ? al.new : al.n) : 0;
    if (!n) return;
    var top = Array.isArray(al.top) ? al.top : [];
    top.forEach(function (x) {
      var c = el("span", "chip alert " + (x.severity || "warning"), "▲ " + alertName(x) + (x.count > 1 ? " ×" + x.count : ""));
      c.title = x.message || "";
      node.appendChild(c);
    });
    if (n > top.length) {
      var by = al.new_by_kind || al.by_kind || {};
      var m = el("span", "chip", (top.length ? "+" : "") + (n - top.length) + " alert" + (n - top.length === 1 ? "" : "s"));
      m.title = Object.keys(by).map(function (k) { return k.replace(/_/g, "-") + " ×" + by[k]; }).join(" · ");
      node.appendChild(m);
    }
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
      top: function (w, by) { return getJSON("/api/top?window=" + encodeURIComponent(w) + "&by=" + encodeURIComponent(by || "intent")); },
      session: function (id, since) {
        return getJSON("/api/session/" + encodeURIComponent(id) + (since != null ? "?since=" + since : ""));
      },
      // the pane's payload; ops=all: every intent step with its calls
      compact: function (id) { return getJSON("/api/session/" + encodeURIComponent(id) + "/compact?ops=all"); },
      // the follow view's tiles: up to 4 sessions in one request
      follow: function (ids) { return getJSON("/api/follow?s=" + ids.map(encodeURIComponent).join(",")); },
      sessionHref: function (id) { return "/s/" + encodeURIComponent(id); },
      followHref: followHref,
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
        '<div class="goal"></div><div class="latest"></div><div class="r2 ic"></div><div class="r2"><span class="last"></span><span class="since"></span></div>' +
        '<div class="r3"><span><b class="el"></b> <span class="mut small elx"></span></span><svg class="spark" viewBox="0 0 60 16" preserveAspectRatio="none"></svg><span><b class="cw"></b> CW · <b class="usd"></b></span></div>' +
        '<div class="stack"></div><div class="r1" style="margin-top:6px"><span class="ag"></span><span class="tc"></span></div><div class="flags"></div>';
      a._cw = new Counter($(".cw", a), fmtK);
      a._usd = new Counter($(".usd", a), fmtUsd);
      if (SRC.followHref) {   // pin to the follow view (not on the demo page)
        var pb = el("button", "pin", "☆");
        pb.onclick = function (e) {
          e.preventDefault(); e.stopPropagation();
          if (!pb.disabled) { pins.set(togglePin(pins.get(), s.id)); pinState(); }
        };
        $(".r1", a).insertBefore(pb, $(".pillw", a));
      }
      return a;
    }
    // every card's pin button and the header's follow link from the stored pins: at most PIN_MAX; a session that
    // ended can be unpinned but not pinned
    function pinState() {
      var list = pins.get(), link = $("#follow-link", root);
      Object.keys(cards).forEach(function (id) {
        var pb = cards[id].querySelector(".pin"), s = cards[id]._s || {};
        if (!pb) return;
        var on = list.indexOf(id) >= 0;
        pb.textContent = on ? "★" : "☆";
        pb.classList.toggle("on", on);
        pb.disabled = !on && (list.length >= PIN_MAX || !!s.ended);
        pb.title = on ? "following: click to unpin" : s.ended ? "this session ended" :
          list.length >= PIN_MAX ? "already following " + PIN_MAX + " sessions" : "pin to the follow view";
      });
      if (link && SRC.followHref) {
        link.style.display = "";
        link.href = SRC.followHref(list);
        link.textContent = "follow" + (list.length ? " (" + list.length + ")" : "") + " →";
      }
    }
    // the session's own live warnings (the same rules as the pane / canvas) as one dot next to the project
    function warnDot(node, s) {
      var ws = Array.isArray(s.warnings) ? s.warnings : [];
      var d = node.querySelector(".wdot");
      if (!ws.length) { if (d) node.removeChild(d); return; }
      if (!d) d = node.insertBefore(el("span", "wdot"), node.firstChild);
      d.title = ws.map(function (w) { return w.text + (w.hint ? " — " + w.hint : ""); }).join("\n");
    }
    // cost per turn, oldest -> newest (the server buckets long sessions to a few dozen points)
    function sparkline(svg, series) {
      var v = Array.isArray(series) ? series : [], key = v.join(",");
      if (svg._k === key) return;
      svg._k = key;
      while (svg.firstChild) svg.removeChild(svg.firstChild);
      if (v.length < 2) return;
      var max = Math.max.apply(null, v) || 1, pts = v.map(function (x, i) {
        return (i * 60 / (v.length - 1)).toFixed(1) + "," + (15 - 14 * x / max).toFixed(1);
      });
      var pl = document.createElementNS("http://www.w3.org/2000/svg", "polyline");
      pl.setAttribute("points", pts.join(" "));
      svg.appendChild(pl);
    }
    function updCard(a, s) {
      var r1 = $(".r1", a);
      $(".proj", a).textContent = s.project || "—";
      warnDot(r1, s);
      goalNode($(".goal", a), s.goal);
      // ASKED: the goal is the first prompt; a later follow-up shows under it
      var lt = $(".latest", a), q = s.asked || {};
      lt.textContent = q.latest ? "↳ " + q.latest + (q.prompt_n > 1 ? " · prompt " + q.prompt_n : "") : "";
      lt.title = q.latest || "";
      var pw = $(".pillw", a), st = s.ended ? "done" : s.status;
      if (a._st !== st) { pw.innerHTML = ""; pw.appendChild(pill(st)); }
      // the current intent as the pane names it ([CAT] label, ▶ while its step is open) and the session's skills
      var ic = $(".ic", a), cur = s.intent && typeof s.intent === "object" ? s.intent : null, sk = Array.isArray(s.skills) ? s.skills : [];
      var key = JSON.stringify([cur, s.current_intent, s.current_group, sk]);
      if (a._ic !== key) {
        ic.innerHTML = "";
        if (cur) {
          var t = el("span", "cat", "[" + String(cur.cat || "other").toUpperCase() + "]");
          t.style.color = tcol(cur.cat);
          ic.appendChild(t);
          var lb = el("span", "il", (cur.open ? "▶ " : "") + cur.label);
          lb.title = cur.label;
          ic.appendChild(lb);
        } else if (s.current_intent || s.current_group) ic.appendChild(intentChip(s.current_intent, s.current_group));
        if (sk.length) { var m = el("span", "skm", "✦ " + sk.slice(0, 2).join(" · ") + (sk.length > 2 ? " +" + (sk.length - 2) : "")); m.title = "skills: " + sk.join(", "); ic.appendChild(m); }
        a._ic = key;
      }
      // NOW: the latest call (▶ while working) and its target; else the last step
      var ls = s.last_step || {}, last = $(".last", a), w = s.now || {}, on = st === "working" && w.tool;
      var what = w.target && (!w.tool || (!w.tool.endsWith(" " + w.target) && w.tool !== w.target)) ? " · " + w.target : "";
      last.textContent = on ? "▶ " + w.tool + what : ls.label ? (ls.err ? "✗ " : "") + ls.label : "";
      last.className = "last" + (on ? " on" : ls.err ? " err" : "");
      a._now = on && w.since_s != null ? {since: w.since_s, at: data && data.now != null ? ts(data.now) : nowS()} : null;
      sparkline($(".spark", a), s.cw_series);
      a._cw.set(s.cw); a._usd.set(s.usd);
      fillStack($(".stack", a), s.intent_mix);
      $(".ag", a).textContent = s.agents_total ? "agents " + (s.agents_running || 0) + "/" + s.agents_total + (s.agents_running ? " running" : "") : "no subagents";
      $(".tc", a).textContent = (s.turns || 0) + " turns · " + (s.tool_calls || 0) + " tools" + (s.errors ? " · " + s.errors + " err" : "");
      var fl = $(".flags", a); fl.innerHTML = "";
      (s.flags || []).forEach(function (f) { fl.appendChild(flagChip(f, s.context_tokens)); });
      alertChips(fl, s.alerts);
      // unacknowledged alerts: a badge by kind in the header row and the card's edge in the worst severity's colour
      if (window.TSAlerts) { TSAlerts.badge(r1, s.alerts, $(".pillw", a)); TSAlerts.mark(a, s.alerts); }
      a._s = s;
      a._st = st;
    }
    function tickElapsed() {
      Object.keys(cards).forEach(function (id) {
        var a = cards[id], s = a._s;
        if (!s) return;
        var st = ts(s.started), lt = ts(s.last_ts), e;
        if (st != null) e = (s.status === "working" ? nowS() : (lt || nowS())) - st;   // ticks only while working
        else e = s.elapsed_s;
        $(".el", a).textContent = fmtDur(e);
        $(".elx", a).textContent = s.status === "idle" && lt ? "· " + fmtDur(nowS() - lt) + " ago" : "";
        $(".since", a).textContent = a._now ? fmtDur(a._now.since + nowS() - a._now.at) : "";
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
      if (d.harness_label) {
        var hSub = $("#harness-sub", root);
        if (hSub) hSub.textContent = d.harness_label + " sessions on this machine";
        var eh = $("#empty-harness", root);
        if (eh) eh.textContent = d.harness_label;
      }
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
      pinState();
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

    // the alerts section: a "needs attention" entry or an alert row scrolls to its card (the step link opens the
    // session canvas on that step)
    function focusCard(sid) {
      var a = cards[sid];
      if (!a) { location.href = SRC.sessionHref(sid); return; }
      a.scrollIntoView({block: "center", behavior: "smooth"});
      a.classList.remove("focus"); void a.offsetWidth; a.classList.add("focus");
    }
    if (window.TSAlerts && $("#alerts", root) && SRC.followHref) {
      TSAlerts.panel($("#alerts", root), {page: "board", onSession: focusCard,
        onJump: function (al, link) { if (link) return false; focusCard(al.sid); return true; }});
    }

    setInterval(tickElapsed, 1000);
    window.addEventListener("storage", function (e) { if (e.key === PIN_KEY) pinState(); });
    initTop(root);
    SRC.board().then(render, function () { $("#empty", root).textContent = "Cannot reach the tokenscope server."; $("#empty", root).style.display = ""; });
    liveLink({
      connEl: $("#conn", root), every: 2000,
      board: render,
      poll: function () { return SRC.board().then(render); },
      resync: function () {}
    });
  }

  // Top spenders: the window's costliest sessions and intents (/api/top, from the cross-session index), or by the
  // by-toggle just its sessions, goals or turns (each with its session's goal); every row opens the session canvas.
  // The index builds in the background: "pending" retries shortly.
  var TOP_KEY = { session: "sessions", goal: "goals", turn: "turns", intent: "intents" };
  function initTop(root) {
    var box = $("#top", root);
    if (!box || !SRC.top) return;
    box.style.display = "";
    var win = "5h", by = "intent", timer = 0;
    function rows(node, list, intents, sub) {
      node.innerHTML = "";
      if (!list.length) { node.appendChild(el("div", "mut small", "nothing in this window")); return; }
      list.forEach(function (x) {
        var a = el("a", "trow");
        a.href = SRC.sessionHref(x.sid);
        a.title = x.short + (x.project ? " · " + x.project : "") + (x.session_label ? " · " + x.session_label : "");
        var lb = el("span", "tl");
        if (intents) { var t = el("span", "cat", "[" + String(x.cat || "other").toUpperCase() + "] "); t.style.color = tcol(x.cat); lb.appendChild(t); }
        lb.appendChild(document.createTextNode(x.label || (intents || sub ? "—" : x.short)));
        if (sub) lb.appendChild(el("span", "ts", " · " + x.short + (x.session_label && x.session_label !== x.label ? " " + x.session_label : "") +
          (x.sessions > 1 ? " +" + (x.sessions - 1) + (x.sessions > 2 ? " sessions" : " session") : "")));
        a.appendChild(el("b", "tu", fmtUsd(x.usd)));
        var bar = el("span", "bar tb"), fill = el("i");
        fill.style.width = Math.min(100, +x.share || 0) + "%";
        bar.appendChild(fill);
        a.appendChild(bar);
        a.appendChild(el("span", "tp", (+x.share || 0).toFixed(1) + "%"));
        a.appendChild(lb);
        a.appendChild(el("span", "tm", fmtK(x.cw) + " CW · " + fmtDur(x.wall_s)));
        node.appendChild(a);
      });
    }
    function load() {
      clearTimeout(timer);
      var w = win, b = by;
      SRC.top(w, b).then(function (d) {
        if (w !== win || b !== by) return;
        if (d.pending) { $("#top-total", root).textContent = "indexing transcripts…"; timer = setTimeout(load, 3000); return; }
        var t = d.total || {};
        $("#top-total", root).textContent = fmtUsd(t.usd) + " · " + fmtK(t.cw) + " CW · " + (t.sessions || 0) + " sessions";
        var one = b !== "intent";
        $("#top-cols", root).classList.toggle("one", one);
        $("#top-col2", root).style.display = one ? "none" : "";
        $("#top-h1", root).textContent = one ? TOP_KEY[b].charAt(0).toUpperCase() + TOP_KEY[b].slice(1) : "Sessions";
        rows($("#top-sessions", root), d[one ? TOP_KEY[b] : "sessions"] || [], false, b === "goal" || b === "turn");
        if (!one) rows($("#top-intents", root), d.intents || [], true);
        timer = setTimeout(load, 60000);
      }, function () { $("#top-total", root).textContent = "unavailable"; timer = setTimeout(load, 60000); });
    }
    Array.prototype.forEach.call($("#top-win", root).children, function (b) {
      b.onclick = function () { win = b.getAttribute("data-w"); sel(); load(); };
    });
    Array.prototype.forEach.call($("#top-by", root).children, function (b) {
      b.onclick = function () { by = b.getAttribute("data-b"); sel(); load(); };
    });
    function sel() {
      Array.prototype.forEach.call($("#top-win", root).children, function (b) { b.classList.toggle("on", b.getAttribute("data-w") === win); });
      Array.prototype.forEach.call($("#top-by", root).children, function (b) { b.classList.toggle("on", b.getAttribute("data-b") === by); });
    }
    sel();
    load();
  }

  // ---------- follow ----------
  // Up to PIN_MAX pinned sessions side by side (/follow?s=a,b,c,d): pinned from a board card or the view's own
  // "add session" picker; the pins live in localStorage and in the URL, and a URL that names sessions wins.
  var PIN_KEY = "tokenscope.follow", PIN_MAX = 4;
  // "a, b,,a" -> ["a", "b"]: unique, non-empty, at most PIN_MAX
  function parsePins(v) {
    var out = [];
    (Array.isArray(v) ? v : String(v || "").split(",")).forEach(function (x) {
      x = String(x || "").trim();
      if (x && out.indexOf(x) < 0 && out.length < PIN_MAX) out.push(x);
    });
    return out;
  }
  // the pins of a page: the URL's ?s= when it names any, else the stored ones
  function resolvePins(search, stored) {
    var m = /[?&]s=([^&#]*)/.exec(search || ""), url = [];
    if (m) { try { url = parsePins(decodeURIComponent(m[1].replace(/\+/g, " "))); } catch (e) { url = []; } }
    return url.length ? url : parsePins(stored);
  }
  // pin / unpin `id`: a new list (unchanged when full)
  function togglePin(list, id) {
    list = parsePins(list);
    var i = list.indexOf(id);
    if (i >= 0) return list.slice(0, i).concat(list.slice(i + 1));
    return list.length < PIN_MAX ? list.concat([id]) : list;
  }
  function followHref(list) { return "/follow" + (list.length ? "?s=" + list.map(encodeURIComponent).join(",") : ""); }
  var pins = {
    get: function () { try { return parsePins(window.localStorage.getItem(PIN_KEY)); } catch (e) { return []; } },
    set: function (list) { try { window.localStorage.setItem(PIN_KEY, parsePins(list).join(",")); } catch (e) { /* private mode */ } }
  };
  // Where an auto-following tile scrolls (flow height h, content height end): to the end, unless a subagent branch
  // is live; then so the region from the first live branch (or the latest main step, if higher) to the end shows,
  // and when that region is taller than the tile, from its top (the live branch stays pinned).
  function pinScrollTop(h, end, top) {
    if (top == null || end - top <= h) return Math.max(0, end - h);
    return Math.max(0, top - 4);
  }
  // follow-view tile status -> pill
  var FSTATUS = {running: "working", ended: "done"};
  function fpill(status) {
    var p = el("span", "pill " + (FSTATUS[status] || status || "idle"));
    p.appendChild(el("span", "dot"));
    p.appendChild(document.createTextNode(status || "idle"));
    return p;
  }

  function initFollow(root) {
    root = root || document;
    var box = $("#tiles", root), tiles = {}, list = resolvePins(location.search, pins.get()), clockOff = 0;
    var busy = false, again = false, timer = 0;
    function nowS() { return Date.now() / 1000 + clockOff; }
    function save() {
      pins.set(list);
      if (window.history && history.replaceState) history.replaceState(null, "", followHref(list));
      box.setAttribute("data-n", String(list.length));
      $("#fempty", root).style.display = list.length ? "none" : "";
      $("#add", root).disabled = list.length >= PIN_MAX;
      $("#add", root).title = list.length >= PIN_MAX ? "already following " + PIN_MAX + " sessions: remove one first" : "follow another running session";
    }
    function unpin(id) {
      list = togglePin(list, id);
      var tile = tiles[id];
      if (tile && tile.parentNode) tile.parentNode.removeChild(tile);
      delete tiles[id];
      save();
    }
    function buildTile(id) {
      var tile = el("div", "ftile");
      tile.innerHTML = '<div class="fth"><span class="proj"></span><span class="fagn"></span><span class="fab"></span><span class="pillw"></span>' +
        '<span class="ftot"><b class="usd"></b> <span class="mut small cw"></span></span><button class="unpin" title="stop following">×</button></div>' +
        '<div class="ftal" style="display:none"></div><a class="fgoal"></a><div class="fnow"></div><div class="fflow"><div class="fsteps"></div></div>' +
        '<button class="fjump" style="display:none">↓ latest</button>';
      tile._usd = new Counter($(".usd", tile), fmtUsd);
      tile._follow = true;
      tile._nodes = {};
      tile._open = {};   // intents / collapsed branches the user opened ("i<scope>:<first step>", "a<agent id>")
      $(".fgoal", tile).href = SRC.sessionHref(id);
      var flow = $(".fflow", tile);
      // auto-follow: stick to the newest step until the user scrolls up; scrolling back down (or ↓) resumes it
      flow.addEventListener("scroll", function () {
        if (tile._auto) return;
        tile._follow = flow.scrollHeight - flow.scrollTop - flow.clientHeight < 24;
        $(".fjump", tile).style.display = tile._follow ? "none" : "";
      });
      // a tile that changes size (one added or removed) stays on its newest step
      if (window.ResizeObserver) new ResizeObserver(function () { if (tile._follow) scrollEnd(tile); }).observe(flow);
      // ↓ latest: back to the auto view (what the user opened closes again), following the newest step
      $(".fjump", tile).onclick = function (e) {
        e.stopPropagation();
        tile._follow = true;
        this.style.display = "none";
        if (Object.keys(tile._open).length) { tile._open = {}; if (tile._s) updTile(tile, tile._s); }
        scrollEnd(tile);
      };
      // opening / closing an intent or a collapsed branch: the user reads, so the tile stops following until ↓
      tile._toggle = function (key) {
        tile._open[key] = !tile._open[key];
        tile._follow = false;
        $(".fjump", tile).style.display = "";
        if (tile._s) updTile(tile, tile._s);
      };
      $(".unpin", tile).onclick = function (e) { e.stopPropagation(); unpin(id); };
      tile._jump = function (step) { return focusTile(id, step); };   // an alert in the tile's block: show its step
      // the whole tile opens the session canvas (not its buttons, not a text selection)
      tile.addEventListener("click", function (e) {
        if (e.target.closest("button") || String(window.getSelection ? window.getSelection() : "")) return;
        if (e.target.closest("a")) return;
        location.href = SRC.sessionHref(id);
      });
      return tile;
    }
    // auto-follow: the newest step, keeping a live subagent branch in view (pinScrollTop)
    function scrollEnd(tile) {
      var flow = $(".fflow", tile), fr = flow.getBoundingClientRect(), top = null;
      function y(e) { return e.getBoundingClientRect().top - fr.top + flow.scrollTop; }
      var live = $(".fsteps > .fbr > .fag.live", tile);
      if (live) {
        var mains = flow.querySelectorAll(".fsteps > .fnode"), last = mains[mains.length - 1];
        top = Math.min(y(live), last ? y(last) : Infinity);
      }
      tile._auto = true;
      cutIntents(tile, flow, top == null);
      flow.scrollTop = pinScrollTop(flow.clientHeight, flow.scrollHeight, top);
      setTimeout(function () { tile._auto = false; }, 50);
    }
    // an overflowing intent flow (e.g. under the tile's alert block) drops whole intents from its top, counted in
    // "+N earlier intents", rather than cutting through the first one; the newest intent always stays
    function cutIntents(tile, flow, on) {
      var box = $(".fsteps", tile), kids = [].slice.call(box.children), groups = [], grp = null;
      kids.forEach(function (e) { e.removeAttribute("data-cut"); });
      box.setAttribute("data-more", tile._more || "");
      if (!on || !tile._intents) return;
      kids.forEach(function (e) {
        if (!grp || e.classList.contains("fint")) groups.push(grp = {n: e.classList.contains("fint") ? 1 : 0, els: []});
        grp.els.push(e);
      });
      var cut = 0;
      for (var k = 0; k < groups.length - 1 && flow.scrollHeight > flow.clientHeight; k++) {
        groups[k].els.forEach(function (e) { e.setAttribute("data-cut", ""); });
        cut += groups[k].n;
        var n = (tile._moreN || 0) + cut;
        if (cut) box.setAttribute("data-more", "+" + n + " earlier intent" + (n > 1 ? "s" : ""));
      }
    }
    function stepNode(x) {
      var nd = el("div", "fnode");
      nd.innerHTML = '<span class="cat"></span><span class="fl"></span><span class="fc"></span>';
      return nd;
    }
    function updStep(nd, x) {
      var key = JSON.stringify([x.cat, x.label, x.usd, x.cw, x.open, x.tested, x.dur_s, x._al]);
      if (nd._k === key) return;
      nd._k = key;
      nd._i = x.i;
      nd.className = "fnode" + (x.open ? " open" : "");
      nd.style.borderLeftColor = gcol(x.cat);
      var c = $(".cat", nd);
      c.textContent = "[" + String(x.cat || "other").toUpperCase() + "]";
      c.style.color = tcol(x.cat);
      var lb = $(".fl", nd);
      lb.textContent = (x.open ? "▶ " : "") + (x.label || "—") + (x.tested === "pass" ? " ✓" : x.tested === "fail" ? " ✗" : "");
      lb.title = x.label || "";
      $(".fc", nd).textContent = (x.usd > 0 && x.usd < 0.005 ? "<$0.01" : fmtUsd(x.usd)) + (num(x.dur_s) != null ? " · " + fmtWall(x.dur_s) : "");
      $(".fc", nd).title = fmtK(x.cw) + " CW";
      // ▲ a step with alerts not acknowledged (main thread)
      var mk = $(".amark", nd);
      if (x._al && !mk) { mk = el("span", "amark"); nd.insertBefore(mk, $(".fc", nd)); }
      if (mk && !x._al) { nd.removeChild(mk); mk = null; }
      if (mk) { mk.className = "amark " + x._al; mk.textContent = "▲"; mk.title = "alert at this step (" + x._al + ")"; }
    }
    // a step's subagents: one branch per subagent (parallel ones as siblings), each with its latest steps and, under
    // the step that launched them, its own subagents; reconciled by id so a poll only updates what changed
    function place(box, node, n) { if (box.children[n] !== node) box.insertBefore(node, box.children[n] || null); }
    function drop(node) { if (node && node.parentNode) node.parentNode.removeChild(node); }
    // intents (steps.intent_blocks; each step row's "intent"): every intent is a header line ("[CAT] label · N steps ·
    // $X"), then "+N other steps" and only its latest step; its other steps stay hidden (their subagent branches
    // still show, under the latest step) until a click on the header opens the intent, a second click closes it.
    // A settled step outside any group is its own intent: the header alone. Steps without an intent (an older
    // server) are listed as they come.
    var SEV_N = {info: 0, warning: 1, critical: 2};
    function intentHead(nd, b, open, al, x) {
      var solo = b.kind === "step" && b.steps === 1, live = solo && x.open;
      var mark = solo ? (x.tested === "pass" ? " ✓" : x.tested === "fail" ? " ✗" : "") : "";
      var key = JSON.stringify([b.cat, b.label, b.kind, b.steps, b.usd, b.dur_s, open, al, live, mark]);
      if (nd._k === key) return;
      nd._k = key;
      nd.className = "fint " + (b.kind || "group") + (open ? " opened" : "") + (live ? " open" : "");
      nd.style.borderLeftColor = gcol(b.cat);
      nd.innerHTML = '<span class="ftg"></span><span class="cat"></span><span class="fl"></span><span class="fc"></span>';
      $(".ftg", nd).textContent = b.steps > 1 ? (open ? "▾" : "▸") : "";
      var c = $(".cat", nd);
      c.textContent = "[" + String(b.cat || "other").toUpperCase() + "]";
      c.style.color = tcol(b.cat);
      $(".fl", nd).textContent = (live ? "▶ " : "") + (b.label || "—") + (b.kind === "pending" ? "…" : "") + mark +
        (b.source === "declared" ? " ✎" : "");
      $(".fl", nd).title = b.kind === "pending" ? "the intent of these steps is not known yet" : (b.label || "");
      $(".fc", nd).textContent = b.steps + " step" + (b.steps === 1 ? "" : "s") + " · " + fmtUsd(b.usd) +
        (num(b.dur_s) != null ? " · " + fmtWall(b.dur_s) : "");
      $(".fc", nd).title = fmtK(b.cw) + " CW";
      if (al) {
        var mk = el("span", "amark " + al, "▲");
        mk.title = "alert in this intent (" + al + ")";
        nd.insertBefore(mk, $(".fc", nd));
      }
      nd.title = b.steps > 1 ? (open ? "click to show only the latest step" : "click to show all its steps") : "";
    }
    function syncSteps(box, steps, store, tile, intents, scope) {
      var n = 0, seen = {}, added = false, hs = store._heads || (store._heads = {});
      intents = intents || [];
      var blocks = [];
      steps.forEach(function (x) {
        var b = x.intent != null ? intents[x.intent] : null, last = blocks[blocks.length - 1];
        if (last && b && last.b === b) last.xs.push(x);
        else blocks.push({b: b, xs: [x]});
      });
      function row(x, hide, hd) {
        seen[x.i] = 1;
        var e = store[x.i];
        if (!e) { e = store[x.i] = {nd: stepNode(x)}; added = true; }
        e.hd = hd;
        updStep(e.nd, x);
        e.nd.style.display = hide ? "none" : "";
        place(box, e.nd, n++);
        return e;
      }
      function branch(x) {
        var e = store[x.i];
        if ((x.agents && x.agents.length) || x.agents_more) {
          if (!e.br) e.br = {el: el("div", "fbr"), ags: {}};
          added = syncBranch(e.br, x.agents || [], x.agents_more, tile, scope + "/" + x.i) || added;
          place(box, e.br.el, n++);
        } else if (e.br) { drop(e.br.el); e.br = null; }
      }
      blocks.forEach(function (k) {
        if (!k.b) { k.xs.forEach(function (x) { row(x, false, null); branch(x); }); return; }
        var b = k.b, key = "i" + scope + ":" + b.first, open = !!tile._open[key], hd = hs[key];
        if (!hd) {
          hd = hs[key] = el("div", "fint");
          hd.onclick = function (e) { e.stopPropagation(); if (hd._n > 1) tile._toggle(key); };
          added = true;
        }
        seen[key] = 1;
        hd._n = b.steps;
        var al = k.xs.reduce(function (w, x) { return x._al && (SEV_N[x._al] || 0) >= (SEV_N[w] == null ? -1 : SEV_N[w]) ? x._al : w; }, null);
        var latest = k.xs[k.xs.length - 1], other = b.steps - 1;
        intentHead(hd, b, open, al, latest);
        place(box, hd, n++);
        if (open) { k.xs.forEach(function (x) { row(x, false, hd); branch(x); }); return; }
        var okey = "o" + key, ot = hs[okey];
        if (other > 0) {
          if (!ot) { ot = hs[okey] = el("div", "fother"); ot.onclick = function (e) { e.stopPropagation(); tile._toggle(key); }; }
          seen[okey] = 1;
          ot.textContent = "+" + other + " other step" + (other > 1 ? "s" : "");
          ot.title = "click to show " + (k.xs.length > 1 ? "them" : "the intent's steps shown here");
          place(box, ot, n++);
        }
        k.xs.forEach(function (x) { if (x !== latest) row(x, true, hd); });
        row(latest, b.kind === "step" && b.steps === 1, hd);   // a one-step intent: its header is the step
        k.xs.forEach(function (x) { branch(x); });   // the intent's subagents stay in view
      });
      Object.keys(hs).forEach(function (k) { if (!seen[k]) { drop(hs[k]); delete hs[k]; } });
      Object.keys(store).forEach(function (i) {
        if (seen[i] || i === "_heads") return;
        drop(store[i].nd);
        if (store[i].br) drop(store[i].br.el);
        delete store[i];
      });
      return added;
    }
    var AG_ST = {running: "▶ running", starting: "starting…", idle: "quiet", done: "done", error: "failed"};
    // a finished branch that is not the latest is one header line (collapsed); a click on it shows its steps
    function syncBranch(br, agents, more, tile, scope) {
      var seen = {}, added = false, n = 0;
      agents.forEach(function (a, k) {
        var key = a.id || "pending:" + k + ":" + a.type;
        seen[key] = 1;
        var e = br.ags[key];
        if (!e) {
          e = br.ags[key] = {el: el("div", "fag"), store: {}};
          e.el.innerHTML = '<div class="fah"><span class="fat"></span><span class="fad"></span><span class="fas"></span>' +
            '<span class="fc"></span></div><div class="fasteps"></div>';
          added = true;
        }
        var okey = "a" + key, shut = a.collapsed && !tile._open[okey];
        e.el.className = "fag " + (a.status || "idle") + (a.live ? " live" : "") + (a.collapsed ? " foldable" : "") + (shut ? " collapsed" : "");
        $(".fah", e.el).onclick = a.collapsed ? function (ev) { ev.stopPropagation(); tile._toggle(okey); } : null;
        $(".fah", e.el).title = a.collapsed ? (shut ? "click to show its steps" : "click to collapse") : "";
        $(".fat", e.el).textContent = (a.collapsed ? (shut ? "▸ " : "▾ ") : "↳ ") + (a.workflow ? "workflow · " : "") + (a.type || "agent");
        $(".fad", e.el).textContent = a.description || "";
        $(".fad", e.el).title = a.description || "";
        $(".fas", e.el).textContent = AG_ST[a.status] || a.status || "";
        $(".fc", e.el).textContent = fmtUsd(a.usd) + (a.steps_n ? " · " + a.steps_n + " step" + (a.steps_n > 1 ? "s" : "") : "");
        $(".fc", e.el).title = fmtK(a.cw) + " CW (its own subagents included)";
        var box = $(".fasteps", e.el), steps = Array.isArray(a.steps) ? a.steps : [];
        var early = a.intents && a.intents.length ? (a.intents_n || 0) - a.intents.length : 0;
        box.setAttribute("data-more", early > 0 ? "+" + early + " earlier intent" + (early > 1 ? "s" : "") :
          !a.intents && a.steps_n > steps.length ? "+" + (a.steps_n - steps.length) + " earlier" : "");
        added = syncSteps(box, steps, e.store, tile, a.intents, scope + "/" + key) || added;
        place(br.el, e.el, n++);
      });
      Object.keys(br.ags).forEach(function (key) { if (!seen[key]) { drop(br.ags[key].el); delete br.ags[key]; } });
      if (more) {
        if (!br.more) br.more = el("div", "fmore");
        br.more.textContent = "+" + more + " more subagent" + (more > 1 ? "s" : "");
        place(br.el, br.more, n++);
      } else if (br.more) { drop(br.more); br.more = null; }
      return added;
    }
    function updTile(tile, s) {
      tile._s = s;
      if (s.missing) {
        $(".proj", tile).textContent = s.id.slice(0, 8);
        $(".fgoal", tile).textContent = "not on this server (yet): it shows up once the session writes";
        tile.classList.add("missing");
        return;
      }
      tile.classList.remove("missing");
      $(".proj", tile).textContent = (s.project || "—") + " · " + s.short;
      var pw = $(".pillw", tile);
      if (tile._st !== s.status) { pw.innerHTML = ""; pw.appendChild(fpill(s.status)); tile._st = s.status; }
      tile.classList.toggle("ended", s.status === "ended");
      goalNode($(".fgoal", tile), s.goal_source ? {name: s.goal, source: s.goal_source} : s.goal);
      tile._usd.set(s.usd);
      $(".cw", tile).textContent = fmtK(s.cw) + " CW";
      var flow = $(".fsteps", tile), steps = Array.isArray(s.steps) ? s.steps : [], marks = s.alert_steps || {};
      steps.forEach(function (x) { x._al = marks[String(x.i)] || null; });
      if (window.TSAlerts) {
        TSAlerts.badge($(".fab", tile), s.alerts); TSAlerts.mark(tile, s.alerts);
        // the tile's alerts in full at its top; a row or its step link shows the latest trigger's step here
        TSAlerts.tileBlock($(".ftal", tile), s.alerts && s.alerts.items, {
          stepHref: function (a) { return SRC.sessionHref(s.id) + "#step=" + a.step + (a.agent ? "&agent=" + encodeURIComponent(a.agent) : ""); },
          onJump: function (a) { return tile._jump ? tile._jump(a.agent ? null : a.step) : false; }});
      }
      var added = syncSteps(flow, steps, tile._nodes, tile, s.intents, "m");
      var ar = $(".fagn", tile);
      ar.textContent = s.agents_running ? s.agents_running + " subagent" + (s.agents_running > 1 ? "s" : "") + " running" : "";
      var more = s.intents ? s.intents_before || 0 : s.steps_n > steps.length ? s.steps_n - steps.length : 0;
      flow.setAttribute("data-more", more ? "+" + more + " earlier " + (s.intents ? "intent" : "step") + (more > 1 ? "s" : "") : "");
      if (!steps.length) flow.setAttribute("data-more", "no steps yet");
      tile._more = flow.getAttribute("data-more"); tile._moreN = more; tile._intents = !!s.intents;
      cutIntents(tile, $(".fflow", tile), tile._follow && !$(".fsteps > .fbr > .fag.live", tile));
      if (tile._follow && (added || tile._seq !== s.seq)) scrollEnd(tile);
      tile._seq = s.seq;
      tile._now = s.now ? {tool: s.now.tool, target: s.now.target, agent: s.now.agent, since: s.now.since_s, at: nowS()} : null;
      tickTile(tile);
    }
    function tickTile(tile) {
      var w = tile._now, n = $(".fnow", tile);
      var what = w && w.target && (!w.tool || (!w.tool.endsWith(" " + w.target) && w.tool !== w.target)) ? " · " + w.target : "";
      n.textContent = w && w.tool ? "▶ " + (w.agent ? w.agent + " › " : "") + w.tool + what + (w.since != null ? " · " + fmtWall(w.since + nowS() - w.at) : "") : "";
    }
    function render(d) {
      if (d.now != null) clockOff = ts(d.now) - Date.now() / 1000;
      $("#redacted", root).style.display = d.redacted ? "" : "none";
      var by = {};
      (d.sessions || []).forEach(function (s) { by[s.id] = s; });
      list.forEach(function (id, n) {
        var s = by[id] || (d.sessions || []).filter(function (x) { return x.id.indexOf(id) === 0; })[0];
        if (!s) return;
        var tile = tiles[id] || (tiles[id] = buildTile(s.id));
        if (box.children[n] !== tile) box.insertBefore(tile, box.children[n] || null);
        updTile(tile, s);
      });
    }
    function load() {
      if (!list.length) return Promise.resolve();
      if (busy) { again = true; return Promise.resolve(); }
      busy = true;
      return SRC.follow(list.slice()).then(render).then(function () {
        busy = false;
        if (again) { again = false; load(); }
      }, function (e) { busy = false; throw e; });
    }
    function soon() { clearTimeout(timer); timer = setTimeout(load, 250); }

    // the "add session" picker: the board's sessions that have not ended and are not followed yet
    var picker = $("#picker", root);
    function closePicker() { picker.style.display = "none"; }
    $("#add", root).onclick = function (e) {
      e.stopPropagation();
      if (picker.style.display !== "none") return closePicker();
      picker.innerHTML = "";
      picker.appendChild(el("div", "mut small", "loading…"));
      picker.style.display = "";
      SRC.board().then(function (b) {
        picker.innerHTML = "";
        var rows = (b.sessions || []).filter(function (s) { return !s.ended && list.indexOf(s.id) < 0; });
        if (!rows.length) { picker.appendChild(el("div", "mut small", "no other running sessions")); return; }
        rows.forEach(function (s) {
          var r = el("button", "prow");
          r.appendChild(el("span", "pg", goalName(s.goal) || "(no prompt yet)"));
          r.appendChild(el("span", "mut small", (s.project || "—") + " · " + s.short + " · " + (STATUS_TXT[s.status] || s.status) + " · " + fmtUsd(s.usd)));
          r.onclick = function (ev) { ev.stopPropagation(); list = togglePin(list, s.id); save(); closePicker(); load(); };
          picker.appendChild(r);
        });
      }, function () { picker.innerHTML = ""; picker.appendChild(el("div", "mut small", "cannot reach the server")); });
    };
    picker.addEventListener("click", function (e) { e.stopPropagation(); });
    document.addEventListener("click", closePicker);

    // the alerts section, a side column: every session's alerts; "needs attention" and alert rows jump to the tile
    // (and its step), pinning a session not followed yet while there is room
    function tileOf(sid) {
      if (tiles[sid]) return tiles[sid];
      var k = Object.keys(tiles).filter(function (id) { return sid.indexOf(id) === 0 || id.indexOf(sid) === 0; })[0];
      return k ? tiles[k] : null;
    }
    function focusTile(sid, step) {
      var tile = tileOf(sid);
      if (!tile) return false;
      tile.scrollIntoView({block: "nearest", behavior: "smooth"});
      tile.classList.remove("focus"); void tile.offsetWidth; tile.classList.add("focus");
      if (step == null) return true;
      var e = tile._nodes[step];
      if (!e) return true;
      var nd = e.nd.style.display === "none" ? e.hd : e.nd;   // a step its intent hides: the intent's header
      if (!nd) return true;
      var flow = $(".fflow", tile);
      tile._follow = false;
      $(".fjump", tile).style.display = "";
      tile._auto = true;
      flow.scrollTop = Math.max(0, nd.offsetTop - flow.offsetTop - flow.clientHeight / 3);
      setTimeout(function () { tile._auto = false; }, 50);
      nd.classList.remove("afocus"); void nd.offsetWidth; nd.classList.add("afocus");
      return true;
    }
    function openSession(sid, step) {
      if (focusTile(sid, step)) return true;
      if (list.length < PIN_MAX) {
        list = togglePin(list, sid); save();
        load().then(function () { setTimeout(function () { focusTile(sid, step); }, 80); });
        return true;
      }
      return false;
    }
    if (window.TSAlerts && $("#fside", root)) {
      TSAlerts.panel($("#fside", root), {page: "follow",
        onCollapse: function (v) { $("#fside", root).classList.toggle("collapsed", v); },
        onSession: function (sid) { if (!openSession(sid)) location.href = SRC.sessionHref(sid); },
        onJump: function (a, link) {
          if (openSession(a.sid, a.agent ? null : a.step)) return true;
          if (!link) location.href = SRC.sessionHref(a.sid);
          return false;
        }});
    }

    save();
    setInterval(function () { Object.keys(tiles).forEach(function (id) { tickTile(tiles[id]); }); }, 1000);
    load();
    liveLink({
      connEl: $("#conn", root), every: 2000,
      poll: load,
      resync: load,
      // a new step of a followed session: refresh soon; the board event (1 s, when anything changed) keeps
      // totals and status current
      step: function (m) { if (m && list.some(function (id) { return String(m.session).indexOf(id) === 0; })) soon(); },
      board: soon
    });
  }

  // ---------- session ----------
  // The page draws the same intents / recap / NOW rows as the Claude Code pane (mod/tokenscope-live), from the same
  // /api/session/<id>/compact payload (with ?ops=all: every step's calls, shown when an older step is expanded).
  // The full session (/api/session/<id> + SSE steps) still drives the header totals and the per-turn ribbon.
  var RIBBON_MAX = 500;        // above this many turns, merge adjacent same-group ribbon segments
  var MAX_HINTS = 3;           // recap hints shown

  // 0.3s, 4s, 1m12s, 1h3m (wall times, as the pane writes them; tenths under 10 s)
  function fmtWall(s) {
    if (+s < 9.95) return (Math.max(0, +s || 0)).toFixed(1).replace(/\.0$/, "") + "s";
    var v = Math.max(0, Math.floor(+s || 0)), h = Math.floor(v / 3600), m = Math.floor(v % 3600 / 60);
    return h ? h + "h" + m + "m" : m ? m + "m" + (v % 60) + "s" : v + "s";
  }
  function num(v) { return typeof v === "number" && isFinite(v) ? v : null; }
  // a path under a home dir as "~/…" (the server already sends drafts that way; this is a second guard)
  function homePath(p) { return String(p).replace(/^\/(home|Users)\/[^/]+(?=\/|$)/, "~").replace(/^[A-Za-z]:\\Users\\[^\\]+/, "~"); }
  function isDeclare(tool) { return /declare_intent$/.test(String(tool || "")); }
  function catTag(cat) {
    var t = el("span", "cat", "[" + String(cat || "other").toUpperCase() + "]");
    t.style.color = tcol(cat);
    return t;
  }
  // "✦ tdd" ("✦ tdd +1"): the skills that drove a step or group
  function skillMark(skills) {
    if (!skills || !skills.length) return null;
    var m = el("span", "skm", "✦ " + skills[0] + (skills.length > 1 ? " +" + (skills.length - 1) : ""));
    m.title = "skills: " + skills.join(", ");
    return m;
  }
  function costText(cw, usd, dur) {
    return [fmtK(cw) + " CW", num(usd) != null ? fmtUsd(usd) : null, num(dur) != null ? fmtWall(dur) : null].filter(Boolean).join(" · ");
  }
  // a step's summary without the skill parts its head already names
  function stepSummary(s) {
    var sum = String(s.summary || "");
    return s.skills && s.skills.length ? sum.split(" · ").filter(function (p) { return p.indexOf("✦") !== 0; }).join(" · ") : sum;
  }
  // "Read store.py · Edit models.py ×2 · Run tests ✓": a group's steps, equal neighbours folded (the pane's ↳ line)
  function stepsDetail(steps) {
    var parts = [];
    steps.forEach(function (s) {
      var mark = s.tested === "pass" ? " ✓" : s.tested === "fail" ? " ✗" : "", label = String(s.label || "").replace(/…$/, "");
      var last = parts[parts.length - 1];
      if (last && last.label === label && last.mark === mark) last.n++;
      else parts.push({label: label, mark: mark, n: 1});
    });
    return parts.map(function (p) { return p.label + (p.n > 1 ? " ×" + p.n : "") + p.mark; }).join(" · ");
  }
  // an operation's label ("unittest 34 passed", "skill:tdd", "Edit store.py") and numbers ("3k CW · 4s")
  function opParts(o) {
    var tool = /^(?:Bash|run_command) /.test(o.tool) ? o.tool.replace(/^(?:Bash|run_command) /, "") : String(o.tool || "?"), t0 = o.target;
    var target = t0 && t0 !== tool && !(new RegExp("[ :]" + t0.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "$")).test(tool) ? " " + t0 : "";
    var t = o.test && typeof o.test === "object" ? o.test : null;
    var test = !t ? "" : t.failed > 0 ? t.failed + " failed" : t.passed != null ? t.passed + " passed" : "";
    return {label: tool + target, test: test, testBad: !!(t && (!t.ok || t.failed > 0)),
            nums: [+o.cw > 0 ? fmtK(o.cw) + " CW" : null, num(o.dur_s) != null ? fmtWall(o.dur_s) : null].filter(Boolean).join(" · ")};
  }
  // `live`: the op belongs to the live step (a call with no result elsewhere was cut short, not running)
  function opRow(o, live) {
    var p = opParts(o), running = o.err == null && live, failed = o.err || p.testBad, skill = o.skill || (/^skill:./.test(o.tool) ? o.tool.slice(6) : null);
    var r = el("div", "op" + (running ? " run" : "") + (failed ? " bad" : ""));
    r.appendChild(el("span", "oi" + (failed ? " bad" : skill ? " sk" : o.err == null && !live ? "" : " ok"),
      failed ? "✗" : skill ? "✦" : running ? "▶" : o.err == null ? "?" : "✓"));
    if (o.err == null && !live) r.title = "no result in the transcript";
    r.appendChild(el("span", "ol", p.label));
    if (p.test) r.appendChild(el("span", "chip " + (p.testBad ? "bad" : "ok"), p.test));
    r.appendChild(el("span", "on", p.nums));
    return r;
  }
  // "✗4 → ✗2 → ✓ 12 passed"
  function testsNode(t) {
    var n = el("span"), all = Array.isArray(t.trend) ? t.trend : [], passed = Array.isArray(t.passed_trend) ? t.passed_trend : [];
    if (t.scope === "subagents") n.appendChild(el("span", "mut", "subagents: "));
    for (var i = Math.max(0, all.length - 5); i < all.length - 1; i++) {
      var f = all[i];
      n.appendChild(f == null ? el("span", "mut", "?") : f > 0 ? el("span", "bad", "✗" + f) : el("span", "ok", "✓" + (passed[i] != null ? passed[i] : "")));
      n.appendChild(el("span", "mut", " → "));
    }
    if (t.ok && !(t.failed > 0)) n.appendChild(el("b", "ok", "✓ " + (t.passed != null ? t.passed + " passed" : "passed")));
    else if (t.failed > 0) { n.appendChild(el("b", "bad", "✗ " + t.failed + " failed")); if (t.passed != null) n.appendChild(el("span", "mut", " · " + t.passed + " passed")); }
    else n.appendChild(el("span", "mut", "? ran " + (t.runs || 1) + "×"));
    if (t.runs > 1) n.appendChild(el("span", "mut", "  (" + t.runs + " runs)"));
    return n;
  }

  function initSession(root) {
    root = root || document;
    var id = window.TS_SESSION_ID || decodeURIComponent((location.pathname.match(/\/s\/([^/?#]+)/) || [])[1] || "") ||
      new URLSearchParams(location.search).get("id");
    var S = null, C = null, maxSeq = -1, turns = [], clockOff = 0;
    var ribbon = $("#ribbon", root);
    var cw = new Counter($("#h-cw", root), fmtK), usd = new Counter($("#h-usd", root), fmtUsd);
    var open = {};      // "g<first>" / "s<i>" -> expanded, as the person set it (else: the live one is)
    var seenCards = {}; // cards already drawn (a new one fades in)
    var REPLAY = new URLSearchParams(location.search).get("replay"), replaying = false;
    $("#back", root).href = SRC.boardHref;

    function nowS() { return Date.now() / 1000 + clockOff; }
    function working() { return S && S.status === "working"; }

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
      renderRecap();   // gone as soon as the session works again
    }
    // the clock ticks only while the session works; otherwise (waiting, idle, finished, a replay that ended) it is
    // the session's actual duration, first event to last
    function tick() {
      if (!S) return;
      var st = ts(S.started), lt = ts(S.last_event_ts || S.last_ts);
      var e = st != null ? (working() ? nowS() : (lt || nowS())) - st : S.elapsed_s;
      $("#h-el", root).textContent = fmtDur(e);
      $("#h-el", root).title = working() ? "running" : "first to last event";
    }

    // ---- ribbon: one segment per main-thread turn ----
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
    function renderRibbon() {
      ribbon.innerHTML = "";
      var merge = turns.length > RIBBON_MAX;
      turns.forEach(function (t) { ribbonAdd(t, ribbon, merge); });
      Array.prototype.forEach.call(ribbon.children, function (x) { x.style.animation = "none"; });
      renderLegend();
    }
    function renderLegend() {
      var used = {}; turns.forEach(function (t) { used[t.group] = (used[t.group] || 0) + (+t.cw || 0); });
      var lg = $("#legend", root); lg.innerHTML = "";
      GROUPS.filter(function (g) { return used[g]; }).forEach(function (g) {
        var s = el("span"); s.innerHTML = '<span class="dot" style="background:' + gcol(g) + '"></span>' + g + " " + fmtK(used[g]);
        lg.appendChild(s);
      });
    }

    // ---- ASKED / NOW / TESTS / FILES / SKILLS ----
    function hrow(box, label, node) {
      var r = el("div", "hr");
      r.appendChild(el("span", "hl", label));
      var v = el("span", "hv");
      if (typeof node === "string") v.textContent = node; else v.appendChild(node);
      r.appendChild(v);
      box.appendChild(r);
    }
    function nowNode() {
      var n = el("span"), st = S.status;
      if (st === "waiting") { n.appendChild(el("span", "warnc", "◼ done, waiting for you")); return n; }
      if (st === "idle") { n.appendChild(el("span", "mut", "◼ idle")); return n; }
      var running = (C.agents || []).filter(function (a) { return a && (a.status === "running" || a.status === "starting"); });
      if (running.length) {
        var b = el("b", null, "▶ " + running.length + (running.length === 1 ? " subagent working" : " subagents working"));
        b.style.color = gcol("delegate"); n.appendChild(b); return n;
      }
      var w = C.now || {}, label = w.label || String(w.intent || C.current_intent || "working").replace(/_/g, " ");
      var lb = el("b", null, "▶ " + label);
      lb.style.color = gcol(w.group);
      n.appendChild(lb);
      var tgt = w.target || w.tool;
      if (tgt && tgt !== label && !label.endsWith(" " + tgt)) n.appendChild(document.createTextNode(" · " + tgt));
      if (w.since_s != null) n.appendChild(el("span", "mut", " · " + fmtWall(w.since_s)));
      return n;
    }
    function renderRows() {
      var box = $("#h-rows", root);
      if (!box) return;
      box.innerHTML = "";
      if (!C) return;
      var a = C.asked || {}, goal = a.goal || goalName(C.goal);
      var g = el("span", goal ? "strong" : "mut", goal || "(no prompt yet)");
      if (a.source === "haiku") g.appendChild(el("span", "haiku", "✦ haiku"));
      hrow(box, "ASKED", g);
      if ((a.prompt_n || 0) > 1 && a.latest) hrow(box, "", el("span", null, "↳ now: " + a.latest));
      hrow(box, "NOW", nowNode());
      if (C.tests) hrow(box, "TESTS", testsNode(C.tests));
      var files = Array.isArray(C.files_edited) ? C.files_edited : [], fn = Math.max(+C.files_edited_n || 0, files.length);
      if (fn) {
        var f = el("span", null, files.length ? "edited " + fn + ": " + files.slice(-6).join(", ") + (files.length > 6 ? ", …" : "") : "edited " + fn + (fn === 1 ? " file" : " files"));
        f.title = files.join("\n");
        hrow(box, "FILES", f);
      }
      var sk = (C.skills || []).filter(function (x) { return x && x.name; });
      if (sk.length) {
        var s = el("span", "skm", "✦ " + sk.map(function (x) { return x.name + (x.count > 1 ? " ×" + x.count : " ×1"); }).join(" · "));
        hrow(box, "SKILLS", s);
      }
    }

    // ---- idle recap: a banner while the session waits for you (or is idle), gone as soon as it works again ----
    function renderRecap() {
      var box = $("#recap", root);
      if (!box) return;
      var rc = C && C.recap;
      if (!rc || !S || (S.status !== "waiting" && S.status !== "idle") || replaying) { box.style.display = "none"; box.innerHTML = ""; box._k = null; return; }
      var key = JSON.stringify(rc);
      if (box._k === key && box.style.display !== "none") return;
      box._k = key;
      box.innerHTML = "";
      box.style.display = "";
      var tot = rc.cw != null ? " · " + fmtK(rc.cw) + " CW" + (rc.usd != null ? " · " + fmtUsd(rc.usd) : "") : "";
      box.appendChild(el("div", "rt", "DONE · recap" + tot));
      function intentLine(lead, name, x, withCost) {
        var r = el("div", "ri");
        lead.forEach(function (t) { r.appendChild(el("b", "rlead", t)); });
        r.appendChild(el("span", "mut", name));
        r.appendChild(catTag(x.cat));
        r.appendChild(el("b", "rlab", x.label || "intent"));
        if (withCost) r.appendChild(el("span", "mut", " · " + costText(x.cw, x.usd)));
        box.appendChild(r);
      }
      var c = rc.costliest, sl = rc.slowest && rc.slowest.s != null ? rc.slowest : null;
      var share = c && c.pct != null ? [c.pct + "%"] : [];
      if (c && sl && sl.label === c.label && sl.cat === c.cat) intentLine(share.concat([fmtWall(sl.s)]), "costliest & slowest", c, true);
      else {
        if (c) intentLine(share, "costliest", c, true);
        if (sl) intentLine([fmtWall(sl.s)], "slowest", sl, false);
      }
      (Array.isArray(rc.hints) ? rc.hints : []).filter(function (h) { return h && (h.action || h.text); }).slice(0, MAX_HINTS).forEach(function (h) {
        var cross = h.kind && h.kind !== "session", hb = el("div", "hint" + (cross ? " cross" : ""));
        var hd = el("div", "ha");
        hd.appendChild(el("span", "harrow", "➜"));
        hd.appendChild(el("b", null, h.action || h.text));
        if (cross) { var tag = el("span", "xtag", String(h.kind).replace(/_/g, "-")); tag.title = "evidence from your other sessions"; hd.appendChild(tag); }
        hb.appendChild(hd);
        if (h.evidence) { var ev = el("div", "hev", h.evidence); if (h.estimate) ev.title = h.estimate; hb.appendChild(ev); }
        if (Array.isArray(h.matches) && h.matches.length) {
          hb.appendChild(el("div", "hev small", "matched: " + h.matches.slice(0, 4).map(function (m) { return (m.sid || "?") + " · " + fmtK(m.cw) + " CW"; }).join(", ")));
        }
        if (h.skill || h.stub_path) {
          var sx = el("div", "hev");
          if (h.skill) sx.appendChild(el("span", "skm", "✦ " + h.skill));
          if (h.stub_path) { if (h.skill) sx.appendChild(document.createTextNode(" · ")); sx.appendChild(el("span", null, "draft: ")); sx.appendChild(el("code", "path", homePath(h.stub_path))); }
          hb.appendChild(sx);
        }
        box.appendChild(hb);
      });
    }

    // ---- intents: one card per group / ungrouped step, oldest first; the live one expanded ----
    function isOpen(key, dflt) { return Object.prototype.hasOwnProperty.call(open, key) ? open[key] : dflt; }
    function toggle(key, dflt) { open[key] = !isOpen(key, dflt); renderIntents(); }
    function stepHead(s, live, provisional) {
      var h = el("div", "sh");
      h.appendChild(catTag(s.cat));
      var lab = el("span", "sl" + (provisional ? " prov" : ""), (live ? "▶ " : "") + s.label + (provisional ? "…" : ""));
      lab.title = s.label + (s.source ? "  (" + s.source + " label)" : "");
      h.appendChild(lab);
      if (s.source === "declared") h.appendChild(el("span", "decl", "✎"));
      var sm = skillMark(s.skills); if (sm) h.appendChild(sm);
      if (s.tested) h.appendChild(el("span", s.tested === "pass" ? "ok" : "bad", s.tested === "pass" ? "✓" : "✗"));
      return h;
    }
    function opsBox(s, live) {
      var ops = (s.ops || []).filter(function (o) { return o && !isDeclare(o.tool); });
      var b = el("div", "ops");
      ops.forEach(function (o) { b.appendChild(opRow(o, live)); });
      if (!ops.length) b.appendChild(el("div", "op mut", "no tool calls (thinking / text only)"));
      return b;
    }
    function card(key, cat, live, expanded, onToggle) {
      var c = el("div", "icard" + (live ? " live" : "") + (expanded ? " open" : "") + (seenCards[key] ? "" : " row"));
      seenCards[key] = 1;
      c.style.borderLeftColor = gcol(cat);
      if (live && working()) c.style.boxShadow = "0 0 0 1px " + gcol(cat) + "55";
      var h = el("div", "ih");
      h.onclick = onToggle;
      h.appendChild(el("span", "caret", "▸"));
      c.appendChild(h);
      return {node: c, head: h};
    }
    function renderIntents() {
      var box = $("#intents", root), meta = $("#i-meta", root);
      if (!box || !S) return;
      var it = C && C.intents;
      box.innerHTML = "";
      if (!it || !Array.isArray(it.steps) || !it.steps.length) {
        box.appendChild(el("div", "empty small", C ? "No intents yet." : (SRC.compact ? "loading intents…" : "Intents need the tokenscope server.")));
        if (meta) meta.textContent = "";
        return;
      }
      var steps = it.steps, groups = Array.isArray(it.groups) ? it.groups : [];
      var order = groups.map(function (g, i) { return i; }).sort(function (a, b) { return groups[a].first - groups[b].first; });
      var gnum = {}; order.forEach(function (gi, n) { gnum[gi] = n + 1; });
      var last = steps[steps.length - 1], liveStep = last && (last.open || working()) ? last : null;
      var decl = it.declared && it.declared.calls;
      if (meta) meta.textContent = steps.length + " step" + (steps.length === 1 ? "" : "s") + " · " + groups.length + " group" + (groups.length === 1 ? "" : "s") + (decl ? " · ✎ declared ×" + decl : "");
      if (steps[0].i > 0) box.appendChild(el("div", "small mut", "… " + steps[0].i + " earlier step" + (steps[0].i === 1 ? "" : "s") + " not shown"));
      var done = {};
      steps.forEach(function (s) {
        var g = s.group != null ? groups[s.group] : null;
        if (!g) {
          var live = s === liveStep, key = "s" + s.i, ex = isOpen(key, live), provisional = s.open && !s.locked;
          var c = card(key, s.cat, live, ex, function () { toggle(key, live); });
          c.head.appendChild(stepHead(s, live, provisional));
          c.head.appendChild(el("span", "im", costText(s.cw, s.usd, s.dur_s)));
          var sum = stepSummary(s);
          if (sum) c.node.appendChild(el("div", "idet", "↳ " + sum));
          if (ex && s.ops) c.node.appendChild(opsBox(s, live));
          else if (ex) c.node.appendChild(el("div", "ops small mut", "calls not in the payload"));
          box.appendChild(c.node);
          return;
        }
        if (done[s.group]) return;
        done[s.group] = 1;
        var kids = steps.filter(function (x) { return x.group === s.group; });
        var glive = kids.indexOf(liveStep) >= 0, gkey = "g" + g.first, gex = isOpen(gkey, glive);
        var gc = card(gkey, g.cat, glive, gex, function () { toggle(gkey, glive); });
        var gh = el("div", "sh");
        gh.appendChild(el("span", "gn", String(gnum[s.group])));
        gh.appendChild(catTag(g.cat));
        var gl = el("span", "sl", (glive ? "▶ " : "") + g.label); gl.title = g.label + (g.source ? "  (" + g.source + " label)" : "");
        gh.appendChild(gl);
        if (g.source === "declared") gh.appendChild(el("span", "decl", "✎"));
        var gsm = skillMark(g.skills); if (gsm) gh.appendChild(gsm);
        gc.head.appendChild(gh);
        gc.head.appendChild(el("span", "im", g.steps + (g.steps === 1 ? " step" : " steps") + " · " + costText(g.cw, g.usd, g.dur_s)));
        if (!gex) gc.node.appendChild(el("div", "idet", "↳ " + stepsDetail(kids)));
        else {
          var list = el("div", "steps");
          kids.forEach(function (x) {
            var xl = x === liveStep, sk = "s" + x.i, sex = isOpen(sk, xl), prov = x.open && !x.locked;
            var r = el("div", "srow" + (xl ? " live" : "") + (sex ? " open" : ""));
            var rh = el("div", "srh");
            rh.appendChild(el("span", "caret", x.ops ? "▸" : "·"));
            rh.appendChild(stepHead(x, xl, prov));
            var sum2 = stepSummary(x);
            if (sum2) rh.appendChild(el("span", "ssum", sum2));
            rh.appendChild(el("span", "im", costText(x.cw, x.usd, x.dur_s)));
            if (x.ops) rh.onclick = function (ev) { ev.stopPropagation(); toggle(sk, xl); };
            r.appendChild(rh);
            if (sex && x.ops) r.appendChild(opsBox(x, xl));
            list.appendChild(r);
          });
          gc.node.appendChild(list);
        }
        box.appendChild(gc.node);
      });
    }
    function setAll(v) {
      var it = C && C.intents;
      if (!it) return;
      (it.groups || []).forEach(function (g) { open["g" + g.first] = v; });
      (it.steps || []).forEach(function (s) { if (s.group == null) open["s" + s.i] = v; });
      renderIntents();
    }
    var ea = $("#i-expand", root), ca = $("#i-collapse", root);
    if (ea) ea.onclick = function () { setAll(true); };
    if (ca) ca.onclick = function () { setAll(false); };

    // ---- subagents ----
    function renderAgents() {
      var box = $("#agents", root);
      if (!box) return;
      var ags = (C && C.agents) || [];
      box.style.display = ags.length ? "" : "none";
      var list = $("#agents-list", root) || box;
      list.innerHTML = "";
      ags.forEach(function (a) {
        var r = el("div", "arow");
        r.appendChild(el("b", null, "↳ " + (a.description || a.type || "agent")));
        if (a.description && a.type && a.type !== "general-purpose") r.appendChild(el("span", "mut", " (" + a.type + ")"));
        r.appendChild(el("span", "mut", " · " + (a.turns || 0) + (a.turns === 1 ? " turn" : " turns") + " · " + fmtK(a.cw || 0) + " CW"));
        var ap = pill(a.status === "running" || a.status === "starting" ? "working" : "idle");
        ap.lastChild.textContent = a.status || "?";
        r.appendChild(ap);
        if (a.parent) r.style.paddingLeft = "22px";
        list.appendChild(r);
      });
    }

    function renderCompact() {
      if (!S || replaying) return;
      renderRows(); renderRecap(); renderIntents(); renderAgents();
    }
    // the compact payload, refetched (coalesced) on every change and every 2 s while the session works
    var cBusy = false, cAgain = false, cTimer = 0, lastC = 0;
    function fetchCompact() {
      if (!SRC.compact || !id) return;
      if (cBusy) { cAgain = true; return; }
      cBusy = true;
      SRC.compact(id).then(function (d) {
        C = d; lastC = Date.now();
        if (S && d.status && !replaying) { S.status = d.status; renderHead(); }
        renderCompact();
      }, function () {}).then(function () { cBusy = false; if (cAgain) { cAgain = false; scheduleCompact(); } });
    }
    function scheduleCompact() { if (!cTimer) cTimer = setTimeout(function () { cTimer = 0; fetchCompact(); }, 250); }
    setInterval(function () { if (S && !replaying && (working() || Date.now() - lastC > 15000)) scheduleCompact(); }, 2000);

    function rebuild() { turns = S.steps.filter(function (x) { return x.k === "turn"; }); }
    // a main-thread step (new or updated): ribbon + a compact refresh; subagent steps only refresh the compact
    function ingest(s, agent) {
      if (s.useq != null && s.useq > maxSeq) maxSeq = s.useq;
      if (s.seq != null && s.seq > maxSeq) maxSeq = s.seq;
      if (agent) { scheduleCompact(); return; }
      var how = upsert(S.steps, s);
      if (!how) return;
      if (s.k === "turn") {
        if (how === "new") { turns.push(s); ribbonAdd(s, ribbon, turns.length > RIBBON_MAX); renderLegend(); }
        else { rebuild(); renderRibbon(); }
      }
      scheduleCompact();
    }
    function agentOf(s, msg) { return s.agent || (msg && msg.agent) || null; }

    function applySummary(d) { // totals/status/flags from ?since or board events
      var was = S.status;
      ["status", "flags", "context_tokens", "totals", "goal", "project", "last_event_ts", "last_ts", "started"].forEach(function (k) { if (d[k] != null) S[k] = d[k]; });
      if (d.cw != null && !d.totals) { S.totals = S.totals || {}; S.totals.cw = d.cw; S.totals.usd = d.usd; S.totals.turns = d.turns; S.totals.tool_calls = d.tool_calls; S.totals.errors = d.errors; }
      renderHead();
      if (was !== S.status) { renderRows(); scheduleCompact(); }
    }

    // ?replay=1: fetch the session once, then feed its main-thread turns in order (ribbon, totals, clock); the
    // intents, rows and recap appear when the feed ends — for demos / recordings
    function replayOf(d) {
      var full = JSON.parse(JSON.stringify(d)), feed = (full.steps || []).slice();
      feed.sort(function (p, q) { return ((p.seq || 0) - (q.seq || 0)) || ((ts(p.ts) || 0) - (ts(q.ts) || 0)); });
      d.steps = []; d.flags = []; d.status = "working"; d.totals = {cw: 0, usd: 0, turns: 0, tool_calls: 0, errors: 0}; d.context_tokens = 0;
      var step0 = feed.length && ts(feed[0].ts);
      if (step0) { d.now = step0; if (d.started == null) d.started = step0 - 1; }
      var k = 0, T = d.totals, ctxMax = +full.context_tokens || 0, nturns = feed.filter(function (x) { return x.k === "turn"; }).length || 1;
      replaying = true;
      setTimeout(function next() {
        if (!S) return;
        if (k >= feed.length) {
          ["status", "flags", "totals", "context_tokens", "last_event_ts"].forEach(function (x) { S[x] = full[x]; });
          replaying = false;
          renderHead(); fetchCompact(); return;
        }
        var x = feed[k++];
        if (x.ts != null) { clockOff = ts(x.ts) - Date.now() / 1000; S.last_event_ts = x.ts; }
        if (x.k === "turn") {
          T.cw += +x.cw || 0; T.usd += +x.usd || 0; T.turns++; T.tool_calls += (x.tools || []).length;
          T.errors += (x.tools || []).filter(function (t) { return t.err; }).length;
          S.context_tokens = Math.round(ctxMax * Math.min(1, T.turns / nturns));
        }
        ingest(x, null);
        renderHead();
        setTimeout(next, 300);
      }, 900);
      return d;
    }

    function load() {
      return SRC.session(id).then(function (d) {
        if (REPLAY) d = replayOf(d);
        S = d; S.steps = (S.steps || []).filter(function (x) { return !x.agent; });
        if (d.id) id = d.id;   // the URL may hold a prefix; SSE messages carry the full id
        if (d.now != null) clockOff = ts(d.now) - Date.now() / 1000;
        maxSeq = d.seq != null ? d.seq : -1;
        S.steps.sort(function (a, b) { return (a.seq || 0) - (b.seq || 0); });
        rebuild();
        $("#notfound", root).style.display = "none";
        $("#body", root).style.display = "";
        renderHead();
        renderRibbon();
        renderIntents();
        if (!REPLAY) fetchCompact();
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
        scheduleCompact();
      });
    }

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
  window.TSLive = {initBoard: initBoard, initSession: initSession, initFollow: initFollow, GCOL: GCOL, fmtK: fmtK, fmtDur: fmtDur,
    pinScrollTop: pinScrollTop,
    pins: {parse: parsePins, resolve: resolvePins, toggle: togglePin, href: followHref, MAX: PIN_MAX}};
  document.addEventListener("DOMContentLoaded", function () {
    var page = document.body.getAttribute("data-page");
    if (page === "board") initBoard();
    else if (page === "session") initSession();
    else if (page === "follow") initFollow();
  });
})();
