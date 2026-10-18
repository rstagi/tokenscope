# TokenScope User & Metric Interpretation Guide

`tokenscope` is an agent token visualizer, live tracker, and economic analyzer for local coding agents. It supports both **Google Antigravity CLI/IDE** and **Claude Code** through a zero-dependency, harness-agnostic architecture.

This guide explains how to run TokenScope, how to configure harnesses, and how to interpret every metric, gauge, and visualization.

---

## 1. Quickstart & Command Reference

TokenScope runs on standard Python 3.11+ without any third-party runtime dependencies.

### Starting the Live Web UI

Launch the live monitoring board:

```sh
# Auto-detects available harness (Antigravity or Claude Code)
python3 -m tokenscope live

# Explicitly monitor Google Antigravity sessions:
python3 -m tokenscope live --harness antigravity

# Explicitly monitor Claude Code sessions:
python3 -m tokenscope live --harness claude

# Run with privacy redaction turned off (shows raw prompts, paths, commands):
python3 -m tokenscope live --no-redact --port 8765
```

Navigate to `http://127.0.0.1:8765/`:
- **Board (`/`)**: Live cards for all recent and running agent sessions.
- **Follow View (`/follow`)**: Real-time side-by-side feed of active sessions and subagent steps.
- **Session Canvas (`/s/<id>`)**: Interactive infinite canvas tracing prompt turns, intent graphs, and subagent branches.

---

### Command Line Tools

#### 1. Listing Sessions (`tokenscope sessions`)
Lists local agent sessions with call count, cost-weighted tokens, API-equivalent cost, session kind, and initial prompt:

```sh
python3 -m tokenscope sessions
python3 -m tokenscope sessions --harness antigravity --since 24h
python3 -m tokenscope sessions --project myrepo --json
```

#### 2. Deep Session Analysis (`tokenscope analyze`)
Provides a complete turn-by-turn breakdown of context accumulation, tool attribution, idle gaps, and token burn:

```sh
python3 -m tokenscope analyze <session-id-or-prefix>
```

#### 3. Top Spenders (`tokenscope top`)
Ranks the most expensive sessions, user goals, turns, or intents over a rolling usage window:

```sh
# Top spenders in the 5-hour usage limit window (or 24h, 7d, 30d):
python3 -m tokenscope top --window 5h --by session
python3 -m tokenscope top --window 24h --by intent
python3 -m tokenscope top --window 7d --by goal
```

#### 4. HTML Report Generation (`tokenscope report`)
Generates a standalone, interactive HTML report with zero external dependencies:

```sh
python3 -m tokenscope report -o report.html --open
python3 -m tokenscope report --session <id> -o session-report.html
```

#### 5. Before vs After Optimization (`tokenscope compare`)
Compares two sessions or sets of sessions (e.g. before and after adding documentation rules or tool filters):

```sh
python3 -m tokenscope compare <session_A> <session_B> -o compare.html --open
```

#### 6. Actionable Fixes (`tokenscope fixes`)
Identifies automated rules to prevent repeated context waste:

```sh
python3 -m tokenscope fixes --session <id>
```

---

## 2. Understanding Token Economics & Metrics

LLMs do not bill or process all tokens equally. TokenScope presents an honest, multi-dimensional view of token consumption rather than a single misleading token sum.

### The Cost-Weighted (CW) Token Metric

When an agent reads 100,000 cached tokens and generates 500 output tokens, counting both equally as "100,500 tokens" hides the real bottleneck: generating output tokens is compute-intensive, whereas reading cached input is computationally cheap.

TokenScope calculates **Cost-Weighted (CW) Tokens**:

$$\text{CW} = (\text{input} \times 1.0) + (\text{cache\_write} \times 1.25) + (\text{cache\_read} \times 0.10) + (\text{output} \times 5.0)$$

#### Component Breakdown:
| Token Type | Multiplier | Why It Has This Weight |
| :--- | :--- | :--- |
| **Fresh Input** | $\mathbf{1.0\times}$ | Baseline uncached prompt tokens and initial tool definitions. |
| **Cache Write** | $\mathbf{1.25\times}$ | Checkpointing prompt context into KV-cache (typically 25% premium over standard input). |
| **Cache Read** | $\mathbf{0.10\times}$ | Re-reading cached context on subsequent turns (typically a 90% discount). |
| **Output** | $\mathbf{5.0\times}$ | Auto-regressive token generation by the model (typically $4\times$ to $5\times$ base input price). |

> [!NOTE]
> **Why CW matters**: A session with high *All-In* tokens but low *CW* tokens is leveraging prompt caching effectively. A session with high *CW* tokens is either generating large code blocks or suffering from frequent cache misses.

---

### API-Equivalent USD Cost ($)

Even when you use a flat-rate subscription (e.g., Gemini Advanced, Claude Pro/Team), your usage is governed by hidden rate limits, daily quotas, or session caps tied to underlying API costs.

TokenScope matches each call's model identifier against real-time API pricing tables (`tokenscope/pricing.py`):
- **Claude Models**: Opus 4.8 / 4.7, Sonnet 5.5 / 5 / 4.6 / 4.5, Haiku 4.5.
- **Gemini Models**: Gemini 3.8 Pro / Flash, Gemini 2.5 Pro, Gemini 2.0 Flash / Pro, Gemini 1.5 Pro / Flash.
- **Fuzzy Model Mapping**: Automatically normalizes UI model labels (e.g., `"Gemini 3.8 Flash (High)"` $\to$ `gemini-3.8-flash`).

The displayed dollar amount represents **what this exact agent execution would cost on a metered API**, providing a clear baseline to compare efficiency across models and workflows.

---

### The Carry Effect & Context Accumulation

One of the most critical insights TokenScope reveals is **The Carry Effect**:

$$\text{Total Cost of File Read} = \text{Read Size} + (\text{Read Size} \times \text{Remaining Turns in Session} \times 0.10)$$

When an agent reads a 10,000-token file on Turn 2 of a 40-turn session:
1. Turn 2 pays for reading and cache-writing the 10,000 tokens.
2. Turns 3 through 40 pay for re-reading those 10,000 tokens on **every single turn**.
3. By Turn 40, that single file read has accumulated over $10,000 + (38 \times 1,000) = 48,000$ tokens of effective compute cost.

> [!TIP]
> **Optimization Strategy**: Use targeted searches (`grep_search` / `Grep`, specific line slices in `view_file` / `Read`) rather than reading whole files early in a long session.

---

### Tool Taxonomy & Cross-Harness Attribution

TokenScope normalizes agent actions into standard categories regardless of which harness generated them:

| Normalized Kind | Antigravity Tool Name | Claude Code Tool Name | Attribution Description |
| :--- | :--- | :--- | :--- |
| `file_read` | `view_file` | `Read` | File inspections, slice reads, notebook views |
| `bash` | `run_command` | `Bash` | Terminal executions, test runners, build commands |
| `edit` | `write_to_file`, `replace_file_content` | `Edit`, `Write`, `MultiEdit` | Code modifications and file writes |
| `search` | `grep_search`, `search_web`, `read_url_content`, `list_dir` | `Grep`, `Glob`, `WebSearch`, `WebFetch` | Code search, workspace discovery, web lookup |
| `subagent_result` | `invoke_subagent`, `manage_subagents`, `send_message` | `Agent`, `Task` | Subagent dispatch, multi-agent results |

#### Measured Usage vs Estimated Size (`~`)
- **Measured counts**: Exact per-turn totals from the model's `usage` payload (input, cache_read, output).
- **Estimated sizes (`~`)**: Suffix `~` denotes character-based estimates ($\approx \text{chars} / 4$) for individual tool outputs within a prompt turn where the API does not break down sub-block usage.

---

## 3. Navigating the Live UI

### 1. The Board (`/`)
The landing page displays all active and recent sessions in descending chronological order:
- **Session Card Header**: Session short ID, project name, elapsed duration, model name, and start timestamp.
- **Status Indicator**:
  - `working` (green pulse): Agent is actively executing tools or waiting for an API response ($< 90\text{s}$ old).
  - `waiting` (blue): Agent finished its response and is waiting for user prompt ($< 30\text{m}$).
  - `idle` (muted): No activity for $> 30\text{m}$.
  - `ended` (dimmed): Session has completed or exited.
- **Totals Badge**: Total CW tokens, API-equivalent USD, total turns, and subagent count.
- **Warning Chips**:
  - `stuck_error`: The agent ran into the identical normalized error $3+$ times without an intervening file edit.
  - `reread_loop`: The same file was read in full $4+$ times without modifications.
  - `browser_loop`: $20+$ consecutive browser/web interactions without file edits.
  - `context_near_full`: Context consumption is $\ge 85\%$ of the active context limit.

---

### 2. The Session Canvas (`/s/<session_id>`)
The session canvas provides a full visual workflow graph (built on React Flow):
- **Intent Nodes**: Each user prompt and subsequent tool chain is clustered into an **Intent Node** (e.g. `[LOCATE]`, `[EDIT]`, `[TEST]`).
  - Head shows intent category and high-level goal.
  - Middle displays total steps, CW tokens, and duration.
  - Foot displays the latest active step.
- **Subagent Branches**: When a tool invokes a subagent (`invoke_subagent` or `Agent`), the canvas branches off to the right, showing the subagent's internal flow in parallel, then rejoins the main thread upon completion.
- **Interactive Side Panel**: Clicking any node opens a deep inspection drawer showing:
  - Exact tool inputs (command line, file path, regex pattern).
  - Truncated tool output preview.
  - Token cost and latency of that individual call.
  - Status indicators (success $\checkmark$ or failure $\times$).
- **Overlays (Top Bar)**:
  - **ASKED**: Real user prompt text.
  - **NOW**: Current running tool or last completed action.
  - **TESTS**: Test runner outcomes parsed from stdout.
  - **CONTEXT**: Dynamic gauge showing context occupancy (0 to 1M+ tokens).
- **Time-Travel Replay (`?replay=1`)**: Append `?replay=1` to the URL to simulate the session's execution from start to finish at accelerated speed.

---

### 3. The Follow View (`/follow`)
Designed for continuous monitoring on a secondary monitor:
- Displays up to 4 concurrent sessions side-by-side.
- Streams live tool execution and subagent actions as they occur via Server-Sent Events (SSE).
- Displays active alerts prominently at the top of each session tile.

---

## 4. Summary & Best Practices

1. **Keep Context Trim**: Avoid commands that output megabytes of raw text into the terminal; pipe through `head`, `grep`, or use targeted file slice viewers.
2. **Watch for Warning Chips**: If `stuck_error` or `reread_loop` appears on the Board, intervene early with guidance before the agent burns through tokens.
3. **Audit Top Spenders Regularly**: Run `tokenscope top --window 24h` at the end of the day to identify which projects and goals consumed the most compute.
