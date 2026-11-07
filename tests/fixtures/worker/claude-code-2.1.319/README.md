# Recorded `claude -p` runs for the headless worker spawner

Real output of `claude -p --output-format json` from Claude Code 2.1.319 (Linux, WSL2, recorded
2026-11-07), to check the spawner's invocation against what the CLI does instead of what we
remember. Six invocations, each with `--max-budget-usd 0.10`, cwd = a scratch repo whose
`CLAUDE.md` says "Project note: the marker sentence is PURPLE-HERON-4417." and an isolated
`COVEY_HOME` with a running `coveyd`. Task text always on **stdin**. Scratch paths are replaced by
`/SCRATCH`. Files are the single JSON object printed on stdout, unedited otherwise.
Wall time is the whole process including start-up.

Common flags: `--output-format json --restricted --permission-prompts none --max-budget-usd 0.10`

- `01-restricted-denied.json`: default model (opus), `--tools "Read,Grep,Glob" --allowedTools "Read,Grep,Glob"
  --max-turns 4 --append-system-prompt "The secret word is KUMQUAT-9."`, stdin asks for the marker, `echo hi`
  and the secret word. `--max-turns` was accepted (no usage error). Stopped by the budget after 3 turns:
  `subtype: error_max_budget_usd`, `is_error: true`, `terminal_reason: budget_exhausted`, **no `result` field**,
  `total_cost_usd` 0.193 (over the cap: the cap is checked between turns). 8.6 s.
- `02-restricted-denied-retry.json`: same with `--tools Read Grep Glob --allowedTools Read Grep Glob`
  (space-separated). Accepted, same outcome (cost 0.185, no `result`), so the space form parses but this run
  cannot show which tools the model got. Use the comma form.
- `03-mcp-whoami.json`: `--model sonnet`, `--strict-mcp-config --mcp-config <file>` where the entry was
  `python -m covey.cli mcp`. That does not work (`covey.cli` has no `__main__`); the model reported the MCP server
  failed with `CONNECTION_CLOSED`. Shows `--restricted` still authenticates and still loads the workdir
  `CLAUDE.md` (model quoted the marker), `--append-system-prompt` text reaches the model (it
  said `KUMQUAT-9`), and a tool absent from `--tools` is "No such tool available". `subtype: success`, 5.8 s.
- `04-mcp-whoami-fixed.json`: same with `mcp.json.sample`'s entry (`python -c "from covey.cli import app; app()" mcp`).
  The server starts, `COVEY_HOME` from its `env` arrives (it reached the scratch daemon), and the model called
  `mcp__covey__covey_whoami`, which returned the daemon's `slug_live` error: an earlier manual shim probe in the
  same directory already held the slug `work-repo` (slug is the workdir name). 7.5 s wall, 4 turns.
- `05-bash-listed-not-allowed.json`: `--tools "Read,Grep,Glob,Bash" --allowedTools "Read,Grep,Glob,mcp__covey__covey_whoami"`,
  `COVEY_AGENT=worker-spike` added to the MCP env, no `--append-system-prompt` (model says it has no secret word).
  `covey_whoami` succeeded (agent `worker-spike`, `live`), and `ps --json` showed it registered under the scratch
  `COVEY_HOME`. `echo hi` **ran** even though Bash is not in `--allowedTools`: Claude Code auto-allows read-only
  commands. `permission_denials` empty. 7.4 s wall with the MCP server configured.
- `06-bash-write-denied.json`: `--tools "Read,Grep,Glob,Bash" --allowedTools "Read,Grep,Glob"`, stdin asks for
  `touch <file>`. Denied; the file was not created. The tool result text the model saw: "Permission for this tool
  use was denied. It requires approval, and this session has no approval surface ... The action was NOT
  performed." The run still ends `subtype: success`, `is_error: false`; the denial is listed in
  top-level `permission_denials` (`tool_name`, `tool_use_id`, `tool_input`). 6.9 s.

`mcp.json.sample` is the `--mcp-config` file used in 04 (05 adds `COVEY_AGENT`).

Top-level fields of the result object (types): `type` str, `subtype` str, `is_error` bool, `result` str
(**absent on error subtypes**), `errors` list (present on error subtypes, absent on success), `session_id` str,
`uuid` str, `total_cost_usd` float, `num_turns` int, `duration_ms` int, `duration_api_ms` int, `stop_reason` str,
`terminal_reason` str (`completed`, `budget_exhausted`), `usage` dict, `modelUsage` dict, `permission_denials` list,
`fast_mode_state` str, `fast_mode_disabled_reason` str, `subagent_stats` dict, `safety_stops` int,
`queued_turn_count` int, `result_index` int, `credential_expires_at` str.
Success runs also carry `api_error_status`, `first_content_frame_ms`, `time_to_request_ms`, `ttft_ms`, `ttft_stream_ms` (see 04).
Not observed: a turn-cap subtype, `--max-turns` being enforced, `--resume`, macOS.
