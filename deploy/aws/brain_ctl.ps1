param(
    [ValidateSet("status", "start", "stop", "tunnel")]
    [string]$Action = "status",
    [string]$Profile = "aws-vm",
    [string]$Region = "ap-southeast-2",
    [string]$KeyName = "aws-agent-brain-key"
)

$ErrorActionPreference = "Stop"

$instJson = aws ec2 describe-instances `
    --filters "Name=tag:Name,Values=aws-agent-brain" "Name=instance-state-name,Values=pending,running,stopping,stopped" `
    --region $Region `
    --profile $Profile `
    --query "Reservations[0].Instances[0].{Id:InstanceId,State:State.Name,Ip:PublicIpAddress,Type:InstanceType}" `
    --output json | ConvertFrom-Json

if (-not $instJson -or -not $instJson.Id) {
    throw "No aws-agent-brain EC2 instance found in $Region."
}

switch ($Action) {
    "status" {
        $instJson | Format-List
    }
    "start" {
        aws ec2 start-instances --instance-ids $instJson.Id --region $Region --profile $Profile | Out-Null
        aws ec2 wait instance-running --instance-ids $instJson.Id --region $Region --profile $Profile
        $ip = aws ec2 describe-instances --instance-ids $instJson.Id --region $Region --profile $Profile --query "Reservations[0].Instances[0].PublicIpAddress" --output text
        Write-Host "Started $($instJson.Id) at $ip"
    }
    "stop" {
        aws ec2 stop-instances --instance-ids $instJson.Id --region $Region --profile $Profile | Out-Null
        Write-Host "Stopping $($instJson.Id) to preserve AWS credits."
    }
    "tunnel" {
        $keyPath = Join-Path $PSScriptRoot "$KeyName.pem"
        if (-not $instJson.Ip) { throw "Instance is not running (state: $($instJson.State)). Run with -Action start first." }
        Write-Host "Opening SSH tunnel http://127.0.0.1:8000 -> $($instJson.Ip):8000 ..."
        ssh -i $keyPath -N -L 8000:127.0.0.1:8000 "ubuntu@$($instJson.Ip)"
    }
}
