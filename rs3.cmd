@echo off
rem rs3 from a Windows terminal: whichever python is installed runs rs3.py beside this file
where python >nul 2>nul
if %errorlevel%==0 (
    python "%~dp0rs3.py" %*
) else (
    py -3 "%~dp0rs3.py" %*
)
