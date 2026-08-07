$ErrorActionPreference = 'Stop'

$action = New-ScheduledTaskAction `
    -Execute 'C:\Python313\pythonw.exe' `
    -Argument 'C:\Tools\teams-busylight\teams_light_bridge.py' `
    -WorkingDirectory 'C:\Tools\teams-busylight'

$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"

# ExecutionTimeLimit 0 = never kill the task (default would stop it after 3 days)
$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 10 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

Unregister-ScheduledTask -TaskName 'TeamsBusyLight' -Confirm:$false -ErrorAction SilentlyContinue

Register-ScheduledTask -TaskName 'TeamsBusyLight' `
    -Description 'Teams busy-light bridge: pushes Teams presence to the ESP8266 light' `
    -Action $action -Trigger $trigger -Settings $settings | Out-Null

Start-ScheduledTask -TaskName 'TeamsBusyLight'
Start-Sleep -Seconds 3
Get-ScheduledTask -TaskName 'TeamsBusyLight' | Select-Object TaskName, State | Format-List
Get-ScheduledTaskInfo -TaskName 'TeamsBusyLight' | Select-Object LastRunTime, LastTaskResult | Format-List
