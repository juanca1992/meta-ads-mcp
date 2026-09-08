"""Callback server for Meta Ads API authentication."""

from .security import diagnostic_print as print

import threading
import socket
import asyncio
import json
import logging
import webbrowser
import os
import secrets
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote
from typing import Dict, Any, Optional

from .utils import logger

# Global token container for communication between threads
token_container = {"token": None, "expires_in": None, "user_id": None}

# Global variables for server thread and state
callback_server_thread = None
callback_server_lock = threading.Lock()
callback_server_running = False
callback_server_port = None
callback_server_instance = None
server_shutdown_timer = None

# Timeout in seconds before shutting down the callback server
CALLBACK_SERVER_TIMEOUT = 180  # 3 minutes timeout


_oauth_pending = None
_oauth_lock = threading.Lock()


def begin_oauth_flow(redirect_uri):
    global _oauth_pending
    state = secrets.token_urlsafe(32)
    with _oauth_lock:
        _oauth_pending = (state, redirect_uri, time.monotonic() + CALLBACK_SERVER_TIMEOUT)
        token_container.clear()
        token_container.update(token=None, expires_in=None, user_id=None)
    return state


def consume_oauth_state(state):
    global _oauth_pending
    with _oauth_lock:
        pending = _oauth_pending
        if not pending or not state or pending[2] <= time.monotonic():
            return None
        if not secrets.compare_digest(state, pending[0]):
            return None
        _oauth_pending = None
        return pending[1]


class CallbackHandler(BaseHTTPRequestHandler):
    def _reply(self, status, message):
        self.send_response(status)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(message.encode())

    def do_GET(self):
        if urlparse(self.path).path != "/callback":
            return self._reply(404, "Not found")
        try:
            self._handle_oauth_callback()
        except Exception:
            logger.error("OAuth callback failed")
            self._reply(500, "Authentication failed; start a new login")

    def _handle_oauth_callback(self):
        params = parse_qs(urlparse(self.path).query)
        redirect_uri = consume_oauth_state(params.get("state", [None])[0])
        if not redirect_uri:
            return self._reply(400, "Invalid or expired login state")
        if params.get("error") or not params.get("code"):
            return self._reply(400, "Authorization cancelled or invalid response")
        from .auth import exchange_authorization_code, process_token_response
        result = exchange_authorization_code(params["code"][0], redirect_uri)
        if not result or not process_token_response(result):
            return self._reply(502, "Token exchange failed; start a new login")
        token_container.update(result)
        self._reply(200, "Authentication successful. You can close this window.")

    # Silence server logs
    def log_message(self, format, *args):
        return


def shutdown_callback_server():
    """
    Shutdown the callback server if it's running
    """
    global callback_server_thread, callback_server_running, callback_server_port, callback_server_instance, server_shutdown_timer
    
    with callback_server_lock:
        if not callback_server_running:
            print("Callback server is not running")
            return
        
        if server_shutdown_timer is not None:
            server_shutdown_timer.cancel()
            server_shutdown_timer = None
        
        try:
            if callback_server_instance:
                print("Shutting down callback server...")
                callback_server_instance.shutdown()
                callback_server_instance.server_close()
                print("Callback server shut down successfully")
            
            if callback_server_thread and callback_server_thread.is_alive():
                callback_server_thread.join(timeout=5)
                if callback_server_thread.is_alive():
                    print("Warning: Callback server thread did not shut down cleanly")
        except Exception as e:
            print(f"Error during callback server shutdown: {e}")
        finally:
            callback_server_running = False
            callback_server_thread = None
            callback_server_port = None
            callback_server_instance = None


def start_callback_server() -> int:
    """
    Start the callback server and return the port number it's running on.
    
    Returns:
        int: Port number the server is listening on
        
    Raises:
        Exception: If the server fails to start
    """
    global callback_server_thread, callback_server_running, callback_server_port, callback_server_instance, server_shutdown_timer
    
    # Check if callback server is disabled
    if os.environ.get("META_ADS_DISABLE_CALLBACK_SERVER"):
        raise Exception("Callback server is disabled via META_ADS_DISABLE_CALLBACK_SERVER environment variable")
    
    with callback_server_lock:
        if callback_server_running:
            print(f"Callback server already running on port {callback_server_port}")
            return callback_server_port
        
        # Find an available port
        port = 8080
        max_attempts = 10
        for attempt in range(max_attempts):
            try:
                # Test if port is available
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.bind(('localhost', port))
                break
            except OSError:
                port += 1
        else:
            raise Exception(f"Could not find an available port after {max_attempts} attempts")
        
        callback_server_port = port
        
        # Start the server in a separate thread
        callback_server_thread = threading.Thread(target=server_thread, daemon=True)
        callback_server_thread.start()
        
        # Wait a moment for the server to start
        import time
        time.sleep(0.5)
        
        if not callback_server_running:
            raise Exception("Failed to start callback server")
        
        # Set up automatic shutdown timer
        def auto_shutdown():
            print(f"Callback server auto-shutdown after {CALLBACK_SERVER_TIMEOUT} seconds")
            shutdown_callback_server()
        
        server_shutdown_timer = threading.Timer(CALLBACK_SERVER_TIMEOUT, auto_shutdown)
        server_shutdown_timer.start()
        
        print(f"Callback server started on http://localhost:{port}")
        return port


def server_thread():
    """Thread function to run the callback server"""
    global callback_server_running, callback_server_instance
    
    try:
        callback_server_instance = HTTPServer(('localhost', callback_server_port), CallbackHandler)
        callback_server_running = True
        print(f"Callback server thread started on port {callback_server_port}")
        callback_server_instance.serve_forever()
    except Exception as e:
        print(f"Callback server error: {e}")
        callback_server_running = False
    finally:
        print("Callback server thread finished")
        callback_server_running = False 