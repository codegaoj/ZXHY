import os, time, json

# Check if web panel process is running
os.system('tasklist /FI "IMAGENAME eq python.exe" /FO CSV > tasklist.txt 2>nul')
with open('tasklist.txt', 'r') as f:
    for line in f:
        if 'python' in line.lower():
            print(line.strip())

# Check latest output files
d = r'g:\BaiduNetdiskDownload\z直播文字\trading-system-core\outputs'
for f in sorted(os.listdir(d)):
    fp = os.path.join(d, f)
    if os.path.isfile(fp):
        mtime = os.path.getmtime(fp)
        t = time.localtime(mtime)
        print(f'{f}: {t.tm_year}-{t.tm_mon:02d}-{t.tm_mday:02d} {t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d}')

# Check a_spot_latest.csv mtime
spot = r'g:\BaiduNetdiskDownload\z直播文字\trading-system-core\data\a_spot_latest.csv'
if os.path.exists(spot):
    mtime = os.path.getmtime(spot)
    t = time.localtime(mtime)
    print(f'\na_spot_latest.csv: {t.tm_year}-{t.tm_mon:02d}-{t.tm_mday:02d} {t.tm_hour:02d}:{t.tm_min:02d}:{t.tm_sec:02d}')
