#!/usr/bin/env python3
"""Test script to analyze H.264 stream packet timing from Pi with video display."""

import socket
import struct
import time
from collections import deque

import av
import cv2
import numpy as np

HOST = "192.168.178.137"
PORT = 8081


def create_low_latency_decoder():
    """Create H.264 decoder with low-latency settings."""
    codec = av.CodecContext.create('h264', 'r')
    codec.options = {
        'flags': 'low_delay',
        'flags2': 'fast',
    }
    codec.thread_type = 'SLICE'  # Avoid frame-level threading delay
    codec.thread_count = 1
    return codec


def is_keyframe(data: bytes) -> bool:
    """Check for H.264 IDR or SPS NAL unit."""
    i = 0
    while i < min(len(data), 100):  # Only check first 100 bytes
        if data[i:i+3] == b'\x00\x00\x01':
            nal_type = data[i+3] & 0x1F
            if nal_type in (5, 7):  # IDR or SPS
                return True
            i += 3
        elif data[i:i+4] == b'\x00\x00\x00\x01':
            if i + 4 < len(data):
                nal_type = data[i+4] & 0x1F
                if nal_type in (5, 7):
                    return True
            i += 4
        else:
            i += 1
    return False


def main():
    print(f"Connecting to {HOST}:{PORT}...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)  # Disable Nagle
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 65536)  # Smaller receive buffer
    sock.connect((HOST, PORT))
    print("Connected!\n")

    # H.264 decoder
    codec = create_low_latency_decoder()
    waiting_for_keyframe = True
    last_good_frame = None

    # Stats
    frame_times = deque(maxlen=100)
    frame_sizes = deque(maxlen=100)
    start_time = time.time()
    last_frame_time = start_time
    frame_count = 0
    decoded_count = 0
    keyframe_count = 0
    total_bytes = 0
    decode_errors = 0

    print(f"{'Frame':>6} | {'Size':>10} | {'Delta':>8} | {'Instant FPS':>11} | {'Avg FPS':>8} | {'Type':>5} | {'Bandwidth':>12}")
    print("-" * 85)

    cv2.namedWindow("Pi Stream", cv2.WINDOW_NORMAL)

    try:
        while True:
            # Read 4-byte length prefix
            header = b''
            while len(header) < 4:
                chunk = sock.recv(4 - len(header))
                if not chunk:
                    raise ConnectionError("Connection closed")
                header += chunk

            frame_len = struct.unpack(">I", header)[0]
            
            # Read frame data
            frame_data = b''
            while len(frame_data) < frame_len:
                chunk = sock.recv(min(frame_len - len(frame_data), 65536))
                if not chunk:
                    raise ConnectionError("Connection closed")
                frame_data += chunk

            now = time.time()
            delta = (now - last_frame_time) * 1000  # ms
            last_frame_time = now
            frame_count += 1
            total_bytes += frame_len

            frame_times.append(delta)
            frame_sizes.append(frame_len)

            # Calculate stats
            instant_fps = 1000 / delta if delta > 0 else 0
            elapsed = now - start_time
            avg_fps = frame_count / elapsed if elapsed > 0 else 0
            bandwidth_kbps = (total_bytes * 8) / elapsed / 1000 if elapsed > 0 else 0
            
            keyframe = is_keyframe(frame_data)
            if keyframe:
                keyframe_count += 1
                waiting_for_keyframe = False
                # Reset codec on keyframe to clear any corruption
                codec = create_low_latency_decoder()
                decode_errors = 0
            frame_type = "KEY" if keyframe else "P"

            print(f"{frame_count:>6} | {frame_len:>10,} | {delta:>7.1f}ms | {instant_fps:>10.1f}fps | {avg_fps:>7.1f}fps | {frame_type:>5} | {bandwidth_kbps:>10.1f}kbps")

            # Decode and display
            if not waiting_for_keyframe:
                try:
                    packets = codec.parse(frame_data)
                    for packet in packets:
                        for frame in codec.decode(packet):
                            bgr_frame = frame.to_ndarray(format='bgr24')
                            last_good_frame = bgr_frame
                            decoded_count += 1
                            decode_errors = 0
                except av.error.InvalidDataError as e:
                    decode_errors += 1
                    if decode_errors <= 3:
                        print(f"  [decode error #{decode_errors}: {e}]")
                    if decode_errors > 5:
                        # Reset and wait for next keyframe
                        waiting_for_keyframe = True
                        codec = create_low_latency_decoder()
                        print("  [resetting decoder, waiting for keyframe]")

            # Always show last good frame with overlay
            if last_good_frame is not None:
                overlay = last_good_frame.copy()
                cv2.putText(overlay, f"FPS: {avg_fps:.1f} (instant: {instant_fps:.1f})", 
                           (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
                cv2.putText(overlay, f"Frame: {frame_count} | Size: {frame_len//1024}KB | {frame_type}", 
                           (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
                cv2.putText(overlay, f"Bandwidth: {bandwidth_kbps:.0f} kbps | Delta: {delta:.1f}ms", 
                           (10, 90), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
                cv2.putText(overlay, f"Decoded: {decoded_count} | Errors: {decode_errors}", 
                           (10, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
                
                cv2.imshow("Pi Stream", overlay)

            # Check for quit
            key = cv2.waitKey(1) & 0xFF
            if key == ord('q') or key == 27:  # q or ESC
                break

            # Summary every 100 frames
            if frame_count % 100 == 0:
                avg_delta = sum(frame_times) / len(frame_times)
                min_delta = min(frame_times)
                max_delta = max(frame_times)
                avg_size = sum(frame_sizes) / len(frame_sizes)
                jitter = max_delta - min_delta
                
                print("\n" + "=" * 85)
                print(f"SUMMARY (last 100 frames):")
                print(f"  Frame timing: avg={avg_delta:.1f}ms, min={min_delta:.1f}ms, max={max_delta:.1f}ms, jitter={jitter:.1f}ms")
                print(f"  Frame size:   avg={avg_size/1024:.1f}KB")
                print(f"  Keyframes:    {keyframe_count} total ({keyframe_count/frame_count*100:.1f}%)")
                print(f"  Bandwidth:    {bandwidth_kbps:.1f} kbps ({bandwidth_kbps/8:.1f} KB/s)")
                print(f"  Decoded:      {decoded_count}/{frame_count}")
                print("=" * 85 + "\n")

    except KeyboardInterrupt:
        print("\n\nStopped by user")
    except Exception as e:
        print(f"\nError: {e}")
    finally:
        cv2.destroyAllWindows()
        sock.close()
        
        # Final stats
        if frame_count > 0:
            elapsed = time.time() - start_time
            print(f"\n{'='*40}")
            print(f"FINAL STATS:")
            print(f"  Frames:    {frame_count}")
            print(f"  Decoded:   {decoded_count}")
            print(f"  Duration:  {elapsed:.1f}s")
            print(f"  Avg FPS:   {frame_count/elapsed:.1f}")
            print(f"  Keyframes: {keyframe_count}")
            print(f"  Total:     {total_bytes/1024/1024:.2f} MB")
            print(f"  Bandwidth: {total_bytes*8/elapsed/1000:.1f} kbps")


if __name__ == "__main__":
    main()
