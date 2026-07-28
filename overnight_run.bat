@echo off
cd /d "e:\Tech\Code\Shopify Theme\Scrapper_and_etc"

echo [%TIME%] === STEP 1: Collecting new agencies === >> overnight_log.txt 2>&1
python shopify_outreach.py --collect >> overnight_log.txt 2>&1

echo [%TIME%] === STEP 2: Scraping emails === >> overnight_log.txt 2>&1
python shopify_outreach.py --scrape >> overnight_log.txt 2>&1

echo [%TIME%] === STEP 3: Enriching with Hunter/Apollo === >> overnight_log.txt 2>&1
python shopify_outreach.py --enrich >> overnight_log.txt 2>&1

echo [%TIME%] === STEP 4: Sending emails === >> overnight_log.txt 2>&1
python shopify_outreach.py --send >> overnight_log.txt 2>&1

echo [%TIME%] === STEP 5: Form-filling remaining === >> overnight_log.txt 2>&1
python shopify_outreach.py --formfill >> overnight_log.txt 2>&1

echo [%TIME%] === ALL DONE === >> overnight_log.txt 2>&1
