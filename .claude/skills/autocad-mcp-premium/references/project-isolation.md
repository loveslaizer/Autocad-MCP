# Isolated projects and staged DWG edits

Live work starts with an agent-owned AutoCAD process, never the operator's
running seat. The default `ISOLATED_INSTANCES=true` uses `DispatchEx`; do not
use `GetActiveObject` or plain `Dispatch` to select a project backend. Legacy
attachment exists only when the operator explicitly disables isolation.

## 1. Create or select the project

```text
project_session_create(project_id="drawing-a", backend="com",
                       window_mode="virtual_desktop")
project_session_switch(project_id="drawing-a")   # only to change the default
```

Give concurrent drawings different `project_id` values. Pass `project_id` on
an existing tool call to route it explicitly; calls without it use the active
project. Each project has its own backend, STA executor, document state,
asyncio lock, staging state and dialog watchdog. One project's call never
shares another project's lock or COM Application proxy.

`project_session_list()` reports the project IDs, owned PIDs, window modes,
staging and watchdog events. `project_session_close(project_id=...)` closes
only that session and its agent-owned AutoCAD process. A PID or HWND that is
not registered as agent-owned is protected, including at cleanup.

Windows are placed on the configured Virtual Desktop when available, with
`bottom`, `offscreen` and `hidden` fallbacks. Screenshots use
`PrintWindow(..., PW_RENDERFULLCONTENT)` so occlusion does not decide what the
agent sees. A failed or blank bound-document capture falls back to a temporary
DXF snapshot and headless rendering; it does not capture another application's
window.

## 2. Existing DWG: stage before any edit

```text
project_session_create(project_id="drawing-a", backend="com",
                       source_dwg="C:/work/source.dwg", use_staging=True)
# Or, on an existing session:
project_stage_dwg(project_id="drawing-a", source_dwg_path="C:/work/source.dwg")
```

Edit only the returned `staging.staged_dwg_path`. The source stays untouched.
The source directory is added to the project search path so relative XREFs
still resolve; references are not rewritten to point into the staging folder.

The safe sequence is:

```text
stage -> drawing_preflight -> geometry -> drawing_verify_requirements
      -> drawing_critique(focus=None) -> project_diff_staged
      -> project_commit_staged -> drawing_deliver
```

Use the measured-drafting gates in the main skill. A critique or verification
that cannot complete is not a pass.

## 3. Diff and commit deliberately

`project_diff_staged(project_id=...)` compares the untouched source with the
shadow copy. Review its changed, added and removed entities before publishing.

`project_commit_staged(project_id=..., output_path=...)` saves the open staged
document and atomically copies it to the output. With no output path the
default is `<source-stem>_AI_v2.dwg` beside the source. Source overwrite is
refused unless `overwrite_if_unlocked=true`, and even then a `.dwl` / `.dwl2`
sidecar or Windows write lock refuses the commit. A separate locked output is
also refused.

A commit is not the delivery check: `drawing_deliver(requirements=...)` still
has to export, reopen and measure the saved artifact.

## 4. Heavy batch work

Prefer `project_run_coreconsole(project_id=..., dwg_path=...,
script_or_lisp=..., timeout=...)` for heavy AutoLISP, audit, purge and plot
batches. It runs `accoreconsole.exe` asynchronously with no window. Staging
follows `STAGING_MODE`; inspect the returned `staging` and stdout/stderr, and
verify the resulting file rather than treating exit code zero as geometric
acceptance.

The modal-dialog watchdog reads and closes only `#32770` dialogs belonging to
the session's agent-owned PID. Protected or unknown processes are skipped.
Never broaden it to dismiss the operator's dialogs.
