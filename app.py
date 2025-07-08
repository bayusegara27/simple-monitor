# --- IMPORTS ---
import os
import psutil
import requests
import time
import platform
import socket
import json
import sqlite3
from dotenv import load_dotenv
from flask import Flask, request, jsonify, send_from_directory
from flask_cors import CORS
from threading import Thread

# Load environment variables from the .env file
load_dotenv()

# --- DATABASE FUNCTIONS (PARENT-ONLY) ---
DB_FILE = 'servers.db'

def init_db():
    """
    Initializes the SQLite database.
    Creates a 'servers' table if it doesn't exist to store agent information.
    This database provides persistence for server data.
    """
    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    # The table stores the unique token, display name, the last full data payload,
    # and timestamps for when the server was first and last seen.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS servers (
            token TEXT PRIMARY KEY,
            server_name TEXT NOT NULL,
            last_data TEXT,
            first_seen REAL,
            last_seen REAL
        )
    ''')
    conn.commit()
    conn.close()
    print("--- [DATABASE] Database 'servers.db' is ready. ---")


# --- LOCAL METRICS GATHERING ---

class NetworkMonitor:
    """A helper class to calculate network speed over time, with error handling for Termux."""
    def __init__(self):
        self.can_monitor_network = False  # Assume network monitoring is disabled by default
        try:
            # Try to access the restricted network stats file
            self.last_io = psutil.net_io_counters()
            self.can_monitor_network = True  # If successful, enable network monitoring
            print("[INFO] Network monitoring is available.")
        except (PermissionError, FileNotFoundError):
            # If permission is denied (common on Android without root), handle it gracefully
            self.last_io = None
            print("[WARNING] Could not access network stats. Network speed monitoring will be disabled.")

        self.last_check = time.time()

    def get_speed(self):
        """
        Calculates network speed. If permission was denied during initialization,
        this will safely return 0 without causing a crash.
        """
        # If monitoring is disabled, return 0 immediately
        if not self.can_monitor_network or self.last_io is None:
            return 0.0, 0.0

        try:
            current_time = time.time()
            current_io = psutil.net_io_counters()
            elapsed_time = current_time - self.last_check

            if elapsed_time < 1:
                return 0.0, 0.0

            bytes_sent = current_io.bytes_sent - self.last_io.bytes_sent
            bytes_recv = current_io.bytes_recv - self.last_io.bytes_recv

            upload_speed_mbps = (bytes_sent * 8) / (elapsed_time * 1024 * 1024)
            download_speed_mbps = (bytes_recv * 8) / (elapsed_time * 1024 * 1024)

            self.last_check = current_time
            self.last_io = current_io

            return upload_speed_mbps, download_speed_mbps
        except Exception as e:
            # Fallback for any other unexpected errors during metric collection
            print(f"[ERROR] An unexpected error occurred in get_speed: {e}")
            return 0.0, 0.0

# Global cache for this machine's metrics
local_metrics_cache = {} 
# Global instance of the network monitor
net_monitor_global = NetworkMonitor()

def update_local_metrics_continuously():
    """
    A background thread that continuously gathers and updates local system metrics.
    Includes robust error handling for environments like non-rooted Termux.
    """
    # Gather static hardware info once to save resources
    try:
        architecture = platform.machine()
        total_ram_gb = psutil.virtual_memory().total / (1024**3)
        total_disk_gb = psutil.disk_usage('/').total / (1024**3)
        cpu_cores = psutil.cpu_count(logical=True)
        try:
            cpu_freq = psutil.cpu_freq()
            cpu_freq_max_ghz = (cpu_freq.max if cpu_freq and cpu_freq.max > 0 else cpu_freq.current) / 1000
        except (PermissionError, FileNotFoundError):
             cpu_freq_max_ghz = 0 # Fallback if frequency can't be read

    except (PermissionError, FileNotFoundError) as e:
        print("\n--- [FATAL ERROR] ---")
        print(f"Initial permission denied to read system hardware info: {e}")
        print("This device cannot be monitored. The agent will not start.")
        print("-----------------------\n")
        return # Exit the function completely

    # Main loop to update dynamic metrics
    while True:
        try:
            # Get current CPU, memory, and disk usage percentages
            # This is the line that causes the '/proc/stat' error
            cpu_percent = psutil.cpu_percent(interval=1)
            mem = psutil.virtual_memory()
            disk = psutil.disk_usage('/')

            # Get a list of the top 20 processes sorted by CPU usage
            processes = []
            process_iter = psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent'])
            sorted_processes = sorted(process_iter, key=lambda p: p.info.get('cpu_percent', 0) or 0, reverse=True)
            for proc in sorted_processes[:20]:
                try:
                    proc_info = proc.info
                    proc_info['net_usage'] = (proc_info.get('cpu_percent', 0) * 5) + (proc_info.get('memory_percent', 0) * 10)
                    processes.append({k: v if v is not None else 0 for k, v in proc_info.items()})
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    pass

            # Get current network speeds
            upload_mbps, download_mbps = net_monitor_global.get_speed()

            # Update the global cache with all gathered metrics
            local_metrics_cache.update({
                'token': os.getenv("TOKEN"),
                'server_name': os.getenv("SERVER_NAME", socket.gethostname()),
                'os': platform.system(),
                'architecture': architecture,
                'total_ram_gb': total_ram_gb,
                'total_disk_gb': total_disk_gb,
                'cpu_cores': cpu_cores,
                'cpu_freq_ghz': cpu_freq_max_ghz,
                'cpu_percent': cpu_percent,
                'mem_percent': mem.percent,
                'disk_percent': disk.percent,
                'net_upload_mbps': upload_mbps,
                'net_download_mbps': download_mbps,
                'processes': processes,
                'last_updated': time.time(),
                'is_offline': False
            })

        except (PermissionError, FileNotFoundError):
            # If a permission error occurs here, it's critical.
            # We will stop the thread to prevent spamming the console.
            print("\n--- [AGENT STOPPED] ---")
            print("Permission denied while trying to read core system stats (e.g., /proc/stat).")
            print("This is a common issue on non-rooted Android/Termux.")
            print("The monitoring agent on this device cannot continue and has been stopped.")
            print("------------------------\n")
            break  # Exit the while loop to terminate this thread.

        except Exception as e:
            print(f"An unexpected error occurred while updating metrics: {e}")

        time.sleep(2)

# --- CHILD AGENT FUNCTIONS (CHILD-ONLY) ---

def run_child_sender():
    """
    A background thread for child agents to send their metrics to the parent server.
    This function only runs if ROLE is 'child'.
    """
    parent_url = os.getenv("PARENT_URL")
    send_interval = int(os.getenv("SEND_INTERVAL", 5))
    server_name = os.getenv("SERVER_NAME", socket.gethostname())

    # Stop if the parent URL or child token is not configured
    if not parent_url or not os.getenv("TOKEN"):
        print("Warning: PARENT_URL or TOKEN is not set. Child will only run in local mode.")
        return

    print(f"--- [CHILD SENDER] Agent started for Server: {server_name} ---")
    print(f"Sending data to: {parent_url} every {send_interval} seconds.")

    # Main loop to send data
    while True:
        try:
            # Send the data from the local cache if it's available
            if local_metrics_cache:
                requests.post(f"{parent_url}/api/metrics", json=local_metrics_cache, timeout=10)
        except requests.exceptions.RequestException as e:
            print(f"[{time.ctime()}] Connection error to Parent: {e}")
        except Exception as e:
            print(f"[{time.ctime()}] An error occurred while sending data: {e}")
        # Wait for the specified interval before sending again
        time.sleep(send_interval)

# --- WEB SERVER (FLASK) ---

# Initialize the Flask application
app = Flask(__name__, static_folder='.', static_url_path='')
# Enable Cross-Origin Resource Sharing to allow the dashboard to fetch data
CORS(app)
# In-memory dictionary to store real-time metrics from all connected servers (Parent-only)
# The key is the server's unique TOKEN.
server_metrics_db = {}

@app.route('/')
def serve_dashboard():
    """Serves the main dashboard.html file."""
    return send_from_directory('.', 'dashboard.html')

@app.route('/api/metrics', methods=['POST'])
def receive_metrics():
    """
    API endpoint for the parent to receive metrics from child agents.
    """
    # This endpoint is for parents only
    if os.getenv('ROLE', 'child').lower() != 'parent':
        return jsonify({"status": "error", "message": "This endpoint is for parents only"}), 403

    # Get the JSON data sent by the child
    data = request.json
    token = data.get('token')
    server_name = data.get('server_name')

    # Basic validation
    if not token or not server_name:
        return jsonify({"status": "error", "message": "token or server_name missing"}), 400
    
    # Add a timestamp to track when the data was received
    data['last_updated'] = time.time()
    # Store the latest data in the in-memory cache
    server_metrics_db[token] = data

    # Persist the data to the SQLite database
    try:
        conn = sqlite3.connect(DB_FILE)
        cursor = conn.cursor()
        # Check if the server (by token) already exists
        cursor.execute("SELECT token FROM servers WHERE token = ?", (token,))
        if cursor.fetchone():
            # If it exists, UPDATE its name, last data, and last seen timestamp
            cursor.execute(
                "UPDATE servers SET server_name = ?, last_data = ?, last_seen = ? WHERE token = ?",
                (server_name, json.dumps(data), time.time(), token)
            )
        else:
            # If it's a new server, INSERT a new record
            cursor.execute(
                "INSERT INTO servers (token, server_name, last_data, first_seen, last_seen) VALUES (?, ?, ?, ?, ?)",
                (token, server_name, json.dumps(data), time.time(), time.time())
            )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error writing to DB: {e}")

    return jsonify({"status": "success"}), 200

@app.route('/api/get_all_servers', methods=['GET'])
def get_all_servers():
    """
    API endpoint that provides all server data to the dashboard frontend.
    """
    role = os.getenv('ROLE', 'child').lower()
    
    if role == 'parent':
        my_token = os.getenv("TOKEN")
        final_server_list = []

        # Step 1: Add the parent's own data to the list first to ensure it appears at the top.
        if my_token and local_metrics_cache:
            server_metrics_db[my_token] = local_metrics_cache # Keep the in-memory cache updated
            final_server_list.append(local_metrics_cache.copy())

        # Step 2: Identify and remove inactive servers from the real-time cache.
        timeout = 5 # A server is considered offline after 5 seconds of no contact.
        inactive_tokens = [
            token for token, data in server_metrics_db.items() 
            if time.time() - data.get('last_updated', 0) > timeout and token != my_token
        ]
        for token in inactive_tokens:
            if token in server_metrics_db:
                del server_metrics_db[token]

        # Step 3: Get all CHILD servers from the persistent DB, sorted by when they were first seen.
        try:
            conn = sqlite3.connect(DB_FILE)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT token, last_data FROM servers WHERE token != ? ORDER BY first_seen ASC", (my_token,))
            all_child_servers = cursor.fetchall()
            conn.close()

            for child_row in all_child_servers:
                token = child_row['token']
                # If the child is in the real-time cache, it's ONLINE. Use the fresh data.
                if token in server_metrics_db:
                    final_server_list.append(server_metrics_db[token])
                # If not, it's OFFLINE. Use the last known data from the DB and mark it as offline.
                else:
                    last_data = json.loads(child_row['last_data'])
                    # Set dynamic metrics to 0 and add the offline flag
                    last_data.update({
                        'cpu_percent': 0, 'mem_percent': 0, 'disk_percent': 0,
                        'net_upload_mbps': 0, 'net_download_mbps': 0,
                        'processes': [], 'is_offline': True,
                    })
                    final_server_list.append(last_data)
        
        except Exception as e:
            print(f"Error reading from DB: {e}")
            # Fallback in case of DB error
            if not final_server_list and local_metrics_cache:
                final_server_list.append(local_metrics_cache)
            return jsonify(final_server_list)
        
        return jsonify(final_server_list)
        
    else: # If the role is 'child', only return its own metrics
        return jsonify([local_metrics_cache] if local_metrics_cache else [])

def run_web_server(host, port):
    """Starts the Flask web server."""
    print(f"--- [WEB SERVER] Dashboard running at http://{host}:{port} ---")
    app.run(host=host, port=port, debug=False)

# --- MAIN EXECUTION BLOCK ---
if __name__ == '__main__':
    # The application will not start if a token is not set in the .env file.
    if not os.getenv("TOKEN"):
        raise ValueError("FATAL: TOKEN is not set in the .env file. Program terminated.")
        
    # Start the background thread to monitor local system metrics.
    local_monitor_thread = Thread(target=update_local_metrics_continuously, daemon=True)
    local_monitor_thread.start()
    time.sleep(1.5) # Wait a moment for the first metrics to be gathered.

    # Check the ROLE from the .env file to determine behavior.
    role = os.getenv('ROLE', 'child').lower()

    if role == 'parent':
        # If parent, initialize the database and start the web server.
        init_db()
        host = os.getenv("PARENT_HOST", "0.0.0.0")
        port = int(os.getenv("PARENT_PORT", 8000))
        run_web_server(host, port)
        
    elif role == 'child':
        # If child, start the sender thread to communicate with the parent.
        if os.getenv("PARENT_URL"):
            sender_thread = Thread(target=run_child_sender, daemon=True)
            sender_thread.start()
        
        # Optionally, a child can also host its own local dashboard.
        host = os.getenv("CHILD_HOST", "127.0.0.1")
        port_str = os.getenv("CHILD_DASHBOARD_PORT", "0")
        port = int(port_str) if port_str.isdigit() else 0
        
        if port > 0:
            run_web_server(host, port)
        else:
            # If no dashboard port is set, run as a pure agent.
            print("--- [CHILD AGENT] Running without a local dashboard. Sending data only. ---")
            # Keep the main thread alive.
            while True: time.sleep(60)
    else:
        print(f"Error: Invalid ROLE '{role}' in .env file. Use 'parent' or 'child'.")
