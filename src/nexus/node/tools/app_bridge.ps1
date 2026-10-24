$ErrorActionPreference = 'Stop'
[Console]::InputEncoding = [Text.UTF8Encoding]::new($false)
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
$request = [Console]::In.ReadToEnd() | ConvertFrom-Json
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$processes = @(Get-Process -Name ([IO.Path]::GetFileNameWithoutExtension($request.executable)) -ErrorAction SilentlyContinue | Where-Object {
    try { $_.MainModule.FileName -eq $request.executable -and $_.MainWindowHandle -ne 0 } catch { $false }
})
if ($processes.Count -ne 1) { throw 'Exactly one configured running application required' }
$process = $processes[0]
if ((Get-FileHash -LiteralPath $request.executable -Algorithm SHA256).Hash.ToLowerInvariant() -ne $request.executable_sha256) { throw 'Application binary changed' }
$window = [System.Windows.Automation.AutomationElement]::FromHandle($process.MainWindowHandle)

function Read-State {
    $controls = @()
    foreach ($id in $request.automation_ids) {
        $condition = [System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::AutomationIdProperty, $id)
        $elements = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $condition)
        if ($elements.Count -ne 1) { throw 'Unique configured control required' }
        $element = $elements[0]
        if ($element.Current.ProcessId -ne $process.Id -or $element.Current.IsPassword) { throw 'Sensitive or foreign control' }
        $value = $null
        $pattern = $null
        if ($element.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern,[ref]$pattern)) {
            $value = $pattern.Current.Value
            if ($value.Length -gt 256) { $value = $value.Substring(0,256) }
        }
        $name = $element.Current.Name
        if ($name.Length -gt 200) { $name = $name.Substring(0,200) }
        $controls += [ordered]@{automation_id=$id;name=$name;value=$value;enabled=$element.Current.IsEnabled;offscreen=$element.Current.IsOffscreen}
    }
    return ([ordered]@{process_id=$process.Id;window_handle=$process.MainWindowHandle.ToInt64();controls=$controls} | ConvertTo-Json -Depth 8 -Compress)
}

$before = Read-State
if ($null -ne $request.action) {
    $sha = [Security.Cryptography.SHA256]::Create()
    try { $digest = ([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($before)))).Replace('-','').ToLowerInvariant() } finally { $sha.Dispose() }
    if ($digest -ne $request.expected_state_sha256) { throw 'Application state changed; observe again' }
    $condition = [System.Windows.Automation.PropertyCondition]::new([System.Windows.Automation.AutomationElement]::AutomationIdProperty, $request.action.automation_id)
    $elements = $window.FindAll([System.Windows.Automation.TreeScope]::Descendants,$condition)
    if ($elements.Count -ne 1) { throw 'Unique configured control required' }
    $element = $elements[0]
    if ($element.Current.ProcessId -ne $process.Id -or $element.Current.IsPassword -or -not $element.Current.IsEnabled -or $element.Current.IsOffscreen) { throw 'Control unavailable' }
    switch ($request.action.kind) {
        'focus' { $element.SetFocus() }
        'invoke' { $element.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke() }
        'set_value' { $element.GetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern).SetValue($request.value) }
        default { throw 'Unsupported configured action' }
    }
}
$after = Read-State
[ordered]@{before=$before;after=$after} | ConvertTo-Json -Depth 4 -Compress
