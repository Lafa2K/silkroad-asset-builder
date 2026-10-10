@echo off
cd /d "%~dp0"
pythonw "%~dp0sro_asset_builder.py"
if errorlevel 1 python "%~dp0sro_asset_builder.py"
