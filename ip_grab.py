from flask import Flask, request, redirect

app = Flask(__name__)

@app.route('/')
def grab_ip():
    try:
        # PythonAnywhere uses proxies; this extracts the visitor's actual public IP
        xf_header = request.headers.get('X-Forwarded-For')

        if xf_header:
            # Grab the first IP in the list and strip whitespace
            ip = str(xf_header).split(',')[0].strip()
        else:
            ip = request.remote_addr

        print(f"[+] Captured IP Address: {ip}")

        # Save to a file in PythonAnywhere's local directory
        with open("captured_ips.txt", "a") as file:
            file.write(f"{ip}\n")

    except Exception as e:
        print(f"[-] Error capturing IP: {e}")

    # Redirect to your YouTube channel
    return redirect("https://www.youtube.com")

# PythonAnywhere will completely ignore this block, which is exactly what we want!
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
