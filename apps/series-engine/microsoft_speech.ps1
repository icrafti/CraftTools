param([Parameter(Mandatory = $true)][string]$Request)
$ErrorActionPreference = "Stop"

Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.SpeechSynthesis.SpeechSynthesizer,Windows.Media.SpeechSynthesis,ContentType=WindowsRuntime]
$streamType = [Windows.Media.SpeechSynthesis.SpeechSynthesisStream,Windows.Media.SpeechSynthesis,ContentType=WindowsRuntime]
$taskMethod = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq "AsTask" -and $_.IsGenericMethodDefinition -and
    $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
$streamTask = $taskMethod.MakeGenericMethod($streamType)
$rows = Get-Content -LiteralPath $Request -Encoding UTF8 -Raw | ConvertFrom-Json
$synth = New-Object Windows.Media.SpeechSynthesis.SpeechSynthesizer
try {
    foreach ($row in $rows) {
        $voice = [Windows.Media.SpeechSynthesis.SpeechSynthesizer]::AllVoices | Where-Object {
            $_.Language -eq $row.language -and
            $(if ($row.voice) { $_.DisplayName -eq $row.voice -or $_.Id -eq $row.voice }
              else { $_.Gender -eq $row.gender })
        } | Select-Object -First 1
        if (!$voice) {
            throw "No installed Microsoft voice for $($row.language), $($row.gender), $($row.voice)"
        }
        $synth.Voice = $voice
        $synth.Options.SpeakingRate = [double]$row.rate
        $task = $streamTask.Invoke($null, @($synth.SynthesizeTextToStreamAsync($row.text)))
        $speech = $task.GetAwaiter().GetResult()
        try {
            $read = [System.IO.WindowsRuntimeStreamExtensions]::AsStreamForRead($speech)
            try {
                $write = [System.IO.File]::Create($row.target)
                try { $read.CopyTo($write) }
                finally { $write.Dispose() }
            }
            finally { $read.Dispose() }
        }
        finally { $speech.Dispose() }
        Write-Output $voice.DisplayName
    }
}
finally { $synth.Dispose() }
