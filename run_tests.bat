@echo off
cd /d "%~dp0"

echo =================================================================
echo  [Patch Verification] Running Automated Regression Test Suite...
echo =================================================================

if exist "venv\Scripts\python.exe" (
    "venv\Scripts\python.exe" -m unittest discover -s tests -p "test_*.py" -v
) else (
    python -m unittest discover -s tests -p "test_*.py" -v
)

set EXIT_CODE=%ERRORLEVEL%
echo.
if %EXIT_CODE% EQU 0 (
    echo [PASS] All patch verification tests passed!
) else (
    echo [FAIL] Some tests failed. Please check the output above.
)

if "%1" NEQ "--no-pause" (
    pause
)
exit /b %EXIT_CODE%
