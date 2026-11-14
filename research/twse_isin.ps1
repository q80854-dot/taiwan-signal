# 證交所 ISIN 公開清單：上市、上櫃、興櫃各一頁（在台灣的個人電腦執行；Windows 內建 PowerShell，不需安裝任何東西）
# 用途：取得每檔股票的「上市日／上櫃日」，把 FinMind 股價中屬於興櫃期間的資料切掉，並可當作「上市多久」的特徵。
# 雲端主機會被擋（2026-11-14 實測：顯示「因為安全性考量，您所執行的頁面無法呈現」），所以在個人電腦執行。
# 每頁原樣保存（不轉碼、不修改）並記錄網址、時間、SHA-256；不是清單頁的記為失敗，不算成功。約 10 秒。
# 跑完會產生 isin_raw.zip，請上傳到 Google Drive「Taiwan Signal 研究資料庫」資料夾。
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Raw = Join-Path $Here "isin_raw"
New-Item -ItemType Directory -Force -Path $Raw | Out-Null
$Man = Join-Path $Raw "manifest.jsonl"
$Pages = [ordered]@{ "listed_twse" = 2; "listed_tpex" = 4; "emerging" = 5 }

foreach ($name in $Pages.Keys) {
    $u = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=$($Pages[$name])"
    $out = Join-Path $Raw "$name.html"
    $rec = [ordered]@{ name = $name; url = $u; fetched_utc = (Get-Date).ToUniversalTime().ToString("o"); source = "TWSE ISIN official"; ok = $false }
    try {
        $r = Invoke-WebRequest -Uri $u -UseBasicParsing -TimeoutSec 120 -Headers @{ "User-Agent" = "Mozilla/5.0" }
        $bytes = $r.RawContentStream.ToArray()
        $rec.bytes = $bytes.Length
        $rec.sha256 = [System.BitConverter]::ToString([System.Security.Cryptography.SHA256]::Create().ComputeHash($bytes)).Replace("-", "").ToLower()
        $text = [System.Text.Encoding]::GetEncoding(950).GetString($bytes)
        if ($text -like "*國際證券辨識號碼*" -and $bytes.Length -gt 50000) {
            [System.IO.File]::WriteAllBytes($out, $bytes)
            $rec.ok = $true
        } else {
            $rec.error = "不是清單頁（可能被擋或網站改版）"
        }
    } catch {
        $rec.error = "$($_.Exception.Message)".Substring(0, [Math]::Min(200, "$($_.Exception.Message)".Length))
    }
    ($rec | ConvertTo-Json -Compress) | Out-File -FilePath $Man -Append -Encoding utf8
    if ($rec.ok) { Write-Host "OK   $name（$($rec.bytes) bytes）" } else { Write-Host "失敗 $name：$($rec.error)" }
    Start-Sleep -Seconds 4
}
$zip = Join-Path $Here "isin_raw.zip"
if (Test-Path $zip) { Remove-Item $zip }
Compress-Archive -Path (Join-Path $Raw "*") -DestinationPath $zip
Write-Host "完成：$zip"
