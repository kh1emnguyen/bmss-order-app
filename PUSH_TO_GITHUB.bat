@echo off
cd /d "%~dp0"
echo === bmss-order-app first push ===
if exist ".git" rmdir /s /q ".git"
git init & git branch -M main
git remote add origin https://github.com/kh1emnguyen/bmss-order-app.git
git config user.email "khiemnguyen166553@gmail.com"
git config user.name "kh1emnguyen"
git add -A
git commit -m "BMSS Order App dashboard"
git push -u origin main
echo.
echo THEN: github.com/kh1emnguyen/bmss-order-app/settings/pages
echo   Source: GitHub Actions → Save
echo.
echo Live at: https://kh1emnguyen.github.io/bmss-order-app/
pause
