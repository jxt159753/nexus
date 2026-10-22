$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..\..")).Path
Set-Location $Root
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw "Missing .venv Python: $Python" }

# The bundled Node must include the reviewed optional image decoder. Do not
# silently build a Node that reports read_image but fails at runtime.
& $Python -c "import PIL; assert PIL.__version__ == '13.0.0', 'Pillow 13.0.0 required'"
if ($LASTEXITCODE -ne 0) {
  throw "Bundled image decoder unavailable or not the pinned version. Install requirements-images.lock into .venv (Pillow==13.0.0)."
}

& $Python -m PyInstaller --noconfirm --clean --onefile `
  --name nexus-node `
  --distpath (Join-Path $Root "apps\windows\src-tauri\binaries") `
  --workpath (Join-Path $Root "var\build\pyinstaller") `
  --specpath (Join-Path $Root "var\build\pyinstaller") `
  --paths (Join-Path $Root "src") `
  --add-data "$Root\src\nexus\node\migrations;nexus/node/migrations" `
  --add-data "$Root\src\nexus\node\tools;nexus/node/tools" `
  --hidden-import aiosqlite `
  --hidden-import websockets `
  --hidden-import pydantic `
  --hidden-import PIL.Image `
  --hidden-import PIL.ImageOps `
  --hidden-import PIL.ImageFile `
  --hidden-import PIL.PngImagePlugin `
  --hidden-import PIL.JpegImagePlugin `
  --hidden-import PIL.GifImagePlugin `
  --hidden-import PIL.WebPImagePlugin `
  --collect-all winpty `
  --collect-submodules nexus.plugins `
  (Join-Path $Root "apps\windows\node\entry.py")

if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed: $LASTEXITCODE" }
Write-Host "Built: $Root\apps\windows\src-tauri\binaries\nexus-node.exe"
