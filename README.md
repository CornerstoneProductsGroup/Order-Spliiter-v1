# Retail Order Splitter (HD + Lowe's)

- **Home Depot**: extracts the value directly under the **'Model Number'** label
- **Lowe's**: extracts the value directly under the **'Model #'** (or **'Model Number'**) label
- Match with a **vendor map (.xlsx)** to route pages into per-vendor PDFs
- **Print Pack**: combine selected vendors in strict, alphabetical order (exact list)
- **Review & Fix** UI to resolve uncertain pages
- Sidebar: per-retailer downloads + default map managers

## Run
```bash
pip install -r requirements.txt
streamlit run app.py
```

## Automation: watch a folder and auto-process PDFs

You can automate the manual “open app and click process” step by running:

```bash
python auto_order_watcher.py --retailer hd --watch-dir ~/Downloads/Rithum --archive-dir ~/Downloads/Rithum/processed
```

When a new PDF appears in `--watch-dir`, the script will automatically:
- run the existing splitter core for the selected retailer
- write per-vendor PDFs
- write `split_report.csv` and `errors_low_confidence.csv`
- build a ZIP output
- build a Print Pack PDF when matching vendors are present

### Common options

```bash
# Process current files one time and exit
python auto_order_watcher.py --retailer lw --watch-dir ~/Downloads/Rithum --include-existing --once

# Use a specific map file
python auto_order_watcher.py --retailer tsc --watch-dir ~/Downloads/Rithum --map-path ./data/vendor_map_tsc.xlsx

# Tune confidence threshold and scan interval
python auto_order_watcher.py --retailer hd --watch-dir ~/Downloads/Rithum --threshold 0.90 --poll-seconds 3
```

### Retailer values

- `hd` = Home Depot
- `lw` = Lowe's
- `tsc` = Tractor Supply

### Notes

- By default, existing PDFs in the watch folder are treated as already seen on startup.  
  Use `--include-existing` if you want to process them.
- The watcher does not log into Rithum itself yet; it automates processing once PDFs are downloaded.
