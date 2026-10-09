"""Explicitly download the pinned model used by the reference implementation.

No download occurs during application or worker startup. The upstream model
artifact is kept outside Git; see docs/image-resolution.md for provenance.
"""
import argparse
import hashlib
from pathlib import Path
from urllib.request import urlopen

MODEL_URL = "https://raw.githubusercontent.com/gaoren002/GPT2Image-Pro/22bdcc968ad646f371de632ab6c4a1bdbbd63774/apps/web/models/realesr-general-x4v3.onnx"
MODEL_SHA256 = "027319ffe4f00ec2550957c0957d44969638a03d2ed2f0329af9fd6cd44a457a"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "data/models/realesr-general-x4v3.onnx")
    args = parser.parse_args()
    if args.output.exists():
        if hashlib.sha256(args.output.read_bytes()).hexdigest() == MODEL_SHA256:
            print(f"Verified existing model: {args.output}")
            return
        raise SystemExit("Output already exists with different contents; choose a new --output path.")
    with urlopen(MODEL_URL, timeout=60) as response:
        data = response.read(10 * 1024 * 1024)
    if hashlib.sha256(data).hexdigest() != MODEL_SHA256:
        raise SystemExit("Model checksum mismatch; no file written.")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as file:
        file.write(data)
    print(f"Verified model saved: {args.output}")


if __name__ == "__main__":
    main()
