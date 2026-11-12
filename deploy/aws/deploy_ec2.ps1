param(
    [string]$Profile = "aws-vm",
    [string]$Region = "ap-southeast-2",
    [string]$InstanceType = "g4dn.xlarge",
    [string]$ModelId = "huihui-ai/Qwen3-8B-abliterated",
    [string]$KeyName = "aws-agent-brain-key",
    [switch]$OnDemand,
    [int]$IdleMinutes = 20
)

$ErrorActionPreference = "Stop"

Write-Host "1. Verifying AWS project credentials ($Profile / $Region)..." -ForegroundColor Cyan
aws sts get-caller-identity --profile $Profile --region $Region | Out-Null

Write-Host "2. Checking GPU vCPU service quotas in $Region..." -ForegroundColor Cyan
$quotaCode = if ($OnDemand) { "L-DB2E81BA" } else { "L-3819A6DF" }
$quotaName = if ($OnDemand) { "Running On-Demand G and VT instances" } else { "All G and VT Spot Instance Requests" }
$quotaVal = aws service-quotas get-service-quota `
    --service-code ec2 `
    --quota-code $quotaCode `
    --region $Region `
    --profile $Profile `
    --query "Quota.Value" `
    --output text 2>$null
Write-Host "   $quotaName quota = $quotaVal vCPUs"
if ([double]$quotaVal -lt 4) {
    Write-Warning "Your $quotaName quota in $Region is $quotaVal vCPUs (< 4 needed for $InstanceType). Request a quota increase in AWS Service Quotas before launching."
    exit 1
}

Write-Host "3. Resolving latest Ubuntu 22.04 NVIDIA Deep Learning Base AMI in $Region..." -ForegroundColor Cyan
$amiId = aws ec2 describe-images `
    --owners amazon `
    --region $Region `
    --profile $Profile `
    --filters "Name=name,Values=Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)*" "Name=state,Values=available" `
    --query "sort_by(Images, &CreationDate)[-1].ImageId" `
    --output text
if (-not $amiId -or $amiId -eq "None") {
    throw "Could not resolve Deep Learning Base AMI in $Region."
}
Write-Host "   AMI: $amiId"

$keyPath = Join-Path $PSScriptRoot "$KeyName.pem"
$existingKey = aws ec2 describe-key-pairs --key-names $KeyName --region $Region --profile $Profile --query "KeyPairs[0].KeyName" --output text 2>$null
if (-not $existingKey -or $existingKey -eq "None") {
    Write-Host "4. Creating EC2 key pair $KeyName -> $keyPath" -ForegroundColor Cyan
    $mat = aws ec2 create-key-pair --key-name $KeyName --region $Region --profile $Profile --query "KeyMaterial" --output text
    Set-Content -LiteralPath $keyPath -Value $mat -Encoding ascii
    icacls $keyPath /inheritance:r /grant:r "$($env:USERNAME):(R)" | Out-Null
}

$sgName = "aws-agent-brain-sg"
$sgId = aws ec2 describe-security-groups --group-names $sgName --region $Region --profile $Profile --query "SecurityGroups[0].GroupId" --output text 2>$null
if (-not $sgId -or $sgId -eq "None") {
    Write-Host "5. Creating SSH-only security group $sgName..." -ForegroundColor Cyan
    $sgId = aws ec2 create-security-group --group-name $sgName --description "SSH tunnel access for AWS Agent Chat Brain" --region $Region --profile $Profile --query "GroupId" --output text
    $myIp = (Invoke-RestMethod -Uri "https://checkip.amazonaws.com").Trim()
    aws ec2 authorize-security-group-ingress --group-id $sgId --protocol tcp --port 22 --cidr "$myIp/32" --region $Region --profile $Profile | Out-Null
}

$apiToken = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(24))
$userDataRaw = Get-Content -LiteralPath (Join-Path $PSScriptRoot "user-data-vllm.sh") -Raw
$userDataRaw = $userDataRaw -replace 'MODEL_ID="\$\{MODEL_ID:-[^}]+\}"', "MODEL_ID=`"$ModelId`""
$userDataRaw = $userDataRaw -replace 'API_KEY="\$\{VLLM_API_KEY:-[^}]+\}"', "API_KEY=`"$apiToken`""
$userDataRaw = $userDataRaw -replace 'IDLE_MINUTES="\$\{IDLE_MINUTES:-[^}]+\}"', "IDLE_MINUTES=`"$IdleMinutes`""
$userDataB64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($userDataRaw))

$runArgs = @(
    "ec2", "run-instances",
    "--image-id", $amiId,
    "--instance-type", $InstanceType,
    "--key-name", $KeyName,
    "--security-group-ids", $sgId,
    "--block-device-mappings", '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":80,"VolumeType":"gp3"}}]',
    "--tag-specifications", 'ResourceType=instance,Tags=[{Key=Name,Value=aws-agent-brain}]',
    "--user-data", $userDataB64,
    "--region", $Region,
    "--profile", $Profile,
    "--query", "Instances[0].InstanceId",
    "--output", "text"
)
if (-not $OnDemand) {
    $runArgs += @("--instance-market-options", '{"MarketType":"spot","SpotOptions":{"SpotInstanceType":"persistent","InstanceInterruptionBehavior":"stop"}}')
}

Write-Host "6. Launching $InstanceType ($(if ($OnDemand) {'On-Demand'} else {'Spot'})) with $ModelId..." -ForegroundColor Cyan
$instanceId = & aws @runArgs
aws ec2 wait instance-running --instance-ids $instanceId --region $Region --profile $Profile
$publicIp = aws ec2 describe-instances --instance-ids $instanceId --region $Region --profile $Profile --query "Reservations[0].Instances[0].PublicIpAddress" --output text

Write-Host ""
Write-Host "EC2 Brain launched!" -ForegroundColor Green
Write-Host "  Instance ID : $instanceId"
Write-Host "  Public IP   : $publicIp"
Write-Host "  Model       : $ModelId"
Write-Host "  API Key     : $apiToken"
Write-Host ""
Write-Host "Put this in aws-agent-chat\.env (after vLLM finishes downloading weights ~3-5m):"
Write-Host "  LLM_BASE_URL=http://127.0.0.1:8000/v1"
Write-Host "  LLM_API_KEY=$apiToken"
Write-Host "  LLM_MODEL=$ModelId"
Write-Host ""
Write-Host "Open SSH tunnel:"
Write-Host "  ssh -i `"$keyPath`" -N -L 8000:127.0.0.1:8000 ubuntu@$publicIp"
