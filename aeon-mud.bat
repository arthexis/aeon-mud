@echo off
rem CALL on the same command line consumes CMD's pending Ctrl+C batch prompt.
rem The subroutine returns Python's exit status without altering CLI arguments.
python "%~dp0aeon_mud_client.py" %* & call :aeon_preserve_exit
exit /b %errorlevel%

:aeon_preserve_exit
exit /b %errorlevel%
