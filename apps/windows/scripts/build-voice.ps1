param(
  [string]$Python = ".venv\Scripts\python.exe",
  [string]$WakeModelDir = "models\kws",
  [string]$OutputDir = "apps\windows\src-tauri\binaries"
)

$ErrorActionPreference = "Stop"
$voiceRoot = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
$pythonPath = (Resolve-Path (Join-Path $voiceRoot $Python)).Path
$outPath = Join-Path $voiceRoot $OutputDir
$modelSource = (Resolve-Path (Join-Path $voiceRoot $WakeModelDir)).Path
$modelTarget = Join-Path $outPath "voice-models"
New-Item -ItemType Directory -Force -Path (Join-Path $modelTarget "kws") | Out-Null
$lock = Get-Content -LiteralPath (Join-Path $voiceRoot "config\voice-models.lock.json") -Raw | ConvertFrom-Json
$kws = $lock.models | Where-Object { $_.kind -eq "kws" }
foreach ($file in $kws.files) {
  $source = Join-Path $modelSource ([IO.Path]::GetFileName($file.path))
  if ((Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant() -ne $file.sha256) {
    throw "Wake model digest mismatch: $source"
  }
  Copy-Item -LiteralPath $source -Destination (Join-Path $modelTarget $file.path)
}
Copy-Item -LiteralPath (Join-Path $voiceRoot "config\voice-models.lock.json") -Destination $modelTarget
Copy-Item -LiteralPath (Join-Path $voiceRoot "config\voice-keywords.example.txt") -Destination (Join-Path $modelTarget "keywords.txt")

$arguments = @(
  "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--name", "nexus-voice",
  "--paths", (Join-Path $voiceRoot "src"),
  "--collect-all", "sherpa_onnx", "--collect-all", "pywebrtc_audio", "--collect-all", "samplerate",
  "--collect-all", "numpy", "--collect-binaries", "sounddevice", "--collect-data", "sounddevice",
  "--hidden-import", "nexus.voice", "--hidden-import", "nexus.contracts.voice",
  "--distpath", $outPath, "--workpath", (Join-Path $voiceRoot "var\voice-completion\build"),
  "--specpath", (Join-Path $voiceRoot "var\voice-completion\build")
)
$site = & $pythonPath -c "import sysconfig; print(sysconfig.get_path('purelib'))"
if ($LASTEXITCODE -ne 0) { throw "Cannot locate build environment" }
if ($site) {
  foreach ($libs in @("numpy.libs", "pywebrtc_audio.libs")) {
    $directory = Join-Path $site $libs
    if (Test-Path -LiteralPath $directory) {
      $arguments += @("--add-binary", "$directory;$libs")
    }
  }
}
$arguments += (Join-Path $voiceRoot "src\nexus\voice\__main__.py")
Push-Location $voiceRoot
try {
  & $pythonPath @arguments
  if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with $LASTEXITCODE" }
  Write-Host "Built: $(Join-Path $outPath 'nexus-voice.exe')"
}
finally { Pop-Location }
