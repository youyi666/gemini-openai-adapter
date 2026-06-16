param(
    [int]$DebugPort = 9222,
    [string]$Model = "gemini-3-flash",
    [int]$ChatRetries = 2,
    [int]$RetryDelaySeconds = 30,
    [switch]$SkipChatTest
)

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent $ScriptDir
$argsForScript = @{
    DebugPort = $DebugPort
    Model = $Model
    ChatRetries = $ChatRetries
    RetryDelaySeconds = $RetryDelaySeconds
}
if ($SkipChatTest) {
    $argsForScript["SkipChatTest"] = $true
}

& (Join-Path $ScriptDir "repair_auth_with_edge_cdp.ps1") @argsForScript
exit $LASTEXITCODE
