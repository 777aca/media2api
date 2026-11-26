"""Submit one synthetic 9:16 image to fal-ai/esrgan and measure it."""
import argparse
import io
import sys
import time
from pathlib import Path

import httpx
from PIL import Image, ImageDraw

from services.config import config
from services.fal_esrgan import upscale
from services.image_calibration import MAX_BODY_BYTES


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Save the returned 4K PNG")
    args = parser.parse_args()
    api_key = str(config.get_image_calibration_settings().get("fal_api_key") or "")
    if not api_key:
        print("Save a fal API key in Image Calibration settings before running this check.", file=sys.stderr)
        return 2
    with Image.new("RGB", (576, 1024)) as image, io.BytesIO() as buffer:
        draw = ImageDraw.Draw(image)
        for y in range(image.height):
            draw.line((0, y, image.width, y), fill=(35, 45 + y // 6, 220 - y // 8))
        for radius in range(10, 250, 8):
            draw.ellipse((288 - radius, 512 - radius, 288 + radius, 512 + radius), outline="white", width=2)
        image.save(buffer, format="PNG")
        source = buffer.getvalue()

    started = time.monotonic()
    try:
        result = upscale(source, (2160, 3840), timeout=300, max_bytes=MAX_BODY_BYTES, api_key=api_key)
        elapsed = time.monotonic() - started
        with Image.open(io.BytesIO(result.image)) as output:
            output.load()
            if (output.format, output.size) != ("PNG", (2160, 3840)):
                raise ValueError(f"Unexpected output: {output.format} {output.size}")
        if args.output:
            args.output.write_bytes(result.image)
    except httpx.HTTPStatusError as error:
        print(f"fal request failed: HTTP {error.response.status_code}", file=sys.stderr)
        return 1
    except (httpx.HTTPError, TimeoutError, ValueError, KeyError, RuntimeError, OSError) as error:
        print(f"Test failed: {type(error).__name__}", file=sys.stderr)
        return 1
    print(f"request_id={result.request_id}  input=576x1024  output=2160x3840")
    print(f"elapsed_seconds={elapsed:.3f}  inference_seconds={result.inference_seconds}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
