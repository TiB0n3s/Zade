@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run-cadence.ps1" -ReviewType monthly -ExperimentReviewType monthly -MaxRun 15 -MaxImport 20 -MaxExperimentReviews 50
