#!/usr/bin/env python3

import asyncio
import websockets
import socket
import json
import os
import time
import signal
import sys
import logging
import traceback

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/home/inventree/data/websocket_server.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger('websocket-server')

# Status file for interprocess communication
STATUS_FILE = '/home/inventree/data/websocket_status.json'

# Get settings from command line arguments
host = '0.0.0.0'  # Always use 0.0.0.0 in Docker
try:
    # Only try to access sys.argv when run as a script
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 9020
except ValueError:
    # Default value when imported as a module
    port = 9020
external_url = sys.argv[2] if len(sys.argv) > 2 else None

# Store active connections
active_connections = set()

def update_status(running=True):
    """Update the status file"""
    status = {
        'running': running,
        'pid': os.getpid(),
        'port': port,
        'host': host,
        'external_url': external_url,
        'last_update': time.time(),
        'container_ip': get_container_ip()
    }
    
    try:
        with open(STATUS_FILE, 'w') as f:
            json.dump(status, f)
        logger.info(f"Updated status file: {status}")
    except Exception as e:
        logger.error(f"Failed to update status file: {e}")

def get_container_ip():
    """Get the IP address of the container"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception as e:
        logger.error(f"Error getting container IP: {e}")
        return '127.0.0.1'

async def websocket_handler(websocket):
    """Handle WebSocket connections"""
    try:
        # Register the connection
        await register(websocket)
        
        # Get remote address for logging
        remote_addr = websocket.remote_address if hasattr(websocket, 'remote_address') else 'unknown'
        logger.info(f"New connection from {remote_addr}")
        
        # Send a welcome message
        welcome = {
            "type": "welcome",
            "message": "Welcome to InvenTree WebSocket Server",
            "server_time": time.time(),
            "client_ip": remote_addr,
            "server_pid": os.getpid(),
            "features": ["events"]  # Let clients know we support events
        }
        await websocket.send(json.dumps(welcome))
        logger.info(f"Sent welcome message to {remote_addr}")
        
        # Start ping task
        ping_task = asyncio.create_task(periodic_ping(websocket))
        
        try:
            # Handle incoming messages
            async for message in websocket:
                logger.info(f"Received message from {remote_addr}: {message}")
                
                # Try to parse as JSON
                try:
                    data = json.loads(message)
                    
                    # Handle ping/test messages
                    if data.get('type') == 'ping':
                        response = {
                            "type": "pong",
                            "received": data,
                            "server_time": time.time(),
                            "server_pid": os.getpid()
                        }
                        await websocket.send(json.dumps(response))
                        logger.info(f"Sent pong response to {remote_addr}")
                    else:
                        # Echo back other messages
                        response = {
                            "type": "echo",
                            "received": data,
                            "server_time": time.time(),
                            "server_pid": os.getpid()
                        }
                        await websocket.send(json.dumps(response))
                        logger.info(f"Echoed message back to {remote_addr}")
                except json.JSONDecodeError:
                    # Just echo back non-JSON messages
                    response = f"Received: {message}"
                    await websocket.send(response)
                    logger.info(f"Sent text response to {remote_addr}")
        finally:
            # Cancel ping task
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass
            
            # Unregister the connection when done
            await unregister(websocket)
            
    except Exception as e:
        logger.error(f"Error in websocket handler: {e}")
        logger.error(traceback.format_exc())

async def periodic_ping(websocket):
    """Send periodic pings to keep the connection alive"""
    try:
        while True:
            # Send a ping every 30 seconds
            await asyncio.sleep(30)
            
            # Check if the connection is still open (compatible with newer websockets versions)
            try:
                # Try to send a ping - will raise an exception if the connection is closed
                ping_message = {
                    "type": "ping",
                    "source": "server",
                    "time": time.time()
                }
                await websocket.send(json.dumps(ping_message))
                logger.debug("Sent keepalive ping")
            except websockets.exceptions.ConnectionClosed:
                logger.info("Connection closed, stopping ping task")
                break
            except Exception as e:
                logger.error(f"Error sending ping: {e}")
                break
                
    except asyncio.CancelledError:
        # Expected when the connection closes
        logger.debug("Ping task cancelled")
        raise
    except Exception as e:
        logger.error(f"Error in ping task: {e}")
        logger.error(traceback.format_exc())

def handle_shutdown(signum, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}, shutting down...")
    update_status(running=False)
    # We'll let the async loop clean up before exiting
    raise KeyboardInterrupt

async def status_updater():
    """Periodically update the status file to show we're still alive"""
    while True:
        try:
            update_status(running=True)
        except Exception as e:
            logger.error(f"Error updating status: {e}")
        await asyncio.sleep(60)  # Update status every minute

async def register(websocket):
    """Register a new client connection"""
    active_connections.add(websocket)
    logger.info(f"Client registered. Total connections: {len(active_connections)}")

async def unregister(websocket):
    """Unregister a client connection"""
    active_connections.remove(websocket)
    logger.info(f"Client unregistered. Total connections: {len(active_connections)}")

async def broadcast(message):
    """Broadcast a message to all connected clients"""
    if active_connections:
        # Convert to JSON if it's not already a string
        if not isinstance(message, str):
            message = json.dumps(message)
            
        # Create tasks to send to each client
        tasks = [asyncio.create_task(client.send(message)) 
                 for client in active_connections]
        
        # Wait for all tasks to complete
        if tasks:
            done, pending = await asyncio.wait(
                tasks,
                return_when=asyncio.ALL_COMPLETED
            )
            
            # Check for exceptions
            for task in done:
                try:
                    task.result()
                except websockets.exceptions.ConnectionClosed:
                    # Connection was closed, this is handled elsewhere
                    pass
                except Exception as e:
                    logger.error(f"Error broadcasting message: {e}")

async def event_reader():
    """Read events from the event file and broadcast them"""
    event_file = '/home/inventree/data/websocket_events.json'
    
    # Create the file if it doesn't exist
    if not os.path.exists(event_file):
        with open(event_file, 'w') as f:
            pass
    
    # Keep track of the file size
    last_size = os.path.getsize(event_file)
    
    while True:
        try:
            # Check if the file has grown
            current_size = os.path.getsize(event_file)
            
            if current_size > last_size:
                # File has grown, read new lines
                with open(event_file, 'r') as f:
                    f.seek(last_size)
                    new_lines = f.readlines()
                
                # Process new events
                for line in new_lines:
                    line = line.strip()
                    if line:
                        try:
                            event_data = json.loads(line)
                            logger.info(f"Broadcasting event: {event_data['event']}")
                            await broadcast(event_data)
                        except json.JSONDecodeError:
                            logger.error(f"Invalid JSON in event file: {line}")
                        except Exception as e:
                            logger.error(f"Error processing event: {e}")
                
                # Update the last size
                last_size = current_size
            
            # Sleep a bit before checking again
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Error in event reader: {e}")
            await asyncio.sleep(5)  # Wait longer after an error

async def main():
    """Main function to start the server"""
    logger.info(f"Starting WebSocket server on {host}:{port} (PID: {os.getpid()})")
    
    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)
    
    # Update status before starting
    update_status(running=True)

    # Start the status updater
    status_task = asyncio.create_task(status_updater())
    
    # Start the event reader
    event_reader_task = asyncio.create_task(event_reader())
    
    # Start the server
    try:
        async with websockets.serve(websocket_handler, host, port):
            logger.info(f"WebSocket server running on {host}:{port}")
            logger.info(f"Container IP: {get_container_ip()}")
            if external_url:
                logger.info(f"External URL: {external_url}")
            
            # Log connection info
            logger.info("====================== CONNECTION INFORMATION ======================")
            logger.info(f"WebSocket server listening on: ws://{host}:{port}")
            logger.info(f"Container IP address: {get_container_ip()}")
            
            if external_url:
                logger.info(f"External WebSocket URL: {external_url}")
            else:
                logger.info("WARNING: No external URL provided!")
                logger.info(f"Clients should connect to: ws://{get_container_ip()}:{port} (internal only)")
                logger.info(f"From outside Docker, try connecting to: ws://YOUR_HOST_IP:{port}")
            
            logger.info("==================================================================")
            
            # Keep running until interrupted
            while True:
                await asyncio.sleep(3600)  # Just keep the main task alive
    except Exception as e:
        logger.error(f"Failed to start server: {e}")
        logger.error(traceback.format_exc())
        update_status(running=False)
        sys.exit(1)
    finally:
        # Cleanup
        status_task.cancel()
        event_reader_task.cancel()
        try:
            await status_task
            await event_reader_task
        except asyncio.CancelledError:
            pass
        logger.info("WebSocket server shut down")
        update_status(running=False)

if __name__ == "__main__":
    # Only run the server when executed directly, not when imported
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped by keyboard interrupt")
    except Exception as e:
        logger.error(f"Unhandled exception: {e}")
        logger.error(traceback.format_exc())
    finally:
        # Make sure status is updated
        update_status(running=False)
        logger.info("Server process terminated") 