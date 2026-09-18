param([int]$Limit = 40)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$smokeRoot = Split-Path -Parent $PSScriptRoot
$smokePlan = Get-Content -LiteralPath (Join-Path $smokeRoot 'fixtures\test-plan.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$smokeAudio = Join-Path $smokeRoot 'fixtures\audio\synthetic'
New-Item -ItemType Directory -Path $smokeAudio -Force | Out-Null
$smokeSpeech = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $smokeVoice = $smokeSpeech.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -eq 'ja-JP' } | Select-Object -First 1
    if (-not $smokeVoice) { throw 'No installed Japanese local voice.' }
    $smokeSpeech.SelectVoice($smokeVoice.VoiceInfo.Name)
    $smokeFormat = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
    foreach ($smokeTest in ($smokePlan.tests | Select-Object -First $Limit)) {
        $smokePath = Join-Path $smokeAudio ($smokeTest.test_id + '.wav')
        if (Test-Path -LiteralPath $smokePath) { continue }
        $smokeSpeech.SetOutputToWaveFile($smokePath, $smokeFormat)
        $smokeSpeech.Speak($smokeTest.reference_text)
        $smokeSpeech.SetOutputToNull()
    }
} finally { $smokeSpeech.Dispose() }
Write-Output 'Local synthetic fixtures generated; human accuracy remains unmeasured.'
