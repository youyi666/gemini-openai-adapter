param(
    [int]$DebugPort = 9222,
    [string]$Model = "gemini-3-flash",
    [int]$ChatRetries = 2,
    [int]$RetryDelaySeconds = 30,
    [switch]$SkipChatTest
)

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$argsForScript = @{
    DebugPort = $DebugPort
    Model = $Model
    ChatRetries = $ChatRetries
    RetryDelaySeconds = $RetryDelaySeconds
}
if ($SkipChatTest) {
    $argsForScript["SkipChatTest"] = $true
}

& (Join-Path $Root "repair_auth_with_edge_cdp.ps1") @argsForScript
exit $LASTEXITCODE
