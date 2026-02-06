#!/usr/bin/env python3
"""
Test script to verify that status events are properly forwarded through SSE streams.
This simulates what the web UI does when connecting to the /events endpoint.
"""

import asyncio
import json
import aiohttp
import sys

async def test_sse_status_events():
    """Test that status events are forwarded through the SSE /events endpoint"""
    url = "http://127.0.0.1:8000/events"
    params = {"task": "simple test message"}
    
    print("Connecting to SSE endpoint...")
    
    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, params=params) as response:
                print(f"Connected. Status: {response.status}")
                
                if response.status != 200:
                    print(f"Error: {response.status}")
                    return
                
                events = []
                status_events = []
                
                async for line in response.content:
                    line_str = line.decode('utf-8').strip()
                    
                    if line_str.startswith('data:'):
                        data_str = line_str[5:].strip()
                        try:
                            event = json.loads(data_str)
                            events.append(event)
                            
                            print(f"Event: {event}")
                            
                            if event.get('type') == 'status':
                                status_events.append(event)
                                print(f"  -> STATUS: {event.get('server')} [{event.get('phase')}]: {event.get('message')}")
                            
                            if event.get('type') == 'end':
                                print("Received end event, finishing...")
                                break
                                
                        except json.JSONDecodeError as e:
                            print(f"JSON decode error: {e}, data: {data_str}")
                    
                    elif line_str.startswith(':'):
                        print(f"Comment: {line_str}")
                
                print("\nSummary:")
                print(f"Total events: {len(events)}")
                print(f"Status events: {len(status_events)}")
                
                if status_events:
                    print("Status events received:")
                    for i, event in enumerate(status_events):
                        print(f"  {i+1}. {event.get('server')} [{event.get('phase')}]: {event.get('message')}")
                else:
                    print("No status events received!")
                
                return len(status_events) > 0
                
    except Exception as e:
        print(f"Error: {e}")
        return False

if __name__ == "__main__":
    success = asyncio.run(test_sse_status_events())
    print(f"\nTest {'PASSED' if success else 'FAILED'}")
    sys.exit(0 if success else 1)