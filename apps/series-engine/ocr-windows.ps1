param([Parameter(Mandatory)][string]$Request, [Parameter(Mandatory)][string]$Output)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Globalization.Language,Windows.Foundation,ContentType=WindowsRuntime]
$null = [Windows.Storage.StorageFile,Windows.Storage,ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.IRandomAccessStream,Windows.Storage.Streams,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap,Windows.Graphics.Imaging,ContentType=WindowsRuntime]
$asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
function Await($Operation, [Type]$ResultType) {
    $task = $asTask.MakeGenericMethod($ResultType).Invoke($null, @($Operation))
    return $task.GetAwaiter().GetResult()
}
$english = [Windows.Globalization.Language]::new('en-US')
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage($english)
if (!$engine) { throw 'Windows English OCR is unavailable. Install English OCR in Windows language settings.' }
$results = @{}
foreach ($entry in (Get-Content -Raw -Encoding UTF8 -LiteralPath $Request | ConvertFrom-Json)) {
    $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($entry.path)) ([Windows.Storage.StorageFile])
    $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
    try {
        $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
        $bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
        try {
            $recognized = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
            $results[$entry.id] = @(
                foreach ($line in $recognized.Lines) {
                    if ($line.Text -notmatch '[A-Za-z]') { continue }
                    $boxes = @($line.Words | ForEach-Object { $_.BoundingRect })
                    $left = ($boxes.X | Measure-Object -Minimum).Minimum
                    $top = ($boxes.Y | Measure-Object -Minimum).Minimum
                    $right = ($boxes.Right | Measure-Object -Maximum).Maximum
                    $bottom = ($boxes.Bottom | Measure-Object -Maximum).Maximum
                    @{text=$line.Text; box=@(($left/$bitmap.PixelWidth),($top/$bitmap.PixelHeight),(($right-$left)/$bitmap.PixelWidth),(($bottom-$top)/$bitmap.PixelHeight))}
                }
            )
        } finally { $bitmap.Dispose() }
    } finally { $stream.Dispose() }
}
[IO.File]::WriteAllText($Output, (ConvertTo-Json -InputObject $results -Depth 7), [Text.UTF8Encoding]::new($false))
