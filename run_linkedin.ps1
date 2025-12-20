$ErrorActionPreference = "Stop"

Set-Location "C:\Users\wolfx\Desktop\Auto Apply\linkedIn_auto_jobs_applier_with_AI"

# Start your command
$proc = Start-Process -FilePath "python" -ArgumentList @(
  "main.py", "--resume", "resume.pdf"
) -PassThru

try {
  # Wait up to 3 hours (10800 seconds)
  Wait-Process -Id $proc.Id -Timeout 10800 -ErrorAction Stop
}
catch {
  # If still running after timeout, kill it
  if (-not $proc.HasExited) {
    Stop-Process -Id $proc.Id -Force
  }
}

#schtasks --% /Create /F /SC DAILY /ST 05:35 /TN "LinkedIn_AutoApply" /TR "C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"C:\Users\wolfx\Desktop\Auto Apply\linkedIn_auto_jobs_applier_with_AI\run_linkedin.ps1\""

