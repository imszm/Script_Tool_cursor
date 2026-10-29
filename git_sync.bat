@echo off
setlocal EnableExtensions EnableDelayedExpansion

rem Field sync for several test PCs sharing this repo.
rem config\local.yaml, logs\ and reports\ stay on this machine and are never staged.
rem If a pull/rebase conflicts, it is aborted so the worktree is not left locked.

cd /d "%~dp0"
>nul 2>&1 chcp 65001
set "GIT_TERMINAL_PROMPT=0"
set "GIT_MERGE_AUTOEDIT=no"
set "RC=0"
title Git Auto-Sync Tool

echo ===========================================
echo       Git Repository Auto-Sync Tool
echo ===========================================
echo.

git --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Git not found. Install Git and add it to PATH.
    goto :fail
)

git rev-parse --is-inside-work-tree >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Not a Git repository. Run this script from the project root.
    goto :fail
)

call :clear_stale_lock
if errorlevel 1 goto :fail

call :abort_in_progress
if errorlevel 1 goto :fail

for /f "delims=" %%b in ('git rev-parse --abbrev-ref HEAD') do set "BRANCH=%%b"
if "!BRANCH!"=="HEAD" (
    echo [ERROR] Detached HEAD. Check out a branch before syncing.
    goto :fail
)
echo [OK]    Branch: !BRANCH!

call :ensure_gitignore
call :protect_local_overrides

echo.
echo [1/4] Repository status:
git status --short
echo.

call :stage_shareable
if errorlevel 1 goto :fail

git diff --cached --quiet
if errorlevel 1 (
    echo [2/4] Staged file summary:
    git --no-pager diff --cached --stat
    echo.
    set "msg="
    set /p "msg=Enter commit message (Press Enter for auto-generated): "
    if "!msg!"=="" (
        for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmm"') do set "TIMESTAMP=%%i"
        set "msg=Routine_update_!TIMESTAMP!"
    )
    echo.
    echo [3/4] Committing: "!msg!"
    git commit -m "!msg!"
    if errorlevel 1 (
        echo [ERROR] Commit failed. No merge or rebase was left in progress.
        goto :fail
    )
) else (
    echo [2/4] No shareable changes to commit.
    echo        config\local.yaml, logs\ and reports\ stay on this PC.
    echo [3/4] Skip commit.
)

echo.
echo [4/4] Updating from origin/!BRANCH! and pushing...
call :sync_remote
if errorlevel 1 goto :fail

echo.
echo ===========================================
echo       SUCCESS: Synced with origin/!BRANCH!.
echo ===========================================
goto :finish

:clear_stale_lock
if not exist ".git\index.lock" exit /b 0
tasklist /FI "IMAGENAME eq git.exe" 2>nul | find /I "git.exe" >nul
if not errorlevel 1 (
    echo [ERROR] Another git.exe holds .git\index.lock.
    echo         Close that window and run this script again.
    exit /b 1
)
echo [WARN]  Removing stale .git\index.lock
del /f /q ".git\index.lock" >nul 2>&1
if exist ".git\index.lock" (
    echo [ERROR] Could not remove .git\index.lock.
    exit /b 1
)
exit /b 0

:abort_in_progress
set "STUCK=0"
if exist ".git\MERGE_HEAD" set "STUCK=1"
if exist ".git\rebase-merge" set "STUCK=1"
if exist ".git\rebase-apply" set "STUCK=1"
if exist ".git\CHERRY_PICK_HEAD" set "STUCK=1"
if exist ".git\REVERT_HEAD" set "STUCK=1"
if "!STUCK!"=="0" exit /b 0

echo [WARN]  A previous merge, rebase, cherry-pick, or revert is still in progress.
echo         Aborting it so this PC is not left conflict-locked.
if exist ".git\MERGE_HEAD" git merge --abort
if exist ".git\rebase-merge" git rebase --abort
if exist ".git\rebase-apply" git rebase --abort
if exist ".git\CHERRY_PICK_HEAD" git cherry-pick --abort
if exist ".git\REVERT_HEAD" git revert --abort
if exist ".git\MERGE_HEAD" (
    echo [ERROR] Could not abort the in-progress merge.
    exit /b 1
)
if exist ".git\rebase-merge" (
    echo [ERROR] Could not abort the in-progress rebase.
    exit /b 1
)
if exist ".git\rebase-apply" (
    echo [ERROR] Could not abort the in-progress rebase.
    exit /b 1
)
if exist ".git\CHERRY_PICK_HEAD" (
    echo [ERROR] Could not abort the in-progress cherry-pick.
    exit /b 1
)
if exist ".git\REVERT_HEAD" (
    echo [ERROR] Could not abort the in-progress revert.
    exit /b 1
)
echo [OK]    Worktree unlocked.
exit /b 0

:ensure_gitignore
if not exist ".gitignore" (
    echo [WARN]  .gitignore is missing. Writing local-override and test-output rules.
    (
        echo /logs/
        echo /reports/
        echo config/local.yaml
    ) > ".gitignore"
)
set "GI_CHANGED=0"
call :ensure_ignore_line "/logs/"
call :ensure_ignore_line "/reports/"
call :ensure_ignore_line "config/local.yaml"
if "!GI_CHANGED!"=="1" (
    echo [OK]    .gitignore now ignores /logs/, /reports/ and config/local.yaml.
) else (
    echo [OK]    .gitignore already ignores local.yaml and test output.
)
exit /b 0

:ensure_ignore_line
rem %~1 is matched as a whole line so a comment cannot satisfy the check.
findstr /x /l /c:"%~1" ".gitignore" >nul 2>&1
if errorlevel 1 (
    >>".gitignore" echo(%~1
    set "GI_CHANGED=1"
    echo [WARN]  Added missing .gitignore rule: %~1
)
exit /b 0

:protect_local_overrides
rem If this clone still tracks local.yaml, keep the on-disk COM ports out of the commit.
rem skip-worktree does not delete the file from the repo, so other PCs will not conflict on pull.
git ls-files -- "config/local.yaml" | findstr /r /c:"." >nul 2>&1
if errorlevel 1 exit /b 0
git update-index --skip-worktree -- "config/local.yaml"
if errorlevel 1 (
    echo [WARN]  Could not mark config/local.yaml skip-worktree. It will still be unstaged.
    exit /b 0
)
echo [OK]    Tracked config/local.yaml will not be committed from this PC.
exit /b 0

:stage_shareable
rem Do not pass local.yaml or logs as pathspecs. Git returns 1 when a named
rem path is ignored, which would abort a healthy sync on every test PC.
git add --all -- .
if errorlevel 1 (
    echo [ERROR] git add failed.
    exit /b 1
)
git restore --staged --quiet -- "config/local.yaml" "logs" "reports" >nul 2>&1
exit /b 0

:sync_remote
git fetch origin
if errorlevel 1 (
    echo [ERROR] git fetch failed. No merge or rebase was started.
    exit /b 1
)

call :rebase_onto_origin
if errorlevel 1 exit /b 1

git push origin HEAD
if not errorlevel 1 exit /b 0

echo [WARN]  Push was rejected. Fetching and rebasing once more.
git fetch origin
if errorlevel 1 (
    echo [ERROR] Fetch failed after the push was rejected.
    exit /b 1
)
call :rebase_onto_origin
if errorlevel 1 exit /b 1

git push origin HEAD
if errorlevel 1 (
    echo [ERROR] Push failed. Check the network, credentials, or branch protection.
    echo         The worktree was not left in a merge conflict.
    exit /b 1
)
exit /b 0

:rebase_onto_origin
git rev-parse --verify --quiet "refs/remotes/origin/!BRANCH!" >nul 2>&1
if errorlevel 1 (
    echo [WARN]  origin/!BRANCH! does not exist yet. Skipping rebase.
    exit /b 0
)
git pull --rebase --autostash origin "!BRANCH!"
if not errorlevel 1 exit /b 0

echo [ERROR] Rebase failed. Aborting so this PC is not left conflict-locked.
git rebase --abort >nul 2>&1
if exist ".git\MERGE_HEAD" git merge --abort >nul 2>&1
if exist ".git\rebase-merge" (
    echo [ERROR] Rebase is still in progress. Resolve it before the next sync.
    exit /b 1
)
if exist ".git\rebase-apply" (
    echo [ERROR] Rebase is still in progress. Resolve it before the next sync.
    exit /b 1
)
echo [OK]    Rebase aborted. Worktree is unlocked.
exit /b 1

:fail
set "RC=1"

:finish
echo.
echo Press any key to exit...
pause >nul
endlocal & exit /b %RC%
