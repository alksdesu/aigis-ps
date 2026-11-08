[CmdletBinding()]
param(
    [int]$Port = 18103,
    [string]$DataDirectory = (Join-Path $PSScriptRoot 'runtime\server'),
    [string]$AssetDirectory = (Join-Path (Split-Path $PSScriptRoot -Parent) 'aigisr_exe'),
    [string]$RulesFile = '',
    [string]$QuestRulesFile = '',
    [string]$TrainingRulesFile = '',
    [string]$SilverHouseRulesFile = '',
    [string]$EvolutionRulesFile = '',
    [string]$RecoveryRulesFile = '',
    [string]$TradeRulesFile = '',
    [string]$TicketRulesFile = '',
    [string]$ItemTicketRulesFile = '',
    [string]$LoginRulesFile = '',
    [string]$EventPassRulesFile = '',
    [string]$EventPassRevivalFile = '',
    [string]$EventLoginRulesFile = '',
    [string]$ComebackLoginRulesFile = '',
    [string]$PurchasedLoginRulesFile = '',
    [string]$DailyGiftRulesFile = '',
    [string]$VoteRulesFile = '',
    [string]$VoteCookingRulesFile = '',
    [string]$EntrustRulesFile = '',
    [string]$MissionWindowsFile = '',
    [string]$CommandTextOverridesFile = '',
    [string]$TowerRulesFile = '',
    [string]$SubjugationRulesFile = '',
    [string]$SerialCodeRulesFile = '',
    [string]$LetsTDRewardsFile = '',
    [int]$AdminPort = 18104,
    [string]$AdminHost = '127.0.0.1',
    [switch]$NoAdmin,
    [switch]$Check
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'environment.ps1')
$pythonPath = Initialize-AigisEnvironment -ProjectRoot $PSScriptRoot
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    & (Join-Path $PSScriptRoot 'setup.ps1')
}
$workspaceRoot = [System.IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent)).TrimEnd('\') + '\'
$resolvedData = [System.IO.Path]::GetFullPath($DataDirectory)
if (-not $resolvedData.StartsWith($workspaceRoot, [StringComparison]::OrdinalIgnoreCase)) {
    throw 'The data directory must remain inside this workspace.'
}
$serverArguments = @('-m', 'aigis_ps.cli')
if ($Check) { $serverArguments += 'doctor' } else { $serverArguments += @('serve', '--port', $Port) }
if (-not ($Check -or $NoAdmin)) { $serverArguments += @('--admin-port', $AdminPort, '--admin-host', $AdminHost) }
$serverArguments += @('--data-dir', $resolvedData, '--asset-dir', $AssetDirectory)
if ($RulesFile) { $serverArguments += @('--rules', $RulesFile) }
if ($QuestRulesFile) { $serverArguments += @('--quest-rules', $QuestRulesFile) }
if ($TrainingRulesFile) { $serverArguments += @('--training-rules', $TrainingRulesFile) }
if ($SilverHouseRulesFile) { $serverArguments += @('--silver-house-rules', $SilverHouseRulesFile) }
if ($EvolutionRulesFile) { $serverArguments += @('--evolution-rules', $EvolutionRulesFile) }
if ($RecoveryRulesFile) { $serverArguments += @('--recovery-rules', $RecoveryRulesFile) }
if ($TradeRulesFile) { $serverArguments += @('--trade-rules', $TradeRulesFile) }
if ($TicketRulesFile) { $serverArguments += @('--ticket-rules', $TicketRulesFile) }
if ($ItemTicketRulesFile) { $serverArguments += @('--item-ticket-rules', $ItemTicketRulesFile) }
if ($LoginRulesFile) { $serverArguments += @('--login-rules', $LoginRulesFile) }
if ($EventPassRulesFile) { $serverArguments += @('--event-pass-rules', $EventPassRulesFile) }
if ($EventPassRevivalFile) { $serverArguments += @('--event-pass-revival', $EventPassRevivalFile) }
if ($EventLoginRulesFile) { $serverArguments += @('--event-login-rules', $EventLoginRulesFile) }
if ($ComebackLoginRulesFile) { $serverArguments += @('--comeback-login-rules', $ComebackLoginRulesFile) }
if ($PurchasedLoginRulesFile) { $serverArguments += @('--purchased-login-rules', $PurchasedLoginRulesFile) }
if ($DailyGiftRulesFile) { $serverArguments += @('--daily-gift-rules', $DailyGiftRulesFile) }
if ($VoteRulesFile) { $serverArguments += @('--vote-rules', $VoteRulesFile) }
if ($VoteCookingRulesFile) { $serverArguments += @('--vote-cooking-rules', $VoteCookingRulesFile) }
if ($EntrustRulesFile) { $serverArguments += @('--entrust-rules', $EntrustRulesFile) }
if ($MissionWindowsFile) { $serverArguments += @('--mission-windows', $MissionWindowsFile) }
if ($CommandTextOverridesFile) { $serverArguments += @('--command-text-overrides', $CommandTextOverridesFile) }
if ($TowerRulesFile) { $serverArguments += @('--tower-rules', $TowerRulesFile) }
if ($SubjugationRulesFile) { $serverArguments += @('--subjugation-rules', $SubjugationRulesFile) }
if ($SerialCodeRulesFile) { $serverArguments += @('--serial-code-rules', $SerialCodeRulesFile) }
if ($LetsTDRewardsFile) { $serverArguments += @('--letstd-rewards', $LetsTDRewardsFile) }
& $pythonPath @serverArguments
if ($LASTEXITCODE -ne 0) { throw 'The local server reported an error.' }

