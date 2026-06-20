$ErrorActionPreference = "Stop"

function Test-AdapterLocalPort {
    param([int]$Port)

    if ($Port -le 0) {
        return $false
    }

    try {
        $client = New-Object Net.Sockets.TcpClient
        try {
            $async = $client.BeginConnect("127.0.0.1", $Port, $null, $null)
            $ok = $async.AsyncWaitHandle.WaitOne(500, $false)
            if (-not $ok) {
                return $false
            }
            $client.EndConnect($async)
            return $client.Connected
        }
        finally {
            $client.Close()
        }
    }
    catch {
        return $false
    }
}

function ConvertTo-AdapterProxyUrl {
    param([string]$Value)

    if ($null -eq $Value) {
        return $null
    }

    $text = ([string]$Value).Trim()
    if (-not $text) {
        return $null
    }

    if ($text -match ";") {
        $parts = $text -split ";"
        foreach ($name in @("https", "http", "socks")) {
            foreach ($part in $parts) {
                $pair = $part -split "=", 2
                if ($pair.Count -eq 2 -and $pair[0].Trim().ToLowerInvariant() -eq $name) {
                    $text = $pair[1].Trim()
                    break
                }
            }
            if ($text -notmatch ";") {
                break
            }
        }
    }

    if ($text -notmatch "^[a-zA-Z][a-zA-Z0-9+.-]*://") {
        $text = "http://$text"
    }

    return $text
}

function Test-AdapterProxyAvailable {
    param([string]$ProxyUrl)

    $normalized = ConvertTo-AdapterProxyUrl $ProxyUrl
    if (-not $normalized) {
        return $false
    }

    try {
        $uri = [Uri]$normalized
    }
    catch {
        return $false
    }

    if ($uri.Host -in @("127.0.0.1", "localhost", "::1")) {
        return (Test-AdapterLocalPort -Port $uri.Port)
    }

    return $true
}

function Format-AdapterProxyForLog {
    param([string]$ProxyUrl)

    $normalized = ConvertTo-AdapterProxyUrl $ProxyUrl
    if (-not $normalized) {
        return "<empty>"
    }

    try {
        $uri = [Uri]$normalized
        if ($uri.UserInfo) {
            $builder = [UriBuilder]$uri
            $builder.UserName = "***"
            $builder.Password = "***"
            return $builder.Uri.AbsoluteUri
        }
    }
    catch {
        return "<invalid>"
    }

    return $normalized
}

function Get-AdapterWindowsProxy {
    try {
        $item = Get-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings" -ErrorAction Stop
        if ([int]$item.ProxyEnable -ne 1) {
            return $null
        }
        return ConvertTo-AdapterProxyUrl ([string]$item.ProxyServer)
    }
    catch {
        return $null
    }
}

function Set-AdapterProxyDefaults {
    if (-not $env:NO_PROXY) {
        $env:NO_PROXY = "localhost,127.0.0.1,::1"
    }
    if (-not $env:no_proxy) {
        $env:no_proxy = "localhost,127.0.0.1,::1"
    }

    if ($env:GEMINI_PROXY) {
        if (Test-AdapterProxyAvailable $env:GEMINI_PROXY) {
            Write-Host ("[Gemini Adapter] GEMINI_PROXY already set: {0}" -f (Format-AdapterProxyForLog $env:GEMINI_PROXY))
            return
        }
        $message = "[Gemini Adapter] GEMINI_PROXY is set but unavailable: {0}" -f (Format-AdapterProxyForLog $env:GEMINI_PROXY)
        if ($env:OPENAI_ADAPTER_STRICT_GEMINI_PROXY -match "^(1|true|yes|on)$") {
            throw $message
        }
        Write-Host $message
    }

    $candidates = @(
        "http://127.0.0.1:17997",
        "http://127.0.0.1:7897",
        $env:HTTPS_PROXY,
        $env:HTTP_PROXY,
        $env:ALL_PROXY,
        (Get-AdapterWindowsProxy)
    )

    foreach ($candidate in $candidates) {
        $normalized = ConvertTo-AdapterProxyUrl $candidate
        if (-not $normalized) {
            continue
        }
        if (Test-AdapterProxyAvailable $normalized) {
            $env:GEMINI_PROXY = $normalized
            Write-Host ("[Gemini Adapter] GEMINI_PROXY selected: {0}" -f (Format-AdapterProxyForLog $normalized))
            return
        }
    }

    Remove-Item Env:\GEMINI_PROXY -ErrorAction SilentlyContinue
    Write-Host "[Gemini Adapter] No available local proxy found; Gemini upstream will try direct network."
}
