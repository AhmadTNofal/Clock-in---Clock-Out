"""Download the ONNX models used for face detection and recognition.

Both models come from the official OpenCV Model Zoo and are loaded through
cv2.dnn, so no extra runtime (torch / onnxruntime) is required.

Usage:  python scripts/fetch_models.py
"""

from __future__ import annotations

import hashlib
import sys
import urllib.request
from pathlib import Path

MODEL_DIR = Path(__file__).resolve().parent.parent / "models"

ZOO = "https://github.com/opencv/opencv_zoo/raw/main/models"

MODELS = {
    "face_detection_yunet_2023mar.onnx": f"{ZOO}/face_detection_yunet/face_detection_yunet_2023mar.onnx",
    "face_recognition_sface_2021dec.onnx": f"{ZOO}/face_recognition_sface/face_recognition_sface_2021dec.onnx",
}


def download(name: str, url: str) -> Path:
    target = MODEL_DIR / name
    if target.exists() and target.stat().st_size > 0:
        print(f"  [skip] {name} already present ({target.stat().st_size:,} bytes)")
        return target

    print(f"  [get ] {name} ...", end="", flush=True)
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".part")
    with urllib.request.urlopen(url, timeout=120) as response, tmp.open("wb") as handle:
        handle.write(response.read())
    tmp.replace(target)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()[:16]
    print(f" done ({target.stat().st_size:,} bytes, sha256:{digest})")
    return target


def main() -> int:
    print(f"Fetching face models into {MODEL_DIR}")
    for name, url in MODELS.items():
        try:
            download(name, url)
        except Exception as exc:  # noqa: BLE001 - surface any network problem plainly
            print(f"\n  [fail] {name}: {exc}", file=sys.stderr)
            return 1
    print("All models present.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
