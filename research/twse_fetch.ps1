# 證交所（上市）官方歷史資料下載（在台灣的個人電腦執行；Windows 內建 PowerShell，不需安裝任何東西）
# 內容：停牌、減資（每年）、除權息、處置、注意股（每月），2020-01 至今。加參數 -OddLot 另抓盤中零股（每日，約 2 小時）。
# 每筆原樣保存並記錄網址、時間、SHA-256；被擋或不是 JSON 的記為失敗，不算成功。可中斷後重跑接續。
param([switch]$OddLot)
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Raw = Join-Path $Here "twse_raw"
New-Item -ItemType Directory -Force -Path $Raw | Out-Null
$Man = Join-Path $Raw "manifest.jsonl"
$Base = "https://www.twse.com.tw/rwd/zh/"
$Pace = 4
$Today = Get-Date

function Get-One($name, $urls) {
    $out = Join-Path $Raw "$name.json"
    if (Test-Path $out) { return }
    $rec = [ordered]@{ name = $name; fetched_utc = (Get-Date).ToUniversalTime().ToString("o"); source = "TWSE official"; ok = $false }
    foreach ($u in $urls) {
        for ($i = 0; $i -lt 3; $i++) {
            try {
                $r = Invoke-WebRequest -Uri $u -UseBasicParsing -TimeoutSec 60 -Headers @{ "User-Agent" = "Mozilla/5.0" }
                $bytes = $r.RawContentStream.ToArray()
                $text = [System.Text.Encoding]::UTF8.GetString($bytes)
                $d = $text | ConvertFrom-Json
                $sha = [System.BitConverter]::ToString([System.Security.Cryptography.SHA256]::Create().ComputeHash($bytes)).Replace("-", "").ToLower()
                $rec.url = $u; $rec.stat = "$($d.stat)"; $rec.sha256 = $sha
                if ("$($d.stat)".ToUpper() -eq "OK" -or "$($d.stat)" -like "*很抱歉*") {
                    $rec.ok = $true
                    [System.IO.File]::WriteAllBytes($out, $bytes)
                }
                break
            } catch {
                $rec.error = "$($_.Exception.Message)".Substring(0, [Math]::Min(200, "$($_.Exception.Message)".Length))
                Start-Sleep -Seconds ($Pace * 3)
            }
        }
        Start-Sleep -Seconds $Pace
        if ($rec.ok) { break }
    }
    ($rec | ConvertTo-Json -Compress) | Out-File -FilePath $Man -Append -Encoding utf8
    if ($rec.ok) { Write-Host "OK   $name" } else { Write-Host "失敗 $name" }
}

for ($y = 2020; $y -le $Today.Year; $y++) {
    $a = "{0}0101" -f $y
    $b = if ($y -lt $Today.Year) { "{0}1231" -f $y } else { $Today.ToString("yyyyMMdd") }
    Get-One "halt_$y" @("${Base}afterTrading/TWTAWU?startDate=$a&endDate=$b&querytype=3&response=json", "${Base}afterTrading/TWTAWU?startDate=$a&endDate=$b&response=json")
    Get-One "reduction_$y" @("${Base}reducation/TWTAUU?startDate=$a&endDate=$b&response=json")
}
$m = Get-Date -Year 2020 -Month 1 -Day 1
while ($m -le $Today) {
    $e = $m.AddMonths(1).AddDays(-1); if ($e -gt $Today) { $e = $Today }
    $A = $m.ToString("yyyyMMdd"); $B = $e.ToString("yyyyMMdd"); $k = $m.ToString("yyyyMM")
    Get-One "exright_$k" @("${Base}exRight/TWT49U?startDate=$A&endDate=$B&response=json")
    Get-One "punish_$k" @("${Base}announcement/punish?startDate=$A&endDate=$B&response=json")
    Get-One "notice_$k" @("${Base}announcement/notice?startDate=$A&endDate=$B&response=json")
    $m = $m.AddMonths(1)
}
if ($OddLot) {
    $d = Get-Date -Year 2020 -Month 10 -Day 26
    while ($d -le $Today) {
        if ($d.DayOfWeek -ne "Saturday" -and $d.DayOfWeek -ne "Sunday") {
            $D = $d.ToString("yyyyMMdd")
            Get-One "oddlot_$D" @("${Base}afterTrading/TWTC7U?date=$D&response=json")
        }
        $d = $d.AddDays(1)
    }
}
$zip = Join-Path $Here "twse_raw.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path (Join-Path $Raw "*") -DestinationPath $zip
$ok = (Select-String -Path $Man -Pattern '"ok":true').Count
Write-Host "`n完成：成功 $ok 份。檔案：$zip"
