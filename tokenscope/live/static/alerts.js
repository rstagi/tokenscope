// tokenscope live — the alerts section shared by the board, the follow view and the session canvas: the alert
// history from GET /api/alerts (at most one alert per kind per session: a new trigger updates it in place, with a
// count, its latest message and step, a severity that only rises; with its state), "needs attention"
// (the sessions with alerts not acknowledged, worst first), ack / dismiss per alert and "ack all" (POST
// /api/alerts/ack|dismiss), and a collapsed state kept in localStorage. Also the per-session badge (unacknowledged
// counts by kind in the worst severity's colour) the board cards and follow tiles draw, and the block of a follow
// tile's own alerts at its top (tileBlock).
(function () {
  "use strict";
  var POLL_MS = 2000;
  var KEY = "tokenscope.alerts";   // localStorage: KEY + ".collapsed." + page, KEY + ".filter." + page
  var KIND = {cost: "cost", off_task: "off-task", looping: "loop"};
  var KIND_LONG = {cost: "cost", off_task: "off-task", looping: "looping"};
  var RULE = {session_usd: "session $", intent_usd: "intent $", jump: "cost jump", unrelated: "off-task",
    repeat_read: "read loop", repeat_search: "search loop", repeat_command: "command loop", repeat_call: "call loop",
    retry: "retry loop", edit_revert: "edit revert"};
  var SEV = ["info", "warning", "critical"];

  function el(tag, cls, text) {
    var e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function ruleName(x) { return RULE[x.rule] || String(x.rule || x.kind || "alert").replace(/_/g, " "); }
  function store(k, v) {
    try {
      if (v === undefined) return window.localStorage.getItem(KEY + "." + k);
      window.localStorage.setItem(KEY + "." + k, v);
    } catch (e) { /* private mode */ }
    return null;
  }
  // 14:02:07 today, else "Oct 14 14:02"
  function clock(t) {
    if (t == null) return "";
    var d = new Date(+t * 1000), now = new Date();
    var hm = ("0" + d.getHours()).slice(-2) + ":" + ("0" + d.getMinutes()).slice(-2);
    if (d.toDateString() === now.toDateString()) return hm + ":" + ("0" + d.getSeconds()).slice(-2);
    return d.toLocaleDateString(undefined, {month: "short", day: "numeric"}) + " " + hm;
  }
  function where(a) {
    if (a.step == null) return "";
    var r = a.first != null && a.first !== a.step ? a.first + "–" + a.step : String(a.step);
    return (a.agent ? "subagent step " : "step ") + r;
  }
  function sessionName(x) { return (x.project || "—") + " · " + (x.short || String(x.sid || x.id || "").slice(0, 8)); }

  var api = {
    list: function (sids) {
      return fetch("/api/alerts" + (sids && sids.length ? "?s=" + sids.map(encodeURIComponent).join(",") : ""),
        {cache: "no-store"}).then(function (r) { if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); });
    },
    act: function (op, body) {
      return fetch("/api/alerts/" + op, {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(body)}).then(function (r) { return r.json(); });
    }
  };

  // "▲ cost · loop" (the kinds with an alert not acknowledged: one alert per kind) in the worst unacknowledged
  // severity's colour; nothing when all is acknowledged.
  // c: a board / follow "alerts" counts object ({new, new_by_kind, new_worst, n})
  function badge(node, c, before) {
    var b = node.querySelector(".abadge");
    var n = c && c.new ? c.new : 0;
    if (!n) { if (b) b.parentNode.removeChild(b); return null; }
    if (!b) { b = el("span", "abadge"); node.insertBefore(b, before || null); }
    var by = c.new_by_kind || {}, parts = [];
    function part(k) { return (KIND[k] || k) + (by[k] > 1 ? " " + by[k] : ""); }   // > 1: an older history
    ["cost", "off_task", "looping"].forEach(function (k) { if (by[k]) parts.push(part(k)); });
    Object.keys(by).forEach(function (k) { if (!KIND[k]) parts.push(part(k)); });
    b.className = "abadge " + (c.new_worst || "warning");
    b.textContent = "▲ " + parts.join(" · ");
    b.title = n + " alert" + (n > 1 ? "s" : "") + " not acknowledged (" + (c.new_worst || "warning") + ")" +
      (c.n > n ? " · " + (c.n - n) + " acknowledged" : "");
    return b;
  }
  // the class a session card / tile takes: "al-critical" / "al-warning" while it has unacknowledged alerts
  function mark(node, c) {
    SEV.forEach(function (s) { node.classList.remove("al-" + s); });
    if (c && c.new) node.classList.add("al-" + (c.new_worst || "warning"));
  }

  // The section. opts: {page: "board" | "follow" | "canvas", sessions: () => [ids] | null (null: every session),
  // onSession(sid, row): a "needs attention" entry clicked, onJump(alert): an alert row clicked (scroll / focus its
  // step or tile), stepHref(alert): where its step link goes, attention: show "needs attention" (default true)}
  function panel(host, opts) {
    opts = opts || {};
    var page = opts.page || "board";
    var root = el("section", "apanel");
    root.innerHTML = '<div class="aph"><button class="atog" title="collapse / expand the alerts"></button>' +
      '<b class="att">Alerts</b><span class="asum"></span><span class="aseg"><button data-f="new" title="only the alerts not acknowledged">new</button>' +
      '<button data-f="all" title="every alert, acknowledged ones dimmed">history</button></span>' +
      '<button class="abtn aackall" title="acknowledge every alert shown">✓ ack all</button>' +
      '<button class="abtn aclear" title="dismiss the acknowledged alerts shown (they leave the history)">clear acked</button></div>' +
      '<div class="abody"><div class="aattn"></div><div class="alist"></div></div>';
    host.appendChild(root);
    var collapsed = store("collapsed." + page) === "1", filter = store("filter." + page) || "all";
    var seen = null, data = null, timer = 0, busy = false, lastKey = "";
    function setCollapsed(v) {
      collapsed = v;
      store("collapsed." + page, v ? "1" : "0");
      root.classList.toggle("collapsed", v);
      root.querySelector(".atog").textContent = v ? "▸" : "▾";
      root.querySelector(".atog").setAttribute("aria-expanded", v ? "false" : "true");
      if (opts.onCollapse) opts.onCollapse(v);
    }
    function setFilter(f) {
      filter = f;
      store("filter." + page, f);
      Array.prototype.forEach.call(root.querySelectorAll(".aseg button"), function (b) { b.classList.toggle("on", b.getAttribute("data-f") === f); });
      if (data) render(data, true);
    }
    root.querySelector(".atog").onclick = function (e) { e.stopPropagation(); setCollapsed(!collapsed); };
    root.querySelector(".aph").addEventListener("click", function (e) { if (collapsed && !e.target.closest("button")) setCollapsed(false); });
    Array.prototype.forEach.call(root.querySelectorAll(".aseg button"), function (b) {
      b.onclick = function (e) { e.stopPropagation(); setFilter(b.getAttribute("data-f")); };
    });
    function scope() { var s = opts.sessions ? opts.sessions() : null; return s && s.length ? s : null; }
    function act(op, body) { return api.act(op, body).then(refresh, refresh); }
    root.querySelector(".aackall").onclick = function (e) {
      e.stopPropagation();
      var s = scope();
      act("ack", s ? {all: true, sessions: s} : {all: true});
    };
    root.querySelector(".aclear").onclick = function (e) {
      e.stopPropagation();
      var ids = (data && data.alerts || []).filter(function (a) { return a.state === "acked"; }).map(function (a) { return a.id; });
      if (ids.length) act("dismiss", {ids: ids});
    };

    function row(a, fresh) {
      var r = el("div", "alrow " + (a.severity || "warning") + " " + (a.state || "new") + (fresh ? " fresh" + (fresh === "upd" ? " upd" : "") : ""));
      r.setAttribute("data-id", a.id);
      var top = el("div", "ar1");
      top.appendChild(el("span", "asev", a.severity === "critical" ? "▲▲" : "▲"));
      top.appendChild(el("span", "atime", clock(a.at != null ? a.at : a.ts)));
      top.lastChild.title = "latest trigger's step time " + clock(a.at) + " · first seen " + clock(a.ts);
      top.appendChild(el("span", "akind k-" + a.kind, KIND_LONG[a.kind] || a.kind));
      var name = el("b", "arule", ruleName(a));
      top.appendChild(name);
      if (a.count > 1) {
        var cn = el("span", "acount", "×" + a.count);
        cn.title = "fired " + a.count + " times in this session: this row is updated in place (latest trigger shown)";
        top.appendChild(cn);
      }
      var rs = Array.isArray(a.reasons) ? a.reasons : [];
      if (rs.length > 1) top.appendChild(el("span", "amore", "+" + (rs.length - 1) + " reason" + (rs.length > 2 ? "s" : "")));
      var sub = el("div", "ar2");
      if (!opts.single) sub.appendChild(el("span", "asess", sessionName(a)));
      var st = where(a);
      if (st) {
        var link = el("a", "astep", st + " ↗");
        link.href = opts.stepHref ? opts.stepHref(a) : "/s/" + encodeURIComponent(a.sid) + "#step=" + a.step + (a.agent ? "&agent=" + encodeURIComponent(a.agent) : "");
        link.title = "jump to this step";
        link.onclick = function (e) {
          e.stopPropagation();
          if (opts.onJump && opts.onJump(a, true) !== false) e.preventDefault();
        };
        sub.appendChild(link);
      }
      if (a.intent && a.intent.label) {
        var it = el("span", "aint", "[" + String(a.intent.cat || "other").toUpperCase() + "] " + a.intent.label);
        it.title = "the intent at this step: " + a.intent.label;
        sub.appendChild(it);
      }
      if (a.raised) top.appendChild(el("span", "araised", "↻ back"));
      var actions = el("span", "aact");
      if (a.state === "new") {
        var ok = el("button", "aok", "✓");
        ok.title = "acknowledge: keep it in the history, no longer needs attention";
        ok.onclick = function (e) { e.stopPropagation(); r.classList.add("leaving"); act("ack", {ids: [a.id]}); };
        actions.appendChild(ok);
      } else actions.appendChild(el("span", "aacked", "✓ acked"));
      var x = el("button", "adis", "×");
      x.title = "dismiss: remove it from the history";
      x.onclick = function (e) { e.stopPropagation(); r.classList.add("leaving"); act("dismiss", {ids: [a.id]}); };
      actions.appendChild(x);
      top.appendChild(actions);
      r.appendChild(top);
      r.appendChild(sub);
      r.appendChild(el("div", "amsg", a.message || ""));
      if (rs.length > 1) {
        var ul = el("div", "areasons");
        rs.slice(1).forEach(function (m) {
          var li = el("div", "areason " + (m.severity || "warning"), "· " + ruleName(m) + ": " + (m.message || ""));
          ul.appendChild(li);
        });
        r.appendChild(ul);
      }
      var ts = Array.isArray(a.triggers) ? a.triggers.slice(0, -1).reverse() : [];
      if (ts.length) {
        var er = el("div", "aearlier", "earlier: " + ts.slice(0, 3).map(function (t) { return ruleName(t) + (t.step != null ? " " + where(t) : ""); }).join(" · ") +
          (ts.length > 3 ? " · +" + (ts.length - 3) + " more" : ""));
        er.title = ts.map(function (t) { return "[" + t.severity + "] " + ruleName(t) + " " + where(t) + ": " + t.message; }).join("\n");
        r.appendChild(er);
      }
      r.onclick = function () { if (opts.onJump) opts.onJump(a, false); };
      r.title = "click to show where it fired";
      return r;
    }
    function attention(d) {
      var box = root.querySelector(".aattn");
      box.innerHTML = "";
      if (opts.attention === false) { box.style.display = "none"; return; }
      var need = (d.sessions || []).filter(function (x) { return x.new > 0; });
      if (!need.length) {
        if ((d.alerts || []).length) box.appendChild(el("span", "aok-all", "✓ nothing needs attention"));
        box.style.display = (d.alerts || []).length ? "" : "none";
        return;
      }
      box.style.display = "";
      box.appendChild(el("span", "alab", "needs attention"));
      need.forEach(function (x) {
        var b = el("button", "asess-chip " + (x.worst || "warning"));
        b.appendChild(el("span", "adot"));
        b.appendChild(el("span", "an", sessionName(x)));
        badge(b, {new: x.new, new_by_kind: x.by_kind, new_worst: x.worst, n: x.total});
        b.title = (x.goal ? x.goal + "\n" : "") + x.new + " alert" + (x.new > 1 ? "s" : "") + " not acknowledged" +
          (x.loaded ? "" : " · not on the board (older session)") + "\nclick to jump to it";
        b.onclick = function (e) { e.stopPropagation(); if (opts.onSession) opts.onSession(x.id, x); };
        box.appendChild(b);
      });
    }
    function render(d, force) {
      data = d;
      var list = (d.alerts || []).filter(function (a) { return filter === "all" || a.state === "new"; });
      var key = JSON.stringify([filter, list.map(function (a) { return [a.id, a.state, a.severity, a.message, a.step, a.count, (a.reasons || []).length]; }),
        (d.sessions || []).map(function (x) { return [x.id, x.new, x.worst, x.project]; })]);
      // fresh: an alert not seen yet, or one updated in place (fired again, escalated, back after an ack)
      var fresh = {};
      function sig(a) { return [a.count, a.severity, a.raised, a.step].join("|"); }
      if (seen) (d.alerts || []).forEach(function (a) { if (a.state === "new" && seen[a.id] !== sig(a)) fresh[a.id] = seen[a.id] ? "upd" : "new"; });
      seen = seen || {};
      (d.alerts || []).forEach(function (a) { seen[a.id] = sig(a); });
      var newN = (d.alerts || []).filter(function (a) { return a.state === "new"; }).length, total = (d.alerts || []).length;
      var nsess = (d.sessions || []).filter(function (x) { return x.new > 0; }).length;
      var sum = root.querySelector(".asum");
      sum.textContent = newN ? newN + " new" + (nsess > 1 ? " in " + nsess + " sessions" : "") + (total > newN ? " · " + total + " total" : "") :
        total ? "all acknowledged · " + total + " total" : "none";
      var worst = (d.alerts || []).filter(function (a) { return a.state === "new"; }).reduce(function (w, a) {
        return SEV.indexOf(a.severity) > SEV.indexOf(w) ? a.severity : w; }, newN ? "info" : null);
      root.className = "apanel" + (collapsed ? " collapsed" : "") + (worst ? " has-" + worst : "");
      root.querySelector(".aackall").disabled = !newN;
      root.querySelector(".aclear").disabled = !(d.alerts || []).some(function (a) { return a.state === "acked"; });
      if (opts.onCounts) opts.onCounts({new: newN, total: total, worst: worst, sessions: d.sessions || []});
      if (!force && key === lastKey && !Object.keys(fresh).length) return;
      lastKey = key;
      attention(d);
      var box = root.querySelector(".alist");
      box.innerHTML = "";
      if (!list.length) box.appendChild(el("div", "aempty", filter === "new" && total ? "every alert is acknowledged (see history)" : "no alerts yet"));
      list.forEach(function (a) { box.appendChild(row(a, fresh[a.id])); });   // one row per alert, updated in place
      if (Object.keys(fresh).length) {
        root.classList.remove("ping"); void root.offsetWidth; root.classList.add("ping");
        if (opts.onFresh) opts.onFresh((d.alerts || []).filter(function (a) { return fresh[a.id]; }));
      }
    }
    function refresh() {
      if (busy) return Promise.resolve();
      busy = true;
      return api.list(scope()).then(function (d) { busy = false; render(d); }, function () { busy = false; });
    }
    function loop() { clearTimeout(timer); timer = setTimeout(function () { refresh().then(loop, loop); }, POLL_MS); }
    setCollapsed(collapsed);
    setFilter(filter);
    refresh().then(loop, loop);
    return {el: root, refresh: refresh, setCollapsed: setCollapsed, isCollapsed: function () { return collapsed; }};
  }

  // A follow tile's own alerts, as a block at its top: one row per alert (at most one per kind: cost, off-task,
  // looping), coloured by severity, with the kind, rule, ×N, "↻ back", the latest step's link, the whole message
  // (wrapped) and the earlier triggers (clamped). Acknowledged ones stay, dimmed and one line of message.
  // items: the tile's alerts.items; opts: {onJump(alert): show its step in the tile, stepHref(alert)}
  function tileBlock(host, items, opts) {
    opts = opts || {};
    items = Array.isArray(items) ? items : [];
    var key = JSON.stringify(items.map(function (a) { return [a.id, a.state, a.severity, a.rule, a.message, a.step, a.first, a.count, a.raised,
      (a.triggers || []).length]; }));
    if (host._key === key) return;
    var was = host._sig || null, sig = {};
    host._key = key;
    host.innerHTML = "";
    host.style.display = items.length ? "" : "none";
    items.forEach(function (a) {
      sig[a.id] = [a.count, a.severity, a.step, a.raised].join("|");
      var upd = was && a.state === "new" && was[a.id] !== sig[a.id];
      var r = el("div", "talrow " + (a.severity || "warning") + " " + (a.state || "new") + (upd ? " fresh" : ""));
      r.setAttribute("data-id", a.id);
      r.setAttribute("data-kind", a.kind);
      var top = el("div", "tar1");
      top.appendChild(el("span", "tsev", (a.severity === "critical" ? "▲▲ " : "▲ ") + (a.severity || "warning")));
      top.appendChild(el("span", "akind k-" + a.kind, KIND_LONG[a.kind] || a.kind));
      top.appendChild(el("b", "arule", ruleName(a)));
      if (a.count > 1) {
        var cn = top.appendChild(el("span", "acount", "×" + a.count));
        cn.title = "fired " + a.count + " times in this session (latest trigger shown)";
      }
      if (a.raised && a.state === "new") top.appendChild(el("span", "araised", "↻ back"));
      if (a.state === "acked") top.appendChild(el("span", "aacked", "✓ acked"));
      var st = where(a);
      if (st) {
        var link = el("a", "astep", st + " ↗");
        link.href = opts.stepHref ? opts.stepHref(a) : "/s/" + encodeURIComponent(a.sid || "") + "#step=" + a.step;
        link.title = "jump to the latest trigger's step";
        link.onclick = function (e) {
          e.stopPropagation();
          if (opts.onJump && opts.onJump(a) !== false) e.preventDefault();
        };
        top.appendChild(link);
      }
      r.appendChild(top);
      r.appendChild(el("div", "tmsg", a.message || ""));
      var ts = Array.isArray(a.triggers) ? a.triggers.slice(0, -1).reverse() : [];
      if (ts.length && a.state === "new") {
        var more = (a.count || ts.length + 1) - 1 - ts.length;
        var er = el("div", "tearlier", "earlier: " + ts.map(function (t) { return ruleName(t) + (t.step != null ? " " + where(t) : ""); }).join(" · ") +
          (more > 0 ? " · +" + more + " more" : ""));
        er.title = ts.map(function (t) { return "[" + t.severity + "] " + ruleName(t) + " " + where(t) + ": " + t.message; }).join("\n");
        r.appendChild(er);
      }
      r.title = "click to show the latest trigger's step in this tile";
      r.onclick = function (e) { e.stopPropagation(); if (opts.onJump) opts.onJump(a); };
      host.appendChild(r);
    });
    host._sig = sig;
  }

  window.TSAlerts = {panel: panel, badge: badge, mark: mark, tileBlock: tileBlock, api: api, ruleName: ruleName, clock: clock};

  // the session canvas (/s/<id>): this session's alert history as a drawer; a row centres the canvas on its step
  function initCanvas() {
    var host = document.getElementById("cside"), m = location.pathname.match(/^\/s\/([^/]+)/);
    if (!host || !m) return;
    var sid = decodeURIComponent(m[1]);
    panel(host, {page: "canvas", single: true, attention: false, sessions: function () { return [sid]; },
      onJump: function (a) { return window.tsCanvasFocus ? !!window.tsCanvasFocus(a) || true : false; }});
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", function () {
    if (document.body.getAttribute("data-page") === "canvas") initCanvas();
  });
  else if (document.body.getAttribute("data-page") === "canvas") initCanvas();
})();
