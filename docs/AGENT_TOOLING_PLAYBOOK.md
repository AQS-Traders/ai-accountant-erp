# Agent Tooling Playbook

**Purpose.** This is the field guide for any AI agent (or human) driving this
repository through the Cline tool harness on the Windows workstation. It records
the *concrete* failure modes that repeatedly wasted whole work cycles in this
project, and the exact alternative that worked each time.

Read this **before** your first tool call. Every entry below was observed, not
theorised. Symptoms are quoted from real tool output.

---

## 0. Environment facts (do not rediscover these)

| Fact | Value |
| --- | --- |
| OS shell | Windows, **PowerShell 5.1.19041.6456** (Windows PowerShell, *not* PowerShell 7) |
| Repo path | `E:\Qoder\Ai Accountant\ERP` (**contains a space** - always quote it) |
| Git binary | `E:\Qoder\mingit\cmd\git.exe` (portable MinGit; `git` may not be on PATH) |
| Working dir for tools | `e:\Qoder` (the tool `run_commands` starts here, **not** in the repo) |
| `gh` CLI | **ABSENT** |
| `vercel` CLI | **ABSENT** |
| Secret store | `E:\Qoder\.secrets\` (outside the repo: `tokens.env`, `Load-Secrets.ps1`, `TEST_CREDENTIALS.local.txt`) |
| GitHub | repo `zameerchattha0-ops/ai-accountant-erp`, default branch `main`, visibility **PUBLIC** |
| Vercel | project `ai-accountant-erp`, team `zameerchattha0-ops` |

Because `run_commands` starts in `e:\Qoder`, **every command that touches the
repo must `Set-Location` or pass `-C` to git.** Prefer
`git -C 'E:\Qoder\Ai Accountant\ERP' ...`, which needs no `Set-Location` and
cannot be left in the wrong directory by a previous command.

---

## 1. Failure modes and their fixes

### F1 - PowerShell `>` redirection writes **UTF-16LE**, not UTF-8

**Symptom.** You run `git diff ... > _d.txt` and then `read_files` it. The file
reads back as:

```
  1 | ??d\0i\0f\0f\0 \0-\0-\0g\0i\0t\0 ...
```

Every character is interleaved with a NUL byte. The content is unreadable and
looks like corruption.

**Root cause.** In PowerShell 5.1, `>` / `Out-File` default to
`-Encoding Unicode` (UTF-16LE). `read_files` reads UTF-8.

**Fix (proven).** Never use `>`. Use one of:

```powershell
# Preferred - explicit UTF-8:
$o | Out-File -Encoding utf8 "$env:TEMP\out.txt"

# Also fine:
$o | Set-Content -Encoding utf8 "$env:TEMP\out.txt"

# For a native command, redirect inside the cmdlet:
& $git --no-pager diff HEAD~1 2>&1 | Out-File -Encoding utf8 "$env:TEMP\d.txt"
```

**Recovery if you already produced a UTF-16 file:**
```powershell
Get-Content -Encoding Unicode 'x.txt' | Out-File -Encoding utf8 'x.txt'
```
Better: just re-run the command with `Out-File -Encoding utf8`.

---

### F2 - Long / multi-line commands get **mangled** by the harness

**Symptom.** You send one long command chained with `;`. The terminal shows the
command reflowed and corrupted, with literal `\x3b` where your `;` was, plus a
stray UUID, and the tool reports:

```
[Shell integration did not report command completion ...]
output was captured with a timing heuristic, may be incomplete
```

or

```
The command's output could not be captured through shell integration.
```

**Root cause.** The command string is echoed through a scrollback-poller and
line-wrapped. Above roughly 3-4 wrapped lines, quoting breaks.

**Fix (proven).** Split by complexity:

1. **Short (1-2 wrapped lines):** inline is fine. Keep it under ~200 chars.
2. **Anything with loops, `foreach`, `try/catch`, or more than 3 statements:**
   write it to a **`.ps1` file with the `editor` tool**, then execute:

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass -File 'C:\Users\ZAMEER\AppData\Local\Temp\job.ps1'
   ```

   This is the single highest-value habit in this document. It eliminated the
   mangling entirely.

3. **Never** build long commands by concatenating `$()` inside double quotes.
   Assign to a variable on its own short line, or use a script file.

---

### F3 - Terminal **scrollback pollution** (reading stale output)

**Symptom.** A command's "result" contains output from a command you ran two
turns ago, or contains your own echoed command text repeated several times.

**Root cause.** The harness sometimes falls back to dumping the terminal's
current buffer when shell integration fails. That buffer still holds old output.

**Fix (proven).**
- Never trust a result block that contains your command text echoed back.
- Always write results to a temp file and read them with `read_files`.
  The file only ever contains that command's output.
- Send `"WROTE"` (a short unique token) as the command's own stdout so you can
  confirm the command ran at all.

---

### F4 - Recursive scans of `E:\Qoder` hang or truncate

**Symptom.** `Get-ChildItem 'E:\Qoder' -Recurse -Force -File -Filter '*TOOLING*'`
runs for a long time and the output is unusable.

**Root cause.** The workspace contains `venv/`, `node_modules/`, `.next/` and
`mingit/` - hundreds of thousands of files.

**Fix (proven).** Scope every search:

```powershell
# Good - one directory:
Get-ChildItem 'E:\Qoder\Ai Accountant\ERP\docs' -File | Select-Object Name

# Good - exclude heavy dirs:
Get-ChildItem 'E:\Qoder\Ai Accountant\ERP' -Recurse -File |
  Where-Object { $_.FullName -notmatch '\\(node_modules|\.next|venv|mingit)\\' }

# Best - use the codebase search tool for content, not the shell.
```

---

### F5 - Detached / background processes cannot be observed

**Symptom.** You start something with `Start-Process`, `Start-Job`, or a trailing
`&`, and the tool reports:

```
[Command completion could not be observed; the command may still be running
 and must not be assumed to have succeeded. The terminal has been left open]
```

**Root cause.** The harness waits for the foreground shell to return. A detached
child outlives it.

**Fix (proven).** Run long jobs **in the foreground**, redirect to a file, and
poll the file:

```powershell
# Step 1 - run in foreground, capture:
Set-Location 'E:\Qoder\Ai Accountant\ERP\frontend'
npm run build 2>&1 | Out-File -Encoding utf8 "$env:TEMP\build.txt"
"BUILD_EXIT=$LASTEXITCODE"
```

If the command genuinely exceeds the harness timeout, split the wait across
calls rather than detaching:

```powershell
Start-Sleep -Seconds 45
Get-Content "$env:TEMP\build.txt" -Tail 20
```

Do **not** assume success. Only `BUILD_EXIT=0` plus the expected summary line
counts as success.

---

### F6 - The 300-second ceiling: "proceeded while leaving it running"

**Symptom.**

```
The command was still starting or running after 300 seconds, so Cline
automatically proceeded while leaving it running in the terminal.
This is partial output; further output is being redirected to this file ...
```

**Root cause.** `run_commands` gives up after ~300s. A single command that does
too much (a full `npm ci` + build, a recursive scan, a loop over many files)
blows the budget. **The command may still be running** - a stray child process
now competes with your next command for the same files.

**Fix (proven).**
- Keep each command under ~60s of expected work.
- For anything genuinely long, run it, then `Start-Sleep` and read its output
  file in a *separate* call (this is normal and expected - not a failure).
- Always give each long job a **unique output filename** so a previous run's
  file cannot be mistaken for the current one.
- Never re-run the same long command "to be sure" before checking its file.

---

### F7 - Line-count / tailing mistakes cause false "failure" reports

**Symptom.** You run `npm test` and pipe to `Select-Object -Last 5`, get an
empty or partial block, and conclude the suite failed.

**Root cause.** The pipeline's exit code is not the child process's exit code,
and `Select-Object` on a *live* stream returns before the child finishes.

**Fix (proven).** Always capture the child's own exit code explicitly:

```powershell
npm run build 2>&1 | Out-File -Encoding utf8 "$env:TEMP\build.txt"
"BUILD_EXIT=$LASTEXITCODE"     # <-- the only trustworthy signal
```

Then read `build.txt` with `read_files`. Search the file for `Failed`,
`error`, `Summary`, `Tests` rather than trusting a tail slice.

---

### F8 - PowerShell 5.1 has no `&&`, no ternary, no `??`

**Symptom.** `ParseError: The token '&&' is not a valid statement separator`.

**Root cause.** This is Windows PowerShell 5.1, not PowerShell 7. The modern
operators (`&&`, `||`, `?:`, `??`) do not exist.

**Fix (proven).** Use `;` and explicit checks:

```powershell
npm ci;  if ($LASTEXITCODE -ne 0) { "NPM_CI_FAILED"; return }
npm run build
```

Use `Start-Job` only if you then `Receive-Job` - otherwise see F5.

---

### F9 - Ghost / stale test-output files mislead you

**Symptom.** You conclude "the suite already passes" because you find
`_pytest_out.txt` or `vitest_verify.txt` in the working tree - but those were
written by a **previous session** against **older code**.

**Root cause.** Ad-hoc diagnostic files were written into the repo root and
`.gitignore` covered only *some* of them, so they survived.

**Fix (proven).**
- Write **all** diagnostics to `$env:TEMP`, never into the repo:
  `Out-File -Encoding utf8 "$env:TEMP\whatever.txt"`.
- If you find a stray `_*.txt` / `*_verify*.txt` in the repo, delete it before
  making claims. A test result is only evidence if you produced it *this run*
  against *this* commit.
- The current tree is clean of these. **Keep it that way.**

---

### F10 - Encoding traps: shell vs. tool

**Decision table (memorise this):**

| Target | How to write it | Why |
| --- | --- | --- |
| `.py`, `.ts`, `.tsx`, `.sql`, `.md`, `.json` in the repo | **`editor` tool** | Writes UTF-8, no shell quoting at all |
| Shell output you intend to `read_files` | `Out-File -Encoding utf8` in `$env:TEMP` | `>` would write UTF-16LE (see F1) |
| A PowerShell job script | **`editor` tool** to `$env:TEMP\job.ps1` | Avoids F2 mangling entirely |
| A `.sql` migration | **`editor` tool** | Guarantees LF/UTF-8 and exact bytes |

**Symptom of getting it wrong.** A `.ps1` written via `>` and then executed
fails with `Unexpected token` or mojibake instead of a real error, because the
interpreter saw UTF-16 BOM bytes or a doubled BOM.

**Rule of thumb.** If the shell is not *executing* it, do not use the shell to
*create* it.

---

## 2. Pre-flight checklist (before your first edit)

1. `git -C 'E:\Qoder\Ai Accountant\ERP' status --porcelain --branch`
   -> know your branch, and that the tree is clean (or what is dirty).
2. `. 'E:\Qoder\.secrets\Load-Secrets.ps1'` -> tokens in the environment.
   It prints **names only**; that is expected and safe.
3. Confirm you are on the intended branch. Never work on `main` directly.
4. Confirm the migration number you intend to use is free:
   `Get-ChildItem database\migrations -Name | Sort-Object | Select-Object -Last 3`
5. Run the two fast gates *before* editing so you know the baseline:
   `python -m pytest app\tests -q` and, in `frontend\`, `npx tsc --noEmit`.
   A pre-existing failure must not be mistaken for one you introduced.

## 3. The commands you will actually use

```powershell
# 1. State (short, safe)
git -C 'E:\Qoder\Ai Accountant\ERP' status --porcelain --branch

# 2. Backend suite -> temp file, then read_files
Set-Location 'E:\Qoder\Ai Accountant\ERP'
python -m pytest app\tests -q -p no:warnings 2>&1 | Out-File -Encoding utf8 "$env:TEMP\pytest.txt"
"PYTEST_EXIT=$LASTEXITCODE"

# 3. Frontend gates -> temp files, then read_files
Set-Location 'E:\Qoder\Ai Accountant\ERP\frontend'
npx tsc --noEmit 2>&1 | Out-File -Encoding utf8 "$env:TEMP\fe_tsc.txt";   "TSC=$LASTEXITCODE"
npm run lint     2>&1 | Out-File -Encoding utf8 "$env:TEMP\fe_lint.txt";  "LINT=$LASTEXITCODE"
npm run test     2>&1 | Out-File -Encoding utf8 "$env:TEMP\fe_test.txt";  "TEST=$LASTEXITCODE"
npm run build    2>&1 | Out-File -Encoding utf8 "$env:TEMP\fe_build.txt"; "BUILD=$LASTEXITCODE"
```

Run step 3's four gates as **four separate short commands**, never one chain.

## 4. Never do these

- `git push --force`, `git reset --hard`, branch deletion -> destroys work.
- `git add .` -> use explicit paths; the repo is **public**.
- Commit, echo, or log any token, password, or `.env` value.
- Read `github_pat.raw`, `tokens.env`, `TEST_CREDENTIALS.local.txt` into chat.
  Inspect them as **metadata only** (exists / size / git-ignored / ever-tracked).
- Run destructive SQL (`DROP`, `TRUNCATE`, migration rollback) without approval.

