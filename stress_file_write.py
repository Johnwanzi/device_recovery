"""
Stress-test file_write on OneKey Pro 2 over WebUSB.

Generates temp files of various sizes (200KB ~ 2MB) and writes each to the
device with chunk=1024. Stops at the first error and dumps the full context
so the failure can be inspected.

Usage:
    python stress_file_write.py                         # default sweep + 3 random sizes
    python stress_file_write.py --sizes 200,500,800,1200,1600,2048   # explicit KB list
    python stress_file_write.py --rounds 3              # repeat the sweep N times
"""

import argparse
import os
import random
import sys
import time
import traceback
from pathlib import Path

# Reuse the workflow stack so behaviour matches step1/step2.
from onekey_webusb import (
    PB_MSG_TYPE,
    PB_MSG_NAME,
    WebUsbDevice,
    encode_file,
    encode_file_write,
    encode_file_delete,
    encode_path_info_query,
    decode_file,
    decode_failure,
    decode_path_info,
)

CHUNK = 1024
DEFAULT_SIZES_KB = [200, 300, 500, 800, 1024, 1200, 1500, 1800, 2048]
DST_PATH = "vol0:stress_test.bin"


def ts() -> str:
    return time.strftime("%H:%M:%S")


def log(level: str, msg: str) -> None:
    sys.stdout.write(f"[{ts()}] [{level}] {msg}\n")
    sys.stdout.flush()


def format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.2f} KB"
    return f"{n / (1024 * 1024):.2f} MB"


def write_once(dev: WebUsbDevice, data: bytes, dst: str, chunk: int) -> dict:
    """Write `data` to `dst` on device, chunk by chunk. Return a dict describing
    the outcome:
        {"ok": True,  "elapsed": s, "speed_bps": f}
        {"ok": False, "elapsed": s, "where": "...", "detail": "..."}
    """
    total = len(data)
    offset = 0
    is_first = True
    chunk_idx = 0
    start = time.monotonic()
    last_print = start

    while offset < total:
        this_chunk = min(chunk, total - offset)
        chunk_data = data[offset:offset + this_chunk]
        file_bytes = encode_file(dst, offset, total, chunk_data)
        pb_payload = encode_file_write(file_bytes, overwrite=is_first, append=False)
        is_first = False
        chunk_idx += 1

        try:
            msg_type, payload = dev.send_and_recv(
                PB_MSG_TYPE["FileWrite"], pb_payload, timeout_ms=10000
            )
        except Exception as e:
            return {
                "ok": False,
                "elapsed": time.monotonic() - start,
                "where": f"send_and_recv chunk#{chunk_idx} offset={offset} this_chunk={this_chunk}",
                "detail": f"exception: {e}",
                "traceback": traceback.format_exc(),
                "offset": offset,
            }

        if msg_type == PB_MSG_TYPE["Failure"]:
            decoded = decode_failure(payload)
            return {
                "ok": False,
                "elapsed": time.monotonic() - start,
                "where": f"FileWrite Failure reply at chunk#{chunk_idx} offset={offset} this_chunk={this_chunk}",
                "detail": f"code={decoded['code']}  msg=\"{decoded['message']}\"",
                "offset": offset,
            }
        if msg_type != PB_MSG_TYPE["File"]:
            return {
                "ok": False,
                "elapsed": time.monotonic() - start,
                "where": f"FileWrite unexpected reply at chunk#{chunk_idx} offset={offset}",
                "detail": f"msg_type={msg_type} ({PB_MSG_NAME.get(msg_type,'?')})",
                "offset": offset,
            }

        decoded = decode_file(payload)
        processed = decoded["processed_byte"] if decoded["processed_byte"] is not None else (offset + this_chunk)
        offset = processed

        now = time.monotonic()
        if now - last_print >= 0.3 or offset >= total:
            elapsed = now - start
            speed = offset / elapsed if elapsed > 0 else 0
            sys.stdout.write(
                f"\r        progress: {offset*100/total:6.2f}%  "
                f"{format_bytes(offset)} / {format_bytes(total)}  "
                f"{format_bytes(int(speed))}/s  chunk#{chunk_idx}    "
            )
            sys.stdout.flush()
            last_print = now

    sys.stdout.write("\n")
    elapsed = time.monotonic() - start
    speed = total / elapsed if elapsed > 0 else 0
    return {"ok": True, "elapsed": elapsed, "speed_bps": speed}


def delete_if_exists(dev: WebUsbDevice, path: str) -> None:
    """Best-effort: clean up the previous test file so each round starts fresh."""
    try:
        msg_type, payload = dev.send_and_recv(
            PB_MSG_TYPE["PathInfoQuery"], encode_path_info_query(path), timeout_ms=5000
        )
    except Exception:
        return
    if msg_type != PB_MSG_TYPE["PathInfo"]:
        return
    info = decode_path_info(payload)
    if not info["exist"]:
        return
    try:
        dev.send_and_recv(
            PB_MSG_TYPE["FileDelete"], encode_file_delete(path), timeout_ms=10000
        )
    except Exception as e:
        log("WARN", f"cleanup file_delete failed: {e}")


def parse_sizes(arg: str) -> list:
    out = []
    for piece in arg.split(","):
        piece = piece.strip()
        if not piece:
            continue
        out.append(int(piece))
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Stress test file_write across sizes 200KB-2MB")
    p.add_argument("--sizes", type=parse_sizes, default=DEFAULT_SIZES_KB,
                   help="comma-separated list of file sizes in KB (default: 200..2048)")
    p.add_argument("--rounds", type=int, default=1, help="repeat the sweep N times")
    p.add_argument("--chunk", type=int, default=CHUNK, help=f"chunk size in bytes (default {CHUNK})")
    p.add_argument("--seed", type=int, default=12345, help="RNG seed for reproducible payload")
    p.add_argument("--dst", default=DST_PATH, help=f"device destination path (default {DST_PATH})")
    p.add_argument("--keep-going", action="store_true",
                   help="don't stop on first failure (default stops at first error)")
    args = p.parse_args()

    rng = random.Random(args.seed)

    log("INFO", "Stress test: file_write")
    log("INFO", f"  sizes (KB): {args.sizes}")
    log("INFO", f"  rounds    : {args.rounds}")
    log("INFO", f"  chunk     : {args.chunk}")
    log("INFO", f"  dst       : {args.dst}")
    log("INFO", f"  on-error  : {'continue' if args.keep_going else 'stop'}")

    dev = WebUsbDevice()
    try:
        dev.open()
    except Exception as e:
        log("FAIL", f"open() failed: {e}")
        return 2

    total_runs = 0
    total_ok = 0
    total_fail = 0
    failures = []  # list of dicts

    try:
        for round_no in range(1, args.rounds + 1):
            log("INFO", f"=== Round {round_no}/{args.rounds} ===")
            for size_kb in args.sizes:
                size = size_kb * 1024
                # random bytes are the worst case for any reuse/compression
                data = rng.randbytes(size)
                total_runs += 1
                log("INFO", f"[{total_runs}] size={size_kb} KB ({size} bytes) -> {args.dst}")

                # Clean up previous round's file so this write starts as overwrite from offset 0.
                delete_if_exists(dev, args.dst)

                result = write_once(dev, data, args.dst, args.chunk)
                if result["ok"]:
                    total_ok += 1
                    log("OK",
                        f"  done in {result['elapsed']:.2f}s "
                        f"({format_bytes(int(result['speed_bps']))}/s)")
                else:
                    total_fail += 1
                    failures.append({
                        "round": round_no,
                        "size_kb": size_kb,
                        "size": size,
                        **result,
                    })
                    log("FAIL", f"  {result['where']}")
                    log("FAIL", f"  {result['detail']}")
                    if "traceback" in result:
                        sys.stdout.write(result["traceback"])
                    if not args.keep_going:
                        log("FAIL", "Stopping at first failure (use --keep-going to continue).")
                        return 1
    finally:
        try:
            dev.close()
        except Exception:
            pass

    log("INFO", "==================== SUMMARY ====================")
    log("INFO", f"  runs    : {total_runs}")
    log("OK" if total_fail == 0 else "FAIL",
        f"  passes  : {total_ok}")
    if total_fail:
        log("FAIL", f"  failures: {total_fail}")
        for f in failures:
            log("FAIL",
                f"    round={f['round']} size={f['size_kb']} KB at offset={f.get('offset','?')}: "
                f"{f['where']} -- {f['detail']}")
    return 0 if total_fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
