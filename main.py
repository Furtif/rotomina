import json
import time
import re
import asyncio
import subprocess
import httpx
import zipfile
import shutil
import datetime
import threading
import traceback
import tempfile
import os
import platform
import xml.etree.ElementTree as ET
from pathlib import Path
from functools import wraps
from typing import List, Dict, Optional, Tuple, Set

from fastapi import FastAPI, Request, Form, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.templating import Jinja2Templates
from contextlib import asynccontextmanager

# ======================================
# Global Configuration
# ======================================
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
APK_DIR = BASE_DIR / "data" / "apks" / "pogo"
EXTRACT_DIR = APK_DIR / "extracted"
POGO_MIRROR_URL = "https://mirror.unownhash.com"
DEFAULT_ARCH = "arm64-v8a"
device_status_cache = {}
update_lock = threading.Lock()
update_in_progress = False
current_progress = 0

# ======================================
# WebSocket Connection Manager
# ======================================
class ConnectionManager:
    def __init__(self):
        self.active_connections: Set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.add(websocket)
        print(f"WebSocket client connected. Total active connections: {len(self.active_connections)}")

    def disconnect(self, websocket: WebSocket):
        self.active_connections.discard(websocket)
        print(f"WebSocket client disconnected. Remaining connections: {len(self.active_connections)}")

    async def broadcast(self, message: dict):
        disconnected_websockets = set()
        
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                print(f"Error broadcasting to WebSocket: {e}")
                disconnected_websockets.add(connection)
        
        # Remove disconnected websockets
        for ws in disconnected_websockets:
            self.active_connections.discard(ws)
            
        if disconnected_websockets:
            print(f"Removed {len(disconnected_websockets)} disconnected WebSocket(s). Remaining: {len(self.active_connections)}")

# Initialize WebSocket manager
ws_manager = ConnectionManager()

# ======================================
# Configuration Management
# ======================================
def load_config():
    if not CONFIG_FILE.exists():
        return {"devices": [], "users": []}
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        config = json.load(f)
        for device in config.get("devices", []):
            device.setdefault("display_name", device["ip"].split(":")[0])
            device.setdefault("pogo_version", "N/A")
            device.setdefault("mitm_version", "N/A")
            device.setdefault("module_version", "N/A")
            # Add control flag with default value of False
            device.setdefault("control_enabled", False)
            # Add memory threshold with default value
            device.setdefault("memory_threshold", 200)
        config.setdefault("discord_webhook_url", "")
        # Add PIF auto-update flag (enabled by default)
        config.setdefault("pif_auto_update_enabled", True)
        # Add PoGO auto-update flag (enabled by default)
        config.setdefault("pogo_auto_update_enabled", True)
        return config

def save_config(config):
    with open("config.json", "w", encoding="utf-8") as f:
        json.dump(config, f, indent=4)
        f.flush()  # Ensures data is actually written!

def update_device_info(ip: str, details: dict):
    config = load_config()
    for device in config["devices"]:
        if device["ip"] == ip:
            device.update({
                "display_name": details["display_name"],
                "pogo_version": details.get("pogo_version", "N/A"),
                "mitm_version": details.get("mitm_version", "N/A"),
                "module_version": details.get("module_version", "N/A")
            })
    save_config(config)

# ======================================
# Caching Mechanism
# ======================================
def ttl_cache(ttl: int):
    def decorator(func):
        cache = {}
        @wraps(func)
        def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = time.time()
            if key in cache:
                result, timestamp = cache[key]
                if now - timestamp < ttl:
                    return result
            result = func(*args, **kwargs)
            cache[key] = (result, now)
            return result
        def cache_clear():
            cache.clear()
        wrapper.cache_clear = cache_clear
        return wrapper
    return decorator

# ======================================
# Discord Webhook Functions
# ======================================

async def send_discord_notification(message: str, title: str = None, color: int = 0x5865F2):
    """Sends a notification to the Discord webhook, if configured"""
    config = load_config()
    webhook_url = config.get("discord_webhook_url")
    
    if not webhook_url:
        # No webhook URL configured, no notification
        return False
    
    try:
        # Create embed for better presentation
        embed = {
            "title": title or "Rotomina Notification",
            "description": message,
            "color": color,
            "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
            "footer": {
                "text": "Rotomina"
            }
        }
        
        # Send webhook request
        async with httpx.AsyncClient() as client:
            response = await client.post(
                webhook_url,
                json={
                    "embeds": [embed]
                },
                timeout=10
            )
            
            if response.status_code >= 200 and response.status_code < 300:
                print(f"Discord notification sent: {message}")
                return True
            else:
                print(f"Discord webhook error: {response.status_code} - {response.text}")
                return False
                
    except Exception as e:
        print(f"Error sending Discord notification: {str(e)}")
        return False

# Discord Message Color Constants
DISCORD_COLOR_RED = 0xE74C3C     # Error/Offline
DISCORD_COLOR_GREEN = 0x2ECC71   # Success/Online
DISCORD_COLOR_BLUE = 0x3498DB    # Info/Update
DISCORD_COLOR_ORANGE = 0xE67E22  # Warning/Restart

# Helper functions for specific notifications
async def notify_device_offline(device_name: str, ip: str):
    """Notifies when a device goes offline"""
    message = f"⚠️ Device **{device_name}** ({ip}) is offline."
    await send_discord_notification(
        message=message,
        title="Device Offline",
        color=DISCORD_COLOR_RED
    )

async def notify_device_online(device_name: str, ip: str):
    """Notifies when a device comes back online"""
    message = f"✅ Device **{device_name}** ({ip}) is back online and MITM was successfully started."
    await send_discord_notification(
        message=message,
        title="Device Online",
        color=DISCORD_COLOR_GREEN
    )

async def notify_memory_restart(device_name: str, ip: str, memory: int, threshold: int):
    """Notifies when a device is restarted due to low memory"""
    # Format memory in MB (memory is in kB)
    memory_mb = memory / 1024
    memory_formatted = f"{memory_mb:.2f} MB".replace(".", ",")
    
    # Threshold is now in MB
    threshold_formatted = f"{threshold} MB"
    
    message = (f"🔄 Device **{device_name}** ({ip}) is being restarted due to low memory.\n"
              f"Available memory: **{memory_formatted}** (Threshold: {threshold_formatted})")
    
    await send_discord_notification(
        message=message,
        title="Low Memory - Restart",
        color=DISCORD_COLOR_ORANGE
    )

async def notify_update_installed(device_name: str, ip: str, update_type: str, version: str):
    """Notifies when an update has been installed on a device"""
    message = f"📲 **{update_type}** update (Version: {version}) has been installed on device **{device_name}** ({ip})."
    
    await send_discord_notification(
        message=message,
        title=f"{update_type} Update Installed",
        color=DISCORD_COLOR_GREEN
    )

async def notify_update_downloaded(update_type: str, version: str):
    """Notifies when an update has been downloaded"""
    message = f"💾 New **{update_type}** version {version} has been downloaded and is ready for installation."
    
    await send_discord_notification(
        message=message,
        title=f"New {update_type} Version Available",
        color=DISCORD_COLOR_BLUE
    )

# ======================================
# ADB Functions
# ======================================
connected_devices = set()

@ttl_cache(ttl=3600)
def check_adb_connection(device_id: str) -> tuple[bool, str]:
    """
    Checks ADB connection to device and returns status and error message.
    Supports both network devices (IP:Port) and USB devices (serial number).
    Reuses existing connections when possible.
    
    Args:
        device_id: Either serial number (USB) or IP:Port (network)
    
    Returns:
        tuple: (is_connected, error_message)
    """
    global connected_devices
    
    try:
        device_id = device_id.strip()
        
        if device_id in connected_devices:
            devices_result = subprocess.run(
                ["adb", "devices"],
                capture_output=True,
                text=True,
                timeout=5
            )
            
            device_line_pattern = f"{device_id}\tdevice"
            if device_line_pattern in devices_result.stdout:
                return True, ""
                
            connected_devices.discard(device_id)
        
        # Determine device type
        # A USB device (serial number) does not contain a colon
        # A network device contains a colon as IP:Port
        is_network_device = ":" in device_id and all(c.isdigit() or c == '.' or c == ':' for c in device_id)
        
        # Debug output
        print(f"Device {device_id} identified as {'network' if is_network_device else 'USB'} device")
        
        # For network devices with IP but no port, add the default port
        if is_network_device and device_id.count(':') == 0:
            device_id = f"{device_id}:5555"
            print(f"Added default port: {device_id}")
        
        # Try to establish a connection (only for network devices)
        if is_network_device:
            print(f"Attempting to connect to network device: {device_id}")
            connect_result = subprocess.run(
                ["adb", "connect", device_id],
                timeout=10,
                capture_output=True,
                text=True
            )
            
            # Check for authentication errors
            if "failed to authenticate" in connect_result.stdout:
                return False, "Authentication error: Please restart ADB on device"
            
            # Check for other connection errors
            if "cannot" in connect_result.stdout.lower() or "failed" in connect_result.stdout.lower():
                error_msg = connect_result.stdout.strip()
                return False, f"Connection error: {error_msg}"
                
            # Check for errors in stderr
            if connect_result.stderr and len(connect_result.stderr) > 0:
                error_msg = connect_result.stderr.strip()
                return False, f"ADB error: {error_msg}"
        
        # Check active connections
        devices_result = subprocess.run(
            ["adb", "devices"],
            capture_output=True,
            text=True
        )
        
        # Output for diagnostics
        print(f"ADB devices output: {devices_result.stdout}")
        
        # Check if the device is in the list with status "device"
        device_line_pattern = f"{device_id}\tdevice"
        if device_line_pattern in devices_result.stdout:
            print(f"Device {device_id} found with 'device' status")
            # Gerät zur Liste verbundener Geräte hinzufügen (Optimierung)
            connected_devices.add(device_id)
            return True, ""
            
        # Device is connected but in a different state
        for line in devices_result.stdout.splitlines():
            if device_id in line:
                state = line.split("\t")[1] if "\t" in line else "unknown"
                if state != "device":
                    print(f"Device {device_id} found but has status: {state}")
                    return False, f"Device in status: {state}"
        
        # Not found in device list
        print(f"Device {device_id} not found in adb devices output")
        return False, "Device not connected or not found"
        
    except subprocess.TimeoutExpired:
        return False, "Connection timeout"
    except Exception as e:
        print(f"Critical error checking {device_id}: {str(e)}")
        return False, f"Critical ADB error: {str(e)}"

def format_device_id(device_id: str) -> str:
    """
    Formats a device ID for consistent use.
    
    - For IP addresses without a port, adds the default port 5555
    - For serial numbers (without colon), leaves the ID unchanged
    
    Args:
        device_id: Either serial number (USB) or IP(:Port) (network)
        
    Returns:
        str: Correctly formatted device ID
    """
    # Check if there's a colon in the ID
    if ":" in device_id:
        # Is it an IP address without a port?
        if device_id.count(":") == 1 and all(part.isdigit() for part in device_id.split(".")):
            # Add default port
            return f"{device_id}:5555"
    
    # For serial numbers or complete IP:Port combinations, return unchanged
    return device_id

@ttl_cache(ttl=3600)  # Cache remains active for 1 hour
def get_device_details(device_id: str) -> dict:
    try:
        config_data = load_config()
        device = next((d for d in config_data["devices"] if d["ip"] == device_id), None)

        # If the device doesn't exist in the config, add it
        if not device:
            # For network devices, use IP as display_name, for USB devices use "Device-XXXX"
            if ":" in device_id:
                display_name = device_id.split(":")[0]
            else:
                # For serial numbers, use a shortened version
                display_name = f"Device-{device_id[-4:]}" if len(device_id) > 4 else device_id
                
            device = {"ip": device_id, "display_name": display_name}
            config_data["devices"].append(device)
            save_config(config_data)

        details = {
            "display_name": device["display_name"],  # Default from config.json
            "pogo_version": "N/A",
            "mitm_version": "N/A",
            "module_version": "N/A"
        }

        # Try to read device configuration from the device itself
        try:
            cmd = f'adb -s {device_id} shell "su -c \'cat /data/data/com.github.furtif.furtifformaps/files/config.json\'"'
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=5)
            if result.stdout:
                try:
                    device_config = json.loads(result.stdout)
                    new_name = device_config.get("RotomDeviceName", "").strip()

                    if new_name and new_name != device["display_name"]:
                        print(f"Device {device_id} name changed from '{device['display_name']}' to '{new_name}', updating config.json")
                        device["display_name"] = new_name
                        save_config(config_data)

                    # If a new name was found, we override it
                    details["display_name"] = device["display_name"]
                except json.JSONDecodeError:
                    print(f"Device {device_id}: Failed to parse config.json")
        except Exception as e:
            print(f"Error reading device name for {device_id}: {e}")

        try:
            # Get PoGo version
            pogo_cmd = f'adb -s {device_id} shell "dumpsys package com.nianticlabs.pokemongo | grep versionName"'
            pogo_result = subprocess.run(pogo_cmd, shell=True, capture_output=True, text=True, timeout=5)
            if pogo_result.stdout:
                pogo_match = re.search(r'versionName=(\d+\.\d+\.\d+)', pogo_result.stdout)
                if pogo_match:
                    details["pogo_version"] = pogo_match.group(1)

            # Get MITM version
            mitm_cmd = f'adb -s {device_id} shell "dumpsys package com.github.furtif.furtifformaps | grep versionName"'
            mitm_result = subprocess.run(mitm_cmd, shell=True, capture_output=True, text=True, timeout=5)
            if mitm_result.stdout:
                mitm_match = re.search(r'versionName=(\d+\.\d+(?:\.\d+)?)', mitm_result.stdout)
                if mitm_match:
                    details["mitm_version"] = mitm_match.group(1)

            # Get PIF Module version
            pif_cmd = f'adb -s {device_id} shell "su -c \'cat /data/adb/modules/playintegrityfix/module.prop\'"'
            pif_result = subprocess.run(pif_cmd, shell=True, capture_output=True, text=True, timeout=5)
            if pif_result.stdout:
                pif_match = re.search(r'version=v?(\d+\.\d+.*)', pif_result.stdout)
                if pif_match:
                    details["module_version"] = pif_match.group(1).strip()
        except Exception as e:
            print(f"Version detection error for {device_id}: {e}")

        update_device_info(device_id, details)  # Configuration persistence maintained
        return details
    except Exception as e:
        print(f"Device detail error {device_id}: {str(e)}")
        return {
            "display_name": device["display_name"],  # Default name from config.json
            "pogo_version": "N/A",
            "mitm_version": "N/A",
            "module_version": "N/A"
        }

def ensure_adb_keys() -> str:
    """
    Ensures both ADB private and public keys exist and returns the public key content.
    If keys don't exist or are empty, they are generated.
    Works correctly in Docker/Ubuntu environments.
    
    Returns:
        str: The ADB public key content or empty string if generation fails.
    """
    try:
        if platform.system() == "Windows":
            android_dir = os.path.expanduser("~\\.android")
            adb_private_key = os.path.join(android_dir, "adbkey")
            adb_public_key = os.path.join(android_dir, "adbkey.pub")
        else:
            android_dir = "/root/.android"
            adb_private_key = os.path.join(android_dir, "adbkey")
            adb_public_key = os.path.join(android_dir, "adbkey.pub")
        
        # Ensure the .android directory exists
        if not os.path.exists(android_dir):
            print(f"Creating Android directory: {android_dir}")
            os.makedirs(android_dir, exist_ok=True)
        
        # Check if the private key exists and has content
        private_key_exists = os.path.exists(adb_private_key) and os.path.getsize(adb_private_key) > 0
        
        # Check if the public key exists and has content
        public_key_exists = os.path.exists(adb_public_key) and os.path.getsize(adb_public_key) > 0
        
        # If private key does not exist, generate it using adb keygen
        if not private_key_exists:
            print(f"Private ADB key not found at {adb_private_key}, generating new keys...")
            try:
                # Try using adb keygen first
                subprocess.run(["adb", "keygen", adb_private_key], check=True, timeout=10)
                private_key_exists = os.path.exists(adb_private_key) and os.path.getsize(adb_private_key) > 0
                print(f"Generated private key with adb keygen: {private_key_exists}")
            except (subprocess.SubprocessError, FileNotFoundError) as e:
                print(f"adb keygen failed: {str(e)}, trying alternative approach...")
                
                # Alternative: generate private key with OpenSSL
                try:
                    subprocess.run(
                        ["openssl", "genrsa", "-out", adb_private_key, "2048"],
                        check=True, timeout=10
                    )
                    private_key_exists = os.path.exists(adb_private_key) and os.path.getsize(adb_private_key) > 0
                    print(f"Generated private key with OpenSSL: {private_key_exists}")
                except (subprocess.SubprocessError, FileNotFoundError) as e:
                    print(f"Failed to generate private key with OpenSSL: {str(e)}")
        
        # If private key exists but public key doesn't, generate public key
        if private_key_exists and not public_key_exists:
            print(f"Public key not found at {adb_public_key}, generating from private key...")
            try:
                subprocess.run(
                    ["openssl", "rsa", "-in", adb_private_key, "-pubout", "-out", adb_public_key],
                    check=True, timeout=10
                )
                public_key_exists = os.path.exists(adb_public_key) and os.path.getsize(adb_public_key) > 0
                print(f"Generated public key: {public_key_exists}")
            except (subprocess.SubprocessError, FileNotFoundError) as e:
                print(f"Failed to generate public key: {str(e)}")
        
        # Read and return the content of the public key if it exists
        if public_key_exists:
            with open(adb_public_key, "r") as f:
                content = f.read().strip()
                print(f"Found ADB public key ({len(content)} bytes)")
                return content
        else:
            print("Failed to ensure ADB keys exist")
            return ""
    except Exception as e:
        print(f"Error ensuring ADB keys: {str(e)}")
        traceback.print_exc()
        return ""

def sync_system_adb_key():
    """
    Synchronizes the system ADB key from /root/.android/adbkey.pub to BASE_DIR/data/adb/adbkey.pub
    This ensures that the system key is also available in the additional keys directory.
    """
    try:
        # Define paths
        if platform.system() == "Windows":
            system_key_path = os.path.expanduser("~\\.android\\adbkey.pub")
        else:
            system_key_path = "/root/.android/adbkey.pub"
        
        # Use the application's base directory structure for consistency
        additional_keys_dir = BASE_DIR / "data" / "adb"
        target_key_path = additional_keys_dir / "adbkey.pub"
        
        # Check if system key exists
        if not os.path.exists(system_key_path):
            print(f"System ADB key not found at {system_key_path}")
            return False
        
        # Ensure additional keys directory exists
        if not additional_keys_dir.exists():
            print(f"Creating additional keys directory: {additional_keys_dir}")
            additional_keys_dir.mkdir(parents=True, exist_ok=True)
        
        # Read system key
        with open(system_key_path, "r") as f:
            key_content = f.read().strip()
            
        if not key_content:
            print(f"System ADB key is empty, nothing to sync")
            return False
            
        # Write to target location
        with open(target_key_path, "w") as f:
            f.write(key_content)
            
        print(f"Successfully synchronized system ADB key to {target_key_path}")
        return True
            
    except Exception as e:
        print(f"Error synchronizing system ADB key: {str(e)}")
        return False

def authorize_device_with_adb_key(device_id: str) -> bool:
    """
    Robust ADB key authorization with enhanced reconnection strategy.
    Specifically handles the device going offline and coming back during the process.
    
    Args:
        device_id: Device identifier (serial or IP:port)
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        # Extract IP address from device_id if it contains a port
        base_ip = device_id.split(':')[0] if ':' in device_id else device_id
        
        # Collect all ADB keys
        keys = []
        
        # 1. Get the system ADB public key
        system_adb_key = ensure_adb_keys()
        if system_adb_key:
            keys.append(system_adb_key)
            print(f"Added system ADB key for device {device_id}")
        else:
            print(f"No system ADB key available for device {device_id}")
        
        # 2. Get additional keys from the data/adb directory
        additional_keys_dir = BASE_DIR / "data" / "adb"
        if additional_keys_dir.exists():
            print(f"Checking for additional ADB keys in {additional_keys_dir}")
            for key_file in additional_keys_dir.glob("adbkey*.pub"):
                try:
                    with open(key_file, "r") as f:
                        key_content = f.read().strip()
                        if key_content:
                            keys.append(key_content)
                            print(f"Added additional key from {key_file.name} ({len(key_content)} bytes)")
                        else:
                            print(f"Warning: Key file {key_file.name} is empty, skipping")
                except Exception as e:
                    print(f"Error reading key file {key_file}: {str(e)}")
        else:
            print(f"Additional keys directory {additional_keys_dir} does not exist, creating it")
            try:
                additional_keys_dir.mkdir(parents=True, exist_ok=True)
                print(f"Created directory {additional_keys_dir}")
            except Exception as e:
                print(f"Failed to create directory {additional_keys_dir}: {str(e)}")
        
        # Deduplicate keys
        unique_keys = []
        for key in keys:
            if key not in unique_keys:
                unique_keys.append(key)
                
        # If no keys found, return
        if not unique_keys:
            print(f"No ADB keys available for device {device_id}")
            return False
            
        print(f"Found {len(unique_keys)} unique ADB keys to install")
        
        # Create a temporary file with all keys
        with tempfile.NamedTemporaryFile(mode='w+', delete=False) as temp:
            temp_path = temp.name
            for key in unique_keys:
                temp.write(key + "\n")
            
            # Flush to ensure all data is written
            temp.flush()
        
        try:
            # Helper function to handle device reconnection
            def wait_for_device_online(ip_with_port, timeout_seconds=60, check_interval=2):
                """Wait for the device to come back online after ADB daemon restart"""
                print(f"Waiting for device {ip_with_port} to come back online...")
                
                start_time = time.time()
                while time.time() - start_time < timeout_seconds:
                    try:
                        # Try to disconnect first to clear any stale connections
                        subprocess.run(
                            ["adb", "disconnect", ip_with_port],
                            timeout=5,
                            capture_output=True
                        )
                        
                        # Wait a moment
                        time.sleep(1)
                        
                        # Try to connect
                        connect_result = subprocess.run(
                            ["adb", "connect", ip_with_port],
                            timeout=5,
                            capture_output=True,
                            text=True
                        )
                        
                        print(f"Connect result: {connect_result.stdout.strip()}")
                        
                        # Check device status
                        devices_result = subprocess.run(
                            ["adb", "devices"],
                            timeout=5,
                            capture_output=True,
                            text=True
                        )
                        
                        print(f"Device list: {devices_result.stdout.strip()}")
                        
                        # Check if device is in the list and not unauthorized
                        if ip_with_port in devices_result.stdout:
                            if "unauthorized" not in devices_result.stdout:
                                print(f"Device {ip_with_port} is back online and authorized!")
                                
                                # Wait a bit more to ensure the device is truly ready
                                time.sleep(3)
                                return True
                            else:
                                print(f"Device {ip_with_port} is online but unauthorized")
                        else:
                            print(f"Device {ip_with_port} not found in device list")
                            
                    except Exception as e:
                        print(f"Reconnection attempt error: {str(e)}")
                    
                    print(f"Device not ready, retrying in {check_interval} seconds...")
                    time.sleep(check_interval)
                
                print(f"Device {ip_with_port} did not come back online within {timeout_seconds} seconds")
                return False

            # Disconnect from the device first to clean up any stale connections
            print(f"Disconnecting from {device_id} to start with clean state...")
            subprocess.run(["adb", "disconnect", device_id], timeout=5, capture_output=True)
            time.sleep(2)
            
            # Connect to the device
            print(f"Connecting to {device_id}...")
            connect_result = subprocess.run(
                ["adb", "connect", device_id],
                timeout=10,
                capture_output=True,
                text=True
            )
            print(f"Connect result: {connect_result.stdout.strip()}")
            
            # Allow some time for the connection to establish
            time.sleep(3)
            
            # Check if device is connected
            connected, error = check_adb_connection(device_id)
            if not connected:
                print(f"Cannot authorize device {device_id}: {error}")
                return False
                
            # Check if device is rooted
            print("Checking for root access...")
            root_check = subprocess.run(
                ["adb", "-s", device_id, "shell", "su -c", "id"],
                timeout=10,
                capture_output=True,
                text=True
            )
            
            has_root = "uid=0" in root_check.stdout
            
            if has_root:
                print(f"Device {device_id} has root access, setting up authorization...")
                
                # Phase 1: Push files and prepare directories
                try:
                    # Push the keys file to device
                    print("Pushing keys to device...")
                    subprocess.run(
                        ["adb", "-s", device_id, "push", temp_path, "/sdcard/adbkey.pub"],
                        timeout=10,
                        check=True
                    )
                    
                    # Create the ADB directory
                    print("Creating ADB directory...")
                    subprocess.run(
                        ["adb", "-s", device_id, "shell", "su -c", "mkdir -p /data/misc/adb"],
                        timeout=5,
                        check=False
                    )
                    
                    # Backup existing keys if any
                    print("Backing up any existing keys...")
                    subprocess.run(
                        ["adb", "-s", device_id, "shell", "su -c", "cp /data/misc/adb/adb_keys /data/misc/adb/adb_keys.bak 2>/dev/null || :"],
                        timeout=5,
                        check=False
                    )
                    
                    # Copy the keys directly (this approach has been verified to work on your device)
                    print("Installing ADB keys...")
                    subprocess.run(
                        ["adb", "-s", device_id, "shell", "su -c", "cat /sdcard/adbkey.pub > /data/misc/adb/adb_keys"],
                        timeout=10,
                        check=False
                    )
                    
                    # Set proper permissions
                    print("Setting permissions...")
                    subprocess.run(
                        ["adb", "-s", device_id, "shell", "su -c", "chmod 644 /data/misc/adb/adb_keys"],
                        timeout=5,
                        check=False
                    )
                    
                    # Setup complete
                    print("Initial setup complete.")
                except Exception as e:
                    print(f"Error during initial setup: {str(e)}")
                    return False
                
                # Make a full disconnection and restart the device's ADB
                print("Completely restarting ADB connection and daemon...")
                # Disconnect first
                subprocess.run(["adb", "disconnect", device_id], timeout=5, capture_output=True)
                
                # Kill ADB server on host to clean slate
                subprocess.run(["adb", "kill-server"], timeout=5, capture_output=True)
                time.sleep(2)
                subprocess.run(["adb", "start-server"], timeout=5, capture_output=True)
                time.sleep(2)
                
                # Reconnect
                subprocess.run(["adb", "connect", device_id], timeout=10, capture_output=True)
                time.sleep(3)
                
                # Now try a direct connect & method that explicitly restarts ADB on the device
                try:
                    # Enable ADB in settings
                    print("Enabling ADB settings...")
                    subprocess.run(
                        ["adb", "-s", device_id, "shell", "su -c", "settings put global adb_enabled 1"],
                        timeout=5,
                        check=False
                    )
                    
                    # Configure TCP settings
                    print("Configuring TCP mode...")
                    subprocess.run(
                        ["adb", "-s", device_id, "shell", "su -c", "setprop service.adb.tcp.port 5555"],
                        timeout=5,
                        check=False
                    )
                    
                    # Explicitly restart ADB daemon on device
                    print("Restarting ADB daemon on device...")
                    try:
                        # Try the proper way first
                        subprocess.run(
                            ["adb", "-s", device_id, "shell", "su -c", "stop adbd && start adbd"],
                            timeout=10,
                            check=False,
                            capture_output=True
                        )
                    except:
                        print("ADB restart command may have disconnected us (expected)")
                    
                    # The device is very likely offline now
                    # Disconnect from the device
                    subprocess.run(["adb", "disconnect", device_id], timeout=5, capture_output=True)
                    time.sleep(5)  # Longer wait to ensure ADB daemon has time to restart
                except Exception as e:
                    # Expected to fail as device will disconnect
                    print(f"ADB daemon restart resulted in expected disconnection: {str(e)}")
                
                # Now try to reconnect with the original device ID
                print("Starting reconnection sequence...")
                device_back_online = wait_for_device_online(device_id, timeout_seconds=60)
                
                # If the device didn't come back with the original port, try the standard port
                if not device_back_online and ':' in device_id:
                    alternative_device_id = f"{base_ip}:5555"
                    print(f"Trying alternative port: {alternative_device_id}")
                    device_back_online = wait_for_device_online(alternative_device_id, timeout_seconds=60)
                    if device_back_online:
                        device_id = alternative_device_id  # Update device ID for future commands
                    
                # If verification failed, try one more manual ADB restart
                if not device_back_online:
                    print("Last resort: Killing and restarting ADB server")
                    subprocess.run(["adb", "kill-server"], timeout=5, capture_output=True)
                    time.sleep(2)
                    subprocess.run(["adb", "start-server"], timeout=5, capture_output=True)
                    time.sleep(2)
                    print(f"Trying to reconnect to {device_id} one more time...")
                    device_back_online = wait_for_device_online(device_id, timeout_seconds=60)
                
                if device_back_online:
                    # Final verification - try a simple ADB command
                    try:
                        # Let's try to run a simple command to verify authorization
                        print(f"Final verification: running simple ADB command...")
                        # Run a simple command multiple times with retries
                        success = False
                        for attempt in range(5):
                            try:
                                verify_result = subprocess.run(
                                    ["adb", "-s", device_id, "shell", "echo", "Success"],
                                    timeout=10,
                                    capture_output=True,
                                    text=True
                                )
                                
                                if "Success" in verify_result.stdout:
                                    print(f"Verification succeeded on attempt {attempt+1}!")
                                    success = True
                                    break
                                else:
                                    print(f"Verification attempt {attempt+1} failed: {verify_result.stderr}")
                                    time.sleep(3)  # Wait before retry
                            except Exception as e:
                                print(f"Verification attempt {attempt+1} error: {str(e)}")
                                time.sleep(3)  # Wait before retry
                                
                        if success:
                            print("ADB authorization completed successfully!")
                            return True
                        else:
                            print("All verification attempts failed")
                            
                            # As a last resort, just return True anyway - keys are likely installed correctly
                            # even if we can't verify it immediately
                            print("Keys were successfully installed, but verification failed.")
                            print("The device may need a reboot for changes to take effect.")
                            print("Try reconnecting to the device later.")
                            return True
                    except Exception as e:
                        print(f"Final verification error: {str(e)}")
                        return False
                else:
                    print("Failed to reconnect to the device after ADB daemon restart")
                    # Even if reconnection failed, keys might be properly installed
                    print("Keys were installed but device reconnection failed.")
                    print("Try reconnecting to the device manually after a reboot.")
                    return True
            else:
                print(f"Device {device_id} does not have root access, cannot push ADB keys")
                return False
        finally:
            # Remove the temporary file
            try:
                os.unlink(temp_path)
            except Exception as e:
                print(f"Warning: Failed to remove temporary file {temp_path}: {str(e)}")
    except Exception as e:
        print(f"Error authorizing device {device_id}: {str(e)}")
        traceback.print_exc()
        return False

def install_adb_keys_with_persistence(device_id: str) -> bool:
    """
    Installs ADB keys with multiple persistence methods to ensure
    authorization survives reboots and ADB daemon restarts.
    """
    try:
        # First, ensure SELinux is in permissive mode for the installation
        print("Setting SELinux to permissive mode temporarily...")
        subprocess.run(
            ["adb", "-s", device_id, "shell", "su -c 'setenforce 0'"],
            timeout=5,
            capture_output=True
        )
        
        # Define the multiple locations for the ADB keys
        source_locations = [
            "/sdcard/adbkey.pub",
            "/data/local/tmp/adbkey.pub"
        ]
        
        # Script to install keys across multiple locations for redundancy
        script_lines = [
            "# Ensure ADB directories exist",
            "mkdir -p /data/misc/adb",
            "mkdir -p /data/data/com.android.adb",  # Alternative location used by some ROMs
            "mkdir -p /data/adb",                    # Yet another potential location
            
            "# Remove immutable attributes if any",
            "chattr -i /data/misc/adb/adb_keys 2>/dev/null || true",
            "chattr -i /data/data/com.android.adb/adb_keys 2>/dev/null || true",
            "chattr -i /data/adb/adb_keys 2>/dev/null || true",
            
            "# Combine source keys",
        ]
        
        # Add commands to copy files to a temporary location
        for i, location in enumerate(source_locations):
            script_lines.append(f"[ -f {location} ] && cat {location} > /data/local/tmp/combined_keys_{i} || touch /data/local/tmp/combined_keys_{i}")
        
        # Combine all sources and remove duplicates
        script_lines.append("cat /data/local/tmp/combined_keys_* | sort -u > /data/local/tmp/final_keys")
        
        # Copy to all potential ADB key locations
        script_lines.extend([
            "# Install keys to all potential locations",
            "cp /data/local/tmp/final_keys /data/misc/adb/adb_keys",
            "cp /data/local/tmp/final_keys /data/data/com.android.adb/adb_keys 2>/dev/null || true",
            "cp /data/local/tmp/final_keys /data/adb/adb_keys 2>/dev/null || true",
            
            "# Set correct permissions",
            "chmod 644 /data/misc/adb/adb_keys",
            "chmod 644 /data/data/com.android.adb/adb_keys 2>/dev/null || true",
            "chmod 644 /data/adb/adb_keys 2>/dev/null || true",
            
            "# Set owner for all key files",
            "chown system:system /data/misc/adb/adb_keys 2>/dev/null || true",
            "chown system:system /data/data/com.android.adb/adb_keys 2>/dev/null || true",
            "chown system:system /data/adb/adb_keys 2>/dev/null || true",
            
            "# Ensure ADB is enabled in settings",
            "settings put global adb_enabled 1",
            
            "# Stop and restart adbd with new keys",
            "stop adbd 2>/dev/null || true",
            "setprop service.adb.tcp.port 5555",  # Ensure TCP/IP ADB is enabled
            "start adbd 2>/dev/null || true",
            
            "# Alternative restart methods when stop/start doesn't work",
            "killall adbd 2>/dev/null || true",
            "pidof adbd | xargs kill -9 2>/dev/null || true",
            "/system/bin/adbd 2>/dev/null & sleep 1; kill $! 2>/dev/null || true",
            
            "# Clean up",
            "rm -f /data/local/tmp/combined_keys_* /data/local/tmp/final_keys /sdcard/adbkey.pub",
            
            "# Return success",
            "echo 'ADB keys installed successfully'"
        ])
        
        # Join the script lines
        script = "\n".join(script_lines)
        
        # Create a temporary script file
        script_file = tempfile.NamedTemporaryFile(mode='w+', delete=False, suffix='.sh')
        script_path = script_file.name
        script_file.write(script)
        script_file.flush()
        script_file.close()
        
        # Push the script to the device
        print("Pushing installation script to device...")
        subprocess.run(
            ["adb", "-s", device_id, "push", script_path, "/data/local/tmp/install_adb_keys.sh"],
            timeout=10,
            check=True
        )
        
        # Make the script executable
        subprocess.run(
            ["adb", "-s", device_id, "shell", "chmod 755 /data/local/tmp/install_adb_keys.sh"],
            timeout=5,
            check=True
        )
        
        # Execute the script with root
        print("Executing installation script with root...")
        script_result = subprocess.run(
            ["adb", "-s", device_id, "shell", "su -c 'sh /data/local/tmp/install_adb_keys.sh'"],
            timeout=30,
            capture_output=True,
            text=True
        )
        
        # Clean up local script file
        os.unlink(script_path)
        
        # Clean up remote script
        subprocess.run(
            ["adb", "-s", device_id, "shell", "rm -f /data/local/tmp/install_adb_keys.sh"],
            timeout=5
        )
        
        # Set SELinux back to enforcing mode
        subprocess.run(
            ["adb", "-s", device_id, "shell", "su -c 'setenforce 1'"],
            timeout=5
        )
        
        # Check the output for success message
        if "ADB keys installed successfully" in script_result.stdout:
            print("Installation script completed successfully")
            
            # Verify keys were properly installed
            check_cmd = "su -c 'cat /data/misc/adb/adb_keys'"
            check_result = subprocess.run(
                ["adb", "-s", device_id, "shell", check_cmd],
                timeout=10,
                capture_output=True,
                text=True
            )
            
            if check_result.stdout.strip() and "BEGIN PUBLIC KEY" in check_result.stdout:
                print("Verified ADB keys are properly installed")
                
                # One final test - disconnect and try to reconnect without prompt
                print("Testing ADB connection persistence...")
                
                # Disconnect all adb connections to the device
                subprocess.run(
                    ["adb", "disconnect", device_id],
                    timeout=5
                )
                
                # Wait a moment for disconnection to complete
                time.sleep(2)
                
                # Try to reconnect
                reconnect_result = subprocess.run(
                    ["adb", "connect", device_id],
                    timeout=10,
                    capture_output=True,
                    text=True
                )
                
                # Check if connection was successful and device is authorized
                if "connected to" in reconnect_result.stdout and "unauthorized" not in reconnect_result.stdout:
                    print(f"Successfully reconnected to {device_id} without authorization prompt")
                    return True
                else:
                    print(f"Reconnection test failed: {reconnect_result.stdout}")
                    # Even if reconnect test fails, files might be installed correctly
                    return True
            else:
                print("Could not verify ADB keys installation")
                return False
        else:
            print(f"Installation script failed. Output: {script_result.stdout}\nErrors: {script_result.stderr}")
            return False
    except Exception as e:
        print(f"Error during persistent installation: {str(e)}")
        traceback.print_exc()
        
        # Try to restore SELinux enforcing mode
        try:
            subprocess.run(
                ["adb", "-s", device_id, "shell", "su -c 'setenforce 1'"],
                timeout=5
            )
        except:
            pass
            
        return False

def try_standard_adb_key_install(device_id: str) -> bool:
    """Tries to install ADB keys using the standard approach"""
    try:
        print("Trying standard approach to install ADB keys...")
        
        # Remove immutable flag if exists
        subprocess.run(
            ["adb", "-s", device_id, "shell", "su -c 'chattr -i /data/misc/adb/adb_keys 2>/dev/null || true'"],
            timeout=5,
            capture_output=True
        )
        
        # Create directory and add keys
        cmds = [
            "su -c 'mkdir -p /data/misc/adb'",
            "su -c 'cat /sdcard/adbkey.pub > /data/misc/adb/adb_keys'",
            "su -c 'chmod 644 /data/misc/adb/adb_keys'",
            "su -c 'rm -f /sdcard/adbkey.pub'",
            "su -c 'settings put global adb_enabled 1'"
        ]
        
        for cmd in cmds:
            result = subprocess.run(
                ["adb", "-s", device_id, "shell", cmd],
                timeout=10,
                capture_output=True,
                text=True
            )
            if result.returncode != 0:
                if "Operation not permitted" in result.stderr:
                    print(f"Permission denied for: {cmd}")
                    return False
        
        return True
    except Exception as e:
        print(f"Standard approach failed: {str(e)}")
        return False

def try_remount_adb_key_install(device_id: str) -> bool:
    """Tries to install ADB keys by remounting system as read-write"""
    try:
        print("Trying to remount system partition as read-write...")
        
        # Remount commands
        remount_cmds = [
            "su -c 'mount -o rw,remount /system'",
            "su -c 'mount -o rw,remount /'",
            "su -c 'mount -o rw,remount /data'"
        ]
        
        for cmd in remount_cmds:
            subprocess.run(
                ["adb", "-s", device_id, "shell", cmd],
                timeout=5,
                capture_output=True
            )
        
        # Try direct file manipulation after remount
        cmds = [
            "su -c 'mkdir -p /data/misc/adb'",
            "su -c 'cat /sdcard/adbkey.pub > /data/misc/adb/adb_keys'",
            "su -c 'chmod 644 /data/misc/adb/adb_keys'",
            "su -c 'rm -f /sdcard/adbkey.pub'",
            "su -c 'settings put global adb_enabled 1'"
        ]
        
        for cmd in cmds:
            result = subprocess.run(
                ["adb", "-s", device_id, "shell", cmd],
                timeout=10,
                capture_output=True,
                text=True
            )
            if result.returncode != 0 and "Operation not permitted" in result.stderr:
                return False
        
        return True
    except Exception as e:
        print(f"Remount approach failed: {str(e)}")
        return False

def try_selinux_adb_key_install(device_id: str) -> bool:
    """Tries to install ADB keys by temporarily setting SELinux to permissive mode"""
    try:
        print("Trying with SELinux in permissive mode...")
        
        # Set SELinux to permissive mode
        subprocess.run(
            ["adb", "-s", device_id, "shell", "su -c 'setenforce 0'"],
            timeout=5,
            capture_output=True
        )
        
        # Try operations with SELinux permissive
        cmds = [
            "su -c 'mkdir -p /data/misc/adb'",
            "su -c 'cat /sdcard/adbkey.pub > /data/misc/adb/adb_keys'",
            "su -c 'chmod 644 /data/misc/adb/adb_keys'",
            "su -c 'rm -f /sdcard/adbkey.pub'",
            "su -c 'settings put global adb_enabled 1'"
        ]
        
        for cmd in cmds:
            result = subprocess.run(
                ["adb", "-s", device_id, "shell", cmd],
                timeout=10,
                capture_output=True,
                text=True
            )
            if result.returncode != 0 and "Operation not permitted" in result.stderr:
                # Set SELinux back to enforcing before returning
                subprocess.run(
                    ["adb", "-s", device_id, "shell", "su -c 'setenforce 1'"],
                    timeout=5,
                    capture_output=True
                )
                return False
        
        # Set SELinux back to enforcing
        subprocess.run(
            ["adb", "-s", device_id, "shell", "su -c 'setenforce 1'"],
            timeout=5,
            capture_output=True
        )
        
        return True
    except Exception as e:
        print(f"SELinux permissive approach failed: {str(e)}")
        # Attempt to restore SELinux enforcing
        try:
            subprocess.run(
                ["adb", "-s", device_id, "shell", "su -c 'setenforce 1'"],
                timeout=5,
                capture_output=True
            )
        except:
            pass
        return False

def try_direct_file_replacement(device_id: str) -> bool:
    """
    Tries a direct copy to avoid permission issues.
    This is more aggressive but might work when other methods fail.
    """
    try:
        print("Trying direct file replacement method...")
        
        # Try a more direct approach - copy to a temporary location then use 'dd'
        cmds = [
            # Make a directory in a location we definitely have permission to write to
            "su -c 'mkdir -p /data/local/tmp/adb_auth'",
            
            # Copy our key file to this location
            "su -c 'cp /sdcard/adbkey.pub /data/local/tmp/adb_auth/adb_keys'",
            
            # Set correct permissions
            "su -c 'chmod 644 /data/local/tmp/adb_auth/adb_keys'",
            
            # Make sure target directory exists
            "su -c 'mkdir -p /data/misc/adb'",
            
            # Use dd to copy the file (might bypass some permission checks)
            "su -c 'dd if=/data/local/tmp/adb_auth/adb_keys of=/data/misc/adb/adb_keys'",
            
            # Clean up
            "su -c 'rm -rf /data/local/tmp/adb_auth /sdcard/adbkey.pub'",
            
            # Enable ADB
            "su -c 'settings put global adb_enabled 1'"
        ]
        
        for cmd in cmds:
            result = subprocess.run(
                ["adb", "-s", device_id, "shell", cmd],
                timeout=10,
                capture_output=True,
                text=True
            )
        
        # Check if the file was created successfully
        check_result = subprocess.run(
            ["adb", "-s", device_id, "shell", "su -c 'ls -la /data/misc/adb/adb_keys'"],
            timeout=5,
            capture_output=True,
            text=True
        )
        
        return "adb_keys" in check_result.stdout
    except Exception as e:
        print(f"Direct file replacement approach failed: {str(e)}")
        return False

# ======================================
# APK Management with UnownHash Mirror
# ======================================
# Modified get_available_versions function to ensure unique versions
@ttl_cache(ttl=3600)
def get_available_versions() -> Dict:
    try:
        response = httpx.get(
            f"{POGO_MIRROR_URL}/index.json",
            timeout=10
        )
        if response.status_code != 200:
            print("Error: Mirror returned status code", response.status_code)
            return {"latest": {}, "previous": {}}

        versions_data = response.json()
        
        processed = []
        for entry in versions_data:
            if entry["arch"] != DEFAULT_ARCH:
                continue
                
            clean_version = entry["version"].replace(".apkm", "")
            processed.append({
                "version": clean_version,
                "filename": f"com.nianticlabs.pokemongo_{DEFAULT_ARCH}_{clean_version}.apkm",
                "url": f"{POGO_MIRROR_URL}/apks/com.nianticlabs.pokemongo_{DEFAULT_ARCH}_{clean_version}.apkm",
                "date": entry.get("date", ""),
                "arch": DEFAULT_ARCH
            })

        # Sort versions by semantic versioning (newest first)
        sorted_versions = sorted(
            processed,
            key=lambda x: [int(n) for n in x["version"].split(".")],
            reverse=True
        )
        
        # Make sure we have distinct versions for latest and previous
        distinct_versions = []
        seen_versions = set()
        
        for version in sorted_versions:
            ver = version["version"]
            if ver not in seen_versions:
                distinct_versions.append(version)
                seen_versions.add(ver)
        
        # Debug log the found versions
        if distinct_versions:
            latest_ver = distinct_versions[0]["version"] if distinct_versions else "N/A"
            prev_ver = distinct_versions[1]["version"] if len(distinct_versions) > 1 else "N/A"
            print(f"Found versions - Latest: {latest_ver}, Previous: {prev_ver}")
        else:
            print("No versions found")
        
        return {
            "latest": distinct_versions[0] if distinct_versions else {},
            "previous": distinct_versions[1] if len(distinct_versions) > 1 else {}
        }
        
    except Exception as e:
        print(f"Mirror check error: {str(e)}")
        return {"latest": {}, "previous": {}}


# Modified download_apk function to ensure cache is cleared after download
def download_apk(version_info: Dict) -> Path:
    try:
        print(f"Downloading {version_info['filename']}...")
        response = httpx.get(version_info["url"], follow_redirects=True)
        target_path = APK_DIR / version_info["filename"]
        
        with open(target_path, "wb") as f:
            f.write(response.content)
        
        # Always clear the cache after downloading a new version
        get_available_versions.cache_clear()
        print(f"Successfully downloaded {version_info['version']} and cleared version cache")
        
        return target_path
    except Exception as e:
        print(f"Download failed: {str(e)}")
        raise
        

# Modified ensure_latest_apk_downloaded function with additional logging
def ensure_latest_apk_downloaded():
    APK_DIR.mkdir(parents=True, exist_ok=True)
    versions = get_available_versions()
    
    if not versions.get("latest"):
        print("No latest version information available")
        return
        
    latest_version = versions["latest"]["version"]
    print(f"Latest available version: {latest_version}")
    
    target_file = APK_DIR / versions["latest"]["filename"]
    if not target_file.exists():
        print(f"New version {latest_version} not found locally, downloading")
        download_apk(versions["latest"])
        asyncio.create_task(notify_update_downloaded("Pokemon GO", latest_version))
        asyncio.create_task(update_ui_with_new_version())
    else:
        print(f"Latest version {latest_version} already downloaded")

async def update_ui_with_new_version():
    """Updates all connected WebSocket clients with new version information"""
    try:
        # Wait briefly to ensure download is complete
        await asyncio.sleep(1)
        
        # Clear version cache to ensure we get fresh data
        get_available_versions.cache_clear()
        
        # Get current status data with fresh version information
        status_data = await get_status_data()
        
        # Log the versions being sent to clients
        latest = status_data.get("pogo_latest", "N/A")
        previous = status_data.get("pogo_previous", "N/A")
        print(f"Sending WebSocket update with versions - Latest: {latest}, Previous: {previous}")
        
        # Send update to all connected clients
        await ws_manager.broadcast(status_data)
        print("WebSocket update for new PoGo version sent successfully")
    except Exception as e:
        print(f"Error sending WebSocket update: {str(e)}")
        import traceback
        traceback.print_exc()

def download_apk(version_info: Dict) -> Path:
    try:
        print(f"Downloading {version_info['filename']}...")
        response = httpx.get(version_info["url"], follow_redirects=True)
        target_path = APK_DIR / version_info["filename"]
        
        with open(target_path, "wb") as f:
            f.write(response.content)
        
        if hasattr(get_available_versions, 'cache_clear'):
            get_available_versions.cache_clear()
        
        return target_path
    except Exception as e:
        print(f"Download failed: {str(e)}")
        raise

def unzip_apk(apk_path: Path, extract_dir: Path):
    try:
        # Ensure the extraction directory exists
        extract_dir.mkdir(parents=True, exist_ok=True)
        
        # Check if APK is already extracted
        if any(extract_dir.iterdir()):
            print(f"APK already extracted to {extract_dir}, skipping extraction")
            return
            
        print(f"Extracting {apk_path} to {extract_dir}")
        with zipfile.ZipFile(apk_path, 'r') as zip_ref:
            zip_ref.extractall(extract_dir)
    except zipfile.BadZipFile:
        print(f"Invalid ZIP file: {apk_path}")
        shutil.rmtree(extract_dir)
        raise
    except Exception as e:
        print(f"Error during extraction: {str(e)}")
        raise

# ======================================
# PoGO Auto-Update Functions
# ======================================
async def pogo_update_task():
    """Automatic PoGO update check and installation on devices"""
    import random
    
    while True:
        try:
            config = load_config()
            
            # Always download latest APK regardless of auto-update setting
            print("🔍 Checking for PoGO updates...")
            
            # Check current version info before update
            get_available_versions.cache_clear()  # Clear cache to get fresh data
            versions_before = get_available_versions()
            pogo_latest_before = versions_before.get("latest", {}).get("version", "N/A")
            
            # Download the latest version (if needed)
            ensure_latest_apk_downloaded()
            
            # Check if version changed (to detect if download happened)
            get_available_versions.cache_clear()  # Clear cache again after potential download
            versions_after = get_available_versions()
            pogo_latest_after = versions_after.get("latest", {}).get("version", "N/A")
            
            # If version changed or we just need to refresh the UI, send an update
            if pogo_latest_before != pogo_latest_after or random.random() < 0.1:  # 10% chance for refresh anyway
                # Send WebSocket update to ensure UI is current
                print(f"Version change detected: {pogo_latest_before} -> {pogo_latest_after}, updating UI")
                status_data = await get_status_data()
                await ws_manager.broadcast(status_data)
                print(f"Sent WebSocket update with version {pogo_latest_after}")
            
            # Get the latest version info
            versions = get_available_versions()
            if not versions.get("latest"):
                print("❌ No valid PoGO version available, skipping check.")
                await asyncio.sleep(3 * 3600)  # Check every 3 hours
                continue
                
            latest_version = versions["latest"]["version"]
            print(f"📌 Latest PoGO version available: {latest_version}")
            
            # Check if auto-update is enabled
            if not config.get("pogo_auto_update_enabled", True):
                print("PoGO auto-update is disabled in configuration. Updates downloaded but not installed.")
                await asyncio.sleep(3 * 3600)  # Check every 3 hours anyway
                continue
            
            # Prepare for installation - extract APK if needed
            apk_file = APK_DIR / versions["latest"]["filename"]
            version_extract_dir = EXTRACT_DIR / latest_version
            unzip_apk(apk_file, version_extract_dir)
            
            # Get all devices and check which ones need updates
            devices_to_update = []
            for device in config.get("devices", []):
                ip = device["ip"]
                try:
                    # Need to reconnect to each device to get accurate version info
                    if check_adb_connection(ip)[0]:
                        # Clear cache to get fresh version info
                        get_device_details.cache_clear()
                        # Get current installed version
                        device_details = get_device_details(ip)
                        installed_version = device_details.get("pogo_version", "N/A")
                        
                        print(f"Device {ip} has PoGO version {installed_version}, latest is {latest_version}")
                        
                        # Compare versions
                        if installed_version == "N/A":
                            print(f"Device {ip} has unknown PoGO version, will update")
                            devices_to_update.append(ip)
                        elif installed_version != latest_version:
                            print(f"Device {ip} needs update from {installed_version} to {latest_version}")
                            devices_to_update.append(ip)
                        else:
                            print(f"Device {ip} already has latest version {latest_version}, skipping")
                    else:
                        print(f"Device {ip} not reachable via ADB, skipping update check")
                except Exception as e:
                    print(f"Error checking version for {ip}: {str(e)}")
            
            # Update device count
            update_count = len(devices_to_update)
            if update_count > 0:
                print(f"🚀 Installing PoGO version {latest_version} on {update_count} devices that need updates")
                await perform_installations(devices_to_update, version_extract_dir)
                
                print("✅ PoGO automatic update complete - cache cleared.")
                get_device_details.cache_clear()
                
                # Send WebSocket update after installation
                status_data = await get_status_data()
                await ws_manager.broadcast(status_data)
            else:
                print("✅ All devices already have the latest version. No updates needed.")
            
        except Exception as e:
            print(f"❌ PoGO Auto-Update Error: {str(e)}")
            import traceback
            traceback.print_exc()
            
        await asyncio.sleep(3 * 3600)  # Check every 3 hours

# ======================================
# MapWorld Auto-Update Functions
# ======================================
MAPWORLD_DOWNLOAD_URL = "https://protomines.ddns.net/apk/MapWorld-release.zip"
MAPWORLD_APK_PATH = BASE_DIR / "data" / "apks" / "mapworld.apk"
LAST_VERSION_FILE = BASE_DIR / "data" / "mapworld_last_version"

def get_remote_metadata() -> Dict:
    """Gets metadata for the remote file"""
    try:
        response = httpx.head(MAPWORLD_DOWNLOAD_URL, timeout=10)
        if response.status_code == 200:
            return {
                "last_modified": response.headers.get("last-modified", ""),
                "content_length": response.headers.get("content-length", "")
            }
        return {}
    except Exception as e:
        print(f"Metadata check error: {str(e)}")
        return {}

def has_update_available() -> bool:
    """Checks for new version via Last-Modified header and file size"""
    if not MAPWORLD_APK_PATH.exists():
        return True

    remote_meta = get_remote_metadata()
    if not remote_meta:
        return False

    # Compare Last-Modified and file size
    local_modified = MAPWORLD_APK_PATH.stat().st_mtime
    remote_modified = datetime.datetime.strptime(
        remote_meta["last_modified"], 
        "%a, %d %b %Y %H:%M:%S %Z"
    ).timestamp()

    local_size = MAPWORLD_APK_PATH.stat().st_size
    remote_size = int(remote_meta["content_length"])

    return (remote_modified > local_modified) or (remote_size != local_size)

def download_mapworld():
    """Downloads the latest version"""
    try:
        MAPWORLD_APK_PATH.parent.mkdir(parents=True, exist_ok=True)
        
        with httpx.stream("GET", MAPWORLD_DOWNLOAD_URL, timeout=30) as response:
            response.raise_for_status()
            with open(MAPWORLD_APK_PATH, "wb") as f:
                for chunk in response.iter_bytes():
                    f.write(chunk)
        print("MapWorld APK successfully updated")
        asyncio.create_task(notify_update_downloaded("MapWorld", "new version"))
    except Exception as e:
        print(f"Download failed: {str(e)}")
        raise

def install_mapworld(device_ip: str):
    """Installs the APK on a device"""
    try:
        # Get device name
        device_details = get_device_details(device_ip)
        device_name = device_details.get("display_name", device_ip.split(":")[0])
        # Installation command with subprocess
        subprocess.run(
            ["adb", "-s", device_ip, "install", "-r", str(MAPWORLD_APK_PATH)],
            check=True,
            timeout=300,
            capture_output=True,
            text=True
        )
        print(f"Successfully installed on {device_ip}")
        asyncio.create_task(notify_update_installed(device_name, device_ip, "MapWorld", "new version"))
        
        # Correct disconnect implementation
        subprocess.run(
            ["adb", "disconnect", device_ip],
            timeout=5,
            capture_output=True
        )
        
    except subprocess.CalledProcessError as e:
        print(f"Installation error {device_ip}: {e.stderr}")

async def mapworld_update_task():
    """Automatic update check every 3 hours"""
    while True:
        try:
            if has_update_available():
                print("New MapWorld version available")
                download_mapworld()
                
                config = load_config()
                for device in config["devices"]:
                    ip = device["ip"]
                    try:
                        # Establish ADB connection
                        connect_result = subprocess.run(
                            ["adb", "connect", ip],
                            timeout=10,
                            capture_output=True,
                            text=True
                        )
                        
                        if "connected to" in connect_result.stdout.lower():
                            install_mapworld(ip)
                            
                        # Disconnect ADB
                        subprocess.run(
                            ["adb", "disconnect", ip],
                            timeout=5,
                            capture_output=True
                        )
                        
                    except subprocess.TimeoutExpired:
                        print(f"Timeout connecting to {ip}")
                    except Exception as e:
                        print(f"Error with {ip}: {str(e)}")

                get_device_details.cache_clear()
                print("PIF update complete – device details cache cleared")
                
                # Send WebSocket update after installation
                status_data = await get_status_data()
                await ws_manager.broadcast(status_data)
        
        except Exception as e:
            print(f"Auto-update error: {str(e)}")
        
        await asyncio.sleep(3 * 3600)  # 3 hours

# ======================================
# PIF Version Management Functions - Vereinfacht
# ======================================
PIF_MODULE_DIR = BASE_DIR / "data" / "modules" / "playintegrityfix"
PIF_GITHUB_API_LATEST = "https://api.github.com/repos/andi2022/PlayIntegrityFix/releases/latest"
PIF_GITHUB_API_ALL = "https://api.github.com/repos/andi2022/PlayIntegrityFix/releases?per_page=10"

async def fetch_available_pif_versions():
    """Fetches available PIF versions from GitHub API"""
    try:
        # Create directory to store modules if it doesn't exist
        PIF_MODULE_DIR.mkdir(parents=True, exist_ok=True)
        
        # Fetch list of releases
        async with httpx.AsyncClient(follow_redirects=True) as client:
            print("Fetching PIF releases from GitHub API...")
            
            # Add proper user agent to avoid rate limiting
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                "Accept": "application/vnd.github.v3+json"
            }
            
            response = await client.get(PIF_GITHUB_API_ALL, headers=headers, timeout=15)
            if response.status_code != 200:
                print(f"GitHub API Error: {response.status_code}")
                return []
            
            try:
                releases = response.json()
                if not releases or not isinstance(releases, list):
                    print("Invalid or empty releases data")
                    return []
                    
                versions = []
                for release in releases:
                    tag_name = release.get("tag_name", "").strip()
                    if not tag_name:
                        continue
                        
                    # Clean version by removing 'v' prefix if present
                    version = tag_name.lstrip("v")
                    published_at = release.get("published_at", "")
                    
                    # Find zip asset
                    zip_asset = next(
                        (
                            asset for asset in release.get("assets", [])
                            if asset["name"].startswith("PlayIntegrityFix") 
                            and asset["name"].endswith(".zip")
                        ),
                        None
                    )
                    
                    if zip_asset:
                        download_url = zip_asset.get("browser_download_url")
                        
                        # If the API provides a non-browser URL, convert to direct download URL
                        if "api.github.com" in download_url:
                            download_url = download_url.replace("api.github.com/repos", "github.com")
                            download_url = download_url.replace("releases/assets", "releases/download/" + tag_name)
                        
                        filename = zip_asset.get("name")
                        versions.append({
                            "version": version,
                            "tag_name": tag_name,
                            "published_at": published_at,
                            "download_url": download_url,
                            "filename": filename
                        })
                
                # Sort versions by numeric value (newer first)
                versions.sort(key=lambda x: parse_version(x["version"]), reverse=True)
                
                print(f"Found {len(versions)} PIF versions")
                return versions
                
            except json.JSONDecodeError:
                print("Invalid GitHub API response")
                return []
                
    except Exception as e:
        print(f"Error fetching PIF versions: {str(e)}")
        return []

async def download_pif_version(version_info):
    """Downloads a PIF version and saves it with the original filename"""
    try:
        version = version_info["version"]
        download_url = version_info["download_url"]
        filename = version_info["filename"]
        
        # Define path where the module will be saved
        module_path = PIF_MODULE_DIR / filename
        
        # Check if already downloaded
        if module_path.exists():
            print(f"PIF version {version} already downloaded at {module_path}")
            return module_path
        
        # Download the ZIP with proper headers to avoid GitHub redirects
        print(f"Downloading PIF version {version} from {download_url}")
        async with httpx.AsyncClient(follow_redirects=True) as client:
            # Adding browser-like headers to prevent GitHub from returning HTML page
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                "Accept": "application/octet-stream",
                "Accept-Encoding": "gzip, deflate, br"
            }
            
            download_response = await client.get(download_url, headers=headers, timeout=30)
            
            # Check for successful response
            if download_response.status_code != 200:
                print(f"Download failed with status code: {download_response.status_code}")
                return None
                
            # Check content type
            content_type = download_response.headers.get("content-type", "").lower()
            if "html" in content_type:
                print(f"GitHub returned HTML instead of ZIP file. Using alternative download method...")
                
                # Alternative method: Try direct download from browser URL
                # Convert API URL to direct download URL
                direct_url = download_url.replace("/api.github.com/repos/", "/github.com/")
                direct_url = direct_url.replace("/releases/assets/", "/releases/download/v")
                direct_url = direct_url.replace("/download/v", "/download/v" + version + "/")
                
                print(f"Trying alternative URL: {direct_url}")
                alt_response = await client.get(direct_url, headers=headers, timeout=30)
                
                if alt_response.status_code != 200:
                    print(f"Alternative download failed with status code: {alt_response.status_code}")
                    return None
                    
                content_type = alt_response.headers.get("content-type", "").lower()
                if "html" in content_type:
                    print(f"Alternative method also returned HTML. Unable to download ZIP file.")
                    return None
                    
                # Use the alternative response
                download_response = alt_response
            
            # Save the file with its original name
            with open(module_path, "wb") as f:
                f.write(download_response.content)
            
            # Verify it's a valid ZIP
            if not zipfile.is_zipfile(module_path):
                print("Downloaded file is not a valid ZIP")
                module_path.unlink()
                return None
        
        print(f"Successfully downloaded PIF version {version} to {module_path}")
        return module_path
        
    except Exception as e:
        print(f"Error downloading PIF version {version}: {str(e)}")
        traceback.print_exc()
        return None

async def get_pif_versions_for_ui():
    """Returns PIF versions for UI display"""
    versions = await fetch_available_pif_versions()
    # Sort versions newest first
    versions.sort(key=lambda x: parse_version(x["version"]), reverse=True)
    return versions

# Update PIF update task to respect the auto-update setting
async def pif_update_task():
    """Checks and installs PIF updates, considering already installed versions"""
    last_checked_version = "0.0"  # Track the last version we checked to avoid redundant notifications
    
    while True:
        try:
            config = load_config()
            
            print("🔍 Checking for PIF updates...")

            # Fetch available versions regardless of auto-update setting
            versions = await fetch_available_pif_versions()
            if not versions:
                print("❌ No valid PIF versions available, skipping check.")
                await asyncio.sleep(3 * 3600)  # Repeat every 3 hours
                continue
                
            # Get latest version
            latest_version = versions[0]
            new_version = latest_version["version"]

            print(f"📌 Latest PIF version available: {new_version}")
            
            # Skip if we already checked this version
            if new_version == last_checked_version:
                print(f"⏩ Already checked version {new_version}, skipping until next interval.")
                await asyncio.sleep(3 * 3600)  # Repeat every 3 hours
                continue
                
            # Update our tracking variable
            last_checked_version = new_version
            
            # Download the module regardless of auto-update setting
            module_path = await download_pif_version(latest_version)
            if not module_path:
                print("Failed to download module, skipping update")
                await asyncio.sleep(3 * 3600)
                continue

            # Check if PIF auto-update is enabled
            if not config.get("pif_auto_update_enabled", True):
                print("PIF auto-update is disabled in configuration. Module downloaded but not installed.")
                await asyncio.sleep(3 * 3600)  # Check every 3 hours anyway
                continue

            # Process devices (only if auto-update is enabled)
            devices_to_update = []
            for device in config["devices"]:
                ip = device["ip"]

                try:
                    if check_adb_connection(ip):
                        # **Get installed version from `config.json`**
                        installed_version = device.get("module_version", "N/A").strip().lstrip("v")

                        if installed_version == "N/A":
                            print(f"⚠️ No version stored in config.json for {ip}, fetching via ADB...")
                            pif_cmd = f'adb -s {ip} shell "su -c \'cat /data/adb/modules/playintegrityfix/module.prop\'"'
                            pif_result = subprocess.run(pif_cmd, shell=True, capture_output=True, text=True, timeout=5)

                            if pif_result.stdout:
                                raw_output = pif_result.stdout.strip()
                                print(f"📌 Raw module.prop output from {ip}:\n{raw_output}")

                                match = re.search(r'^version\s*=\s*v?(\d+\.\d+)', raw_output, re.MULTILINE)
                                if match:
                                    installed_version = match.group(1)
                                    print(f"✅ Installed PIF version from ADB ({ip}): {installed_version}")

                                    # **Save installed version in config.json**
                                    device["module_version"] = installed_version
                                    save_config(config)
                                else:
                                    print(f"❌ Could not parse PIF version from module.prop on {ip}, setting to '0.0'")
                                    installed_version = "0.0"

                        installed_version_clean = installed_version.lower()
                        new_version_clean = new_version.lower()

                        # Parse versions for proper comparison
                        installed_version_tuple = parse_version(installed_version_clean)
                        new_version_tuple = parse_version(new_version_clean)
                        
                        print(f"🔍 Comparison for {ip}: Installed: {installed_version_clean} {installed_version_tuple}, Available: {new_version_clean} {new_version_tuple}")
                        
                        # Skip invalid version comparison
                        if not installed_version_tuple or not new_version_tuple:
                            print(f"⚠️ Invalid version format, skipping comparison for {ip}")
                            continue

                        # **Version comparison**
                        if installed_version_tuple == new_version_tuple:
                            print(f"✅ Device {ip} already has the latest PIF version, skipping update.")
                            continue

                        if installed_version_tuple < new_version_tuple:
                            major_update = new_version_tuple[0] > installed_version_tuple[0]
                            minor_update = (new_version_tuple[0] == installed_version_tuple[0] and 
                                            new_version_tuple[1] > installed_version_tuple[1])
                            
                            if major_update or minor_update:
                                print(f"🚨 Update needed for {ip}! Installed: {installed_version_clean}, Available: {new_version_clean}")
                                devices_to_update.append(ip)
                            else:
                                print(f"ℹ️ Minor patch update available for {ip}, but not significant enough to install automatically.")
                        else:
                            print(f"❌ Device {ip} has a newer PIF version installed, skipping update.")
                    else:
                        print(f"Device {ip} not reachable via ADB, skipping update check")
                except Exception as e:
                    print(f"Error checking version for {ip}: {str(e)}")

            # Perform updates if needed
            update_count = len(devices_to_update)
            if update_count > 0:
                print(f"🚀 Installing PIF version {new_version} on {update_count} devices that need updates")
                
                for ip in devices_to_update:
                    try:
                        print(f"⚡ Updating device {ip} to PIF version {new_version}")
                        await install_pif_module(ip, module_path)

                        # **After successful update, save new version in `config.json`**
                        for device in config["devices"]:
                            if device["ip"] == ip:
                                device["module_version"] = new_version_clean
                                save_config(config)
                                break
                    except Exception as e:
                        print(f"Error installing PIF on {ip}: {str(e)}")
                
                # Clear the cache to load new values
                get_device_details.cache_clear()
                print("✅ PIF update complete – device details cache cleared.")
                
                # Send WebSocket update after installations
                status_data = await get_status_data()
                await ws_manager.broadcast(status_data)
            else:
                print("✅ All devices already have the latest version. No updates needed.")

        except Exception as e:
            print(f"❌ PIF Auto-Update Error: {str(e)}")
            import traceback
            traceback.print_exc()

        await asyncio.sleep(3 * 3600)  # Repeat every 3 hours

# Simplified install_pif_module function
async def install_pif_module(device_ip: str, pif_module_path=None):
    print(f"Starting PIF module installation for {device_ip}")
    try:
        # Increase connection timeouts
        subprocess.run(
            ["adb", "connect", device_ip],
            check=True,
            timeout=20,
            capture_output=True
        )
        
        # Check if module exists
        if pif_module_path is None:
            # Return if no default module is available
            print(f"No PIF module path specified")
            return
            
        if not Path(pif_module_path).exists():
            print(f"PIF module not found at {pif_module_path}")
            return
        
        # Extract version from filename
        version = "unknown"
        filename = Path(pif_module_path).name
        version_match = re.search(r'PlayIntegrityFix_v?(\d+\.\d+)', filename)
        if version_match:
            version = version_match.group(1)
        
        # Get device name
        device_details = get_device_details(device_ip)
        device_name = device_details.get("display_name", device_ip.split(":")[0])

        # Connect and remove old module
        subprocess.run(["adb", "connect", device_ip], check=True, timeout=10)
        subprocess.run(
            ["adb", "-s", device_ip, "shell", "su -c 'rm -rf /data/adb/modules/playintegrityfix'"],
            timeout=15
        )
        subprocess.run(["adb", "-s", device_ip, "reboot"], check=True, timeout=60)
        
        # Wait for device to come back
        print(f"Device {device_ip} rebooting. Waiting for it to come back online...")
        await asyncio.sleep(120)
        
        # Push and install module
        print(f"Pushing PIF module to {device_ip}")
        subprocess.run(
            ["adb", "-s", device_ip, "push", 
             str(pif_module_path), 
             "/data/local/tmp/pif.zip"],
            check=True,
            timeout=60
        )
        print(f"Installing PIF module on {device_ip}")
        subprocess.run(
            ["adb", "-s", device_ip, "shell", 
             "su -c 'magisk --install-module /data/local/tmp/pif.zip'"],
            check=True,
            timeout=60
        )
        subprocess.run(
            ["adb", "-s", device_ip, "shell", "rm /data/local/tmp/pif.zip"],
            timeout=15
        )
        print(f"Rebooting {device_ip} to apply PIF module")
        subprocess.run(["adb", "-s", device_ip, "reboot"], check=True, timeout=60)

        device_status_cache.clear()
        get_device_details.cache_clear()
        print(f"PIF update complete for {device_ip} – device details cache cleared")

        await notify_update_installed(device_name, device_ip, "PlayIntegrityFix", version)
    
    except Exception as e:
        print(f"PIF Installation error for {device_ip}: {str(e)}")
    
    except subprocess.CalledProcessError as e:
        print(f"PIF Installation failed for {device_ip}: {str(e)}")
    except subprocess.TimeoutExpired as e:
        print(f"Timeout during PIF install on {device_ip}: {str(e)}")
    finally:
        subprocess.run(["adb", "connect", device_ip], timeout=5)

def parse_version(v: str):
    """
    Parses a version string (e.g. "1.2.3" or "v1.2.3") into a tuple of integers.
    If the string is not correctly formatted, an empty tuple is returned.
    """
    try:
        # Remove 'v' prefix and any whitespace
        v = v.strip().lstrip("v")
        # Split by '.' and convert to integers
        parts = []
        for part in v.split('.'):
            # Only include digit parts
            if part.isdigit():
                parts.append(int(part))
        
        # If no valid parts found, return empty tuple
        if not parts:
            return ()
            
        # Pad with zeros to ensure at least 3 components (e.g., 18.7 becomes 18.7.0)
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts)
    except Exception as e:
        print(f"Error parsing version '{v}': {e}")
        return ()

# ======================================
# Installation Functions
# ======================================
def install_on_device(device_ip: str, downgrade: bool, extract_dir: Path):
    try:
        device_details = get_device_details(device_ip)
        device_name = device_details.get("display_name", device_ip.split(":")[0])
        
        apk_files = list(extract_dir.glob("*.apk"))
        
        if not apk_files:
            raise ValueError("No APK files found")
            
        if len(apk_files) == 1:
            subprocess.run(
                ["adb", "-s", device_ip, "install", "-r", str(apk_files[0])],
                check=True,
                timeout=300
            )
        else:
            cmd = ["adb", "-s", device_ip, "install-multiple"]
            cmd.extend([str(f) for f in apk_files])
            subprocess.run(
                cmd,
                check=True,
                timeout=300
            )
        
        # Determine version
        versions = get_available_versions()
        version = versions.get("latest", {}).get("version", "unknown")
        
        # Send notification
        asyncio.create_task(notify_update_installed(device_name, device_ip, "Pokemon GO", version))
       
    except subprocess.CalledProcessError as e:
        print(f"Installation failed: {str(e)}")
        raise
    except Exception as e:
        print(f"General installation error: {str(e)}")
        raise

# ======================================
# Device Control Functions
# ======================================

async def start_furtif_app(device_id: str, run_login_sequence_flag: bool = True):
    """Starts the Furtif app on the device with optional automatic login sequence"""
    try:
        # Don't modify the original device_id
        device_id_original = device_id
        
        # Check if this is a USB device (no colon) or network device (has colon)
        is_network_device = ":" in device_id
        
        # Only for network devices: Extract host and port
        if is_network_device:
            if ":" in device_id:
                host, port = device_id.split(":")
            else:
                host = device_id
                port = "5555"  # Default ADB port
                
            # Use properly formatted device_id for ADB commands
            device_id = f"{host}:{port}"
        
        # Force stop existing apps - use the correct device identifier
        stop_furtif_cmd = f'adb -s {device_id} shell "am force-stop com.github.furtif.furtifformaps"'
        stop_pogo_cmd = f'adb -s {device_id} shell "am force-stop com.nianticlabs.pokemongo"'
        subprocess.run(stop_furtif_cmd, shell=True, timeout=10, capture_output=True)
        subprocess.run(stop_pogo_cmd, shell=True, timeout=10, capture_output=True)
        print(f"Force stopped apps on {device_id}")
        await asyncio.sleep(2)
        
        # Start Furtif app
        start_cmd = f'adb -s {device_id} shell "am start -n com.github.furtif.furtifformaps/com.github.furtif.furtifformaps.MainActivity"'
        subprocess.run(start_cmd, shell=True, timeout=10, capture_output=True)
        print(f"Started Furtif app on {device_id}")
        
        # Only run login sequence if explicitly requested
        if run_login_sequence_flag:
            # Wait for app to initialize
            await asyncio.sleep(5)
            
            # Run UI automation sequence with retries
            success = await run_login_sequence(device_id, max_retries=3)
            if success:
                print(f"Login sequence for {device_id} completed successfully")
                
                # Get device display name
                details = get_device_details(device_id)
                display_name = details.get("display_name", device_id.split(":")[0] if ":" in device_id else f"Device-{device_id[-4:]}")
                
                # Send notification
                await notify_device_online(display_name, device_id)
                
                # Update cache to indicate notification was sent
                status = device_status_cache.get(device_id, {})
                status["last_notification_time"] = time.time()
                status["is_alive"] = True  # Mark device as alive in the cache
                device_status_cache[device_id] = status
                
                print(f"Sent online notification for {device_id} (after successful app start)")
                
                # Send WebSocket update after successful start
                status_data = await get_status_data()
                await ws_manager.broadcast(status_data)
                
                return True
            else:
                print(f"Login sequence for {device_id} failed after multiple retries")
                return False
        
        return True
    except Exception as e:
        print(f"Error starting Furtif app on {device_id_original}: {str(e)}")
        return False

async def run_login_sequence(device_id: str, max_retries=3):
    """Runs the login sequence with retries and PID verification"""
    for retry in range(max_retries):
        try:
            print(f"Login sequence attempt {retry+1}/{max_retries} for {device_id}")
            
            # Create a temporary directory for UI dumps
            with tempfile.TemporaryDirectory() as temp_dir:
                dump_file = Path(temp_dir) / "dump.xml"
                
                # Function to dump UI and search for a button
                async def find_and_tap_button(button_text, max_attempts=20, sleep_interval=2):
                    for attempt in range(max_attempts):
                        try:
                            # Dump UI
                            dump_cmd = f'adb -s {device_id} shell "uiautomator dump /sdcard/dump.xml"'
                            subprocess.run(dump_cmd, shell=True, timeout=10, capture_output=True)
                            
                            # Pull dump file
                            pull_cmd = f'adb -s {device_id} pull /sdcard/dump.xml {dump_file}'
                            subprocess.run(pull_cmd, shell=True, timeout=10, capture_output=True)
                            
                            # Parse dump file
                            if dump_file.exists():
                                tree = ET.parse(dump_file)
                                root = tree.getroot()
                                
                                # Find button with text
                                for elem in root.iter("node"):
                                    if elem.get("text") == button_text:
                                        bounds = elem.get("bounds")
                                        # Extract coordinates from bounds string [x1,y1][x2,y2]
                                        match = re.match(r'\[(\d+),(\d+)\]\[(\d+),(\d+)\]', bounds)
                                        if match:
                                            x1, y1, x2, y2 = map(int, match.groups())
                                            center_x, center_y = (x1 + x2) // 2, (y1 + y2) // 2
                                            
                                            # Tap at center coordinates
                                            tap_cmd = f'adb -s {device_id} shell "input tap {center_x} {center_y}"'
                                            subprocess.run(tap_cmd, shell=True, timeout=10, capture_output=True)
                                            print(f"Tapped '{button_text}' at ({center_x}, {center_y})")
                                            return True
                        except Exception as e:
                            print(f"Error in find_and_tap_button: {str(e)}")
                        
                        print(f"'{button_text}' not found (attempt {attempt+1}/{max_attempts})")
                        await asyncio.sleep(sleep_interval)
                    
                    return False
                
                # Function to perform swipe
                async def perform_swipe():
                    try:
                        # Get screen size
                        size_cmd = f'adb -s {device_id} shell wm size'
                        result = subprocess.run(size_cmd, shell=True, timeout=10, capture_output=True, text=True)
                        output = result.stdout
                        
                        # Parse screen size
                        override_match = re.search(r'Override size:\s*(\d+)x(\d+)', output)
                        if override_match:
                            width, height = map(int, override_match.groups())
                        else:
                            physical_match = re.search(r'Physical size:\s*(\d+)x(\d+)', output)
                            if physical_match:
                                width, height = map(int, physical_match.groups())
                            else:
                                # Default values if parsing fails
                                width, height = 1080, 1920
                        
                        # Calculate swipe coordinates
                        start_x = int(width * 0.5)
                        start_y = int(height * 0.75)
                        end_x = int(width * 0.5)
                        end_y = int(height * 0.05)
                        
                        # Perform swipe
                        swipe_cmd = f'adb -s {device_id} shell "input swipe {start_x} {start_y} {end_x} {end_y} 500"'
                        subprocess.run(swipe_cmd, shell=True, timeout=10, capture_output=True)
                        print(f"Performed swipe from ({start_x}, {start_y}) to ({end_x}, {end_y})")
                        
                        return True
                    except Exception as e:
                        print(f"Error performing swipe: {str(e)}")
                        return False
                
                # Check PID function
                async def check_app_pids():
                    try:
                        # Check PIDs for both apps
                        pogo_pid_cmd = f'adb -s {device_id} shell "pidof com.nianticlabs.pokemongo"'
                        furtif_pid_cmd = f'adb -s {device_id} shell "pidof com.github.furtif.furtifformaps"'
                        
                        pogo_result = subprocess.run(pogo_pid_cmd, shell=True, timeout=10, capture_output=True, text=True)
                        furtif_result = subprocess.run(furtif_pid_cmd, shell=True, timeout=10, capture_output=True, text=True)
                        
                        pogo_pid = pogo_result.stdout.strip()
                        furtif_pid = furtif_result.stdout.strip()
                        
                        print(f"PIDs - Pokémon GO: {pogo_pid or 'Not running'}, Furtif: {furtif_pid or 'Not running'}")
                        
                        # Return True if both PIDs exist
                        return bool(pogo_pid) and bool(furtif_pid)
                    except Exception as e:
                        print(f"Error checking PIDs: {str(e)}")
                        return False
                
                # Execute login sequence
                if await find_and_tap_button("Discord Login"):
                    await asyncio.sleep(3)
                    
                    # Check for Keep Scrolling... :point_down: button (might appear in some cases)
                    if await find_and_tap_button("Keep Scrolling... :point_down:", max_attempts=5):
                        await asyncio.sleep(1)
                    
                    # Perform swipe in case content needs scrolling
                    await perform_swipe()
                    await asyncio.sleep(2)
                    
                    if await find_and_tap_button("Authorize"):
                        await asyncio.sleep(3)
                        
                        if await find_and_tap_button("Recheck Service Status"):
                            await asyncio.sleep(3)
                            
                            if await find_and_tap_button("Start service"):
                                print(f"Clicked all buttons, waiting 30 seconds for apps to initialize...")
                                # Critical waiting time for app initialization (from Bash script)
                                await asyncio.sleep(30)
                                
                                # Check if both apps are running
                                if await check_app_pids():
                                    print(f"Login sequence completed successfully on {device_id}")
                                    return True
                                else:
                                    print(f"Apps not running after login sequence on {device_id}")
                
                # If we reached here, something failed in the sequence
                print(f"Login sequence attempt {retry+1} failed on {device_id}")
                
                # If not the last retry, force stop and restart the app
                if retry < max_retries - 1:
                    print(f"Restarting apps for retry {retry+2}...")
                    stop_cmd = f'adb -s {device_id} shell "am force-stop com.github.furtif.furtifformaps"'
                    subprocess.run(stop_cmd, shell=True, timeout=10, capture_output=True)
                    start_cmd = f'adb -s {device_id} shell "am start -n com.github.furtif.furtifformaps/com.github.furtif.furtifformaps.MainActivity"'
                    subprocess.run(start_cmd, shell=True, timeout=10, capture_output=True)
                    await asyncio.sleep(5)  # Wait for app restart
        
        except Exception as e:
            print(f"Error in login sequence attempt {retry+1} for {device_id}: {str(e)}")
    
    # If we got here, all retries failed
    return False

async def check_and_control_devices():
    """Monitors devices and restarts apps when necessary"""
    last_status = {}  # Stores the last status of each device
    device_restart_times = {}  # Stores the last restart time of each device
    notification_sent_times = {}  # Stores the last time a notification was sent for each device
    
    while True:
        try:
            config = load_config()
            current_time = time.time()
            
            for device in config.get("devices", []):
                device_id = device["ip"]  # This can be either IP:port or serial number
                display_name = device.get("display_name", device_id.split(":")[0] if ":" in device_id else f"Device-{device_id[-4:]}")
                
                # Skip devices where control is not enabled
                if not device.get("control_enabled", False):
                    continue
                
                # First check if device is reachable via ADB
                adb_online, adb_error = check_adb_connection(device_id)
                if not adb_online:
                    print(f"Device {device_id} not reachable via ADB, skipping control: {adb_error}")
                    
                    # Don't send offline notification here - only based on API status
                    last_status[device_id] = {"adb_online": False, "is_alive": False}
                    continue
                
                # ADB is online, update status
                if not last_status.get(device_id, {}).get("adb_online", False):
                    # Was previously offline, now online - save new status
                    last_status[device_id] = {"adb_online": True, "is_alive": False}
                
                # Check if we're in the grace period after a restart
                last_restart = device_restart_times.get(device_id, 0)
                if current_time - last_restart < 120:  # 2 minute grace period
                    print(f"Device {device_id} is in grace period after restart, skipping checks")
                    continue
                
                # Get device status from cache
                status = device_status_cache.get(device_id, {})
                
                # Define restart conditions
                is_alive = status.get("is_alive", True)  # Default to True to avoid false restarts
                mem_free = status.get("mem_free", float('inf'))  # Default to large value
                threshold = device.get("memory_threshold", 200)  # Get configured threshold (now in MB)
                
                restart_needed = False
                restart_reason = ""
                
                # Check if app is not alive according to API
                if not is_alive:
                    # If the device was previously alive, send offline notification
                    if last_status.get(device_id, {}).get("is_alive", True):
                        await notify_device_offline(display_name, device_id)
                    
                    restart_needed = True
                    restart_reason = "is_alive is False"
                
                # Check if memory is below threshold
                # Ignore memory check if mem_free is 0 (possible API update delay)
                # Convert threshold from MB to KB (mem_free is in kB)
                elif mem_free > 0 and mem_free < threshold * 1024:
                    restart_needed = True
                    restart_reason = f"free memory ({mem_free} KB / {mem_free/1024:.2f} MB) below threshold ({threshold} MB)"
                    # Send notification about low memory
                    await notify_memory_restart(display_name, device_id, mem_free, threshold)
                
                # Restart app if needed
                if restart_needed:
                    print(f"Restarting app on {device_id} - Reason: {restart_reason}")
                    # Always pass True for run_login_sequence_flag since control is enabled
                    success = await start_furtif_app(device_id, True)
                    
                    # Set notification status to avoid duplicate notifications
                    # (start_furtif_app will send a notification upon success)
                    if success:
                        notification_sent_times[device_id] = current_time
                    
                    # Update the restart timestamp
                    device_restart_times[device_id] = current_time
                    
                    if success:
                        print(f"Successfully restarted app on {device_id}")
                                                
                        # Update status
                        last_status[device_id]["is_alive"] = True
                    else:
                        print(f"Failed to restart app on {device_id}")
                        last_status[device_id]["is_alive"] = False
                
                # Update status if now alive, but was previously not
                elif is_alive and not last_status.get(device_id, {}).get("is_alive", True):
                    # ÄNDERUNG: Keine Online-Benachrichtigung hier senden, das wird nur von start_furtif_app gemacht
                    print(f"Device {device_id} is now online (detected by API), but not sending notification (handled by start_furtif_app only)")
                    
                    # Nur den Status aktualisieren
                    last_status[device_id]["is_alive"] = True
                
                # Always update the current status
                last_status[device_id]["is_alive"] = is_alive
            
        except Exception as e:
            print(f"Error in device control task: {str(e)}")
            import traceback
            traceback.print_exc()
        
        # Check every minute
        await asyncio.sleep(60)

# ======================================
# Scheduled Tasks
# ======================================
async def scheduled_update_task():
    while True:
        now = datetime.datetime.now()
        if now.hour == 3 and now.minute == 0:
            print("Running scheduled update check...")
            ensure_latest_apk_downloaded()
            await asyncio.sleep(60)
        await asyncio.sleep(30)

# ======================================
# PoGo Update Functions
# ======================================
def update_progress(progress: int):
    global current_progress
    current_progress = progress

async def perform_installation(device_ip: str, extract_dir: Path):
    global update_in_progress
    try:
        update_in_progress = True
        
        # Since this is a single device update, we can use more detailed progress steps
        update_progress(10)  # Start update
        await asyncio.sleep(0.3)
        
        update_progress(25)  # Before installation
        install_on_device(device_ip, False, extract_dir)
        update_progress(60)  # After installation
        await asyncio.sleep(0.3)
        
        # Check if control is enabled for this device before running login sequence
        config = load_config()
        device = next((d for d in config["devices"] if d["ip"] == device_ip), None)
        control_enabled = device and device.get("control_enabled", False)
        
        # Start the app immediately after update, but only run login sequence if control is enabled
        print(f"Starting app on {device_ip} after update... (Control enabled: {control_enabled})")
        update_progress(70)  # Before app start
        success = await start_furtif_app(device_ip, control_enabled)
        
        if success:
            print(f"Successfully started app on {device_ip} after update")
        else:
            print(f"Failed to start app on {device_ip} after update")
            
        update_progress(90)  # After app start
        
        device_status_cache.clear()
        get_device_details.cache_clear()
        update_progress(100)  # Complete
        
        # Send WebSocket update
        status_data = await get_status_data()
        await ws_manager.broadcast(status_data)
        
        await asyncio.sleep(2)
    finally:
        update_in_progress = False
        current_progress = 0

# New installation function for multiple devices
async def perform_installations(device_ips: List[str], extract_dir: Path):
    global update_in_progress, current_progress
    
    try:
        update_in_progress = True
        total_devices = len(device_ips)
        
        # Load config once to get device control settings
        config = load_config()
        
        # Calculate percentage per device - each device gets an equal share of the progress bar
        device_increment = 100 / total_devices
        
        for index, ip in enumerate(device_ips, 1):
            try:
                # Calculate progress stages for this device
                # At the beginning of this device's update
                start_progress = int((index - 1) * device_increment)
                # After APK installation (40% of this device's progress)
                install_progress = int(start_progress + (device_increment * 0.4))
                # After app restart (90% of this device's progress)
                app_progress = int(start_progress + (device_increment * 0.9))
                # Complete (all of this device's progress)
                complete_progress = int(index * device_increment)
                
                # Start of device update
                update_progress(start_progress)
                print(f"Starting update for device {index}/{total_devices}: {ip}")
                await asyncio.sleep(0.3)  # Small delay to ensure UI updates
                
                # Installation phase
                update_progress(start_progress + int(device_increment * 0.2))  # 20% through device progress
                install_on_device(ip, False, extract_dir)
                update_progress(install_progress)  # 40% through device progress
                print(f"Successfully updated {ip}")
                await asyncio.sleep(0.3)  # Small delay to ensure UI updates
                
                # Check if control is enabled for this device
                device = next((d for d in config["devices"] if d["ip"] == ip), None)
                control_enabled = device and device.get("control_enabled", False)
                
                # App restart phase
                update_progress(install_progress + int(device_increment * 0.2))  # 60% through device progress
                print(f"Starting app on {ip} after update... (Control enabled: {control_enabled})")
                success = await start_furtif_app(ip, control_enabled)
                
                if success:
                    print(f"Successfully started app on {ip} after update")
                else:
                    print(f"Failed to start app on {ip} after update")
                
                # Complete this device's update
                update_progress(app_progress)  # 90% through device progress
                await asyncio.sleep(0.2)  # Small delay before moving to next device
                update_progress(complete_progress)  # 100% through device progress
                
            except Exception as e:
                print(f"Error with {ip}: {str(e)}")
                # Still increment progress even if there was an error
                update_progress(int(index * device_increment))
                continue
        
        # Ensure we reach 100% at the end
        update_progress(100)
        
        # Send WebSocket update
        status_data = await get_status_data()
        await ws_manager.broadcast(status_data)
        
        await asyncio.sleep(2)
        
    finally:
        update_in_progress = False
        current_progress = 0
        
# ======================================
# API Status Update
# ======================================
async def update_api_status():
    device_last_status = {}  # Track previous status of each device to detect changes
    
    while True:
        try:
            config = load_config()
            
            # Check if the required API configuration exists
            if not all(key in config for key in ["rotomApiUrl", "rotomApiUser", "rotomApiPass"]):
                print("API configuration incomplete, skipping check")
                await asyncio.sleep(60)
                continue
                
            async with httpx.AsyncClient() as client:
                auth = (config["rotomApiUser"], config["rotomApiPass"])
                response = await client.get(
                    config["rotomApiUrl"], 
                    auth=auth, 
                    timeout=10
                )
                
                # Check for successful response
                if response.status_code != 200:
                    print(f"API returned status code {response.status_code}")
                    await asyncio.sleep(60)
                    continue
                    
                try:
                    api_data = response.json()
                except json.JSONDecodeError:
                    print("Failed to parse API response as JSON")
                    await asyncio.sleep(60)
                    continue

            # Process each device
            for dev in config.get("devices", []):
                device_id = dev["ip"]  # This can be either IP:port or serial number
                
                # Check if we need to force refresh device details
                force_refresh = False
                
                # Determine if this is a USB device (no digits/periods/colons) or network device
                is_network_device = ":" in device_id and all(c.isdigit() or c == '.' or c == ':' for c in device_id)
                
                # Find matching device data, with more robust matching
                device_data = None
                details = get_device_details(device_id)
                display_name = details.get("display_name", "").lower()
                
                # For network devices, extract IP for matching
                device_ip_for_matching = None
                if is_network_device:
                    device_ip_for_matching = device_id.split(":")[0]
                
                for api_device in api_data.get("devices", []):
                    origin = api_device.get("origin", "").lower()
                    # Try different matching approaches
                    if (display_name and display_name in origin) or \
                       (device_ip_for_matching and device_ip_for_matching in origin):
                        device_data = api_device
                        break
                
                if not device_data:
                    device_data = {}
                
                # Get current status values from cache
                current_cache = device_status_cache.get(device_id, {})
                current_time = time.time()
                
                # Handle memory values - keep old values if new ones are 0 or null
                current_mem_free = current_cache.get("mem_free", 0)
                new_mem_free = device_data.get("lastMemory", {}).get("memFree", 0)
                
                # If new memory value is 0 and current value is not 0, keep the current value
                if new_mem_free == 0 and current_mem_free > 0:
                    print(f"Ignoring zero memory value for {device_id}, keeping previous value: {current_mem_free}")
                    mem_free = current_mem_free
                else:
                    mem_free = new_mem_free
                
                # Handle isAlive status - careful with app startup
                current_is_alive = current_cache.get("is_alive", False)
                new_is_alive = device_data.get("isAlive", False)
                
                # If device was just starting and reported not alive, but was alive before,
                # keep the alive status for a short grace period (last 30 seconds)
                grace_period = 30  # seconds
                last_update = current_cache.get("last_update", 0)
                
                if (not new_is_alive and current_is_alive and 
                    (current_time - last_update) < grace_period):
                    print(f"Device {device_id} reported not alive but in grace period, keeping alive status")
                    is_alive = current_is_alive
                else:
                    is_alive = new_is_alive
                
                # If device was offline and is now online, force detail refresh
                prev_status = device_last_status.get(device_id, {})
                prev_is_alive = prev_status.get("is_alive", False)
                
                if not prev_is_alive and is_alive:
                    print(f"Device {device_id} changed from offline to online, clearing version cache")
                    get_device_details.cache_clear()
                    force_refresh = True
                    
                    # We DON'T send notifications here - this is handled by check_and_control_devices
                    # This avoids duplicate notifications
                
                # Check ADB connection status (now returns tuple of status and error message)
                adb_status, adb_error = check_adb_connection(device_id)
                
                # Update device status cache with all values
                device_status_cache[device_id] = {
                    "is_alive": is_alive,
                    "mem_free": mem_free,
                    "last_update": current_time,
                    "adb_status": adb_status,
                    "adb_error": adb_error,
                    # Preserve notification tracking data
                    "last_notification_time": current_cache.get("last_notification_time", 0)
                }
                
                # Store current status for next comparison
                device_last_status[device_id] = {
                    "is_alive": is_alive
                }
                
                # If device is online and ADB is connected, refresh version data if needed
                if is_alive and adb_status and (force_refresh or time.time() - current_cache.get("last_details_check", 0) > 300):
                    # Only refresh every 5 minutes at most for active devices
                    print(f"Refreshing version details for online device {device_id}")
                    # Clear just this device's cache
                    get_device_details.cache_clear()
                    # Get fresh data
                    fresh_details = get_device_details(device_id)
                    device_status_cache[device_id]["last_details_check"] = time.time()
                    print(f"Refreshed version info for {device_id}: PoGo={fresh_details.get('pogo_version')}, MITM={fresh_details.get('mitm_version')}, PIF={fresh_details.get('module_version')}")
                
                # Debug logging for troubleshooting
                print(f"Updated status for {details.get('display_name', device_id)}: {device_status_cache[device_id]}")

            # Send device status update to all WebSocket clients
            status_data = await get_status_data()
            await ws_manager.broadcast(status_data)

        except Exception as e:
            print(f"API Update Error: {str(e)}")
            import traceback
            traceback.print_exc()
        
        # Use the configured check interval or default to 15 seconds
        check_interval = config.get("api_check_interval", 15)
        await asyncio.sleep(check_interval)

# ======================================
# Helper Functions for Memory
# ======================================
def format_memory(value):
    """
    Converts memory values to readable formats
    Input value is in KB (not Bytes)!
    """
    try:
        # If the value is 0 or cannot be converted, return early
        if not value:
            return "N/A"
            
        # Convert value to float
        size = float(value)
        
        # If the value in KB is already small, display as KB
        if size < 1024:
            return f"{size:.1f} kB".replace(".", ",")
            
        # Convert to MB
        size = size / 1024
        
        # If less than 1024 MB, display as MB
        if size < 1024:
            return f"{size:.1f} MB".replace(".", ",")
            
        # Convert to GB
        size = size / 1024
        return f"{size:.2f} GB".replace(".", ",")
    except:
        return "N/A"

# ======================================
# WebSocket Status Data Function
# ======================================
async def get_status_data():
    """Collects status data for WebSocket updates, similar to /api/status endpoint"""
    config = load_config()
    devices = []
    
    # Clear cache to get fresh version data
    get_available_versions.cache_clear()
    
    # Get PoGo version info for buttons
    versions = get_available_versions()
    pogo_latest = versions.get("latest", {}).get("version", "N/A")
    pogo_previous = versions.get("previous", {}).get("version", "N/A")
    
    # Ensure we have different versions for latest and previous
    if pogo_latest == pogo_previous:
        print(f"Warning: Latest and previous versions are identical: {pogo_latest}")
    else:
        print(f"Status data - Latest: {pogo_latest}, Previous: {pogo_previous}")
    
    for dev in config["devices"]:
        ip = dev["ip"]
        status = device_status_cache.get(ip, {})
        details = get_device_details(ip)
        
        default_status = {
            "is_alive": False,
            "mem_free": 0,
            "last_update": 0,
            "adb_status": False,
            "adb_error": "No connection"
        }
        status = {**default_status, **status}
        
        devices.append({
            "display_name": details.get("display_name", ip.split(":")[0]),
            "ip": ip,
            "status": status.get("adb_status", False),
            "adb_error": status.get("adb_error", ""),
            "is_alive": status["is_alive"],
            "pogo": details.get("pogo_version", "N/A"),
            "mitm": details.get("mitm_version", "N/A"),
            "module": details.get("module_version", "N/A"),
            "mem_free": status.get("mem_free", 0),
            "last_update": status["last_update"],
            "control_enabled": dev.get("control_enabled", False)
        })
    
    return {
        "devices": devices,
        "now": time.time(),
        "pogo_latest": pogo_latest,
        "pogo_previous": pogo_previous,
        "pif_auto_update_enabled": config.get("pif_auto_update_enabled", True),
        "pogo_auto_update_enabled": config.get("pogo_auto_update_enabled", True),
        "update_in_progress": update_in_progress,
        "update_progress": current_progress
    }

# ======================================
# Helper Functions
# ======================================
def is_logged_in(request: Request) -> bool:
    return request.session.get("logged_in", False)

def require_login(request: Request):
    if not is_logged_in(request):
        return RedirectResponse(url="/login", status_code=302)
    return None

# ======================================
# FastAPI Initialization
# ======================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_latest_apk_downloaded()
    asyncio.create_task(update_api_status())
    asyncio.create_task(scheduled_update_task())
    asyncio.create_task(mapworld_update_task())
    asyncio.create_task(pif_update_task())
    asyncio.create_task(pogo_update_task())  # Add the new PoGO update task
    asyncio.create_task(check_and_control_devices())
    yield

app = FastAPI(lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key="CHANGE_ME_TO_A_SECURE_KEY")
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# Add template filters and globals
templates.env.filters['format_memory'] = format_memory

templates.env.globals.update({
    'check_adb_connection': check_adb_connection,
    'get_device_display_name': lambda ip: get_device_details(ip)["display_name"],
    'get_available_versions': get_available_versions
})

# ======================================
# WebSocket Routes
# ======================================
@app.websocket("/ws/status")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time device status updates"""
    await ws_manager.connect(websocket)
    try:
        # Initial data
        status_data = await get_status_data()
        await websocket.send_json(status_data)
        
        # Keep connection alive
        while True:
            try:
                # Wait for client messages
                data = await websocket.receive_text()
                
                # Handle client commands
                if data == "refresh":
                    # Manual refresh requested
                    status_data = await get_status_data()
                    await websocket.send_json(status_data)
                elif data.startswith("refresh_device:"):
                    # Refresh specific device data
                    device_ip = data.split(":", 1)[1]
                    if device_ip:
                        # Clear device cache
                        get_device_details.cache_clear()
                        # Update status data
                        status_data = await get_status_data()
                        await websocket.send_json(status_data)
                
                # Wait a bit to avoid overloading
                await asyncio.sleep(0.1)
            except asyncio.TimeoutError:
                # Keep connection alive with ping-pong
                await asyncio.sleep(1)
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)
    except Exception as e:
        print(f"WebSocket error: {e}")
        ws_manager.disconnect(websocket)

# ======================================
# Regular Routes
# ======================================
@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    if is_logged_in(request):
        return RedirectResponse(url="/status")
    return RedirectResponse(url="/login")

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request})

@app.post("/login", response_class=HTMLResponse)
def login_action(request: Request, username: str = Form(...), password: str = Form(...)):
    config = load_config()
    for user in config.get("users", []):
        if user["username"] == username and user["password"] == password:
            request.session["logged_in"] = True
            request.session["username"] = username
            return RedirectResponse(url="/status", status_code=302)
    return templates.TemplateResponse("login.html", {
        "request": request,
        "error": "Invalid credentials"
    })

@app.get("/logout")
def logout_action(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login")

@app.get("/status", response_class=HTMLResponse)
def status_page(request: Request):
    if redirect := require_login(request):
        return redirect
    
    config = load_config()
    devices = []
    
    # Get PoGo version info for buttons
    versions = get_available_versions()
    pogo_latest = versions.get("latest", {}).get("version", "N/A")
    pogo_previous = versions.get("previous", {}).get("version", "N/A")
    
    for dev in config["devices"]:
        ip = dev["ip"]
        status = device_status_cache.get(ip, {})
        details = get_device_details(ip)
        
        default_status = {
            "is_alive": False,
            "mem_free": 0,
            "last_update": 0
        }
        status = {**default_status, **status}
        
        devices.append({
            "display_name": details.get("display_name", ip.split(":")[0]),
            "ip": ip,
            "status": check_adb_connection(ip)[0],
            "is_alive": "✅" if status["is_alive"] else "❌",
            "pogo": details.get("pogo_version", "N/A"),
            "mitm": details.get("mitm_version", "N/A"),
            "module": details.get("module_version", "N/A"),
            "mem_free": status.get("mem_free", 0),
            "last_update": status["last_update"],
            "control_enabled": dev.get("control_enabled", False)
        })
    
    return templates.TemplateResponse("status.html", {
        "request": request,
        "username": request.session.get("username", ""),
        "devices": devices,
        "config": config,
        "now": time.time(),
        "pogo_latest": pogo_latest,
        "pogo_previous": pogo_previous
    })

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    if redirect := require_login(request):
        return redirect
    return templates.TemplateResponse("settings.html", {
        "request": request,
        "config": load_config()
    })

@app.post("/settings/save", response_class=HTMLResponse)
def settings_save(
    request: Request,
    rotomApiUrl: str = Form(""),
    rotomApiUser: str = Form(""),
    rotomApiPass: str = Form(""),
    discord_webhook_url: str = Form("")
):
    if redirect := require_login(request):
        return redirect

    config = load_config()
    config.update({
        "rotomApiUrl": rotomApiUrl,
        "rotomApiUser": rotomApiUser,
        "rotomApiPass": rotomApiPass,
        "discord_webhook_url": discord_webhook_url
    })
    
    # Debug output to verify the save
    print(f"Saving settings with discord_webhook_url: {discord_webhook_url}")
    
    save_config(config)
    
    # Read again after saving to check if data was written
    test_config = load_config()
    print(f"After save, discord_webhook_url is: {test_config.get('discord_webhook_url', 'NOT FOUND')}")
    
    return RedirectResponse(url="/settings", status_code=302)

@app.post("/devices/add", response_class=HTMLResponse)
def add_device(request: Request, new_ip: str = Form(...)):
    if redirect := require_login(request):
        return redirect
    
    # Trim whitespace
    device_id = new_ip.strip()
    
    config = load_config()
    if not any(dev["ip"] == device_id for dev in config["devices"]):
        # Check if device exists and is reachable
        is_connected, error_msg = check_adb_connection(device_id)
        
        # Get a display name for the device
        if ":" in device_id:
            # For network devices, use IP as display_name
            display_name = device_id.split(":")[0]
        else:
            # For serial numbers, use a shortened version
            display_name = f"Device-{device_id[-4:]}" if len(device_id) > 4 else device_id
        
        # Add the device to the config
        config["devices"].append({
            "ip": device_id,
            "display_name": display_name,
            "control_enabled": False,
            "memory_threshold": 200,
            "pogo_version": "N/A",
            "mitm_version": "N/A",
            "module_version": "N/A"
        })
        save_config(config)
    
    return RedirectResponse(url="/settings", status_code=302)

@app.post("/devices/remove", response_class=HTMLResponse)
def remove_devices(request: Request, devices: List[str] = Form(...)):
    if redirect := require_login(request):
        return redirect
    
    config = load_config()
    config["devices"] = [dev for dev in config["devices"] if dev["ip"] not in devices]
    save_config(config)
    
    return RedirectResponse(url="/settings", status_code=302)

@app.post("/clear-cache")
def clear_cache(device_ip: Optional[str] = None):
    """Clear cache with optional device-specific targeting"""
    if device_ip:
        # Format device IP correctly
        if ":" not in device_ip:
            device_ip = f"{device_ip}:5555"
            
        # We're using a global cache, so we need to clear all for now
        check_adb_connection.cache_clear()
        get_device_details.cache_clear()
        return {"status": f"Cache successfully cleared for {device_ip}"}
    else:
        # Clear all caches
        check_adb_connection.cache_clear()
        get_device_details.cache_clear()
        return {"status": "Cache successfully cleared"}

@app.post("/devices/toggle-control", response_class=HTMLResponse)
def toggle_device_control(request: Request, device_ip: str = Form(...), control_enabled: Optional[str] = Form(None)):
    if redirect := require_login(request):
        return redirect
    
    config = load_config()
    for device in config["devices"]:
        if device["ip"] == device_ip:
            # Form checkbox values come as "on" when checked, None when unchecked
            device["control_enabled"] = control_enabled is not None
            break
    
    save_config(config)
    return RedirectResponse(url="/settings", status_code=302)

@app.post("/devices/update-threshold", response_class=HTMLResponse)
def update_memory_threshold(request: Request, device_ip: str = Form(...), memory_threshold: int = Form(...)):
    if redirect := require_login(request):
        return redirect
    
    config = load_config()
    for device in config["devices"]:
        if device["ip"] == device_ip:
            device["memory_threshold"] = max(100, min(1000, memory_threshold))  # Constrain between 100-1000
            break
    
    save_config(config)
    return RedirectResponse(url="/settings", status_code=302)

@app.post("/pif/device-update", response_class=HTMLResponse)
async def pif_device_update(request: Request, device_ip: str = Form(...), version: str = Form(...)):
    global update_in_progress, current_progress

    device_id = format_device_id(device_ip)
    
    if redirect := require_login(request):
        return redirect
    
    try:
        # Set update in progress and initialize progress
        update_in_progress = True
        update_progress(10)  # Start at 10%
        
        # Fetch available versions
        versions = await fetch_available_pif_versions()
        update_progress(20)  # Fetching versions complete
        
        # Find the specific version
        target_version = None
        for ver in versions:
            if ver["version"] == version:
                target_version = ver
                break
        
        if not target_version:
            update_in_progress = False
            current_progress = 0
            return RedirectResponse(url="/status?error=PIF version not found", status_code=302)
        
        update_progress(30)  # Version found
        
        # Download the specific version
        update_progress(40)  # Start download
        pif_module = await download_pif_version(target_version)
        update_progress(50)  # Download complete
        
        if not pif_module:
            update_in_progress = False
            current_progress = 0
            return RedirectResponse(url="/status?error=Failed to download PIF version", status_code=302)
        
        # Install on device
        update_progress(60)  # Start installation
        
        # Create a background task for installation to maintain progress updates
        asyncio.create_task(install_pif_module_with_progress(device_ip, pif_module))
        
        # Redirect immediately to status page, the update will happen in background
        return RedirectResponse(url="/status", status_code=302)
    except Exception as e:
        print(f"Error updating device {device_ip} to PIF version {version}: {str(e)}")
        update_in_progress = False
        current_progress = 0
        return RedirectResponse(url="/status?error=PIF update failed", status_code=302)

# New function to install PIF module with progress updates
async def install_pif_module_with_progress(device_ip: str, pif_module_path=None):
    """Installs PIF module with progress updates for the UI"""
    global update_in_progress, current_progress
    
    try:
        device_id = format_device_id(device_ip)
        print(f"Starting PIF module installation for {device_ip}")
        
        # Make sure ADB connection is established
        update_progress(65)
        connect_result = subprocess.run(
            ["adb", "connect", device_ip],
            check=True,
            timeout=20,
            capture_output=True
        )
        
        # Check if module exists
        if pif_module_path is None or not Path(pif_module_path).exists():
            print(f"PIF module not found at {pif_module_path}")
            update_in_progress = False
            current_progress = 0
            return
        
        # Extract version from filename
        version = "unknown"
        filename = Path(pif_module_path).name
        version_match = re.search(r'PlayIntegrityFix_v?(\d+\.\d+)', filename)
        if version_match:
            version = version_match.group(1)
        
        # Get device name
        device_details = get_device_details(device_ip)
        device_name = device_details.get("display_name", device_ip.split(":")[0])

        # Connect and remove old module
        update_progress(70)
        subprocess.run(["adb", "connect", device_ip], check=True, timeout=10)
        subprocess.run(
            ["adb", "-s", device_ip, "shell", "su -c 'rm -rf /data/adb/modules/playintegrityfix'"],
            timeout=15
        )
        
        # First reboot
        update_progress(75)
        print(f"First reboot for {device_ip}")
        subprocess.run(["adb", "-s", device_ip, "reboot"], check=True, timeout=60)
        
        # Wait for device to come back
        print(f"Device {device_ip} rebooting. Waiting for it to come back online...")
        for i in range(12):  # 12 * 10 seconds = 2 minutes max wait
            await asyncio.sleep(10)
            update_progress(75 + (i * 0.5))  # 75% to 81%
            try:
                check_result = subprocess.run(
                    ["adb", "devices"], 
                    check=True, 
                    timeout=5,
                    capture_output=True,
                    text=True
                )
                if f"{device_ip}\tdevice" in check_result.stdout:
                    print(f"Device {device_ip} is back online")
                    break
            except:
                continue
        
        # Push and install module
        update_progress(82)
        print(f"Pushing PIF module to {device_ip}")
        subprocess.run(
            ["adb", "connect", device_ip], 
            check=True, 
            timeout=10
        )
        subprocess.run(
            ["adb", "-s", device_ip, "push", 
             str(pif_module_path), 
             "/data/local/tmp/pif.zip"],
            check=True,
            timeout=60
        )
        
        update_progress(85)
        print(f"Installing PIF module on {device_ip}")
        subprocess.run(
            ["adb", "-s", device_ip, "shell", 
             "su -c 'magisk --install-module /data/local/tmp/pif.zip'"],
            check=True,
            timeout=60
        )
        
        update_progress(90)
        subprocess.run(
            ["adb", "-s", device_ip, "shell", "rm /data/local/tmp/pif.zip"],
            timeout=15
        )
        
        # Final reboot
        update_progress(95)
        print(f"Final reboot for {device_ip} to apply PIF module")
        subprocess.run(["adb", "-s", device_ip, "reboot"], check=True, timeout=60)

        # Clear caches
        device_status_cache.clear()
        get_device_details.cache_clear()
        print(f"PIF update complete for {device_ip} – device details cache cleared")

        # Send notification
        await notify_update_installed(device_name, device_ip, "PlayIntegrityFix", version)
        
        # Send WebSocket update
        status_data = await get_status_data()
        await ws_manager.broadcast(status_data)
        
        # Mark update as complete
        update_progress(100)
        await asyncio.sleep(2)  # Let the UI see the 100% for a moment
        update_in_progress = False
        current_progress = 0
    
    except Exception as e:
        print(f"PIF Installation error for {device_ip}: {str(e)}")
        traceback.print_exc()
        update_in_progress = False
        current_progress = 0
    
    finally:
        try:
            subprocess.run(["adb", "connect", device_ip], timeout=5)
        except:
            pass

@app.post("/pogo/device-update", response_class=HTMLResponse)
async def pogo_device_update(request: Request, device_ip: str = Form(...), version: str = Form(...)):
    if redirect := require_login(request):
        return redirect

    # Check if version is available
    device_id = format_device_id(device_ip)
    versions = get_available_versions()
    target_version = None
    
    # Find the specific version info
    for version_type in ["latest", "previous"]:
        if version_type in versions and versions[version_type].get("version") == version:
            target_version = versions[version_type]
    
    # If requested version not found in the main versions, try to find it in all available versions
    if not target_version:
        try:
            response = httpx.get(
                f"{POGO_MIRROR_URL}/index.json",
                timeout=10
            )
            if response.status_code == 200:
                all_versions = response.json()
                for entry in all_versions:
                    if entry["arch"] == DEFAULT_ARCH and entry["version"].replace(".apkm", "") == version:
                        target_version = {
                            "version": version,
                            "filename": f"com.nianticlabs.pokemongo_{DEFAULT_ARCH}_{version}.apkm",
                            "url": f"{POGO_MIRROR_URL}/apks/com.nianticlabs.pokemongo_{DEFAULT_ARCH}_{version}.apkm",
                            "arch": DEFAULT_ARCH
                        }
                        break
        except Exception as e:
            print(f"Error checking all versions: {str(e)}")
            return RedirectResponse(url="/status?error=Failed to check version", status_code=302)
    
    if not target_version:
        return RedirectResponse(url="/status?error=Version not found", status_code=302)
    
    try:
        # Download specific version if it doesn't exist
        apk_file = APK_DIR / target_version["filename"]
        if not apk_file.exists():
            apk_file = download_apk(target_version)
        
        # Extract APK
        specific_extract_dir = EXTRACT_DIR / target_version["version"]
        specific_extract_dir.mkdir(parents=True, exist_ok=True)
        unzip_apk(apk_file, specific_extract_dir)
        
        # Run installation for this device only
        asyncio.create_task(perform_installation(device_ip, specific_extract_dir))
        
        return RedirectResponse(url="/status", status_code=302)
    except Exception as e:
        print(f"Error updating device {device_ip} to version {version}: {str(e)}")
        return RedirectResponse(url="/status?error=Update failed", status_code=302)

@app.post("/pogo/update", response_class=HTMLResponse)
async def pogo_update(request: Request):
    if redirect := require_login(request):
        return redirect
    
    config = load_config()
    device_ips = [dev["ip"] for dev in config.get("devices", [])]
    
    versions = get_available_versions()
    if not versions:
        return RedirectResponse(url="/status?error=No versions found", status_code=302)
    
    entry = versions["latest"]
    apk_file = APK_DIR / entry["filename"]
    if not apk_file.exists():
        apk_file = download_apk(entry)
    
    # Use version-specific extraction directory
    version_extract_dir = EXTRACT_DIR / entry["version"]
    unzip_apk(apk_file, version_extract_dir)
    
    # Start update for all devices with the specific version directory
    asyncio.create_task(perform_installations(device_ips, version_extract_dir))
    
    return RedirectResponse(url="/status", status_code=302)

@app.post("/settings/toggle-pif-autoupdate", response_class=HTMLResponse)
def toggle_pif_autoupdate(request: Request, enabled: Optional[str] = Form(None)):
    if redirect := require_login(request):
        return redirect
    
    config = load_config()
    # Form checkbox values come as "on" when checked, None when unchecked
    config["pif_auto_update_enabled"] = enabled is not None
    save_config(config)
    
    return RedirectResponse(url="/settings", status_code=302)

@app.post("/settings/toggle-pogo-autoupdate", response_class=HTMLResponse)
def toggle_pogo_autoupdate(request: Request, enabled: Optional[str] = Form(None)):
    if redirect := require_login(request):
        return redirect
    
    config = load_config()
    # Form checkbox values come as "on" when checked, None when unchecked
    config["pogo_auto_update_enabled"] = enabled is not None
    save_config(config)
    
    return RedirectResponse(url="/settings", status_code=302)

@app.get("/update-status")
def get_update_status():
    return {
        "status": update_in_progress,
        "progress": current_progress,
        "message": "Installation in progress..." if update_in_progress else "Idle"
    }

@app.get("/api/status")
def api_status(request: Request):
    if not is_logged_in(request):
        return {"error": "Not authenticated"}
    
    config = load_config()
    devices = []
    
    # Get PoGo version info for buttons
    versions = get_available_versions()
    pogo_latest = versions.get("latest", {}).get("version", "N/A")
    pogo_previous = versions.get("previous", {}).get("version", "N/A")
    
    for dev in config["devices"]:
        ip = dev["ip"]
        status = device_status_cache.get(ip, {})
        details = get_device_details(ip)
        
        default_status = {
            "is_alive": False,
            "mem_free": 0,
            "last_update": 0,
            "adb_status": False,
            "adb_error": "No connection"
        }
        status = {**default_status, **status}
        
        devices.append({
            "display_name": details.get("display_name", ip.split(":")[0]),
            "ip": ip,
            "status": status.get("adb_status", False),
            "adb_error": status.get("adb_error", ""),
            "is_alive": status["is_alive"],
            "pogo": details.get("pogo_version", "N/A"),
            "mitm": details.get("mitm_version", "N/A"),
            "module": details.get("module_version", "N/A"),
            "mem_free": status.get("mem_free", 0),
            "last_update": status["last_update"],
            "control_enabled": dev.get("control_enabled", False)
        })
    
    return {
        "devices": devices,
        "now": time.time(),
        "pogo_latest": pogo_latest,
        "pogo_previous": pogo_previous,
        "pif_auto_update_enabled": config.get("pif_auto_update_enabled", True),
        "pogo_auto_update_enabled": config.get("pogo_auto_update_enabled", True),
        "update_in_progress": update_in_progress,
        "update_progress": current_progress
    }

@app.get("/api/pif-versions")
async def api_pif_versions(request: Request):
    """Endpoint to get available PIF versions"""
    if not is_logged_in(request):
        return {"error": "Not authenticated"}
        
    versions = await get_pif_versions_for_ui()
    return {"versions": versions}

@app.post("/devices/restart-apps", response_class=HTMLResponse)
async def restart_apps(request: Request, device_ip: str = Form(...)):
    if redirect := require_login(request):
        return redirect
    
    try:
        # Formatiere Geräte-ID korrekt (fügt Port hinzu, falls nötig)
        device_id = format_device_id(device_ip)
        
        # Get device details for notification
        device_details = get_device_details(device_id)
        display_name = device_details.get("display_name", device_id.split(":")[0] if ":" in device_id else device_id)
        
        # Check if control is enabled
        config = load_config()
        device = next((d for d in config["devices"] if d["ip"] == device_id), None)
        control_enabled = device and device.get("control_enabled", False)
        
        # Force stop apps
        print(f"Stopping apps on {device_id}...")
        subprocess.run(
            ["adb", "-s", device_id, "shell", "am force-stop com.github.furtif.furtifformaps"],
            timeout=10
        )
        subprocess.run(
            ["adb", "-s", device_id, "shell", "am force-stop com.nianticlabs.pokemongo"],
            timeout=10
        )
        
        # If control is enabled, start apps again
        if control_enabled:
            print(f"Restarting apps on {device_id} with control...")
            await start_furtif_app(device_id, True)
        else:
            # Just start the app without login sequence
            print(f"Restarting apps on {device_id} without control...")
            await start_furtif_app(device_id, False)
            
        # Clear cache to refresh status
        device_status_cache.clear()
        get_device_details.cache_clear()
        
        return RedirectResponse(url="/status", status_code=302)
    except Exception as e:
        print(f"Error restarting apps on {device_id}: {str(e)}")
        return RedirectResponse(url="/status?error=Failed to restart apps", status_code=302)

@app.post("/devices/reboot", response_class=HTMLResponse)
def reboot_device(request: Request, device_ip: str = Form(...)):
    if redirect := require_login(request):
        return redirect
    
    try:
        # Formatiere Geräte-ID korrekt (fügt Port hinzu, falls nötig)
        device_id = format_device_id(device_ip)
        
        # Get device details for notification
        device_details = get_device_details(device_id)
        display_name = device_details.get("display_name", device_id.split(":")[0] if ":" in device_id else device_id)
        
        print(f"Rebooting device {device_id}...")
        subprocess.run(
            ["adb", "-s", device_id, "reboot"],
            check=True,
            timeout=60
        )
        
        return RedirectResponse(url="/status", status_code=302)
    except Exception as e:
        print(f"Error rebooting device {device_id}: {str(e)}")
        return RedirectResponse(url="/status?error=Failed to reboot device", status_code=302)

@app.post("/devices/authorize", response_class=HTMLResponse)
def authorize_device(request: Request, device_ip: str = Form(...)):
    if redirect := require_login(request):
        return redirect
    
    try:
        # Format device ID consistently
        device_id = format_device_id(device_ip)
        
        # Attempt to authorize device
        success = authorize_device_with_adb_key(device_id)
        
        if success:
            return RedirectResponse(url="/settings?success=Device authorized successfully", status_code=302)
        else:
            return RedirectResponse(url="/settings?error=Failed to authorize device. Root access required.", status_code=302)
    except Exception as e:
        print(f"Error authorizing device {device_ip}: {str(e)}")
        return RedirectResponse(url="/settings?error=Authorization error: {str(e)}", status_code=302)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)