# Windows Server monitoring

The Windows collector sends a heartbeat, uptime, CPU usage, physical memory usage and used/total space for mounted fixed volumes. It uses Windows PowerShell 5.1 and the standard Kervax agent report API. The collector does not execute panel commands, install Linux helpers or update itself.

1. Add a Windows server in Kervax. In its installation dialog, download `windows-monitor.ps1` and copy the server's agent token. The download is also available at `/api/agent/windows-monitor.ps1` on the panel.
2. Save the script on the server. Start an elevated Windows PowerShell session and run the command below with your panel's HTTPS URL. Enter the agent token when prompted.

   ```powershell
   powershell.exe -NoProfile -File .\windows-monitor.ps1 -Install -PanelUrl 'https://panel.example.com'
   ```

   If your execution policy is `RemoteSigned` and Windows blocks the download, review the script, then run `Unblock-File -LiteralPath .\windows-monitor.ps1` and retry. Follow your organization's signing policy if it requires signed scripts.

   The panel certificate must be trusted by the Windows computer. If agent source restrictions are enabled, allow the Windows server's outgoing address in the panel.
3. The scheduled task runs every minute as `SYSTEM`. To send the first report immediately, run `Start-ScheduledTask -TaskName 'Kervax Windows Monitor'`. Check that the server appears online in Kervax and shows CPU, memory and disk usage. The offline timeout should exceed the reporting interval.

Run the install command again to replace the collector or enter a new token. The script and `config.json` are stored in `%ProgramData%\KervaxMonitor`, accessible only to `SYSTEM` and local Administrators. To inspect the local metrics without reading the token or sending a report, use `powershell.exe -NoProfile -File .\windows-monitor.ps1 -Preview`.

To troubleshoot a report failure, run the installed script from elevated Windows PowerShell and check the error message.

```powershell
powershell.exe -NoProfile -File "$env:ProgramData\KervaxMonitor\windows-monitor.ps1"
```

To remove monitoring, unregister the task and remove the installed directory from elevated Windows PowerShell. Revoke the server's token in the panel when retiring the host.

```powershell
Unregister-ScheduledTask -TaskName 'Kervax Windows Monitor'
Remove-Item -LiteralPath (Join-Path $env:ProgramData 'KervaxMonitor') -Recurse
```
