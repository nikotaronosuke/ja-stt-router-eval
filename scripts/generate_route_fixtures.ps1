$ErrorActionPreference = 'Stop'
# Generate five non-personal Japanese clips with the local Windows voice for the
# audio-path probe (stt_eval.route_probe). Nothing is played, recorded or sent.
$routeSpeech = $null
$routeStage = 'assembly'
try {
    Add-Type -AssemblyName System.Speech
    $routeRoot = Split-Path -Parent $PSScriptRoot
    $routeAudio = Join-Path $routeRoot 'fixtures\audio\route-source'
    $routeManifest = Join-Path $routeRoot 'fixtures\manifests\route-fixtures.json'
    if ((Test-Path -LiteralPath $routeAudio) -or (Test-Path -LiteralPath $routeManifest)) { throw 'exists' }
    $routeTexts = @(
        @('図書館の開館時間は午前九時です。','図書館'),
        @('明日の天気予報では午後から雨が降ります。','天気予報'),
        @('在庫一覧を確認して、足りない商品を注文してください。','在庫一覧'),
        @('SharePointに保存した資料の更新日時を確認してください。','SharePoint'),
        @('WordPressの更新が完了したら画面を確認してください。','WordPress')
    )
    $routeStage = 'local_voice'
    $routeSpeech = New-Object System.Speech.Synthesis.SpeechSynthesizer
    $routeVoice = $routeSpeech.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -eq 'ja-JP' } | Select-Object -First 1
    if (-not $routeVoice) { throw 'voice_missing' }
    $routeSpeech.SelectVoice($routeVoice.VoiceInfo.Name)
    $routeFormat = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(16000, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)
    $routeStage = 'synthesis'
    New-Item -ItemType Directory -Path $routeAudio | Out-Null
    $routeTests = @()
    for ($routeIndex = 0; $routeIndex -lt $routeTexts.Count; $routeIndex++) {
        $routeId = 'route_{0:D2}' -f ($routeIndex + 1)
        $routePath = Join-Path $routeAudio ($routeId + '.wav')
        $routeSpeech.SetOutputToWaveFile($routePath, $routeFormat)
        $routeSpeech.Speak($routeTexts[$routeIndex][0])
        $routeSpeech.SetOutputToNull()
        $routeTests += @{test_id = $routeId; condition = 'tts_route'; audio_kind = 'synthetic_smoke';
            reference_text = $routeTexts[$routeIndex][0]; keywords = @($routeTexts[$routeIndex][1]);
            proper_nouns = @(); decision_keywords = @($routeTexts[$routeIndex][1]);
            audio = ('../audio/route-source/' + $routeId + '.wav'); reference_review = 'synthetic_text';
            source = 'nonpersonal_local_windows_tts'; external_transmission_approved = $false}
    }
    $routeJson = @{schema_version = 1; tests = $routeTests} | ConvertTo-Json -Depth 6
    [System.IO.File]::WriteAllText($routeManifest, $routeJson, [System.Text.UTF8Encoding]::new($false))
    Write-Output '{"status":"generated","count":5,"played":false,"recorded":false,"sent":false}'
} catch {
    Write-Output ('{"status":"generation_failed","stage":"' + $routeStage + '","details":"withheld"}')
    exit 1
} finally {
    if ($routeSpeech) { $routeSpeech.Dispose() }
}
