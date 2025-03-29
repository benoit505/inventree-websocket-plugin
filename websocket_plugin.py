"""
WebSocket Plugin for InvenTree
Provides WebSocket server functionality

@license MIT
"""

import asyncio
import logging
import time
import threading
import importlib.util
import os
import socket
import random
import subprocess
import signal
from django.utils.translation import gettext_lazy as _
import fcntl
import errno
import json

from plugin import InvenTreePlugin
from plugin.mixins import SettingsMixin, EventMixin

from .version import WEBSOCKET_PLUGIN_VERSION

# Set up logging
logger = logging.getLogger("inventree")

def debug_log(message):
    """Write to debug log file and log at ERROR level for visibility"""
    try:
        with open('/home/inventree/data/plugin_debug.log', 'a') as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} - WEBSOCKET: {message}\n")
    except Exception as e:
        logger.error(f"WEBSOCKET: Failed to write to debug log: {str(e)}")
    
    # Also log at ERROR level for maximum visibility
    logger.error(f"WEBSOCKET: {message}")

debug_log("Loading WebSocket plugin module")

# Check if websockets is available
websockets_available = importlib.util.find_spec("websockets") is not None
debug_log(f"Websockets module available (initial check): {websockets_available}")

try:
    from port_checker import check_port, debug_log as pc_debug_log
    port_checker_available = True
except ImportError:
    port_checker_available = False
    debug_log("Port checker module not available")

def get_container_ip():
    """Get the IP address of the container"""
    try:
        # Try to get the container's IP address
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Doesn't need to be reachable, just used to determine interface IP
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception as e:
        debug_log(f"Error getting container IP: {str(e)}")
        return '127.0.0.1'

# Log the container's IP address
container_ip = get_container_ip()
debug_log(f"Container IP address: {container_ip}")

def kill_process_on_port(port):
    """Kill any process using the specified port"""
    debug_log(f"Attempting to find processes using port {port}")
    try:
        # Let's simply check if the port is available
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(('0.0.0.0', port))
            s.close()
            debug_log(f"Port {port} is available")
            return True
        except Exception as e:
            debug_log(f"Port {port} is already in use: {str(e)}")
            return False
    except Exception as e:
        debug_log(f"Error checking port {port}: {str(e)}")
        return False

# Global instance tracker
_plugin_instance = None

# Add a global variable to track if the server is running
_server_running = False

# Add this after your global variables
_lockfile_path = '/home/inventree/data/websocket_plugin.lock'
_lockfile = None

# Add near the top with other globals
_status_file_path = '/home/inventree/data/websocket_status.json'

def acquire_lock():
    """Acquire an exclusive lock to prevent multiple plugin instances from starting servers"""
    global _lockfile
    
    # Check if the lock file exists but is stale (from a crashed process)
    try:
        # If the file exists, check if it's actually locked
        if os.path.exists(_lockfile_path):
            debug_log(f"Lock file exists (PID: {os.getpid()}), checking if it's stale...")
            try:
                # Try to open and lock it - if we can, it was stale
                test_lock = open(_lockfile_path, 'w')
                try:
                    fcntl.flock(test_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    debug_log(f"Found stale lock file, replacing it (PID: {os.getpid()})")
                    # Write our PID to the file
                    test_lock.write(str(os.getpid()))
                    test_lock.flush()
                    _lockfile = test_lock  # Keep the lock
                    return True
                except IOError as e:
                    # The lock file is actually locked by another process
                    if e.errno == errno.EAGAIN:
                        debug_log(f"Lock file is actively held by another process (our PID: {os.getpid()})")
                    else:
                        debug_log(f"Error checking lock: {str(e)}")
                    test_lock.close()
                    return False
            except Exception as e:
                debug_log(f"Error checking stale lock: {str(e)}")
    except Exception as e:
        debug_log(f"Error handling existing lock file: {str(e)}")
    
    # Create a new lock file
    try:
        _lockfile = open(_lockfile_path, 'w')
        fcntl.flock(_lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # Write our PID to the file
        _lockfile.write(str(os.getpid()))
        _lockfile.flush()
        debug_log(f"Successfully acquired lock (PID: {os.getpid()})")
        return True
    except IOError as e:
        if e.errno == errno.EAGAIN:
            debug_log(f"Another instance already has the lock (our PID: {os.getpid()})")
        else:
            debug_log(f"Error acquiring lock: {str(e)}")
        return False

def release_lock():
    """Release the exclusive lock"""
    global _lockfile
    if _lockfile:
        try:
            fcntl.flock(_lockfile, fcntl.LOCK_UN)
            _lockfile.close()
            debug_log("Released lock")
            return True
        except Exception as e:
            debug_log(f"Error releasing lock: {str(e)}")
    return False

def inspect_environment():
    """Log information about the environment"""
    debug_log("Environment inspection:")
    try:
        # Check Docker environment
        is_docker = os.path.exists('/.dockerenv')
        debug_log(f"Running in Docker container: {is_docker}")
        
        # Check network interfaces
        import socket
        debug_log("Network interfaces:")
        
        try:
            # Get all network interfaces
            import netifaces
            for interface in netifaces.interfaces():
                addrs = netifaces.ifaddresses(interface)
                if netifaces.AF_INET in addrs:
                    for addr in addrs[netifaces.AF_INET]:
                        debug_log(f"  {interface}: {addr['addr']}")
        except ImportError:
            # Fallback if netifaces is not available
            hostname = socket.gethostname()
            debug_log(f"Hostname: {hostname}")
            try:
                debug_log(f"IP address: {socket.gethostbyname(hostname)}")
            except:
                debug_log("Could not determine IP address")
                
        # Check available ports
        for port in range(9020, 9030):
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind(('0.0.0.0', port))
                debug_log(f"Port {port} is available")
                s.close()
            except Exception as e:
                debug_log(f"Port {port} is in use or unavailable: {str(e)}")
                
    except Exception as e:
        debug_log(f"Error during environment inspection: {str(e)}")

def update_server_status(running=False, port=None, pid=None):
    """Update the server status file"""
    status = {
        'running': running,
        'port': port,
        'pid': pid or os.getpid(),
        'last_update': time.time(),
        'hostname': socket.gethostname(),
        'container_ip': get_container_ip()
    }
    
    try:
        with open(_status_file_path, 'w') as f:
            json.dump(status, f)
        debug_log(f"Updated server status: {status}")
    except Exception as e:
        debug_log(f"Error updating server status: {str(e)}")

def get_server_status():
    """Get the current server status"""
    if not os.path.exists(_status_file_path):
        return None
    
    try:
        with open(_status_file_path, 'r') as f:
            return json.load(f)
    except Exception as e:
        debug_log(f"Error reading server status: {str(e)}")
        return None

class WebSocketPlugin(SettingsMixin, EventMixin, InvenTreePlugin):
    """
    WebSocket Plugin class with event handling
    """
    
    NAME = "WebSocket"
    SLUG = "websocket"
    TITLE = "WebSocket Server"
    DESCRIPTION = "Provides WebSocket server functionality"
    VERSION = WEBSOCKET_PLUGIN_VERSION
    AUTHOR = "InvenTree Contributors"

    # Define required packages
    REQUIRES = ['websockets>=10.0']

    # Define settings
    SETTINGS = {
        'ENABLED': {
            'name': _('Enable Server'),
            'description': _('Enable WebSocket server'),
            'default': False,
            'validator': bool,
        },
        'HOST': {
            'name': _('Host Address'),
            'description': _('WebSocket server host address (use 0.0.0.0 to listen on all interfaces)'),
            'default': '0.0.0.0',
        },
        'PORT': {
            'name': _('Port'),
            'description': _('WebSocket server port (will try next available if busy)'),
            'default': '9000',
        },
        'EXTERNAL_URL': {
            'name': _('External URL'),
            'description': _('External WebSocket URL for clients (e.g. ws://example.com:8948)'),
            'default': '',
        },
        'FORCE_PORT': {
            'name': _('Force Port'),
            'description': _('Force the use of the specified port by killing any process using it'),
            'default': False,
            'validator': bool,
        },
        'ENABLE_EVENTS': {
            'name': _('Enable Event Publishing'),
            'description': _('Publish InvenTree events to WebSocket clients'),
            'default': True,
            'validator': bool,
        },
        'EVENT_TYPES': {
            'name': _('Event Types'),
            'description': _('Comma-separated list of event types to publish (leave empty for all)'),
            'default': 'part_part.saved,part_part.created,stock_stockitem.saved,stock_stockitem.created,'
                       'part_partparameter.saved,part_partparameter.created,part_partparameter.deleted,'
                       'part_partparametertemplate.saved,part_partparametertemplate.created,part_partparametertemplate.deleted,'
                       'stock_stockitemparameter.saved,stock_stockitemparameter.created,stock_stockitemparameter.deleted,'
                       'company_companyparameter.saved,company_companyparameter.created,company_companyparameter.deleted',
        },
    }

    def __new__(cls, *args, **kwargs):
        """Implement singleton pattern"""
        global _plugin_instance
        if _plugin_instance is None:
            debug_log("Creating new WebSocketPlugin instance")
            _plugin_instance = super(WebSocketPlugin, cls).__new__(cls)
            _plugin_instance._initialized = False
        return _plugin_instance

    def startup(self):
        """Called when plugin is first loaded"""
        debug_log(f"Plugin startup method called (PID: {os.getpid()})")
        
        # Start a background thread for periodic health checks
        self.health_check_thread = threading.Thread(target=self.periodic_health_check, daemon=True)
        self.health_check_thread.start()
        
        # Check status file first
        status = get_server_status()
        if status and status.get('running'):
            server_pid = status.get('pid')
            last_update = status.get('last_update', 0)
            
            # Only trust status if updated in the last 5 minutes
            if time.time() - last_update < 300:
                debug_log(f"Status file indicates server is running (PID: {server_pid})")
                
                # Check if that process is still alive
                try:
                    # Sending signal 0 just checks if process exists
                    os.kill(server_pid, 0)
                    debug_log(f"Process {server_pid} is still alive")
                    
                    # Server is running, update our internal state
                    global _server_running
                    _server_running = True
                    
                    # Update timestamp in status file
                    update_server_status(running=True, port=status.get('port'), pid=server_pid)
                    
                    # No need to start another server
                    return True
                except OSError:
                    debug_log(f"Process {server_pid} is no longer running, will start a new server")
            else:
                debug_log("Status file is too old, will recheck server status")
        
        # Check using lsof if no valid status file
        if self.check_running_servers():
            debug_log("Found existing WebSocket server process")
            return True
        
        # No running server found, clean up and start a new one
        debug_log("Cleaning up any stale WebSocket servers")
        self.cleanup_ports(force=True)
        
        # Check if WebSocket server should be enabled
        if self.get_setting('ENABLED', False):
            debug_log("WebSocket server is enabled, starting server")
            self.start_server_async()
        else:
            debug_log("WebSocket server is disabled in settings")
        
        return True

    def __init__(self):
        """Initialize the plugin"""
        global _server_running
        
        if hasattr(self, '_initialized') and self._initialized:
            debug_log(f"WebSocketPlugin already initialized, skipping __init__ (PID: {os.getpid()})")
            return
            
        super().__init__()
        debug_log(f"Initializing WebSocket Plugin (PID: {os.getpid()})")
        
        # Check for force_start flag
        force_start_path = '/home/inventree/data/force_start_websocket'
        if os.path.exists(force_start_path):
            debug_log("Found force_start flag file, will force start the server")
            try:
                os.remove(force_start_path)
            except:
                pass
            # We'll force start later after initialization
            self._force_start = True
        else:
            self._force_start = False
        
        # Initialize instance variables
        self.server = None
        self.thread = None
        self.loop = None
        self.actual_host = None
        self.actual_port = None
        
        # Cache external URL setting
        self.external_url = self.get_setting('EXTERNAL_URL', '')
        debug_log(f"External URL setting: {self.external_url}")
        
        # Check if websockets is available
        try:
            import websockets
            debug_log(f"Successfully imported websockets module (version: {websockets.__version__})")
        except ImportError:
            debug_log("Failed to import websockets module")
            
        self._initialized = True
        debug_log("WebSocket Plugin initialized")

        # Auto-start if enabled and no server is running yet
        if self.get_setting('ENABLED', False) and not _server_running:
            debug_log("Auto-starting WebSocket server during initialization")
            self.start_server_async()
        elif _server_running:
            debug_log("WebSocket server already running, not starting another one")

        # Call this function when plugin is loaded
        inspect_environment()

        # At the end of init:
        if self._force_start:
            debug_log("Forcing server start as requested")
            self.force_start_server()

    def start_server_async(self):
        """Start the WebSocket server asynchronously"""
        global _server_running
        debug_log("start_server_async called")
        
        # Check if ANY server is already running
        if _server_running:
            debug_log("A WebSocket server is already running. Not starting another one.")
            return True
        
        # Check server status file
        status = get_server_status()
        if status and status.get('running'):
            server_pid = status.get('pid')
            last_update = status.get('last_update', 0)
            
            # Only trust status if updated in the last 5 minutes
            if time.time() - last_update < 300:
                try:
                    # Check if process is still alive
                    os.kill(server_pid, 0)
                    debug_log(f"WebSocket server process {server_pid} is already running")
                    _server_running = True
                    return True
                except OSError:
                    debug_log(f"WebSocket server process {server_pid} is no longer running")
            else:
                debug_log("Status file is too old")
        
        # Get settings for server
        host = '0.0.0.0'  # Always use 0.0.0.0 in Docker
        port_str = self.get_setting('PORT', '9020')
        debug_log(f"Port setting (raw): {port_str}")
        
        try:
            port = int(port_str)
            debug_log(f"Port setting (parsed): {port}")
        except ValueError:
            debug_log(f"Invalid port setting: {port_str}, using default 9020")
            port = 9020
        
        # Get external URL setting
        external_url = self.get_setting('EXTERNAL_URL', '')
        
        # Before starting, check if port is available
        if not self.verify_port_availability(port):
            debug_log(f"Port {port} is not available and could not be freed")
            return False
        
        # Updated path to the server script
        server_script = "/home/inventree/data/plugins/websocket/server/websocket_server.py"
        
        # If the server file doesn't exist, copy it from the plugin directory
        if not os.path.exists(server_script):
            debug_log(f"Server script not found: {server_script}")
            # Copy the script from the plugin dir to data dir
            try:
                plugin_script = os.path.join(os.path.dirname(__file__), "server", "websocket_server.py")
                if os.path.exists(plugin_script):
                    import shutil
                    os.makedirs(os.path.dirname(server_script), exist_ok=True)
                    shutil.copy(plugin_script, server_script)
                    os.chmod(server_script, 0o755)  # Make executable
                    debug_log(f"Copied server script to {server_script}")
                else:
                    debug_log(f"Source script not found: {plugin_script}")
                    return False
            except Exception as e:
                debug_log(f"Error copying server script: {str(e)}")
                return False
        
        # Launch the server as a separate process with explicit interpreter
        debug_log(f"Launching WebSocket server script: {server_script}")
        try:
            cmd = ["/usr/local/bin/python3", server_script, str(port)]
            if external_url:
                cmd.append(external_url)
            
            # Use subprocess.Popen to start without waiting
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True  # Detach from parent process
            )
            
            debug_log(f"Started WebSocket server process with PID: {process.pid}")
            
            # Wait a moment for the server to start
            time.sleep(2)
            
            # Check if process is still running
            try:
                os.kill(process.pid, 0)
                debug_log(f"WebSocket server process {process.pid} is running")
                _server_running = True
                return True
            except OSError:
                debug_log(f"WebSocket server process {process.pid} failed to start")
                # Try to get any error output
                stderr_output = process.stderr.read().decode('utf-8', errors='ignore')
                debug_log(f"Server stderr output: {stderr_output}")
                return False
            
        except Exception as e:
            debug_log(f"Error starting WebSocket server: {str(e)}")
            return False

    def run_server_thread(self, host, port):
        """Run the WebSocket server in a thread"""
        debug_log(f"Server thread starting with host={host}, port={port}")
        
        # Import websockets in the thread context
        try:
            import websockets
            debug_log(f"Imported websockets in thread (version: {websockets.__version__})")
        except ImportError:
            debug_log("Failed to import websockets in thread")
            return
            
        # Set the event loop for this thread
        asyncio.set_event_loop(self.loop)
        debug_log("Event loop set in thread")
        
        # Start the server
        try:
            self.loop.run_until_complete(self.start_server(host, port))
            debug_log("Loop completed - this should not happen unless server stopped")
        except Exception as e:
            debug_log(f"Error in server thread: {str(e)}")
            
    async def start_server(self, host, port):
        """Start the WebSocket server"""
        debug_log(f"start_server coroutine called with host={host}, port={port}")
        
        # Always use 0.0.0.0 inside Docker
        if host != '0.0.0.0' and host != '127.0.0.1':
            debug_log(f"Cannot bind to external IP {host} from inside Docker container. Using 0.0.0.0 instead.")
            host = '0.0.0.0'
        
        try:
            import websockets
            debug_log(f"Imported websockets in coroutine (version: {websockets.__version__})")
        except ImportError:
            debug_log("Failed to import websockets in coroutine")
            return
            
        # Try to start the server
        debug_log(f"About to start WebSocket server on {host}:{port}")
        
        try:
            # Try with the specified port directly
            self.server = await websockets.serve(
                self.websocket_handler,
                host,
                port
            )
            self.actual_host = host
            self.actual_port = port
            debug_log(f"WebSocket server started successfully on {self.actual_host}:{self.actual_port}")
            
            # Update server status file
            update_server_status(running=True, port=self.actual_port)
            
            # Get external URL setting
            external_url = self.get_setting('EXTERNAL_URL', '')
            
            # Log connection information with extra details
            debug_log("====================== CONNECTION INFORMATION ======================")
            debug_log(f"WebSocket server listening on: ws://{self.actual_host}:{self.actual_port}")
            debug_log(f"Container IP address: {container_ip}")
            
            if external_url:
                debug_log(f"External WebSocket URL (from settings): {external_url}")
            else:
                debug_log("WARNING: No external URL set in plugin settings!")
                debug_log(f"Clients should connect to: ws://{container_ip}:{self.actual_port} (internal only)")
                debug_log(f"From outside Docker, try connecting to: ws://192.168.0.21:{self.actual_port}")
            
            debug_log("==================================================================")
            
            # Keep the server running
            await asyncio.Future()
        except Exception as e:
            debug_log(f"Failed to start WebSocket server: {str(e)}")
            # Release our lock since we failed to start
            release_lock()
            global _server_running
            _server_running = False

    async def websocket_handler(self, websocket, path):
        """Handle WebSocket connections"""
        remote_addr = websocket.remote_address if hasattr(websocket, 'remote_address') else 'unknown'
        debug_log(f"New WebSocket connection from {remote_addr}")
        
        try:
            # Send a welcome message immediately
            welcome = {
                "type": "welcome", 
                "message": "Welcome to InvenTree WebSocket Server",
                "server_time": time.time(),
                "client_ip": remote_addr
            }
            await websocket.send(json.dumps(welcome))
            debug_log(f"Sent welcome message to {remote_addr}")
            
            # Handle incoming messages
            async for message in websocket:
                debug_log(f"Received message from {remote_addr}: {message}")
                
                # Try to parse as JSON
                try:
                    data = json.loads(message)
                    
                    # Handle ping/test messages
                    if data.get('type') == 'ping':
                        response = {
                            "type": "pong",
                            "received": data,
                            "server_time": time.time()
                        }
                        await websocket.send(json.dumps(response))
                        debug_log(f"Sent pong response to {remote_addr}")
                    else:
                        # Echo back other messages
                        response = {
                            "type": "echo",
                            "received": data,
                            "server_time": time.time()
                        }
                        await websocket.send(json.dumps(response))
                        debug_log(f"Echoed message back to {remote_addr}")
                except json.JSONDecodeError:
                    # Just echo back non-JSON messages
                    response = f"Received: {message}"
                    await websocket.send(response)
                    debug_log(f"Sent text response to {remote_addr}")
                    
        except Exception as e:
            debug_log(f"Error in websocket handler for {remote_addr}: {str(e)}")

    def get_websocket_url(self):
        """Get the WebSocket URL for clients to connect to"""
        if not self.is_server_running():
            return None
            
        # Use the configured external URL if available
        external_url = self.get_setting('EXTERNAL_URL', '')
        if external_url:
            debug_log(f"Using configured external URL: {external_url}")
            return external_url
            
        # Fall back to container networking if no external URL is set
        # This is less reliable for external clients
        connect_host = container_ip if self.actual_host == '0.0.0.0' else self.actual_host
        return f"ws://{connect_host}:{self.actual_port}"

    def is_server_running(self):
        """Check if the WebSocket server is running"""
        global _server_running
        running = (_server_running or 
                   (self.server is not None and self.thread is not None and self.thread.is_alive()))
        debug_log(f"Server running check: {running}")
        return running

    def stop_plugin(self):
        """Stop the plugin and cleanup"""
        debug_log(f"stop_plugin called (PID: {os.getpid()})")
        
        # Check if server is running from status file
        status = get_server_status()
        if status and status.get('running'):
            server_pid = status.get('pid')
            
            if server_pid:
                debug_log(f"Stopping WebSocket server process {server_pid}")
                try:
                    # Try to terminate gracefully
                    os.kill(server_pid, signal.SIGTERM)
                    
                    # Wait a moment
                    time.sleep(1)
                    
                    # Check if it's still running
                    try:
                        os.kill(server_pid, 0)
                        debug_log(f"Process {server_pid} still running, forcing kill")
                        os.kill(server_pid, signal.SIGKILL)
                    except OSError:
                        debug_log(f"Process {server_pid} terminated successfully")
                except Exception as e:
                    debug_log(f"Error stopping server process: {str(e)}")
        
        # Update status file
        update_server_status(running=False)
        
        # Reset the server state
        global _server_running
        _server_running = False
        
        return True

    def cleanup_existing_servers(self):
        """Clean up any existing server instances"""
        global _server_running
        
        debug_log("Cleaning up any existing server instances")
        
        if self.server:
            debug_log("Closing existing server")
            try:
                # Schedule server close in the event loop
                asyncio.run_coroutine_threadsafe(self.server.close(), self.loop)
                debug_log("Server close requested")
            except Exception as e:
                debug_log(f"Error closing server: {str(e)}")
        
        # Reset the server state
        self.server = None
        _server_running = False
        
        debug_log("Server cleanup completed")
        return True

    def find_available_port(self, start_port, max_attempts=10):
        """Find an available port starting from start_port"""
        if port_checker_available:
            for offset in range(max_attempts):
                port = start_port + offset
                if check_port(port):
                    debug_log(f"Found available port: {port}")
                    return port
            # If we get here, we couldn't find an available port
            debug_log(f"Could not find available port after {max_attempts} attempts")
            return None
        else:
            # Fallback to original implementation
            debug_log("Port checker not available, using socket check")
            for offset in range(max_attempts):
                port = start_port + offset
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.bind(('0.0.0.0', port))
                    s.close()
                    debug_log(f"Found available port: {port}")
                    return port
                except Exception as e:
                    debug_log(f"Port {port} is not available: {str(e)}")
            
            debug_log(f"Could not find available port after {max_attempts} attempts")
            return None

    def cleanup_ports(self, force=False):
        """Clean up any processes using our WebSocket ports"""
        debug_log("Cleaning up WebSocket ports")
        
        # Only force-kill if specified
        if force:
            try:
                # See if our cleanup script exists and is executable
                cleanup_script = "/home/inventree/data/plugins/port_checker/cleanup_ports.py"
                if os.path.exists(cleanup_script) and os.access(cleanup_script, os.X_OK):
                    debug_log(f"Running port cleanup script: {cleanup_script}")
                    subprocess.run([cleanup_script, "9020", "9030"], check=False)
                else:
                    debug_log("Cleanup script not found or not executable")
                    
                    # Fallback to manual cleanup
                    port = int(self.get_setting('PORT', 9020))
                    for offset in range(11):  # Try ports 9020-9030
                        # Use "fuser" command to kill process using port
                        try:
                            debug_log(f"Attempting to clean port {port + offset}")
                            subprocess.run(["fuser", "-k", f"{port + offset}/tcp"], check=False)
                        except Exception as e:
                            debug_log(f"Error cleaning port {port + offset}: {str(e)}")
                            
            except Exception as e:
                debug_log(f"Error during port cleanup: {str(e)}")
        
        # Reset running state
        global _server_running
        _server_running = False
        self.server = None
        
        return True

    def verify_port_availability(self, port):
        """Verify port availability and try to free it if needed"""
        debug_log(f"Verifying availability of port {port}")
        
        # Try to bind to the port directly
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(('0.0.0.0', port))
            s.close()
            debug_log(f"Port {port} is available")
            return True
        except socket.error as e:
            debug_log(f"Port {port} is not available: {str(e)}")
            
            # If we have permission, try to kill any process using this port
            if self.get_setting('FORCE_PORT', False):
                debug_log("Force port setting enabled, attempting to free the port")
                
                # Try fuser command (simplest approach)
                try:
                    debug_log(f"Using fuser to kill processes on port {port}")
                    result = subprocess.run(['fuser', '-k', f'{port}/tcp'], 
                                           capture_output=True, text=True, check=False)
                    debug_log(f"Fuser output: {result.stdout.strip()}")
                    
                    # Give processes time to terminate
                    time.sleep(1.0)
                except Exception as e:
                    debug_log(f"Error using fuser: {str(e)}")
                
                # Try again after cleanup
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.bind(('0.0.0.0', port))
                    s.close()
                    debug_log(f"Port {port} is now available after cleanup")
                    return True
                except socket.error as e2:
                    debug_log(f"Port {port} is still not available after cleanup: {str(e2)}")
                
        return False

    def force_start_server(self):
        """Force start the server by cleaning up any stale locks or processes"""
        debug_log(f"Force starting WebSocket server (PID: {os.getpid()})")
        
        # Clean up ports
        self.cleanup_ports(force=True)
        
        # Remove the lock file if it exists
        try:
            if os.path.exists(_lockfile_path):
                os.remove(_lockfile_path)
                debug_log("Removed existing lock file")
        except Exception as e:
            debug_log(f"Error removing lock file: {str(e)}")
        
        # Update status file to indicate no server is running
        update_server_status(running=False)
        
        # Reset global state
        global _server_running
        _server_running = False
        
        # Start the server
        return self.start_server_async()

    def check_running_servers(self):
        """Check for any already running WebSocket server processes"""
        debug_log("Checking for running WebSocket server processes")
        
        try:
            # Look for python processes using our port
            port = int(self.get_setting('PORT', 9020))
            result = subprocess.run(['lsof', '-i', f':{port}'], 
                                    capture_output=True, text=True, check=False)
            
            if result.returncode == 0 and result.stdout.strip():
                debug_log(f"Found process using port {port}:")
                debug_log(result.stdout.strip())
                return True
            else:
                debug_log(f"No process found using port {port}")
                return False
        except Exception as e:
            debug_log(f"Error checking for running servers: {str(e)}")
            return False

    def check_server_health(self):
        """Check if the WebSocket server is running"""
        # Check status file
        status = get_server_status()
        if not status or not status.get('running'):
            debug_log("Status file indicates server is not running")
            return False
        
        # Check if process is alive
        server_pid = status.get('pid')
        if not server_pid:
            debug_log("No PID in status file")
            return False
        
        try:
            os.kill(server_pid, 0)
            debug_log(f"Server process {server_pid} is alive")
            
            # Also check if port is actually in use
            port = status.get('port')
            if port:
                result = subprocess.run(['lsof', '-i', f':{port}'], 
                                      capture_output=True, text=True, check=False)
                if result.returncode == 0 and str(server_pid) in result.stdout:
                    debug_log(f"Server is using port {port}")
                    
                    # Check last status update time
                    last_update = status.get('last_update', 0)
                    if time.time() - last_update > 300:  # 5 minutes
                        debug_log(f"Status file is too old (last update: {last_update})")
                        return False
                        
                    return True
                else:
                    debug_log(f"Port {port} is not being used by PID {server_pid}")
            
            return False
        except OSError:
            debug_log(f"Server process {server_pid} is not running")
            return False

    def periodic_health_check(self):
        """Periodically check server health and restart if needed"""
        while True:
            try:
                time.sleep(60)  # Check every minute
                
                if self.get_setting('ENABLED', False):
                    if not self.check_server_health():
                        debug_log("Health check failed, restarting server")
                        self.start_server_async()
                    else:
                        debug_log("Health check passed, server is running")
            except Exception as e:
                debug_log(f"Error in health check: {str(e)}")

    def check_websockets_module(self):
        """Check if the websockets module is available and reinstall if needed"""
        try:
            import websockets
            debug_log(f"Successfully imported websockets module (version: {websockets.__version__})")
            return True
        except ImportError:
            debug_log("Failed to import websockets module, attempting to install it")
            try:
                result = subprocess.run(["pip", "install", "websockets>=10.0"], 
                                      capture_output=True, text=True, check=False)
                if result.returncode == 0:
                    debug_log("Successfully installed websockets module")
                    try:
                        import websockets
                        debug_log(f"Successfully imported websockets module after install (version: {websockets.__version__})")
                        return True
                    except ImportError:
                        debug_log("Still failed to import websockets module after install")
                else:
                    debug_log(f"Failed to install websockets module: {result.stderr}")
            except Exception as e:
                debug_log(f"Error attempting to install websockets module: {str(e)}")
            return False

    # This method gets called for every event
    def process_event(self, event, *args, **kwargs):
        """Process a triggered event from InvenTree"""
        
        # Log all events received for debugging purposes
        debug_log(f"⭐ EVENT RECEIVED: {event}")
        debug_log(f"⭐ EVENT KWARGS: {kwargs}")
        
        if not self.get_setting('ENABLE_EVENTS', True):
            debug_log(f"Events disabled, ignoring event: {event}")
            return
            
        # Get list of event types to publish
        event_types = self.get_setting('EVENT_TYPES', '')
        
        # If empty, publish all events
        if not event_types:
            should_publish = True
        else:
            # Check if this event type is in our list
            event_types = [e.strip() for e in event_types.split(',')]
            should_publish = event in event_types
            
        if should_publish:
            debug_log(f"Publishing event: {event}")
            
            # Start with basic event data
            event_data = {
                'type': 'event',
                'event': event,
                'timestamp': time.time(),
                'data': kwargs
            }
            
            # Enhance event data based on type
            if 'parameter' in event.lower():
                debug_log(f"Enhancing parameter event: {event}")
                self.enhance_parameter_event(event_data)
            
            # Publish to clients via our WebSocket server
            self.publish_event(event_data)
        else:
            debug_log(f"Event {event} not in publish list, ignoring")

    def enhance_parameter_event(self, event_data):
        """Add more information to parameter events"""
        try:
            # Try to guess the model based on the event name itself
            event_name = event_data.get('event', '')
            if 'parameter' in event_name.lower():
                # Extract app and model from event name
                parts = event_name.split('.')
                if len(parts) >= 1:
                    event_parts = parts[0].split('_')
                    if len(event_parts) >= 2:
                        app_name = event_parts[0]
                        model_part = event_parts[1]
                        
                        debug_log(f"Trying to find model in app {app_name} for {model_part}")
                        
                        # Try to locate the model dynamically
                        from django.apps import apps
                        all_models = apps.get_app_config(app_name).get_models()
                        
                        for model in all_models:
                            if 'parameter' in model.__name__.lower():
                                debug_log(f"Found potential parameter model: {model.__name__}")
                                # Try to use this model...

            # Extract information from event data
            model_name = event_data.get('data', {}).get('model', '')
            obj_id = event_data.get('data', {}).get('id')
            
            debug_log(f"Enhancing parameter event for model: {model_name}, id: {obj_id}")
            
            if not obj_id or not model_name:
                debug_log("Missing model or id in parameter event data")
                return
            
            # For parameter events, we want to extract:
            # 1. The parameter name
            # 2. The parameter value
            # 3. The parent item (part, stock item, etc.)
            
            parameter_models = {
                'PartParameter': ('part', 'PartParameter'),
                'PartParameterTemplate': ('part', 'PartParameterTemplate'),
                'StockItemParameter': ('stock', 'StockItemParameter'),
                'CompanyParameter': ('company', 'CompanyParameter')
            }
            
            if model_name in parameter_models:
                # Import Django models
                from django.apps import apps
                
                app_name, model_class = parameter_models[model_name]
                
                # Get the model class
                try:
                    Model = apps.get_model(app_name, model_class)
                    debug_log(f"Found model class: {Model}")
                    
                    # Get the parameter object
                    try:
                        parameter = Model.objects.get(pk=obj_id)
                        debug_log(f"Found parameter object: {parameter}")
                        
                        # Add parameter information to event data
                        extra_data = {
                            'parameter_name': getattr(parameter, 'name', 'Unknown'),
                            'parameter_value': getattr(parameter, 'data', '')
                        }
                        
                        debug_log(f"Parameter info: {extra_data}")
                        
                        # Add parent item information if available
                        if hasattr(parameter, 'part'):
                            part = parameter.part
                            extra_data['parent_type'] = 'Part'
                            extra_data['parent_id'] = part.pk
                            extra_data['parent_name'] = part.name
                            debug_log(f"Added part info: {part.name} (ID: {part.pk})")
                            
                        elif hasattr(parameter, 'stock_item'):
                            stock_item = parameter.stock_item
                            extra_data['parent_type'] = 'StockItem'
                            extra_data['parent_id'] = stock_item.pk
                            if hasattr(stock_item, 'part') and stock_item.part:
                                extra_data['parent_part'] = stock_item.part.name
                            debug_log(f"Added stock item info: ID {stock_item.pk}")
                            
                        elif hasattr(parameter, 'company'):
                            company = parameter.company
                            extra_data['parent_type'] = 'Company'
                            extra_data['parent_id'] = company.pk
                            extra_data['parent_name'] = company.name
                            debug_log(f"Added company info: {company.name} (ID: {company.pk})")
                        
                        # Update the event data
                        event_data['data'].update(extra_data)
                        debug_log(f"Updated event data with parameter info")
                        
                    except Model.DoesNotExist:
                        debug_log(f"Parameter with ID {obj_id} not found")
                        # This can happen with deleted events
                        if event_data['event'].endswith('deleted'):
                            debug_log("This is a deletion event, so the object not existing is expected")
                        
                except Exception as e:
                    debug_log(f"Error enhancing parameter event: {str(e)}")
                    import traceback
                    debug_log(traceback.format_exc())
                
        except Exception as e:
            debug_log(f"Error processing parameter event: {str(e)}")
            import traceback
            debug_log(traceback.format_exc())

    # Optional filter method to reduce workload
    def wants_process_event(self, event, *args, **kwargs):
        """Return True if we want to process this event"""
        
        debug_log(f"🔍 FILTER CHECK: {event}")
        
        if not self.get_setting('ENABLE_EVENTS', True):
            debug_log(f"Events disabled, filtering out event: {event}")
            return False
            
        # Get list of event types to publish
        event_types = self.get_setting('EVENT_TYPES', '')
        
        # If empty, publish all events
        if not event_types:
            debug_log(f"No event filter specified, accepting event: {event}")
            return True
        else:
            # Check if this event type is in our list
            event_types = [e.strip() for e in event_types.split(',')]
            if event in event_types:
                debug_log(f"Event {event} is in filter list, accepting")
                return True
            else:
                debug_log(f"Event {event} not in filter list, filtering out")
                return False

    def publish_event(self, event_data):
        """Publish an event to connected WebSocket clients"""
        debug_log(f"Publishing event to WebSocket: {event_data['event']}")
        
        # Convert event data to JSON
        try:
            event_json = json.dumps(event_data)
            debug_log(f"Serialized event data: {event_json[:100]}...")  # Log first 100 chars
        except Exception as e:
            debug_log(f"Error serializing event data: {str(e)}")
            return
        
        # Write event to a file that the WebSocket server can read
        event_file = '/home/inventree/data/websocket_events.json'
        
        try:
            # Append to the file
            with open(event_file, 'a') as f:
                f.write(event_json + '\n')  # One event per line
            debug_log(f"Event written to file successfully: {event_data['event']}")
        except Exception as e:
            debug_log(f"Error writing event to file: {str(e)}")