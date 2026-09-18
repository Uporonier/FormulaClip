$ErrorActionPreference = "Stop"
$pythonRoot = (Get-Command python).Source | Split-Path
$tclRoot = Join-Path $pythonRoot "tcl"
$tkinterRoot = Join-Path $pythonRoot "Lib\tkinter"
$dllRoot = Join-Path $pythonRoot "DLLs"
$out = Join-Path $PSScriptRoot "release-dist\FormulaClip"

& "$PSScriptRoot\.venv\Scripts\pyinstaller.exe" --noconfirm --clean --windowed --name FormulaClip `
  --icon "$PSScriptRoot\assets\formulaclip-icon.ico" `
  --hidden-import tkinter --hidden-import tkinter.ttk `
  --add-data "$PSScriptRoot\assets;assets" `
  --add-data "$tclRoot;tcl" `
  --add-binary "$dllRoot\tk86t.dll;." --add-binary "$dllRoot\tcl86t.dll;." `
  --add-binary "$dllRoot\_tkinter.pyd;." `
  --distpath "$PSScriptRoot\release-dist" --workpath "$PSScriptRoot\release-build" `
  --specpath "$PSScriptRoot\release-spec" "$PSScriptRoot\main.py"

# Some Python distributions expose tkinter outside the virtual environment;
# copy the standard-library package beside the executable as a final fallback.
Copy-Item -Recurse -Force $tkinterRoot (Join-Path $out "_internal\tkinter")
Copy-Item -Force (Join-Path $dllRoot "_tkinter.pyd") (Join-Path $out "_internal\_tkinter.pyd")
Write-Host "Built $out\FormulaClip.exe"
