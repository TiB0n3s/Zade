@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\prune-backups.ps1"
