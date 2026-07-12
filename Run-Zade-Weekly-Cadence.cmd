@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run-cadence.ps1" -ReviewType weekly -ExperimentReviewType weekly -MaxRun 10 -MaxImport 10 -MaxExperimentReviews 25
