@echo off

git status
git add .
git commit -m "Add"

if errorlevel 1 goto end

git push origin main

:end
pause