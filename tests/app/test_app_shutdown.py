#!/usr/bin/env python3
"""Test script to verify graceful shutdown behavior with active connections."""

import asyncio
import aiohttp
import logging
import signal
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


async def test_server_with_connection():
    """Test the server with an active SSE connection and then send SIGINT."""
    
    # Start the server as a subprocess
    logger.info("Starting API server...")
    # Launch server without capturing stdout/stderr to avoid leftover reader
    # threads that may cause ResourceWarning during pytest cleanup. We still
    # monitor the process via polling and only communicate if it exits.
    server_process = subprocess.Popen(
        [sys.executable, "-m", "agent_system.app"],
        cwd=Path(__file__).parent.parent,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL
    )
    
    # Wait for server to start with timeout and health check
    logger.info("Waiting for server to start...")
    start_timeout = 10  # seconds
    for i in range(start_timeout):
        if server_process.poll() is not None:
            logger.error(f"Server process exited early with code {server_process.returncode}")
            stdout, stderr = server_process.communicate()
            # communicate() returns text strings because we opened pipes with text=True
            stdout = stdout or ""
            stderr = stderr or ""
            logger.error(f"Early exit stdout: {stdout}")
            logger.error(f"Early exit stderr: {stderr}")
            return False

        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=1)) as session:
                async with session.get("http://127.0.0.1:8000/health") as response:
                    if response.status == 200:
                        logger.info("Server is ready")
                        break
        except Exception:
            # Server not ready yet
            pass

        await asyncio.sleep(1)
    else:
        logger.error("Server failed to start within timeout")
        # Ensure the subprocess is terminated to avoid ResourceWarning in
        # Popen.__del__ if the process remains running when garbage-collected.
        try:
            if server_process.poll() is None:
                server_process.terminate()
                try:
                    server_process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    server_process.kill()
                    server_process.wait(timeout=1)
        except Exception:
            pass
        return False
    
    try:
        # Create an SSE connection to simulate active client with timeout
        logger.info("Connecting to server events stream...")
        timeout = aiohttp.ClientTimeout(total=30, sock_read=5)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            try:
                async with session.get("http://127.0.0.1:8000/events") as response:
                    if response.status == 200:
                        logger.info("Connected to events stream")
                        
                        # Read a few lines to establish connection with timeout
                        lines_read = 0
                        read_start = asyncio.get_event_loop().time()
                        async for line in response.content:
                            if lines_read >= 3:  # Read just a few lines
                                break
                            if asyncio.get_event_loop().time() - read_start > 5:  # 5 second timeout
                                logger.info("Timeout reading stream lines, proceeding with shutdown test")
                                break
                            # response.content yields bytes even when session is text-mode; decode defensively
                            try:
                                text_line = line.decode('utf-8', errors='replace').strip()
                            except Exception:
                                text_line = str(line)
                            logger.debug(f"Received: {text_line}")
                            lines_read += 1
                        
                        logger.info("Active connection established, sending SIGINT...")
                        # Send SIGINT to the server process
                        server_process.send_signal(signal.SIGINT)
                        
                        # Give server time to shut down gracefully with timeout
                        shutdown_timeout = 5
                        for i in range(shutdown_timeout):
                            if server_process.poll() is not None:
                                logger.info(f"Server shut down gracefully after {i+1} seconds")
                                break
                            await asyncio.sleep(1)
                        else:
                            logger.warning("Server did not shut down within timeout")
                        
                    else:
                        logger.error(f"Failed to connect to server: {response.status}")
                        return False
                        
            except asyncio.TimeoutError:
                logger.info("Connection timeout as expected during shutdown")
            except Exception as e:
                logger.info(f"Connection closed as expected during shutdown: {e}")
                
    finally:
        # Ensure server is stopped with proper cleanup
        try:
            if server_process.poll() is None:
                logger.info("Force terminating server process...")
                server_process.terminate()
                try:
                    server_process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning("Server did not terminate, killing...")
                    server_process.kill()
                    server_process.wait(timeout=2)
        except Exception:
            pass

        # We don't attempt to read stdout/stderr because we launched the
        # process with DEVNULL to avoid threads and leftover handles.
        logger.info(f"Server exit code: {server_process.returncode}")

        return server_process.returncode == 0


if __name__ == "__main__":
    result = asyncio.run(test_server_with_connection())
    if result:
        print("✅ Server shutdown test PASSED")
    else:
        print("❌ Server shutdown test FAILED")
        sys.exit(1)
