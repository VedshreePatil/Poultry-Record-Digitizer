"""
quality_check.py — Pre-OCR image quality check.
Detects blurry or low-contrast images before sending to OCR pipeline.
"""
import cv2
import numpy as np


def check_image_quality(image_path: str) -> dict:
    img = cv2.imread(image_path)
    if img is None:
        return {"ok": False,
                "message": "Could not read image file.",
                "blur_score": 0, "contrast_score": 0}

    gray           = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blur_score     = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    contrast_score = float(np.std(gray))

    issues = []
    if blur_score     < 80.0:
        issues.append(f"image appears blurry (score: {blur_score:.1f})")
    if contrast_score < 30.0:
        issues.append(f"low contrast (score: {contrast_score:.1f})")

    if issues:
        return {
            "ok":      False,
            "message": "⚠️ Image quality is low — " + ", ".join(issues)
                       + ". Please upload a clearer photo.",
            "blur_score":     round(blur_score, 1),
            "contrast_score": round(contrast_score, 1),
        }

    return {
        "ok":             True,
        "message":        "",
        "blur_score":     round(blur_score, 1),
        "contrast_score": round(contrast_score, 1),
    }