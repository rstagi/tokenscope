/* tokenscope live: the session page as an infinite canvas (React Flow). Built into tokenscope/live/static/canvas.js
   by web/canvas/build.mjs; the page reads only /api/session/<id>/flow (docs: tokenscope/live/server.py).

   Layout: a top-down flow in time order. The main thread is the left column; a subagent branches off the node that
   launched it into a column on the right (parallel fan-outs side by side, nested subagents further right) and the
   main flow continues below its last node once it has returned. Placement is incremental: a node is placed once,
   when it first appears, and never moves (only the newest node of a lane may grow as its group gains steps). */
import React, {useCallback, useEffect, useMemo, useRef, useState} from "react";
import {createRoot} from "react-dom/client";
import {
  ReactFlow, ReactFlowProvider, Background, Controls, MiniMap, Handle, Position, useReactFlow
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import "./canvas.css";

// ---------- formatting (as static/live.js) ----------
const GCOL = {investigate: "#4c6ef5", change: "#f0645a", verify: "#f08c00", ship: "#7950f2", delegate: "#d4a017",
  research: "#12a594", wait: "#8d6e63", plan: "#e64980", report: "#adb5bd", overhead: "#ced4da", other: "#b8bec6",
  respond: "#c5cad1"};
const TCOL = {overhead: "#868e96", other: "#868e96", respond: "#868e96", report: "#868e96"};
const gcol = (g) => GCOL[g] || "#b8bec6";
const tcol = (g) => TCOL[g] || gcol(g);
const num = (v) => (typeof v === "number" && isFinite(v) ? v : null);
function fmtK(n) {
  n = +n || 0;
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 1 : 2) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + "k";
  return String(Math.round(n));
}
const fmtUsd = (n) => { n = +n || 0; return "$" + (n >= 100 ? n.toFixed(0) : n >= 10 ? n.toFixed(1) : n.toFixed(2)); };
function fmtWall(s) {
  if (+s < 9.95) return Math.max(0, +s || 0).toFixed(1).replace(/\.0$/, "") + "s";
  const v = Math.max(0, Math.floor(+s || 0)), h = Math.floor(v / 3600), m = Math.floor((v % 3600) / 60);
  return h ? h + "h" + m + "m" : m ? m + "m" + (v % 60) + "s" : v + "s";
}
const costText = (cw, usd, dur) =>
  [fmtK(cw) + " CW", num(usd) != null ? fmtUsd(usd) : null, num(dur) != null ? fmtWall(dur) : null].filter(Boolean).join(" · ");
const isDeclare = (tool) => /declare_intent$/.test(String(tool || ""));
const goalName = (g) => (g && typeof g === "object" ? g.name : g);
const homePath = (p) => String(p).replace(/^\/(home|Users)\/[^/]+(?=\/|$)/, "~");
function stepSummary(x) {
  const sum = String(x.summary || "");
  return x.skills && x.skills.length ? sum.split(" · ").filter((p) => p.indexOf("✦") !== 0).join(" · ") : sum;
}
// ---------- alerts (tokenscope/alerts.py; payload key "alerts") ----------
// An alert points at a step of a lane (agent null: the main thread) and the range it covers (first..step). Its marker
// sits on the node holding that step; the node's tooltip lists every alert whose step it holds.
const ALERT_NAMES = {session_usd: "session $", intent_usd: "intent $", jump: "cost jump", unrelated: "off-task",
  repeat_read: "read loop", repeat_search: "search loop", repeat_command: "command loop", repeat_call: "call loop",
  retry: "retry loop", edit_revert: "edit revert"};
const SEV = {info: 0, warning: 1, critical: 2};
const alertName = (a) => ALERT_NAMES[a.rule] || String(a.rule || a.kind || "alert").replace(/_/g, " ");
const worst = (as) => as.reduce((w, a) => (SEV[a.severity] || 0) > (SEV[w] || 0) ? a.severity : w, "info");
const stepRange = (a) => a.step != null ? "step " + (a.first != null && a.first !== a.step ? a.first + "–" : "") + a.step : "";
const times = (a) => (a.count > 1 ? " ×" + a.count : "");
// one alert per kind (alerts.collapse): the latest trigger leads, the earlier ones follow
const alertTip = (as) => as.map((a) => "▲ " + alertName(a) + times(a) + (a.step != null ? " · " + stepRange(a) : "") + "\n" + a.message +
  (a.reasons || []).slice(1).map((m) => "\n  · " + alertName(m) + ": " + m.message).join("") +
  (Array.isArray(a.triggers) && a.triggers.length > 1 ? "\n  earlier: " + a.triggers.slice(0, -1).reverse().slice(0, 4)
    .map((t) => alertName(t) + (t.step != null ? " " + stepRange(t) : "")).join(", ") + (a.triggers.length > 5 ? ", …" : "") : "")).join("\n\n");
// the alerts still needing attention: an acknowledged one stays in the alerts drawer (static/alerts.js), not on nodes
const openAlerts = (C) => (Array.isArray(C.alerts) ? C.alerts : []).filter((a) => a && a.state !== "acked");
function alertsByLane(C) {
  const out = {};
  openAlerts(C).forEach((a) => {
    if (a.step == null) return;
    const lane = a.agent ? "a:" + a.agent : "m";
    (out[lane] = out[lane] || []).push(a);
  });
  return out;
}
// hovering a marker shows its alerts in a tooltip drawn over the canvas (a node clips its own content); the canvas
// sets the setter
let showTip = () => {};
function AlertMark({as}) {
  if (!as || !as.length) return null;
  const tip = alertTip(as);
  return <span className={"amark " + worst(as)} aria-label={tip} data-tip={tip}
    onMouseEnter={(e) => showTip({text: tip, sev: worst(as), x: e.clientX, y: e.clientY})} onMouseLeave={() => showTip(null)}>
    ▲{as.length > 1 ? " " + as.length : ""}</span>;
}
function Tip({tip}) {
  if (!tip) return null;
  const left = Math.min(tip.x + 14, window.innerWidth - 380), top = Math.min(tip.y + 14, window.innerHeight - 160);
  return <div className={"atip " + tip.sev} style={{left, top}}>{tip.text}</div>;
}

// ---------- graph model: lanes of slots ----------
// A slot is one node of a lane: an intent (as steps.intent_blocks): an intent group; a run of steps outside any group
// whose label is not settled yet (open, or waiting for the model): "pending intent"; else a settled step outside any
// group, its own intent. Keyed by its first step index (a step that later joins a group keeps the slot: the node stays
// where it is and grows). A node shows the intent, "+N other steps" and its latest step; the side panel all of them.
const NODE_W = 340, HEAD_H = 70, ROW_H = 19, AGENT_H = 46, GAP_Y = 26, GAP_X = 60;
const COL_W = NODE_W + GAP_X;
const PENDING = "pending intent";

function slotsOf(it, lane) {
  const out = [], stepSlot = {};
  if (!it || !Array.isArray(it.steps)) return {slots: out, stepSlot};
  const groups = Array.isArray(it.groups) ? it.groups : [];
  let cur = null;
  it.steps.forEach((x) => {
    const g = x.group != null ? groups[x.group] : null;
    const kind = g ? "group" : x.locked ? "step" : "pending", ref = g ? x.group : kind === "step" ? x.i : null;
    if (!(cur && cur.kind === kind && cur.ref === ref && cur.steps[cur.steps.length - 1].i === x.i - 1)) {
      const first = g ? g.first : x.i;
      cur = {key: lane + ":" + first, lane, first, kind, ref, group: g, groupIndex: g ? x.group : null, steps: [],
        ts0: x.ts0 != null ? x.ts0 : null};
      out.push(cur);
    }
    cur.steps.push(x);
    stepSlot[x.i] = cur.first;
    if (cur.ts0 == null && x.ts0 != null) cur.ts0 = x.ts0;
  });
  out.forEach((sl) => {
    const last = sl.steps[sl.steps.length - 1];
    sl.cat = sl.group ? sl.group.cat : last.cat;
    sl.label = sl.group ? sl.group.label : sl.kind === "pending" ? PENDING : last.label;
  });
  return {slots: out, stepSlot};
}
const slotSteps = (sl) => (sl.group ? sl.group.steps : sl.steps.length);
function slotHeight(sl) {
  return slotSteps(sl) > 1 ? HEAD_H + 2 * ROW_H + 4 : HEAD_H;
}
const running = (a) => a && (a.status === "running" || a.status === "starting");

// Everything the canvas draws, derived from one /flow payload
function model(C) {
  const lanes = {};
  const main = slotsOf(C.intents, "m");
  lanes.m = {id: "m", agent: null, slots: main.slots, stepSlot: main.stepSlot};
  const agents = (C.flow && C.flow.agents) || [];
  agents.forEach((a) => {
    const id = "a:" + a.id, ls = slotsOf(a.intents, id);
    lanes[id] = {id, agent: a, slots: ls.slots, stepSlot: ls.stepSlot};
  });
  return {lanes, agents};
}
function launcherKey(lanes, a) {
  const L = a.launch;
  if (!L) return null;
  const lane = lanes[L.agent ? "a:" + L.agent : "m"];
  if (!lane || L.step == null) return null;
  const first = lane.stepSlot[L.step];
  return first != null ? lane.id + ":" + first : null;
}

// Incremental placement: `P` (persisted across payloads) = {pos: {key: {x, y}}, col: {laneId: column}, colBottom:
// [px], colOwner: [laneId]}. Only unplaced items are placed; their order is time order.
function place(P, M, isDone) {
  const {lanes, agents} = M, heights = {};
  Object.values(lanes).forEach((L) => L.slots.forEach((sl) => { heights[sl.key] = slotHeight(sl); }));
  agents.forEach((a) => { heights["a:" + a.id] = AGENT_H; });
  const bottomOf = (laneId) => {   // bottom edge of a lane's lowest placed node (current heights)
    let b = -Infinity;
    const L = lanes[laneId];
    if (laneId !== "m" && P.pos[laneId]) b = P.pos[laneId].y + AGENT_H;
    if (L) L.slots.forEach((sl) => { const p = P.pos[sl.key]; if (p) b = Math.max(b, p.y + heights[sl.key]); });
    return b;
  };
  const items = [];
  Object.values(lanes).forEach((L) => L.slots.forEach((sl, n) => items.push({kind: "slot", sl, L, t: sl.ts0, n})));
  agents.forEach((a) => items.push({kind: "agent", a, t: a.launch_ts != null ? a.launch_ts : a.first_ts, n: -1}));
  let lastT = -Infinity;
  items.forEach((x) => { if (x.t == null) x.t = Infinity; });
  items.sort((p, q) => (p.t - q.t) || (p.kind === q.kind ? 0 : p.kind === "slot" ? -1 : 1) || (p.n - q.n));
  items.forEach((x) => {
    if (x.kind === "agent") {
      const id = "a:" + x.a.id;
      if (P.pos[id]) return;
      const lk = launcherKey(lanes, x.a), parentLane = x.a.launch && x.a.launch.agent ? "a:" + x.a.launch.agent : "m";
      const lp = lk && P.pos[lk];
      const y = lp ? lp.y : Math.max(0, bottomOf(parentLane) + GAP_Y);
      const minCol = (P.col[parentLane] || 0) + 1;
      let c = minCol;
      // a column is free at y when its lane has ended and its lowest node is above y (parallel fan-outs: side by side)
      while (P.colOwner[c] && !(isDone(P.colOwner[c]) && bottomOf(P.colOwner[c]) + GAP_Y <= y)) c++;
      P.col[id] = c; P.colOwner[c] = id;
      P.pos[id] = {x: c * COL_W, y};
      return;
    }
    const {sl, L} = x;
    if (P.pos[sl.key]) return;
    let y = bottomOf(L.id);
    if (L.id === "m") {
      // rejoin: a node after a subagent returned goes below that subagent's flow
      M.agents.forEach((a) => {
        const id = "a:" + a.id;
        if (P.pos[id] && !running(a) && a.last_ts != null && x.t !== Infinity && a.last_ts <= x.t) y = Math.max(y, bottomOf(id));
      });
      y = y === -Infinity ? 0 : y + GAP_Y;
      P.pos[sl.key] = {x: 0, y};
    } else {
      if (!P.pos[L.id]) return;   // its lane header comes first
      y = (y === -Infinity ? P.pos[L.id].y + AGENT_H : y) + GAP_Y * 0.6;
      P.pos[sl.key] = {x: P.pos[L.id].x, y};
    }
    lastT = Math.max(lastT, x.t);
  });
  return heights;
}

// ---------- nodes ----------
function CatTag({cat}) { return <span className="cat" style={{color: tcol(cat)}}>[{String(cat || "other").toUpperCase()}]</span>; }
function Marks({x}) {
  return <>
    {x.source === "declared" ? <span className="decl" title="declared intent">✎</span> : null}
    {x.skills && x.skills.length ? <span className="skm" title={"skills: " + x.skills.join(", ")}>✦ {x.skills[0]}{x.skills.length > 1 ? " +" + (x.skills.length - 1) : ""}</span> : null}
    {x.tested ? <span className={x.tested === "pass" ? "ok" : "bad"}>{x.tested === "pass" ? "✓" : "✗"}</span> : null}
  </>;
}
const HANDLES = <>
  <Handle type="target" position={Position.Top} id="t" isConnectable={false} />
  <Handle type="source" position={Position.Bottom} id="b" isConnectable={false} />
  <Handle type="source" position={Position.Right} id="r" isConnectable={false} />
  <Handle type="target" position={Position.Left} id="l" isConnectable={false} />
</>;

function slotCost(sl) {
  const g = sl.group, xs = sl.steps;
  if (g) return {cw: g.cw, usd: g.usd, dur_s: g.dur_s};
  const sum = (k) => xs.reduce((n, x) => n + (+x[k] || 0), 0);
  const t0 = xs[0].ts0, t1 = xs[xs.length - 1].ts1;
  return {cw: sum("cw"), usd: sum("usd"), dur_s: xs.length === 1 ? xs[0].dur_s : num(t0) != null && num(t1) != null ? t1 - t0 : null};
}
function IntentNode({data}) {
  const {sl, live, selected, n, alerts} = data, g = sl.group, last = sl.steps[sl.steps.length - 1];
  const pending = sl.kind === "pending", single = slotSteps(sl) === 1 && !pending;
  const provisional = single && last.open && !last.locked;
  const label = sl.label + (provisional || pending ? "…" : "");
  const c = slotCost(sl), k = slotSteps(sl);
  const meta = (k + (k === 1 ? " step · " : " steps · ")) + costText(c.cw, c.usd, c.dur_s);
  const detail = single ? stepSummary(last) : null;
  const al = alerts || [], other = k - 1;
  const src = single ? last : g;
  return <div className={"inode" + (live ? " live" : "") + (selected ? " sel" : "") + (pending ? " pending" : "") + (al.length ? " alerted " + worst(al) : "")} style={{borderLeftColor: gcol(sl.cat), height: data.h}}>
    {HANDLES}
    <div className="ihd">
      {n != null ? <span className="gn">{n}</span> : null}
      <CatTag cat={sl.cat} />
      <span className={"sl" + (provisional || pending ? " prov" : "")} title={pending ? "the intent of these steps is not known yet" : label + (src && src.source ? "  (" + src.source + " label)" : "")}>{live && single ? "▶ " : ""}{label}</span>
      {src ? <Marks x={src} /> : null}
      <AlertMark as={al} />
    </div>
    <div className="imeta">{meta}</div>
    {single ? (detail ? <div className="idet" title={detail}>↳ {detail}</div> : <div className="idet mut"> </div>) : <div className="irows">
      {other > 0 ? <div className="irow mut iother" title="click the node to see every step">+{other} other step{other > 1 ? "s" : ""}<AlertMark as={al.filter((a) => a.step !== last.i)} /></div> : null}
      <div className={"irow" + (last.open ? " open" : "")}>
        <CatTag cat={last.cat} /><span className="rl" title={last.label}>{live ? "▶ " : ""}{last.label}</span><Marks x={last} /><AlertMark as={al.filter((a) => a.step === last.i)} /><span className="rn">{costText(last.cw, null, last.dur_s)}</span>
      </div>
    </div>}
  </div>;
}
function AgentNode({data}) {
  const a = data.a, st = a.status || "?";
  return <div className={"anode" + (running(a) ? " live" : "") + (data.selected ? " sel" : "")} style={{height: AGENT_H}}>
    {HANDLES}
    <div className="ahd"><span className="aarrow">↳</span><b className="atype">{a.type || "agent"}</b>
      <span className={"apill " + (running(a) ? "run" : st === "error" ? "err" : "")}>{st}</span></div>
    <div className="adesc" title={a.description || ""}>{a.description || "subagent"}<span className="mut"> · {a.turns || 0} turns · {fmtK(a.cw)} CW{num(a.usd) ? " · " + fmtUsd(a.usd) : ""}</span></div>
  </div>;
}
const NODE_TYPES = {intent: IntentNode, agent: AgentNode};

// ---------- side panel: a node's steps and calls ----------
function opParts(o) {
  const tool = /^Bash ./.test(o.tool) ? o.tool.slice(5) : String(o.tool || "?"), t0 = o.target;
  const esc = (s) => s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const target = t0 && t0 !== tool && !new RegExp("[ :]" + esc(String(t0)) + "$").test(tool) ? " " + t0 : "";
  const t = o.test && typeof o.test === "object" ? o.test : null;
  const test = !t ? "" : t.failed > 0 ? t.failed + " failed" : t.passed != null ? t.passed + " passed" : "";
  return {label: tool + target, test, testBad: !!(t && (!t.ok || t.failed > 0)),
    nums: [+o.cw > 0 ? fmtK(o.cw) + " CW" : null, num(o.dur_s) != null ? fmtWall(o.dur_s) : null].filter(Boolean).join(" · ")};
}
function OpRow({o, live}) {
  const p = opParts(o), run = o.err == null && live, failed = o.err || p.testBad, skill = o.skill || (/^skill:./.test(o.tool) ? o.tool.slice(6) : null);
  const icon = failed ? "✗" : skill ? "✦" : run ? "▶" : o.err == null ? "?" : "✓";
  return <div className={"op" + (run ? " run" : "")} title={o.err == null && !live ? "no result in the transcript" : ""}>
    <span className={"oi" + (failed ? " bad" : skill ? " sk" : o.err == null && !live ? "" : " ok")}>{icon}</span>
    <span className="ol">{p.label}</span>
    {p.test ? <span className={"chip " + (p.testBad ? "bad" : "ok")}>{p.test}</span> : null}
    <span className="on">{p.nums}</span>
  </div>;
}
function StepBlock({x, live}) {
  const ops = (x.ops || []).filter((o) => o && !isDeclare(o.tool)), sum = stepSummary(x);
  return <div className="pstep">
    <div className="psh"><CatTag cat={x.cat} /><b className="psl">{live ? "▶ " : ""}{x.label}</b><Marks x={x} /><span className="pn">{costText(x.cw, x.usd, x.dur_s)}</span></div>
    {sum ? <div className="psum">↳ {sum}</div> : null}
    <div className="ops">{ops.length ? ops.map((o, k) => <OpRow key={k} o={o} live={live} />) : <div className="op mut">no tool calls (thinking / text only)</div>}</div>
  </div>;
}
function AlertList({as}) {
  if (!as || !as.length) return null;
  return <div className="palerts">{as.map((x) => <div key={x.id} className={"palert " + x.severity}>
    <b>▲ {alertName(x)}{times(x)}</b> <span className="mut">{stepRange(x)}</span>
    <div>{x.message}</div></div>)}</div>;
}
function Panel({sel, onClose}) {
  if (!sel) return null;
  const {sl, a, live} = sel;
  if (a && !sl) {
    return <aside className="panel"><button className="pclose" onClick={onClose} title="close">×</button>
      <div className="ptitle"><b>↳ {a.type || "agent"}</b> <span className="mut">{a.status}</span></div>
      <div className="pmeta">{a.description || "subagent"}</div>
      <div className="pmeta">{a.turns || 0} turns · {costText(a.cw, a.usd)}</div>
      {!a.intents ? <div className="mut small">no transcript yet</div> : null}
    </aside>;
  }
  const g = sl.group, last = sl.steps[sl.steps.length - 1], c = slotCost(sl), k = slotSteps(sl);
  return <aside className="panel"><button className="pclose" onClick={onClose} title="close">×</button>
    {a ? <div className="pmeta">in subagent <b>{a.type}</b>{a.description ? " · " + a.description : ""}</div> : null}
    <div className="ptitle"><CatTag cat={sl.cat} /> <b>{sl.label}{sl.kind === "pending" ? "…" : ""}</b> {g || k === 1 ? <Marks x={g || last} /> : null}</div>
    <div className="pmeta">{k + (k === 1 ? " step · " : " steps · ") + costText(c.cw, c.usd, c.dur_s)}</div>
    <AlertList as={sel.alerts} />
    {sl.steps.map((s) => <StepBlock key={s.i} x={s} live={live && s === last} />)}
  </aside>;
}

// ---------- overlays: header rows, recap ----------
function TestsNode({t}) {
  const all = Array.isArray(t.trend) ? t.trend : [], passed = Array.isArray(t.passed_trend) ? t.passed_trend : [], out = [];
  if (t.scope === "subagents") out.push(<span key="s" className="mut">subagents: </span>);
  for (let i = Math.max(0, all.length - 5); i < all.length - 1; i++) {
    const f = all[i];
    out.push(f == null ? <span key={i} className="mut">?</span> : f > 0 ? <span key={i} className="bad">✗{f}</span> : <span key={i} className="ok">✓{passed[i] != null ? passed[i] : ""}</span>);
    out.push(<span key={"a" + i} className="mut"> → </span>);
  }
  if (t.ok && !(t.failed > 0)) out.push(<b key="r" className="ok">✓ {t.passed != null ? t.passed + " passed" : "passed"}</b>);
  else if (t.failed > 0) out.push(<b key="r" className="bad">✗ {t.failed} failed{t.passed != null ? " · " + t.passed + " passed" : ""}</b>);
  else out.push(<span key="r" className="mut">? ran {t.runs || 1}×</span>);
  return <span>{out}</span>;
}
function NowNode({C}) {
  const st = C.status;
  if (st === "waiting") return <span className="warnc">◼ done, waiting for you</span>;
  if (st === "idle") return <span className="mut">◼ idle</span>;
  const run = (C.agents || []).filter(running);
  if (run.length) return <b style={{color: gcol("delegate")}}>▶ {run.length} subagent{run.length === 1 ? "" : "s"} working</b>;
  const w = C.now || {}, label = w.label || String(w.intent || C.current_intent || "working").replace(/_/g, " ");
  return <span><b style={{color: gcol(w.group)}}>▶ {label}</b>{w.target || w.tool ? " · " + (w.target || w.tool) : ""}{w.since_s != null ? <span className="mut"> · {fmtWall(w.since_s)}</span> : null}</span>;
}
function Head({C, elapsed, open, setOpen, conn, replay, onRestart}) {
  const H = C.header || {}, T = H.totals || {}, a = C.asked || {}, goal = a.goal || goalName(H.goal) || C.goal;
  const ctx = +C.context_tokens || 0, pct = Math.min(100, ctx / 2000);
  const files = Array.isArray(C.files_edited) ? C.files_edited : [], fn = Math.max(+C.files_edited_n || 0, files.length);
  const sk = (C.skills || []).filter((s) => s && s.name);
  const st = C.status || "idle";
  return <div className="ov head">
    <div className="hbar">
      <a className="back" href="/" title="back to the list of sessions">← all sessions</a>
      <span className={"pill " + st}><span className="dot" />{st === "waiting" ? "waiting for you" : st}</span>
      <b className="hgoal" title={goal || ""}>{goal || "(no prompt yet)"}</b>
      <button className="tgl" onClick={() => setOpen(!open)} title={open ? "collapse" : "expand"}>{open ? "▾" : "▸"}</button>
    </div>
    <div className="kp">
      <span>⏱ <b>{elapsed != null ? fmtWall(elapsed) : "–"}</b></span>
      <span><b>{fmtK(T.cw != null ? T.cw : C.cw)}</b> CW</span>
      <span><b>{fmtUsd(T.usd != null ? T.usd : C.usd)}</b></span>
      <span>{T.turns || C.turns || 0} turns · {T.tool_calls || C.tool_calls || 0} tools{(T.errors || C.errors) ? " · " + (T.errors || C.errors) + " err" : ""}</span>
      <span className="gauge" title={"context " + fmtK(ctx) + " / 200k"}><i className={pct >= 75 ? "hot" : pct >= 50 ? "warm" : ""} style={{width: pct + "%"}} /></span>
      <span className="conn">{replay ? <>▶ replay {Math.round(100 * (replay.fed / Math.max(1, replay.records)))}%{replay.done ? " · done" : ""} <button className="tgl" onClick={onRestart}>restart</button></> : conn}</span>
    </div>
    {open ? <div className="hrows">
      <span className="hl">PROJECT</span><span className="hv mut">{(H.project || "—") + (H.model ? " · " + H.model : "") + " · " + String(H.id || "").slice(0, 8)}</span>
      <span className="hl">ASKED</span><span className="hv"><b>{goal || "(no prompt yet)"}</b>{a.source === "haiku" ? <span className="haiku">✦ haiku</span> : null}</span>
      {(a.prompt_n || 0) > 1 && a.latest ? <><span className="hl" /><span className="hv">↳ now: {a.latest}</span></> : null}
      <span className="hl">NOW</span><span className="hv"><NowNode C={C} /></span>
      {C.tests ? <><span className="hl">TESTS</span><span className="hv"><TestsNode t={C.tests} /></span></> : null}
      {fn ? <><span className="hl">FILES</span><span className="hv" title={files.join("\n")}>{files.length ? "edited " + fn + ": " + files.slice(-6).join(", ") + (files.length > 6 ? ", …" : "") : "edited " + fn + (fn === 1 ? " file" : " files")}</span></> : null}
      {sk.length ? <><span className="hl">SKILLS</span><span className="hv skm">✦ {sk.map((s) => s.name + " ×" + (s.count || 1)).join(" · ")}</span></> : null}
      {openAlerts(C).length ? <><span className="hl">ALERTS</span><span className="hv hvw">{openAlerts(C).slice(0, 6).map((a) =>
        <span key={a.id} className={"chip alert " + a.severity + (a.active ? " on" : "")} title={alertTip([a])}
          onMouseEnter={(e) => showTip({text: alertTip([a]), sev: a.severity, x: e.clientX, y: e.clientY})} onMouseLeave={() => showTip(null)}>▲ {alertName(a)}{times(a)}{a.step != null ? " · " + a.step : ""}</span>)}
        {openAlerts(C).length > 6 ? <span className="mut"> +{openAlerts(C).length - 6}</span> : null}</span></> : null}
      {(H.flags || []).length ? <><span className="hl">FLAGS</span><span className="hv">{H.flags.map((f, k) => <span key={k} className="chip warn">{String(f.flag).replace(/_/g, " ")}{f.count > 1 ? " ×" + f.count : ""}</span>)}</span></> : null}
    </div> : null}
  </div>;
}
function Recap({rc, open, setOpen}) {
  const c = rc.costliest, sl = rc.slowest && rc.slowest.s != null ? rc.slowest : null;
  const line = (lead, name, x, cost) => <div className="ri" key={name}>
    {lead.map((t, k) => <b key={k} className="rlead">{t}</b>)}<span className="mut">{name}</span><CatTag cat={x.cat} /><b>{x.label || "intent"}</b>
    {cost ? <span className="mut"> · {costText(x.cw, x.usd)}</span> : null}</div>;
  const share = c && c.pct != null ? [c.pct + "%"] : [];
  const lines = c && sl && sl.label === c.label && sl.cat === c.cat ? [line(share.concat([fmtWall(sl.s)]), "costliest & slowest", c, true)]
    : [c ? line(share, "costliest", c, true) : null, sl ? line([fmtWall(sl.s)], "slowest", sl, false) : null];
  const hints = (Array.isArray(rc.hints) ? rc.hints : []).filter((h) => h && (h.action || h.text)).slice(0, 3);
  return <div className="ov recap">
    <div className="rt">DONE · recap{rc.cw != null ? " · " + fmtK(rc.cw) + " CW" + (rc.usd != null ? " · " + fmtUsd(rc.usd) : "") : ""}
      <button className="tgl" onClick={() => setOpen(!open)}>{open ? "▾" : "▸"}</button></div>
    {open ? <>{lines}
      {hints.map((h, k) => {
        const cross = h.kind && h.kind !== "session";
        return <div key={k} className={"hint" + (cross ? " cross" : "")}>
          <div className="ha"><span className="harrow">➜</span><b>{h.action || h.text}</b>{cross ? <span className="xtag" title="evidence from your other sessions">{String(h.kind).replace(/_/g, "-")}</span> : null}</div>
          {h.evidence ? <div className="hev" title={h.estimate || ""}>{h.evidence}</div> : null}
          {Array.isArray(h.matches) && h.matches.length ? <div className="hev small">matched: {h.matches.slice(0, 4).map((m) => (m.sid || "?") + " · " + fmtK(m.cw) + " CW").join(", ")}</div> : null}
          {h.skill || h.stub_path ? <div className="hev">{h.skill ? <span className="skm">✦ {h.skill}</span> : null}{h.skill && h.stub_path ? " · " : ""}{h.stub_path ? <>draft: <code className="path">{homePath(h.stub_path)}</code></> : null}</div> : null}
        </div>;
      })}</> : null}
  </div>;
}

// ---------- data ----------
const SID = window.TS_SESSION_ID || decodeURIComponent((location.pathname.match(/\/s\/([^/?#]+)/) || [])[1] || "") ||
  new URLSearchParams(location.search).get("id");
const REPLAY = /^(1|true)$/.test(new URLSearchParams(location.search).get("replay") || "");
const DURATION = new URLSearchParams(location.search).get("duration");

function useFlow() {
  const [C, setC] = useState(null), [err, setErr] = useState(null), [conn, setConn] = useState("");
  const busy = useRef(false), again = useRef(false), restart = useRef(REPLAY), fetchedAt = useRef(0);
  const load = useCallback(() => {
    if (busy.current) { again.current = true; return; }
    busy.current = true;
    const q = REPLAY ? "?replay=1" + (restart.current ? "&restart=1" : "") + (DURATION ? "&duration=" + encodeURIComponent(DURATION) : "") : "";
    restart.current = false;
    fetch("/api/session/" + encodeURIComponent(SID) + "/flow" + q, {cache: "no-store"})
      .then((r) => { if (r.status === 404) throw new Error("404"); if (!r.ok) throw new Error("HTTP " + r.status); return r.json(); })
      .then((d) => { fetchedAt.current = Date.now(); setC(d); setErr(null); },
        (e) => setErr(e.message === "404" ? "Unknown session “" + SID + "”: it may have been idle for more than 6 h, or the id is wrong." : "Cannot reach the tokenscope server."))
      .then(() => { busy.current = false; if (again.current) { again.current = false; setTimeout(load, 150); } });
  }, []);
  useEffect(() => {
    if (!SID) { setErr("No session id in the URL."); return undefined; }
    load();
    if (REPLAY) { const t = setInterval(load, 600); return () => clearInterval(t); }
    let es = null, t2 = 0;
    if (window.EventSource) {
      es = new EventSource("/events");
      es.onopen = () => setConn("● live");
      es.onerror = () => setConn("○ polling");
      es.addEventListener("step", (e) => {
        try { const m = JSON.parse(e.data); if (m.session && SID && m.session.indexOf(SID) === 0) { clearTimeout(t2); t2 = setTimeout(load, 250); } } catch (x) { /* ignore */ }
      });
    }
    const t = setInterval(load, 2500);
    return () => { clearInterval(t); if (es) es.close(); };
  }, [load]);
  const restartReplay = useCallback(() => { restart.current = true; load(); }, [load]);
  return {C, err, conn, fetchedAt, restartReplay};
}

// ---------- canvas ----------
function Canvas() {
  const {C, err, conn, fetchedAt, restartReplay} = useFlow();
  const rf = useReactFlow();
  const P = useRef({pos: {}, col: {m: 0}, colOwner: [], gen: 0});
  const [follow, setFollow] = useState(true), [selKey, setSelKey] = useState(null);
  const [headOpen, setHeadOpen] = useState(true), [recapOpen, setRecapOpen] = useState(true);
  const [tip, setTip] = useState(null);
  showTip = setTip;
  const [, tick] = useState(0);
  useEffect(() => { const t = setInterval(() => tick((n) => n + 1), 1000); return () => clearInterval(t); }, []);
  const lastReplayFed = useRef(null);

  // a restarted replay starts from an empty canvas
  if (C && C.replay && lastReplayFed.current != null && C.replay.fed < lastReplayFed.current) P.current = {pos: {}, col: {m: 0}, colOwner: [], gen: P.current.gen + 1};
  if (C && C.replay) lastReplayFed.current = C.replay.fed;

  const view = useMemo(() => {
    if (!C) return null;
    const M = model(C);
    const byId = {};
    M.agents.forEach((a) => { byId["a:" + a.id] = a; });
    const heights = place(P.current, M, (laneId) => !running(byId[laneId]));
    const working = C.status === "working";
    const byLane = alertsByLane(C);
    const nodes = [], edges = [], liveKeys = [];
    let groupN = 0;
    Object.values(M.lanes).forEach((L) => {
      const a = L.agent;
      if (a && P.current.pos[L.id]) {
        nodes.push({id: L.id, type: "agent", position: P.current.pos[L.id], data: {a, selected: selKey === L.id},
          width: NODE_W, height: AGENT_H, draggable: false});
      }
      const laneLive = L.id === "m" ? working : running(a) && working;
      let prev = a ? L.id : null;
      L.slots.forEach((sl, k) => {
        const pos = P.current.pos[sl.key];
        if (!pos) return;
        const last = k === L.slots.length - 1;
        const live = laneLive && last;
        if (live) liveKeys.push({key: sl.key, t: sl.ts0 || 0});
        const idx = new Set(sl.steps.map((s) => s.i));
        const alerts = (byLane[L.id] || []).filter((x) => idx.has(x.step));
        nodes.push({id: sl.key, type: "intent", position: pos, draggable: false, width: NODE_W, height: heights[sl.key],
          data: {sl, live, selected: selKey === sl.key, h: heights[sl.key], n: L.id === "m" && sl.kind !== "step" ? ++groupN : null, alerts}});
        if (prev) edges.push({id: "e:" + prev + ">" + sl.key, source: prev, target: sl.key, sourceHandle: "b", targetHandle: "t",
          type: "smoothstep", style: {stroke: a ? gcol("delegate") : "#c3cad6", strokeWidth: 1.5}});
        prev = sl.key;
      });
    });
    // subagents: launch edge from the launching node, rejoin edge into the next node of the launching lane
    M.agents.forEach((a) => {
      const id = "a:" + a.id;
      if (!P.current.pos[id]) return;
      const lk = launcherKey(M.lanes, a);
      if (lk && P.current.pos[lk]) edges.push({id: "l:" + id, source: lk, target: id, sourceHandle: "r", targetHandle: "l",
        type: "smoothstep", animated: running(a), style: {stroke: gcol("delegate"), strokeWidth: 1.5}});
      if (!running(a) && a.last_ts != null) {
        const L = M.lanes[id], parent = M.lanes[a.launch && a.launch.agent ? "a:" + a.launch.agent : "m"];
        const tail = L && L.slots.length ? L.slots[L.slots.length - 1].key : id;
        const next = parent && parent.slots.find((s) => s.ts0 != null && s.ts0 >= a.last_ts && P.current.pos[s.key]);
        if (next && P.current.pos[tail]) edges.push({id: "j:" + id, source: tail, target: next.key, sourceHandle: "b", targetHandle: "t",
          type: "smoothstep", style: {stroke: "#c3cad6", strokeWidth: 1.2, strokeDasharray: "5 4"}});
      }
    });
    // the node to follow: the newest live node, else the newest node of the main flow
    liveKeys.sort((p, q) => p.t - q.t);
    const mainSlots = M.lanes.m.slots.filter((s) => P.current.pos[s.key]);
    const target = liveKeys.length ? liveKeys[liveKeys.length - 1].key : mainSlots.length ? mainSlots[mainSlots.length - 1].key : null;
    return {M, nodes, edges, target, heights, byId};
  }, [C, selKey]);

  // auto-follow: pan smoothly to the live node whenever it changes (or grows)
  const followKey = view && view.target ? view.target + ":" + view.heights[view.target] + ":" + view.nodes.length : null;
  const first = useRef(true);
  useEffect(() => {
    if (!view || !view.target || !follow) return;
    const p = P.current.pos[view.target], h = view.heights[view.target] || HEAD_H;
    if (!p) return;
    if (first.current && C.status !== "working" && !REPLAY) {   // a finished session opens on the whole flow
      first.current = false;
      // room for the header / recap overlays at the top
      setTimeout(() => rf.fitView({padding: {top: "230px", left: "30px", right: "30px", bottom: "40px"}, maxZoom: 1}), 50);
      return;
    }
    const z = first.current ? 0.9 : rf.getZoom();
    rf.setCenter(p.x + NODE_W / 2 + (view.nodes.some((n) => n.position.x > 0) ? COL_W * 0.5 : 0), p.y + h / 2, {zoom: z, duration: first.current ? 0 : 600});
    first.current = false;
  }, [followKey, follow]); // eslint-disable-line react-hooks/exhaustive-deps

  // a pan / zoom by the person pauses following (a click on a node also starts a "move": only a changed viewport counts)
  const moveFrom = useRef(null);
  const onMoveStart = useCallback((ev, vp) => { moveFrom.current = ev ? vp : null; }, []);
  const onMoveEnd = useCallback((ev, vp) => {
    const a = moveFrom.current;
    moveFrom.current = null;
    if (ev && a && (Math.abs(a.x - vp.x) > 2 || Math.abs(a.y - vp.y) > 2 || Math.abs(a.zoom - vp.zoom) > 1e-3)) setFollow(false);
  }, []);
  const onNodeClick = useCallback((ev, n) => setSelKey((k) => (k === n.id ? null : n.id)), []);

  // jump to an alert's step (the alerts drawer, static/alerts.js, calls window.tsCanvasFocus; /s/<id>#step=N[&agent=X]
  // on load): stop following, select the node holding it and centre it
  const focusAlert = useCallback((a) => {
    if (!view || !a || a.step == null) return false;
    const lane = a.agent ? "a:" + a.agent : "m";
    const n = view.nodes.find((x) => x.type === "intent" && x.data.sl.lane === lane && x.data.sl.steps.some((s) => s.i === a.step));
    if (!n) return false;
    setFollow(false);
    setSelKey(n.id);
    rf.setCenter(n.position.x + NODE_W / 2, n.position.y + (view.heights[n.id] || HEAD_H) / 2, {zoom: Math.max(rf.getZoom(), 0.85), duration: 500});
    return true;
  }, [view, rf]);
  useEffect(() => { window.tsCanvasFocus = focusAlert; }, [focusAlert]);
  const hashDone = useRef(false);
  useEffect(() => {
    if (hashDone.current || !view || !view.nodes.length) return;
    const q = new URLSearchParams(String(window.location.hash || "").replace(/^#/, ""));
    if (!q.has("step")) { hashDone.current = true; return; }
    const a = {step: +q.get("step"), agent: q.get("agent") || null};
    setTimeout(() => { if (focusAlert(a)) hashDone.current = true; }, 120);
  }, [view, focusAlert]);

  if (err && !C) return <div className="empty">{err}</div>;
  if (!C) return <div className="empty">loading…</div>;
  let sel = null;
  if (selKey && view) {
    const n = view.nodes.find((x) => x.id === selKey);
    if (n) sel = n.type === "agent" ? {a: n.data.a} : {sl: n.data.sl, live: n.data.live, a: view.byId[n.data.sl.lane] || null, alerts: n.data.alerts};
  }
  const H = C.header || {};
  const st = (window.performance && fetchedAt.current) ? (Date.now() - fetchedAt.current) / 1000 : 0;
  const elapsed = H.started != null ? (C.status === "working" ? (C.now_ts || 0) + (REPLAY ? 0 : st) : (H.last_event_ts || C.now_ts)) - H.started : C.elapsed_s;
  const showRecap = C.recap && (C.status === "waiting" || C.status === "idle");
  return <div className="wrap">
    <ReactFlow nodes={view.nodes} edges={view.edges} nodeTypes={NODE_TYPES} onMoveStart={onMoveStart} onMoveEnd={onMoveEnd} onNodeClick={onNodeClick}
      onPaneClick={() => setSelKey(null)} minZoom={0.08} maxZoom={2} nodesConnectable={false} nodesDraggable={false}
      elementsSelectable={false} proOptions={{hideAttribution: true}} onlyRenderVisibleElements={view.nodes.length > 300}>
      <Background gap={22} size={1} color="#dde2ea" />
      <Controls position="bottom-left" showInteractive={false} onFitView={() => setFollow(false)} onZoomIn={() => setFollow(false)} onZoomOut={() => setFollow(false)} />
      <MiniMap position="bottom-right" pannable zoomable nodeColor={(n) => n.type === "agent" ? gcol("delegate") :
        gcol(n.data.sl.cat)} nodeStrokeColor={(n) => n.data && n.data.alerts && n.data.alerts.length ? "#e03131" : "transparent"}
        nodeStrokeWidth={6} maskColor="rgba(240,242,246,.7)" />
    </ReactFlow>
    <Head C={C} elapsed={elapsed} open={headOpen} setOpen={setHeadOpen} conn={err ? "○ offline" : conn} replay={C.replay} onRestart={() => { setFollow(true); restartReplay(); }} />
    {showRecap ? <Recap rc={C.recap} open={recapOpen} setOpen={setRecapOpen} /> : null}
    {!view.nodes.length ? <div className="ov emptyov">No intents yet.</div> : null}
    {!follow ? <button className="follow" onClick={() => setFollow(true)}>⦿ follow live</button> : null}
    <Panel sel={sel} onClose={() => setSelKey(null)} />
    <Tip tip={tip} />
  </div>;
}

function App() { return <ReactFlowProvider><Canvas /></ReactFlowProvider>; }
createRoot(document.getElementById("canvas")).render(<App />);
