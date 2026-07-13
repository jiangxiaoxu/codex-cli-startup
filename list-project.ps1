[CmdletBinding()]
param(
    [Parameter(Position = 0, ValueFromRemainingArguments = $true)]
    [string[]]$Project
)

$selectorExePath = Join-Path $PSScriptRoot "list-project.exe"
if (Test-Path -LiteralPath $selectorExePath -PathType Leaf) {
    $selectorCommand = $selectorExePath
    $selectorArguments = @()
}
else {
    $pythonPath = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
    if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
        $pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
        if ($null -eq $pythonCommand) {
            Write-Error "Neither list-project.exe nor Python was found."
            return
        }
        $pythonPath = $pythonCommand.Source
    }

    $selectorCommand = $pythonPath
    $selectorPath = Join-Path $PSScriptRoot "list_project.py"
    $selectorArguments = @($selectorPath)
}
$resultPath = [System.IO.Path]::GetTempFileName()
$selectorArguments += @("--output-file", $resultPath)
if ($Project.Count -gt 0) {
    $selectorArguments += "--"
    $selectorArguments += $Project -join " "
}

try {
    & $selectorCommand @selectorArguments
    if ($LASTEXITCODE -ne 0) {
        return
    }
    $selectedPaths = @(Get-Content -LiteralPath $resultPath -Encoding utf8)
    if ($selectedPaths.Count -ne 1 -or [string]::IsNullOrWhiteSpace($selectedPaths[0])) {
        Write-Error "The project selector did not return exactly one path."
        return
    }
}
finally {
    Remove-Item -LiteralPath $resultPath -Force -ErrorAction SilentlyContinue
}

Set-Location -LiteralPath $selectedPaths[0]
