# Windows Server monitoring

The Windows collector sends a heartbeat, uptime, CPU usage, physical memory usage and used/total space for mounted fixed volumes. It uses Windows PowerShell 5.1 and the standard Kervax agent report API. The collector does not execute panel commands, install Linux helpers or update itself.

1. Select **Windows** when adding the server in Kervax. Copy the installation command and paste it into Windows PowerShell running as Administrator. The command contains the server's agent credential. Share it only with the server's administrators.
2. The command downloads the collector over HTTPS, verifies its SHA-256 checksum, sends the first report and registers a scheduled task. The panel certificate must be trusted by the Windows computer. If agent source restrictions are enabled, allow the Windows server's outgoing address in the panel.
3. Check that the server appears online in Kervax and shows CPU, memory and disk usage. The scheduled task runs every minute as `SYSTEM`. The offline timeout should exceed the reporting interval.

Run the installation command again to replace the collector. The script and `config.json` are stored in `%ProgramData%\KervaxMonitor`, accessible only to `SYSTEM` and local Administrators. To inspect the local metrics without reading the token or sending a report, download `windows-monitor.ps1` from `/api/agent/windows-monitor.ps1` and use `powershell.exe -NoProfile -File .\windows-monitor.ps1 -Preview`.

To troubleshoot a report failure, run the installed script from elevated Windows PowerShell and check the error message.

```powershell
powershell.exe -NoProfile -File "$env:ProgramData\KervaxMonitor\windows-monitor.ps1"
```

To remove monitoring, unregister the task and remove the installed directory from elevated Windows PowerShell. Revoke the server's token in the panel when retiring the host.

```powershell
Unregister-ScheduledTask -TaskName 'Kervax Windows Monitor'
Remove-Item -LiteralPath (Join-Path $env:ProgramData 'KervaxMonitor') -Recurse
```
