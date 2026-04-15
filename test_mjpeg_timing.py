#!/usr/bin/env python3
"""Test script to analyze MJPEG stream packet timing from Pi with video display."""

import socket
import struct
import time
from collections import deque

import cv2
import numpy as np

HOST = "192.168.178.137"
PORT = 8081


def main():
    print(f"Connecting to {HOST}:{PORT}...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)
    sock.connect((HOST, PORT))
    print("Connected!\n")

    # Stats
    frame_times = deque(maxlen=100)
    frame_sizes = deque(maxlen=100)
    start_time = time.time()
    last_frame_time = start_time
    frame_count = 0
    total_bytes = 0
    decode_errors = 0

    print(f"{'Frame':>6} | {'Size':>10} | {'Delta':>8} | {'Instant FPS':>11} | {'Avg FPS':>8} | {'Bandwidth':>12}")
    print("-" * 75)

    cv2.namedWindow("MJPEG Stream", cv2.WINDOW_NORMAL)

    try:
        while True:
            # Read 4-byte length header
            header = b""
            while len(header) < 4:
                chunk = sock.recv(4 - len(header))
                if not chunk:
                    print("\nConnection closed by server")
                    return
                header += chunk

            frame_len = struct.unpack(">I", header)[0]
            
            # Read frame data
            frame_data = b""
            while len(frame_data) < frame_len:
                chunk = sock.recv(min(65536, frame_len - len(frame_data)))
                if not chunk:
                    print("\nConnection closed during frame read")
                    return
                frame_data += chunk

            now = time.time()
            delta = (now - last_frame_time) * 1000
            last_frame_time = now
            frame_count += 1
            total_bytes += frame_len

            frame_times.append(delta)
            frame_sizes.append(frame_len)

            # Calculate stats
            avg_delta = sum(frame_times) / len(frame_times) if frame_times else 0
            instant_fps = 1000 / delta if delta > 0 else 0
            avg_fps = 1000 / avg_delta if avg_delta > 0 else 0
            elapsed = now - start_time
            bandwidth_kbps = (total_bytes * 8 / 1000) / elapsed if elapsed > 0 else 0

            print(f"{frame_count:>6} | {frame_len:>10,} | {delta:>7.1f}ms | {instant_fps:>10.1f}fps | {avg_fps:>7.1f}fps | {bandwidth_kbps:>10.1f}kbps")

            # Decode and display JPEG
            try:
                jpg_array = np.frombuffer(frame_data, dtype=np.uint8)
                frame = cv2.imdecode(jpg_array, cv2.IMREAD_COLOR)
                if frame is not None:
                    cv2.imshow("MJPEG Stream", frame)
                    decode_errors = 0
                else:
                    decode_errors += 1
                    print(f"  [decode returned None]")
            except Exception as e:
                decode_errors += 1
                print(f"  [decode error: {e}]")

            # Check for quit
            key = cv2.waitKey(1) & 0xFF
            if key == ord('q'):
                break

    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
    finally:
        sock.close()
        cv2.destroyAllWindows()
        
        # Print summary
        elapsed = time.time() - start_time
        print(f"\n{'='*50}")
        print(f"Total frames: {frame_count}")
        print(f"Total time: {elapsed:.1f}s")
        print(f"Average FPS: {frame_count/elapsed:.1f}" if elapsed > 0 else "N/A")
        print(f"Average frame size: {total_bytes/frame_count/1024:.1f}KB" if frame_count > 0 else "N/A")
        print(f"Average bandwidth: {total_bytes*8/1000/elapsed:.1f}kbps" if elapsed > 0 else "N/A")
        print(f"Decode errors: {decode_errors}")


if __name__ == "__main__":
    main()
