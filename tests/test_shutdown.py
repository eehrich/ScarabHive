#!/usr/bin/env python3
"""Test script to verify graceful shutdown behavior with active connections."""

import asyncio
import aiohttp
import logging
import signal
import subprocess
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


async def test_server_with_connection():
    """Test the server with an active SSE connection and then send SIGINT."""
    
    # Start the server as a subprocess
    logger.info("Starting API server...")
    server_process = subprocess.Popen(
        [sys.executable, "-m", "agent_system.agent.interface_api"],
        cwd=Path(__file__).parent.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    
    # Wait for server to start
    await asyncio.sleep(2)
    
    try:
        # Create an SSE connection to simulate active client
        logger.info("Connecting to server status stream...")
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get("http://127.0.0.1:8000/status/stream") as response:
                    if response.status == 200:
                        logger.info("Connected to status stream")
                        
                        # Read a few lines to establish connection
                        lines_read = 0
                        async for line in response.content:
                            if lines_read >= 3:  # Read just a few lines
                                break
                            logger.debug(f"Received: {line.decode().strip()}")
                            lines_read += 1
                        
                        logger.info("Active connection established, sending SIGINT...")
                        # Send SIGINT to the server process
                        server_process.send_signal(signal.SIGINT)
                        
                        # Give server time to shut down gracefully
                        await asyncio.sleep(3)
                        
                    else:
                        logger.error(f"Failed to connect to server: {response.status}")
                        
            except Exception as e:
                logger.info(f"Connection closed as expected during shutdown: {e}")
                
    finally:
        # Ensure server is stopped
        if server_process.poll() is None:
            logger.info("Terminating server process...")
            server_process.terminate()
            server_process.wait(timeout=5)
        
        # Get the server output
        stdout, stderr = server_process.communicate()
        
        logger.info(f"Server exit code: {server_process.returncode}")
        if stdout:
            logger.info("Server stdout (last 500 chars):")
            logger.info(stdout[-500:])
        if stderr:
            logger.info("Server stderr (last 500 chars):")
            logger.info(stderr[-500:])
            
        # Check if we see the expected shutdown messages
        if "shutting down gracefully" in stderr.lower() or "shutdown complete" in stderr.lower():
            logger.info("✅ Graceful shutdown detected in logs")
        else:
            logger.warning("⚠️  Graceful shutdown messages not found in stderr")
            
        return server_process.returncode == 0


if __name__ == "__main__":
    result = asyncio.run(test_server_with_connection())
    if result:
        print("✅ Server shutdown test PASSED")
    else:
        print("❌ Server shutdown test FAILED")
        sys.exit(1)
