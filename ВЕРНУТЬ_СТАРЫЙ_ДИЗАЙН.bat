@echo off
rem Otkat dizajna: pereklyuchaet oba proekta na staruyu rabochuyu versiyu (vetka master).
rem Nichego ne udalyaet - novyj dizajn ostayotsya v vetke redesign.
cd /d C:\seven11-apply
git switch master
cd /d C:\saling
git switch master
echo.
echo ================================================
echo Gotovo: vernulas STARAYA versiya interfejsa.
echo Novyj dizajn ne udalyon - on zhdyot v vetke redesign.
echo Teper perezapusti prilozhenie: JobApplyHub.bat
echo ================================================
pause
