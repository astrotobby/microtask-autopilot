# Sign in to Claude Code with your own Claude account, for this window only.
# Windows has machine-wide ANTHROPIC_* variables that point Claude Code at Google's Gemini endpoint.
# They are cleared ONLY inside this PowerShell process; nothing system-wide is changed.
foreach ($name in 'ANTHROPIC_BASE_URL', 'ANTHROPIC_API_KEY', 'ANTHROPIC_MODEL', 'ANTHROPIC_AUTH_TOKEN') {
    Remove-Item -Path "Env:$name" -ErrorAction SilentlyContinue
}
Get-ChildItem Env: | Where-Object { $_.Name -like 'CLAUDE_CODE_*' -or $_.Name -eq 'CLAUDECODE' } | ForEach-Object { Remove-Item -Path ("Env:" + $_.Name) -ErrorAction SilentlyContinue }
Write-Host "Starting Claude Code sign-in. A browser page will open: approve it there with your Claude account." -ForegroundColor Cyan
claude auth login
Write-Host ""
Write-Host "Sign-in status:" -ForegroundColor Cyan
claude auth status
