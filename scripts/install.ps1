# Graph-MIND, installed in one step on Windows: no Python needed beforehand.
#
#   irm https://raw.githubusercontent.com/goyohan0611-png/graph-mind/main/scripts/install.ps1 | iex
#
# It installs uv (Astral's installer, to your user folder, no administrator rights), which
# brings its own Python 3.12; installs Graph-MIND with it; then connects your AI apps
# (graph-mind-install). Arguments after the script go to graph-mind-install, e.g. --name 고요한
# or --join <code>. Set GRAPH_MIND_VERSION to install one version (CI does).
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Write-Host "Graph-MIND: installing uv (it brings Python)..."
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
    $env:Path = "$env:USERPROFILE\.local\bin;$env:Path"
}

$spec = if ($env:GRAPH_MIND_VERSION) { "graph-mind-memory==$env:GRAPH_MIND_VERSION" } else { "graph-mind-memory" }
Write-Host "Graph-MIND: installing $spec..."
uv tool install --python 3.12 --upgrade $spec
if ($LASTEXITCODE -ne 0) { throw "uv could not install $spec" }

$bin = (uv tool dir --bin).Trim()
& (Join-Path $bin "graph-mind-install.exe") @args
if ($LASTEXITCODE -ne 0) { throw "graph-mind-install failed" }
