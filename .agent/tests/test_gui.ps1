param(
    [Parameter(Mandatory=$true)][string]$Executable,
    [Parameter(Mandatory=$true)][string]$InputAudio,
    [Parameter(Mandatory=$true)][string]$OutputDirectory,
    [Parameter(Mandatory=$true)][string]$Preview,
    [switch]$PublicPreview
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()
$assembly = [Reflection.Assembly]::LoadFrom((Resolve-Path -LiteralPath $Executable))
$type = $assembly.GetType('PortableAudioConverter.MainWindow', $true)
$flags = [Reflection.BindingFlags]'Instance,NonPublic'
$form = [Activator]::CreateInstance($type, $true)
try {
    $form.Show()
    $type.GetMethod('SetInput', $flags).Invoke($form, @((Resolve-Path -LiteralPath $InputAudio).Path)) | Out-Null
    $type.GetField('destination', $flags).GetValue($form).Text = (Resolve-Path -LiteralPath $OutputDirectory).Path
    $type.GetField('convert', $flags).GetValue($form).PerformClick()
    $deadline = [DateTime]::UtcNow.AddSeconds(90)
    do {
        [Windows.Forms.Application]::DoEvents()
        Start-Sleep -Milliseconds 25
        $busy = $type.GetField('cancellation', $flags).GetValue($form)
        if ([DateTime]::UtcNow -gt $deadline) { throw 'GUI conversion timed out' }
    } while ($null -ne $busy)
    $output = $type.GetField('lastOutput', $flags).GetValue($form)
    if (!$output -or !(Test-Path -LiteralPath $output)) { throw 'GUI did not produce output' }
    $value = $type.GetField('progress', $flags).GetValue($form).Value
    if ($value -ne 100) { throw "Expected progress 100, got $value" }
    if (!$type.GetField('open', $flags).GetValue($form).Enabled) { throw 'Open-folder button is disabled' }
    if (!$type.GetField('convert', $flags).GetValue($form).Enabled) { throw 'Convert button remains disabled' }
    if ($type.GetField('cancel', $flags).GetValue($form).Enabled) { throw 'Cancel remains enabled after completion' }
    if ($PublicPreview) {
        $type.GetField('input', $flags).GetValue($form).Text = 'C:\Audio\example.wav'
        $type.GetField('destination', $flags).GetValue($form).Text = 'C:\Audio\Converted'
        $type.GetField('status', $flags).GetValue($form).Text = 'Done: C:\Audio\Converted\example.mp3'
    }
    $form.Refresh()
    [Windows.Forms.Application]::DoEvents()
    $bitmap = New-Object Drawing.Bitmap($form.Width, $form.Height)
    try { $form.DrawToBitmap($bitmap, (New-Object Drawing.Rectangle(0,0,$form.Width,$form.Height))); $bitmap.Save($Preview, [Drawing.Imaging.ImageFormat]::Png) }
    finally { $bitmap.Dispose() }
    [pscustomobject]@{ ok=$true; output=$output; progress=$value; preview=$Preview } | ConvertTo-Json -Compress
}
finally { $form.Dispose() }
