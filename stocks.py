from machine import Pin, SoftI2C
import ssd1306

import time
import math
import gc

import urequests
import ujson
import network

i2c = SoftI2C(scl=Pin(22), sda=Pin(21))

oled_width = 128
oled_height = 64

oled = ssd1306.SSD1306_I2C(oled_width, oled_height, i2c)

oled.text('Loading', 32, 28)

oled.show()

SSID = "RyaniPhone"
PASSWORD = "12345678"

wlan = network.WLAN(network.STA_IF)
wlan.active(True)
wlan.config(pm=wlan.PM_NONE)

print("Scanning for available Wi-Fi networks...")

networks = wlan.scan()

for net in networks:
    try:
        ssid = net[0].decode('utf-8')
    except UnicodeDecodeError:
        ssid = str(net[0])
        
    rssi = net[3]
    channel = net[2]
    
    if ssid.strip():
        print(f"{ssid:<25} | {rssi:<15} dBm | {channel:<7}")

print(f"\nFound {len(networks)} networks:")
print(f"{'SSID':<25} | {'Signal (RSSI)':<15} | {'Channel':<7}")
print("-" * 55)

print("Connecting to Wi-Fi...")
wlan.connect(SSID, PASSWORD)

timeout = 15
start = time.time()
while not wlan.isconnected():
    if time.time() - start > timeout:
        status = wlan.status()
        print(f"\nConnection failed. wlan.status() = {status}")
        break
    time.sleep(0.5)
    print(".", end="")

if wlan.isconnected():
    print("\nConnected! Network config:", wlan.ifconfig())

range_opts = ["1d", "1mo", "ytd"]
interval_opts = ["5m", "90m", "1wk"]

stock_tickers = ["^DJI", "NVDA", "TSM", "GOOG", "MSFT", "AMZN", "AVGO"]

stock_prices = [[] for _ in stock_tickers]
stock_change = [[] for _ in stock_tickers]
stock_pct = [[] for _ in stock_tickers]
stock_closes = [[] for _ in stock_tickers]

def get_stock_data(symbol, range_="1d", interval="5m"):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range_}&interval={interval}"
    r = urequests.get(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        data = ujson.loads(r.text)
    finally:
        r.close()

    result = data["chart"]["result"][0]
    meta = result["meta"]
    price = meta["regularMarketPrice"]
    prev_close = meta["chartPreviousClose"]
    change = price - prev_close
    pct = change / prev_close * 100

    closes = result["indicators"]["quote"][0]["close"]
    closes = [c for c in closes if c is not None]
    
    return [price, change, pct, closes]
        
def draw_sparkline(oled, closes, bound_x_min, bound_y_min, bound_x_max, bound_y_max):
    if len(closes) < 2:
        return
    
    market_low, market_high = min(closes), max(closes)

    if market_high != market_low:
        span = market_high - market_low 
    else:
        span = 1

    num_datapoints = len(closes)

    previous_x, previous_y = bound_x_min, bound_y_max - int((closes[0] - market_low) / span * (bound_y_max - bound_y_min))
    for i in range(1, num_datapoints):
        x_pos = bound_x_min + int(i / (num_datapoints - 1) * (bound_x_max - bound_x_min))
        y_pos = bound_y_max - int((closes[i] - market_low) / span * (bound_y_max - bound_y_min))
        oled.line(previous_x, previous_y, x_pos, y_pos, 1)
        previous_x, previous_y = x_pos, y_pos

def fetch_with_retry(ticker, range_, interval, retries=10):
    for attempt in range(retries):
        print(f"Attempt no: {attempt + 1}")
        try:
            print(f"Obtained ${ticker} for {range_} per {interval}")
            return get_stock_data(ticker, range_, interval)
        except OSError as e:
            print(f"{ticker} {range_}/{interval} failed ({e}), retry {attempt + 1}/{retries}")
            time.sleep_ms(800)
    return None 

for i in range(len(range_opts)):
    for j in range(len(stock_tickers)):
        gc.collect()
        print(f"Requested ${stock_tickers[j]} for {range_opts[i]} per {interval_opts[i]}")
        data = fetch_with_retry(stock_tickers[j], range_opts[i], interval_opts[i])
        if data is None:
            stock_prices[j].append(None)
            stock_change[j].append(None)
            stock_pct[j].append(None)
            stock_closes[j].append([])
            continue
        stock_prices[j].append(data[0])
        stock_change[j].append(data[1])
        stock_pct[j].append(data[2])
        stock_closes[j].append(data[3])
        time.sleep_ms(400)

current_stock = 0

bounds = [32, 16, 128, 64]

title_pos = 0
title = " mpy-stocks "

last_time = time.ticks_ms()

while True:
    now = time.ticks_ms()
    delta_ms = time.ticks_diff(now, last_time)
    last_time = now

    dt = delta_ms / 1000
    
    time_per_stock = 20000

    current_stock = math.floor((time.ticks_ms()/time_per_stock) % (len(stock_tickers)))
    current_mode = math.floor((time.ticks_ms()/(time_per_stock/len(range_opts))) % (len(range_opts)))
    
    title_pos += dt * 4
    
    if title_pos > 0:
        title_pos = -128 - (len(title) * 8 * 3)
    
    oled.fill(0)

    title_spacing = 8
    
    title_string = f"{"-" * title_spacing}{title}{"-" * title_spacing}{title}{"-" * title_spacing}{title}{"-" * title_spacing}"
    oled.text(title_string, int(title_pos), -2, 1)
    
    if stock_change[current_stock][current_mode] > 0:
        stock_glyph = "^"
    elif stock_change[current_stock][current_mode] < 0:
        stock_glyph = "v"
    else:
        stock_glyph = "-"

    oled.text(f"({stock_glyph}){stock_tickers[current_stock]}:{stock_prices[current_stock][current_mode]:>{12 - len(stock_tickers[current_stock])}}", 0, 8, 1)
    
    oled.text("t:", 0, 16)
    
    oled.text(f"{range_opts[current_mode]:>3}", 4, 24)
    
    oled.text("int:", 0, 32)
    
    oled.text(f"{interval_opts[current_mode]:>3}", 4, 40)
    
    oled.text("pct:", 0, 48)
    if stock_pct[current_stock][current_mode] > 0:
        if stock_pct[current_stock][current_mode] >= 1:
            oled.text(f"+{str(stock_pct[current_stock][current_mode])[0:3]}", 0, 56)
        else:
            oled.text(f"+{str(stock_pct[current_stock][current_mode])[1:4]}", 0, 56)
    else:
        if stock_pct[current_stock][current_mode] <= -1:
            oled.text(f"-{str(stock_pct[current_stock][current_mode])[1:4]}", 0, 56)
        else:
            oled.text(f"-{str(stock_pct[current_stock][current_mode])[2:5]}", 0, 56)
        
    oled.rect(int(bounds[0]), int(bounds[1]), int(bounds[2] - bounds[0]), int(bounds[3] - bounds[1]), 1)
    
    draw_sparkline(oled, stock_closes[current_stock][current_mode], bounds[0] + 1, bounds[1] + 1, bounds[2] - 1, bounds[3] - 1)
    
    oled.show()
