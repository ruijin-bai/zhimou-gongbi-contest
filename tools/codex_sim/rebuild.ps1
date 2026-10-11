$ErrorActionPreference = 'Stop'
Set-Location (Resolve-Path (Join-Path $PSScriptRoot '../..'))
python -X utf8 tools/codex_sim/generate.py
if ($LASTEXITCODE -ne 0) { throw '仿真生成失败，查看控制台；仅操作 data_codex。' }
Write-Host '已重建独立仿真。启动：python -X utf8 tools/codex_sim/serve.py'
