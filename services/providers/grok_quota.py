"""Minimal bounded protobuf decoder for Grok Web's weekly credit snapshot."""
import math
import struct
import time


def fields(data):
    index = 0
    def varint():
        nonlocal index
        result = 0
        for shift in range(0, 70, 7):
            if index >= len(data):
                raise ValueError("truncated protobuf")
            byte = data[index]; index += 1
            result |= (byte & 127) << shift
            if byte < 128:
                return result
        raise ValueError("invalid protobuf varint")
    while index < len(data):
        tag = varint(); number, wire = tag >> 3, tag & 7
        if not number:
            raise ValueError("invalid protobuf field")
        if wire == 0:
            value = varint()
        elif wire in (1, 2, 5):
            size = varint() if wire == 2 else 8 if wire == 1 else 4
            if size > len(data) - index:
                raise ValueError("truncated protobuf field")
            value = data[index:index+size]; index += size
        else:
            raise ValueError("unsupported protobuf wire type")
        yield number, wire, value


def weekly(body):
    if len(body) > 4*1024*1024:
        raise ValueError("weekly response too large")
    offset, message = 0, None
    while offset < len(body):
        if len(body)-offset < 5:
            raise ValueError("truncated grpc frame")
        flag, size = body[offset], int.from_bytes(body[offset+1:offset+5], "big")
        offset += 5
        if size > len(body)-offset:
            raise ValueError("truncated grpc payload")
        frame = body[offset:offset+size]; offset += size
        if flag == 0 and message is None:
            message = frame
        elif flag == 128:
            trailers = dict(line.split(":", 1) for line in frame.decode().splitlines() if ":" in line)
            if trailers.get("grpc-status", "0").strip() != "0":
                raise ValueError("grpc error")
        elif flag != 0:
            raise ValueError("unsupported grpc frame")
    if message is None:
        raise ValueError("missing weekly message")
    config = next((v for n,w,v in fields(message) if n == 1 and w == 2), None)
    if config is None:
        raise ValueError("missing weekly config")
    usage, start, end, precise = None, None, None, False
    for n,w,v in fields(config):
        if n == 1 and w == 5:
            usage = struct.unpack("<f", v)[0]
        elif n in (4,5) and w == 2:
            values = {number: value for number,wire,value in fields(v) if wire == 0}
            seconds, nanos = values.get(1,0), values.get(2,0)
            precise |= bool(nanos)
            if n == 4: start = seconds + nanos/1e9
            else: end = seconds + nanos/1e9
    if start is None or end is None or not 86400 <= end-start <= 31*86400 or (usage is None and not precise):
        raise ValueError("invalid weekly period")
    usage = usage or 0.0
    if not math.isfinite(usage) or not 0 <= usage <= 100:
        raise ValueError("invalid weekly usage")
    return {"remaining": max(0,10000-round(usage*100)), "total":10000, "usage_percent":usage,
            "reset_at":end, "synced_at":time.time()}
