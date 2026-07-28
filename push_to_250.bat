@echo off
cd /d "e:\Tech\Code\Shopify Theme\Scrapper_and_etc"

:WAIT_LOOP
tasklist /FI "PID eq %1" 2>NUL | find /I "%1" >NUL
if "%ERRORLEVEL%"=="0" (
    timeout /t 30 /nobreak >NUL
    goto WAIT_LOOP
)

echo [%TIME%] Previous pipeline done. Starting push-to-250 run... >> push250_log.txt 2>&1

:RUN
echo [%TIME%] === STEP 1: Collect === >> push250_log.txt 2>&1
python shopify_outreach.py --collect >> push250_log.txt 2>&1

echo [%TIME%] === STEP 2: Scrape === >> push250_log.txt 2>&1
python shopify_outreach.py --scrape >> push250_log.txt 2>&1

echo [%TIME%] === STEP 3: Enrich === >> push250_log.txt 2>&1
python shopify_outreach.py --enrich >> push250_log.txt 2>&1

echo [%TIME%] === STEP 4: Send === >> push250_log.txt 2>&1
python shopify_outreach.py --send >> push250_log.txt 2>&1

echo [%TIME%] === STEP 5: Form-fill === >> push250_log.txt 2>&1
python shopify_outreach.py --formfill >> push250_log.txt 2>&1

echo [%TIME%] === DONE === >> push250_log.txt 2>&1
