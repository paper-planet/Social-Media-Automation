from flask import Flask, request, redirect

app = Flask(__name__)

@app.route('/')
def grab_ip():
    try:
        # Check if the header exists and has text (used when hosted online)
        xf_header = request.headers.get('X-Forwarded-For')
        
        if xf_header:
            # Safely extract the first IP from the proxy list and clean it up
            ip = str(xf_header).split(',')[0].strip()
        else:
            # FIXED: Changed remote_address to remote_addr
            ip = request.remote_addr
            
        print(f"[+] Captured IP Address: {ip}")
        
        # Save to file
        with open("captured_ips.txt", "a") as file:
            file.write(f"{ip}\n")
            
    except Exception as e:
        # Prints any unexpected errors to your console
        print(f"[-] Error capturing IP: {e}")
        
    # Redirect the visitor directly to your YouTube channel
    return redirect("https://www.youtube.com/@oatmeal-dota2")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
