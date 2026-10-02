# 用仓库 .venv 的解释器：系统 Python 没装 cv2 等依赖（CI 不走这个脚本，见 build.yml）
$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
Get-ChildItem -Path (Join-Path $PSScriptRoot "tests\*.py") | ForEach-Object {
  Write-Host "Running tests in $($_.FullName)"
  try {
      # Run the Python unittest command
      & $python -m unittest $_.FullName

      # Check if the previous command succeeded
      if ($LASTEXITCODE -ne 0) {
          throw "Tests failed in $($_.FullName)"
      }
  } catch {
      # Stop the loop and return the error
      Write-Error $_
      exit 1
  }
}