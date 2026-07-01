# Create a new skill skeleton under skills\<name>\
# Usage: .\scripts\new-skill.ps1 -Name <skill-name> [-Category <category>]
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)]
    [ValidatePattern('^[a-z0-9][a-z0-9-]{1,63}$')]
    [string]$Name,

    [ValidateSet('devops','coding','research','misc')]
    [string]$Category = 'misc'
)

$ErrorActionPreference = 'Stop'

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$TemplateDir = Join-Path $RepoRoot 'docs\skill-template'
$TargetDir = Join-Path $RepoRoot "skills\$Name"

if (Test-Path $TargetDir) {
    throw "Target already exists: $TargetDir"
}
if (-not (Test-Path $TemplateDir)) {
    throw "Template not found: $TemplateDir"
}

# Copy template
New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null
Copy-Item -Path "$TemplateDir\*" -Destination $TargetDir -Recurse -Force

# Replace placeholders in SKILL.md
$SkillFile = Join-Path $TargetDir 'SKILL.md'
if (Test-Path $SkillFile) {
    # Derive human-readable title from slug (hello-world -> Hello World)
    $Title = ($Name -split '-' | ForEach-Object { $_.Substring(0,1).ToUpper() + $_.Substring(1) }) -join ' '
    # Generate a minimal placeholder description (<60 chars) so validation passes
    $Desc = "TODO: Describe what '$Name' does and when to use it."

    # Read as UTF-8 (template contains non-ASCII chars)
    $content = Get-Content -Raw -Encoding UTF8 $SkillFile
    # Use multiline mode so ^/$ match each line (template has CRLF on Windows)
    $content = $content -replace '(?m)^name: TODO_FILL_NAME\r?$', "name: $Name"
    $content = $content -replace '(?m)^description: .*\r?$', "description: `"$Desc`""
    $content = $content -replace '(?m)^    category: TODO_FILL_CATEGORY\r?$', "    category: $Category"
    $content = $content -replace '(?m)^# TODO: Skill Title\r?$', "# $Title"
    # Write back as UTF-8 (without BOM) so downstream parsers stay happy
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($SkillFile, $content, $utf8NoBom)
}

Write-Host "Created skill: $TargetDir" -ForegroundColor Green
Write-Host "Next steps:"
Write-Host "  1. Edit $SkillFile (fill in description, procedure, etc.)"
Write-Host "  2. Run: python $(Join-Path $RepoRoot 'scripts\validate-skills.py')"
